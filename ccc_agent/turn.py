"""Per-turn (Stop-boundary) finalize handler — the supervisor side of the
control channel.

At a Stop the agent is idle, so the supervisor (which runs OUTSIDE the sandbox
and can see both the live BranchFS view and the real base) commits a turn by
**selectively copying the in-scope changes from the view into the base** and
applying in-scope deletes.  This needs no branchfs teardown and never disturbs
the agent's live mount, so the session simply continues:

  turn-finalize  in-scope changes -> copied to base, agent continues (committed)
                 out-of-scope     -> held; needs-approval (+ token) unless
                                     default_keep is set, in which case kept in
                                     branch and agent continues
                 nothing changed  -> noop
  turn-approve   yes -> copy the approved out-of-scope paths to base
                 keep/no -> remember as kept in the branch only (not committed,
                            not re-prompted)
                 discard/revert -> remember as rejected and ask the agent to
                                   undo it (not re-prompted)
                 per-file -> commit some, keep some, discard some
  turn-resolve   later user request -> commit/keep/discard previously remembered
                                      paths by name

The branch's deltas are intentionally left in place (commit-in-place under a
live FUSE mount churns inodes -> ESTALE on NFS); re-applying an unchanged path
is idempotent, and the supervisor records per-path decisions in the session so
out-of-scope paths that were already committed, kept, or discarded are not
re-prompted after controller restarts.

Threat model is naive/accidental (see docs/architecture.md): the supervisor
holds commit authority and only copies out-of-scope work to base on a relayed
user "yes"; an agent can spoof its OWN approval but never escapes the in-scope
policy.
"""

import binascii
import os
import shutil
import threading

from .commit_failures import is_permission_denied
from .control import (VERDICT_COMMITTED, VERDICT_HELD, VERDICT_KEPT_STATUS,
                      VERDICT_NEEDS_APPROVAL, VERDICT_NEEDS_KEPT_REVIEW,
                      VERDICT_NOOP)
from .policy import PolicyConfig, classify, filter_ignored
from .previous_commits import DECISION_COMMITTED, TURN_PATH_DECISIONS


DECISION_KEPT = "kept"
DECISION_DISCARDED = "discarded"


def _new_token():
    return binascii.hexlify(os.urandom(12)).decode("ascii")


class TurnController(object):
    """Stateful per-session handler; thread-safe (the control server may call
    from a connection thread)."""

    def __init__(self, session, store, backend, alias_map):
        self.session = session
        self.store = store
        self.backend = backend
        self.alias_map = alias_map
        self._lock = threading.Lock()
        self._pending = {}       # approval_token -> frozenset(visible paths)

    # -- helpers -----------------------------------------------------------
    def _roots(self):
        return dict(self.session.protected_roots)

    def _live_changes(self):
        changes = []
        for _n, root in sorted(self.session.protected_roots.items()):
            changes.extend(self.backend.status(root))
        config = PolicyConfig.from_dict(self.session.policy)
        return filter_ignored(changes, config, self.alias_map)

    def _decision_map(self):
        decisions = self.session.policy.setdefault(TURN_PATH_DECISIONS, {})
        if not isinstance(decisions, dict):
            decisions = {}
            self.session.policy[TURN_PATH_DECISIONS] = decisions
        return decisions

    def _paths_with_decisions(self, *values):
        values = set(values)
        return {path for path, decision in self._decision_map().items()
                if decision in values}

    def _mark_paths(self, paths, decision):
        decisions = self._decision_map()
        for path in sorted(set(paths)):
            decisions[path] = decision

    def _out_of_scope_paths(self, changes):
        config = PolicyConfig.from_dict(self.session.policy)
        out_of_scope, deny = classify(changes, config, self.alias_map)
        paths = set(out_of_scope)
        paths.update(m.path for m in deny)
        return paths

    def _apply(self, changes):
        """Copy each change from the live view into the base (or delete it).

        Idempotent: re-applying an unchanged path just re-copies identical
        bytes.  Reads come from the FUSE view (root.mount) so they reflect the
        agent's latest content; writes go straight to the real underlay.
        """
        roots = self._roots()
        applied = []
        denied = []
        for ch in changes:
            root = roots.get(ch.root)
            if root is None:
                continue
            visible = self.alias_map.canonicalize(root.visible)
            rel = os.path.relpath(self.alias_map.canonicalize(ch.path), visible)
            dst = os.path.join(root.base, rel)
            try:
                if ch.op == "D":
                    if os.path.islink(dst) or os.path.isfile(dst):
                        os.unlink(dst)
                    elif os.path.isdir(dst):
                        shutil.rmtree(dst)
                elif ch.kind == "dir":
                    os.makedirs(dst, exist_ok=True)
                else:
                    src = os.path.join(root.mount, rel)
                    if os.path.exists(src):
                        parent = os.path.dirname(dst)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        shutil.copy2(src, dst)
            except Exception as exc:
                if not is_permission_denied(exc):
                    raise
                denied.append(ch.path)
                continue
            applied.append(ch.path)
        return applied, denied

    # -- control ops -------------------------------------------------------
    def finalize_turn(self, default_keep=False):
        with self._lock:
            # No freeze/thaw: the hook calls this synchronously at a Stop while
            # the agent is idle, so there are no concurrent writes — and
            # freezing a branch under its live FUSE mount invalidates the
            # agent's cached inodes (ESTALE) when it resumes.
            changes = self._live_changes()
            if not changes:
                self.session.add_event("turn-noop")
                self.store.save(self.session)
                return {"verdict": VERDICT_NOOP, "changed": 0}

            oos = self._out_of_scope_paths(changes)
            committed_paths = self._paths_with_decisions(DECISION_COMMITTED)
            resolved_paths = self._paths_with_decisions(
                DECISION_COMMITTED, DECISION_KEPT, DECISION_DISCARDED)
            new_oos = oos - resolved_paths
            # commit in-scope changes + any previously approved out-of-scope;
            # keep remembered/rejected in-scope paths out of repeated retries
            # until the user explicitly resolves them.
            held_paths = self._paths_with_decisions(
                DECISION_KEPT, DECISION_DISCARDED)
            to_apply = [c for c in changes
                        if c.path in committed_paths or
                        (c.path not in oos and c.path not in held_paths)]
            committed, permission_denied = self._apply(to_apply)
            self._mark_paths(committed, DECISION_COMMITTED)
            permission_kept = []
            if permission_denied:
                permission_kept = self._keep_paths(permission_denied)
                self.session.add_event(
                    "turn-permission-denied",
                    "%d path(s) kept in branch" % len(permission_kept))

            if new_oos:
                if default_keep:
                    kept = self._keep_paths(set(new_oos) | set(permission_kept))
                    self.session.add_event(
                        "turn-default-kept",
                        "%d new out-of-scope path(s) kept in branch" %
                        len(new_oos))
                    self.store.save(self.session)
                    return {"verdict": (VERDICT_COMMITTED if committed
                                        else VERDICT_HELD),
                            "committed": committed, "kept": kept,
                            "held": kept, "default_keep": True,
                            "permission_denied": permission_kept}
                token = _new_token()
                self._pending[token] = frozenset(new_oos)
                self.session.add_event(
                    "turn-needs-approval",
                    "%d new out-of-scope path(s)" % len(new_oos))
                self.store.save(self.session)
                return {"verdict": VERDICT_NEEDS_APPROVAL,
                        "out_of_scope": sorted(new_oos),
                        "approval_token": token,
                        "committed": committed,
                        "kept": permission_kept,
                        "permission_denied": permission_kept}

            if permission_kept:
                self.store.save(self.session)
                return {"verdict": (VERDICT_COMMITTED if committed
                                    else VERDICT_HELD),
                        "committed": committed, "kept": permission_kept,
                        "held": permission_kept,
                        "permission_denied": permission_kept}

            self.session.add_event("turn-committed",
                                   "%d change(s) applied" % len(committed))
            self.store.save(self.session)
            return {"verdict": VERDICT_COMMITTED if committed else VERDICT_NOOP,
                    "committed": committed}

    _YES = ("yes", "y", "true", "1", "approve", "ok", "all", "accept",
            "commit")
    _KEEP = ("", "no", "n", "keep", "kept", "hold", "held", "later")
    _REVERT = ("revert", "reject", "discard", "abort", "undo")

    def _commit_paths(self, paths):
        """Copy the chosen view paths into base and mark them allowed so the
        session-end finalize agrees (the lingering deltas would otherwise
        re-flag as out-of-scope despite already being in base)."""
        paths = set(paths)
        if not paths:
            return []
        changes = [c for c in self._live_changes() if c.path in paths]
        committed, denied = self._apply(changes)
        self._mark_paths(committed, DECISION_COMMITTED)
        if denied:
            self._keep_paths(denied)
            self.session.add_event(
                "turn-permission-denied",
                "%d approved path(s) kept in branch" % len(denied))
        scopes = self.session.policy.setdefault("allowed_scopes", [])
        for path in committed:
            if path not in scopes:
                scopes.append(path)
        return committed

    def _keep_paths(self, paths):
        paths = sorted(set(paths))
        self._mark_paths(paths, DECISION_KEPT)
        return paths

    def _discard_paths(self, paths):
        paths = sorted(set(paths))
        self._mark_paths(paths, DECISION_DISCARDED)
        return paths

    def _kept_path_view(self):
        live_paths = {c.path for c in self._live_changes()}
        remembered = self._paths_with_decisions(DECISION_KEPT)
        kept = sorted(remembered & live_paths)
        stale = sorted(remembered - live_paths)
        return kept, stale

    def kept_status(self):
        """Read-only status for non-workspace paths kept in the live branch."""
        with self._lock:
            kept, stale = self._kept_path_view()
            return {"verdict": VERDICT_KEPT_STATUS, "kept": kept,
                    "stale": stale, "count": len(kept)}

    def review_kept(self):
        """Final/idle check: ask the user only if kept paths still exist."""
        with self._lock:
            kept, stale = self._kept_path_view()
            if not kept:
                return {"verdict": VERDICT_NOOP, "kept": [], "stale": stale,
                        "message": "no kept non-workspace paths"}
            joined = ",".join(kept)
            msg = (
                "kept non-workspace paths remain in BranchFS only; ask the "
                "user whether to commit, discard, or keep them, then run "
                "ccc-agent turn-resolve commit --paths %s or "
                "ccc-agent turn-resolve discard --paths %s" % (joined, joined))
            self.session.add_event("turn-kept-review-requested",
                                   "%d path(s)" % len(kept))
            self.store.save(self.session)
            return {"verdict": VERDICT_NEEDS_KEPT_REVIEW, "kept": kept,
                    "stale": stale, "message": msg}

    def _check_no_overlap(self, groups):
        seen = {}
        for name, paths in groups:
            for path in paths:
                if path in seen:
                    raise ValueError(
                        "path %s appears in both %s and %s" %
                        (path, seen[path], name))
                seen[path] = name

    def _resolve_granular(self, pending, commit_paths=(), keep_paths=(),
                          discard_paths=(), default_keep=True):
        commit_paths = set(commit_paths or ())
        keep_paths = set(keep_paths or ())
        discard_paths = set(discard_paths or ())
        self._check_no_overlap((
            ("commit", commit_paths),
            ("keep", keep_paths),
            ("discard", discard_paths),
        ))
        pending = set(pending)
        commit_now = commit_paths & pending
        discard_now = discard_paths & pending
        keep_now = keep_paths & pending
        specified = commit_now | discard_now | keep_now
        if default_keep:
            keep_now |= pending - specified

        committed = self._commit_paths(commit_now)
        kept = self._keep_paths(keep_now)
        revert = self._discard_paths(discard_now)
        return committed, kept, revert

    def approve_turn(self, approval_token, decision, paths=None,
                     commit_paths=None, keep_paths=None, discard_paths=None):
        """Resolve an out-of-scope turn with one of the four review actions:

          accept-all   decision in _YES                -> commit every flagged path
          reject/revert decision in _REVERT            -> remember + tell the agent to
                                                          undo them (naive model:
                                                          the supervisor cannot
                                                          safely strip deltas under
                                                          a live mount)
          keep          decision = "no"/"keep" (default)-> leave deltas uncommitted,
                                                          session continues
          file-level    paths=[...]                    -> commit only that subset,
                                                          hold the rest
          granular      commit_paths/keep_paths/
                        discard_paths                  -> commit, keep, and discard
                                                          different files; omitted
                                                          pending paths are kept
        """
        with self._lock:
            if approval_token not in self._pending:
                raise KeyError("unknown or already-resolved approval token")
            pending = set(self._pending.pop(approval_token))
            decision = str(decision or "").strip().lower()

            if commit_paths or keep_paths or discard_paths:
                committed, kept, revert = self._resolve_granular(
                    pending, commit_paths=commit_paths, keep_paths=keep_paths,
                    discard_paths=discard_paths)
                self.session.add_event(
                    "turn-approved-granular",
                    "committed %d, kept %d, discard-requested %d" %
                    (len(committed), len(kept), len(revert)))
                self.store.save(self.session)
                return {"verdict": (VERDICT_COMMITTED if committed
                                    else VERDICT_HELD),
                        "committed": committed, "kept": kept,
                        "held": kept, "revert": revert}

            if paths:
                chosen = set(paths) & pending
                held = pending - chosen
                committed = self._commit_paths(chosen)
                kept = self._keep_paths(held)
                self.session.add_event(
                    "turn-approved-partial",
                    "committed %d, held %d" % (len(chosen), len(held)))
                self.store.save(self.session)
                return {"verdict": VERDICT_COMMITTED, "committed": committed,
                        "held": kept, "kept": kept}

            if decision in self._YES:
                committed = self._commit_paths(pending)
                self.session.add_event("turn-approved",
                                       "user approved %d path(s)" % len(pending))
                self.store.save(self.session)
                return {"verdict": VERDICT_COMMITTED, "committed": committed}

            if decision in self._REVERT:
                revert = self._discard_paths(pending)
                self.session.add_event("turn-revert-requested",
                                       "%d path(s)" % len(pending))
                self.store.save(self.session)
                return {"verdict": VERDICT_HELD, "revert": revert,
                        "message": "the user rejected these changes; revert "
                                   "them in your workspace (restore original "
                                   "content or delete files you created)"}

            # default: keep deltas, do not commit, session continues
            kept = self._keep_paths(pending)
            self.session.add_event("turn-held",
                                   "user kept %d path(s) uncommitted"
                                   % len(pending))
            self.store.save(self.session)
            return {"verdict": VERDICT_HELD, "denied": kept, "kept": kept,
                    "held": kept}

    def resolve_turn(self, decision, paths):
        """Resolve already-remembered paths later in the same live session.

        This is for a user follow-up after choosing ``keep``: the path remains in
        the branch, is not re-prompted, and can later be explicitly committed or
        rejected by name through the same trusted supervisor socket.
        """
        with self._lock:
            paths = set(paths or ())
            if not paths:
                raise ValueError("turn-resolve requires at least one path")
            decision = str(decision or "").strip().lower()
            if decision in self._YES:
                committed = self._commit_paths(paths)
                self.session.add_event("turn-resolved-commit",
                                       "%d path(s)" % len(committed))
                self.store.save(self.session)
                return {"verdict": VERDICT_COMMITTED,
                        "committed": committed}
            if decision in self._REVERT:
                revert = self._discard_paths(paths)
                self.session.add_event("turn-resolved-discard",
                                       "%d path(s)" % len(revert))
                self.store.save(self.session)
                return {"verdict": VERDICT_HELD, "revert": revert,
                        "message": "the user rejected these changes; revert "
                                   "them in your workspace (restore original "
                                   "content or delete files you created)"}
            if decision in self._KEEP:
                kept = self._keep_paths(paths)
                self.session.add_event("turn-resolved-keep",
                                       "%d path(s)" % len(kept))
                self.store.save(self.session)
                return {"verdict": VERDICT_HELD, "kept": kept,
                        "held": kept}
            raise ValueError("unknown turn decision %r" % decision)

    # -- ControlServer entrypoint -----------------------------------------
    def handle(self, request):
        op = request.get("op")
        if op == "turn-finalize":
            return self.finalize_turn(
                default_keep=bool(request.get("default_keep")))
        if op == "turn-approve":
            return self.approve_turn(request.get("approval_token"),
                                     request.get("decision", "no"),
                                     paths=request.get("paths"),
                                     commit_paths=request.get("commit_paths"),
                                     keep_paths=request.get("keep_paths"),
                                     discard_paths=request.get("discard_paths"))
        if op == "turn-resolve":
            return self.resolve_turn(request.get("decision", "keep"),
                                     request.get("paths"))
        if op == "turn-kept-status":
            return self.kept_status()
        if op == "turn-review-kept":
            return self.review_kept()
        return {"ok": False, "error": "unknown op %r" % (op,)}
