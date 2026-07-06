"""Per-turn (Stop-boundary) finalize handler — the supervisor side of the
control channel.

At a Stop the agent is idle, so the supervisor (which runs OUTSIDE the sandbox
and can see both the live BranchFS view and the real base) commits a turn by
**selectively copying the in-scope changes from the view into the base** and
applying in-scope deletes.  This needs no branchfs teardown and never disturbs
the agent's live mount, so the session simply continues:

  turn-finalize  in-scope changes -> copied to base, agent continues (committed)
                 out-of-scope     -> held; needs-approval (+ token) relayed to
                                     the user via the agent UI
                 nothing changed  -> noop
  turn-approve   yes -> copy the approved out-of-scope paths to base
                 no  -> record as denied (left in the branch for session-end
                        review; not re-prompted)

The branch's deltas are intentionally left in place (commit-in-place under a
live FUSE mount churns inodes -> ESTALE on NFS); re-applying an unchanged path
is idempotent, and the supervisor tracks which out-of-scope paths were already
approved/denied so they are not re-prompted.

Threat model is naive/accidental (see docs/architecture.md): the supervisor
holds commit authority and only copies out-of-scope work to base on a relayed
user "yes"; an agent can spoof its OWN approval but never escapes the in-scope
policy.
"""

import binascii
import os
import shutil
import threading

from .control import (VERDICT_COMMITTED, VERDICT_HELD, VERDICT_NEEDS_APPROVAL,
                      VERDICT_NOOP)
from .policy import PolicyConfig, classify, filter_ignored


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
        self._approved = set()   # out-of-scope visible paths the user approved
        self._denied = set()     # out-of-scope visible paths the user declined
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
        for ch in changes:
            root = roots.get(ch.root)
            if root is None:
                continue
            visible = self.alias_map.canonicalize(root.visible)
            rel = os.path.relpath(self.alias_map.canonicalize(ch.path), visible)
            dst = os.path.join(root.base, rel)
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
            applied.append(ch.path)
        return applied

    # -- control ops -------------------------------------------------------
    def finalize_turn(self):
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
            new_oos = oos - self._approved - self._denied
            # commit in-scope changes + any previously approved out-of-scope
            to_apply = [c for c in changes
                        if c.path not in oos or c.path in self._approved]
            committed = self._apply(to_apply)

            if new_oos:
                token = _new_token()
                self._pending[token] = frozenset(new_oos)
                self.session.add_event(
                    "turn-needs-approval",
                    "%d new out-of-scope path(s)" % len(new_oos))
                self.store.save(self.session)
                return {"verdict": VERDICT_NEEDS_APPROVAL,
                        "out_of_scope": sorted(new_oos),
                        "approval_token": token,
                        "committed": committed}

            self.session.add_event("turn-committed",
                                   "%d change(s) applied" % len(committed))
            self.store.save(self.session)
            return {"verdict": VERDICT_COMMITTED if committed else VERDICT_NOOP,
                    "committed": committed}

    _YES = ("yes", "y", "true", "1", "approve", "ok", "all", "accept")
    _REVERT = ("revert", "reject", "discard", "undo")

    def _commit_paths(self, paths):
        """Copy the chosen view paths into base and mark them allowed so the
        session-end finalize agrees (the lingering deltas would otherwise
        re-flag as out-of-scope despite already being in base)."""
        paths = set(paths)
        if not paths:
            return []
        self._approved |= paths
        scopes = self.session.policy.setdefault("allowed_scopes", [])
        for path in paths:
            if path not in scopes:
                scopes.append(path)
        changes = [c for c in self._live_changes() if c.path in paths]
        return self._apply(changes)

    def approve_turn(self, approval_token, decision, paths=None):
        """Resolve an out-of-scope turn with one of the four review actions:

          accept-all   decision in _YES                -> commit every flagged path
          reject/revert decision in _REVERT            -> hold + tell the agent to
                                                          undo them (naive model:
                                                          the supervisor cannot
                                                          safely strip deltas under
                                                          a live mount)
          keep          decision = "no"/"keep" (default)-> leave deltas uncommitted,
                                                          session continues
          file-level    paths=[...]                    -> commit only that subset,
                                                          hold the rest
        """
        with self._lock:
            if approval_token not in self._pending:
                raise KeyError("unknown or already-resolved approval token")
            pending = set(self._pending.pop(approval_token))
            decision = str(decision or "").strip().lower()

            if paths:
                chosen = set(paths) & pending
                held = pending - chosen
                committed = self._commit_paths(chosen)
                self._denied |= held
                self.session.add_event(
                    "turn-approved-partial",
                    "committed %d, held %d" % (len(chosen), len(held)))
                self.store.save(self.session)
                return {"verdict": VERDICT_COMMITTED, "committed": committed,
                        "held": sorted(held)}

            if decision in self._YES:
                committed = self._commit_paths(pending)
                self.session.add_event("turn-approved",
                                       "user approved %d path(s)" % len(pending))
                self.store.save(self.session)
                return {"verdict": VERDICT_COMMITTED, "committed": committed}

            if decision in self._REVERT:
                self._denied |= pending
                self.session.add_event("turn-revert-requested",
                                       "%d path(s)" % len(pending))
                self.store.save(self.session)
                return {"verdict": VERDICT_HELD, "revert": sorted(pending),
                        "message": "the user rejected these changes; revert "
                                   "them in your workspace (restore original "
                                   "content or delete files you created)"}

            # default: keep deltas, do not commit, session continues
            self._denied |= pending
            self.session.add_event("turn-held",
                                   "user kept %d path(s) uncommitted"
                                   % len(pending))
            self.store.save(self.session)
            return {"verdict": VERDICT_HELD, "denied": sorted(pending)}

    # -- ControlServer entrypoint -----------------------------------------
    def handle(self, request):
        op = request.get("op")
        if op == "turn-finalize":
            return self.finalize_turn()
        if op == "turn-approve":
            return self.approve_turn(request.get("approval_token"),
                                     request.get("decision", "no"),
                                     paths=request.get("paths"))
        return {"ok": False, "error": "unknown op %r" % (op,)}
