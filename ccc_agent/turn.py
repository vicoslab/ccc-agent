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
                 discard/revert -> drop the selected BranchFS deltas/tombstones
                 per-file -> commit some, keep some, discard some
  turn-resolve   later user request -> commit/keep/discard previously remembered
                                      paths by name

The branch's deltas are intentionally left in place (commit-in-place under a
live FUSE mount churns inodes -> ESTALE on NFS); re-applying an unchanged path
is idempotent, and the supervisor records per-path commit/keep decisions in the
session so out-of-scope paths that were already committed or kept are not
re-prompted after controller restarts.

Mutating agent decisions are admitted only through the process-pinned MCP
connection. Lifecycle hooks retain finalize/workspace signaling, while ordinary
agent subprocesses cannot call turn-approve or turn-resolve on the production
supervisor.
"""

import binascii
import os
import shutil
import threading

from .commit_failures import is_permission_denied
from .control import (VERDICT_COMMITTED, VERDICT_DISCARDED, VERDICT_HELD,
                      VERDICT_KEPT_STATUS, VERDICT_NEEDS_APPROVAL,
                      VERDICT_NEEDS_KEPT_REVIEW, VERDICT_NOOP,
                      VERDICT_WORKSPACE_UPDATED)
from .paths import is_within, normalize
from .policy import PolicyConfig, classify, filter_ignored
from .previous_commits import DECISION_COMMITTED, TURN_PATH_DECISIONS


DECISION_KEPT = "kept"
WORKSPACE_SCOPES = "workspace_scopes"
HOOK_WORKSPACE_REFS = "hook_workspace_refs"


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

    def _change_root_rel(self, ch):
        roots = self._roots()
        root = roots.get(ch.root)
        if root is None:
            return None, None
        visible = self.alias_map.canonicalize(root.visible)
        rel = os.path.relpath(self.alias_map.canonicalize(ch.path), visible)
        return root, rel

    def _canonical_key(self, path):
        """Canonical comparison key for absolute visible paths/scopes."""
        return self.alias_map.canonicalize(normalize(str(path)))

    def _validate_workspace(self, path):
        """Return (visible path, canonical path) for a safe workspace scope.

        A turn workspace is only a commit/review policy scope. It must live in
        one of this session's protected visible roots; otherwise adding it would
        either be meaningless or accidentally bless writes that BranchFS is not
        supervising.
        """
        visible_path = normalize(str(path or ""))
        canonical = self.alias_map.canonicalize(visible_path)
        for root in self.session.protected_roots.values():
            root_visible = self.alias_map.canonicalize(root.visible)
            if is_within(canonical, root_visible):
                return visible_path, canonical
        raise ValueError("workspace %s is not under a protected root" %
                         visible_path)

    def _unique_paths_by_canonical(self, paths):
        result = []
        seen = set()
        for path in paths:
            try:
                visible, canonical = self._validate_workspace(path)
            except ValueError:
                # Legacy/corrupt dynamic workspace entries should not make
                # every turn command unusable. They remain out of the dynamic
                # workspace list; preserved non-workspace allowed scopes are
                # handled separately by _rewrite_workspace_scopes().
                continue
            if canonical not in seen:
                result.append(visible)
                seen.add(canonical)
        return result

    def _workspace_scopes(self):
        workspaces = self.session.policy.get(WORKSPACE_SCOPES)
        if not isinstance(workspaces, list):
            workspaces = [self.session.workspace] if self.session.workspace else []
        workspaces = self._unique_paths_by_canonical(workspaces)
        self.session.policy[WORKSPACE_SCOPES] = workspaces
        return workspaces

    def _rewrite_workspace_scopes(self, workspaces):
        old_keys = {self._canonical_key(path)
                    for path in self._workspace_scopes()}
        new_workspaces = self._unique_paths_by_canonical(workspaces)
        preserved = []
        for scope in self.session.policy.get("allowed_scopes", ()):
            try:
                key = self._canonical_key(scope)
            except ValueError:
                continue
            if key not in old_keys:
                preserved.append(normalize(str(scope)))

        allowed = []
        seen = set()
        for scope in list(new_workspaces) + preserved:
            try:
                key = self._canonical_key(scope)
            except ValueError:
                continue
            if key not in seen:
                allowed.append(scope)
                seen.add(key)

        self.session.policy[WORKSPACE_SCOPES] = new_workspaces
        self.session.policy["allowed_scopes"] = allowed
        return new_workspaces, allowed

    def _workspace_response(self, action, workspace, workspaces, allowed,
                            hook_session=None, added=False, owned=False,
                            removed=False):
        return {"verdict": VERDICT_WORKSPACE_UPDATED,
                "action": action,
                "workspace": workspace,
                "workspaces": list(workspaces),
                "allowed_scopes": list(allowed),
                "hook_session": hook_session,
                "added": bool(added),
                "owned": bool(owned),
                "removed": bool(removed)}

    def _validate_hook_session(self, hook_session):
        hook_session = str(hook_session or "").strip()
        if not hook_session:
            raise ValueError("hook workspace operation requires hook_session")
        return hook_session

    def _hook_workspace_refs(self):
        refs = self.session.policy.get(HOOK_WORKSPACE_REFS)
        if not isinstance(refs, dict):
            refs = {}
            self.session.policy[HOOK_WORKSPACE_REFS] = refs
        clean = {}
        for key, entry in refs.items():
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            owners = entry.get("owners")
            if not path or not isinstance(owners, list):
                continue
            unique = []
            seen = set()
            for owner in owners:
                owner = str(owner or "").strip()
                if owner and owner not in seen:
                    unique.append(owner)
                    seen.add(owner)
            if unique:
                clean[str(key)] = {"path": path, "owners": unique}
        self.session.policy[HOOK_WORKSPACE_REFS] = clean
        return clean

    def add_workspace(self, path, hook_session):
        """Hook session start: add a workspace only if the session owns it.

        If the path is already an allowed/static scope, the hook session does not
        take ownership, so its finish event cannot remove the pre-existing scope.
        If another hook session owns the same dynamic scope, we add a second owner
        and remove the scope only after the last owner finishes.
        """
        with self._lock:
            hook_session = self._validate_hook_session(hook_session)
            workspace, canonical = self._validate_workspace(path)
            workspaces = list(self._workspace_scopes())
            refs = self._hook_workspace_refs()
            key = canonical
            for old_key, entry in list(refs.items()):
                if old_key == key:
                    continue
                owners = [owner for owner in entry.get("owners", [])
                          if owner != hook_session]
                if len(owners) == len(entry.get("owners", [])):
                    continue
                if owners:
                    entry["owners"] = owners
                else:
                    refs.pop(old_key, None)
                    workspaces = [item for item in workspaces
                                  if self._canonical_key(item) != old_key]
            workspace_keys = {self._canonical_key(item) for item in workspaces}
            allowed_keys = {self._canonical_key(item)
                            for item in self.session.policy.get("allowed_scopes", ())}
            added = False
            owned = False

            if key in refs:
                owners = refs[key].setdefault("owners", [])
                if hook_session not in owners:
                    owners.append(hook_session)
                owned = True
            elif key in allowed_keys or key in workspace_keys:
                owned = False
            else:
                workspaces.append(workspace)
                refs[key] = {"path": workspace, "owners": [hook_session]}
                added = True
                owned = True

            workspaces, allowed = self._rewrite_workspace_scopes(workspaces)
            if not self.session.workspace:
                self.session.workspace = workspace
            self.session.add_event("turn-workspace-add",
                                   "%s %s" % (hook_session, workspace))
            self.store.save(self.session)
            return self._workspace_response("add", workspace, workspaces,
                                            allowed, hook_session=hook_session,
                                            added=added, owned=owned)

    def remove_workspace(self, path, hook_session):
        """Hook session finish: remove only scopes this hook session added."""
        with self._lock:
            hook_session = self._validate_hook_session(hook_session)
            workspace, canonical = self._validate_workspace(path)
            workspaces = list(self._workspace_scopes())
            refs = self._hook_workspace_refs()
            key = canonical
            removed = False
            entry = refs.get(key)
            if entry is not None:
                owners = [owner for owner in entry.get("owners", [])
                          if owner != hook_session]
                if owners:
                    entry["owners"] = owners
                else:
                    refs.pop(key, None)
                    workspaces = [item for item in workspaces
                                  if self._canonical_key(item) != key]
                    removed = True

            workspaces, allowed = self._rewrite_workspace_scopes(workspaces)
            try:
                current_key = self._canonical_key(self.session.workspace)
            except ValueError:
                current_key = None
            if current_key == key and removed and workspaces:
                self.session.workspace = workspaces[0]
            self.session.add_event("turn-workspace-remove",
                                   "%s %s" % (hook_session, workspace))
            self.store.save(self.session)
            return self._workspace_response("remove", workspace, workspaces,
                                            allowed, hook_session=hook_session,
                                            removed=removed)

    def reset_agent_workspaces(self):
        """Drop stale hook-owned inner agent-session workspace scopes.

        Called before launching/resuming a contained agent process. Any live
        inner agent sessions from the previous process are gone; fresh
        SessionStart hooks will re-add their current workspace.
        """
        with self._lock:
            refs = self._hook_workspace_refs()
            stale_keys = set(refs)
            removed = []
            workspaces = []
            for item in self._workspace_scopes():
                key = self._canonical_key(item)
                if key in stale_keys:
                    removed.append(item)
                else:
                    workspaces.append(item)
            self.session.policy[HOOK_WORKSPACE_REFS] = {}
            workspaces, allowed = self._rewrite_workspace_scopes(workspaces)
            if removed:
                self.session.add_event("turn-workspace-reset",
                                       "%d stale scope(s)" % len(removed))
            self.store.save(self.session)
            return {"verdict": VERDICT_WORKSPACE_UPDATED,
                    "action": "reset",
                    "removed": removed,
                    "workspaces": list(workspaces),
                    "allowed_scopes": list(allowed)}

    def _apply(self, changes):
        """Copy each change from the live view into the base (or delete it).

        Idempotent: re-applying an unchanged path just re-copies identical
        bytes.  Reads come from the FUSE view (root.mount) so they reflect the
        agent's latest content; writes go straight to the real underlay.
        """
        applied = []
        denied = []
        for ch in changes:
            root, rel = self._change_root_rel(ch)
            if root is None or rel is None:
                continue
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
                DECISION_COMMITTED, DECISION_KEPT)
            new_oos = oos - resolved_paths
            # commit in-scope changes + any previously approved out-of-scope;
            # keep remembered/rejected in-scope paths out of repeated retries
            # until the user explicitly resolves them.
            held_paths = self._paths_with_decisions(DECISION_KEPT)
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

    def _unmark_paths(self, paths):
        decisions = self._decision_map()
        for path in sorted(set(paths)):
            decisions.pop(path, None)

    def _discard_paths(self, paths):
        paths = sorted(set(paths))
        if not paths:
            return [], []
        live_by_path = {c.path: c for c in self._live_changes()}
        discarded = []
        stale = []
        for path in paths:
            change = live_by_path.get(path)
            if change is None:
                stale.append(path)
                continue
            root, rel = self._change_root_rel(change)
            if root is None or rel is None:
                stale.append(path)
                continue
            self.backend.revert_path(root, rel)
            discarded.append(path)

        remaining = {c.path for c in self._live_changes()}
        failed = sorted(set(discarded) & remaining)
        if failed:
            raise RuntimeError("discard did not remove live BranchFS change(s): %s"
                               % ", ".join(failed))
        self._unmark_paths(discarded + stale)
        return discarded, stale

    def _decision_path_view(self, decision):
        live_paths = {c.path for c in self._live_changes()}
        remembered = self._paths_with_decisions(decision)
        current = sorted(remembered & live_paths)
        stale = sorted(remembered - live_paths)
        return current, stale

    def _kept_path_view(self):
        return self._decision_path_view(DECISION_KEPT)

    def kept_status(self):
        """Read-only status for per-turn decisions in the live branch."""
        with self._lock:
            kept, stale = self._kept_path_view()
            committed, committed_stale = self._decision_path_view(
                DECISION_COMMITTED)
            return {"verdict": VERDICT_KEPT_STATUS, "kept": kept,
                    "committed": committed,
                    "stale": stale,
                    "committed_stale": committed_stale,
                    "count": len(kept),
                    "committed_count": len(committed)}

    def review_kept(self):
        """Final/idle check: ask the user only if kept paths still exist."""
        with self._lock:
            kept, stale = self._kept_path_view()
            if not kept:
                return {"verdict": VERDICT_NOOP, "kept": [], "stale": stale,
                        "message": "no kept non-workspace paths"}
            msg = (
                "%d kept non-workspace path(s) remain pending. Use the ccc "
                "MCP server: call ccc_list_kept if exact paths are needed, "
                "then ccc_commit_kept, ccc_discard_kept, or ccc_keep_kept. "
                "Commit/discard require nested human confirmation." % len(kept))
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
        discarded, discard_stale = self._discard_paths(discard_now)
        return committed, kept, discarded, discard_stale

    def approve_turn(self, approval_token, decision, paths=None,
                     commit_paths=None, keep_paths=None, discard_paths=None):
        """Resolve an out-of-scope turn with one of the four review actions:

          accept-all   decision in _YES                -> commit every flagged path
          reject/revert decision in _REVERT            -> remove selected branch
                                                          deltas/tombstones via
                                                          BranchFS revert-path
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
                committed, kept, discarded, discard_stale = self._resolve_granular(
                    pending, commit_paths=commit_paths, keep_paths=keep_paths,
                    discard_paths=discard_paths)
                self.session.add_event(
                    "turn-approved-granular",
                    "committed %d, kept %d, discarded %d" %
                    (len(committed), len(kept), len(discarded)))
                self.store.save(self.session)
                verdict = (VERDICT_COMMITTED if committed else
                           VERDICT_HELD if kept else
                           VERDICT_DISCARDED if discarded else VERDICT_NOOP)
                return {"verdict": verdict, "committed": committed,
                        "kept": kept, "held": kept,
                        "discarded": discarded, "stale": discard_stale}

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
                discarded, stale = self._discard_paths(pending)
                self.session.add_event("turn-discarded",
                                       "%d path(s)" % len(discarded))
                self.store.save(self.session)
                return {"verdict": (VERDICT_DISCARDED if discarded
                                    else VERDICT_NOOP),
                        "discarded": discarded, "stale": stale}

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
            current_kept, _stale = self._kept_path_view()
            not_kept = sorted(paths - set(current_kept))
            if not_kept:
                raise ValueError(
                    "turn-resolve accepts only currently remembered kept paths: %s"
                    % ", ".join(not_kept))
            decision = str(decision or "").strip().lower()
            if decision in self._YES:
                committed = self._commit_paths(paths)
                self.session.add_event("turn-resolved-commit",
                                       "%d path(s)" % len(committed))
                self.store.save(self.session)
                return {"verdict": VERDICT_COMMITTED,
                        "committed": committed}
            if decision in self._REVERT:
                discarded, stale = self._discard_paths(paths)
                self.session.add_event("turn-resolved-discard",
                                       "%d path(s)" % len(discarded))
                self.store.save(self.session)
                return {"verdict": (VERDICT_DISCARDED if discarded
                                    else VERDICT_NOOP),
                        "discarded": discarded, "stale": stale}
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
        if op == "turn-add-workspace":
            return self.add_workspace(request.get("path"),
                                      request.get("hook_session"))
        if op == "turn-remove-workspace":
            return self.remove_workspace(request.get("path"),
                                         request.get("hook_session"))
        return {"ok": False, "error": "unknown op %r" % (op,)}
