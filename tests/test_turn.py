"""Supervisor-side per-turn handler tests (ccc_agent.turn.TurnController),
driven by FakeBranchFS — no FUSE.

Covers the Stop-boundary state machine with selective view->base apply:
in-scope -> copied to base + session continues; out-of-scope -> needs-approval
+ token; approve yes/no; mixed turns; and that decided out-of-scope paths are
not re-prompted.
"""

import os
import tempfile
import unittest

from ccc_agent.branchfs import FakeBranchFS
from ccc_agent.control import (VERDICT_COMMITTED, VERDICT_DISCARDED,
                               VERDICT_HELD, VERDICT_NEEDS_APPROVAL,
                               VERDICT_NOOP)
from ccc_agent.paths import AliasMap
from ccc_agent.runner import RootSpec
from ccc_agent.session import SessionStore
from ccc_agent.turn import TurnController


class TurnHarness(object):
    def __init__(self, tmp):
        self.base = os.path.join(tmp, "base")
        os.makedirs(os.path.join(self.base, "Projects", "proj-a"))
        self.backend = FakeBranchFS()
        self.store = SessionStore(os.path.join(tmp, "state"))
        self.alias = AliasMap.for_home("domen", home_subdir="")
        spec = RootSpec(name="r", base=self.base,
                        store=os.path.join(tmp, "store"),
                        visible="/storage/user", home_subdir="")
        self.session = self.store.create(
            owner="domen", agent_kind="t", agent_command=["x"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "workspace-auto",
                    "allowed_scopes": ["/storage/user/Projects/proj-a"]},
            protected_roots={})
        self.session.protected_roots = {
            "r": spec.materialize(self.session.session_id,
                                  self.store.state_dir)}
        self.store.save(self.session)
        self.root = self.session.protected_roots["r"]
        self.backend.start_daemon(self.root)
        self.backend.create_branch(self.root)
        self.backend.mount(self.root)
        self.tc = TurnController(self.session, self.store, self.backend,
                                 self.alias)

    def write(self, rel, content):
        p = os.path.join(self.root.mount, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(content)

    def base_has(self, rel):
        return os.path.isfile(os.path.join(self.base, rel))


class TestTurnController(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = TurnHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_in_scope_turn_commits_and_session_continues(self):
        self.h.write("Projects/proj-a/a.txt", "one")
        resp = self.h.tc.finalize_turn()
        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("Projects/proj-a/a.txt"))
        self.assertEqual(self.h.backend.branch_state(self.h.root), "open")
        # next turn keeps working; only the new file matters
        self.h.write("Projects/proj-a/b.txt", "two")
        resp2 = self.h.tc.finalize_turn()
        self.assertEqual(resp2["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("Projects/proj-a/b.txt"))

    def test_in_scope_turn_records_committed_paths_for_review_display(self):
        self.h.write("Projects/proj-a/a.txt", "one")

        self.h.tc.finalize_turn()

        persisted = self.h.store.load(self.h.session.session_id)
        self.assertEqual(
            persisted.policy["turn_path_decisions"][
                "/storage/user/Projects/proj-a/a.txt"],
            "committed")

    def test_out_of_scope_turn_needs_approval_and_does_not_commit(self):
        self.h.write("escape.txt", "x")          # /storage/user/escape.txt
        resp = self.h.tc.finalize_turn()
        self.assertEqual(resp["verdict"], VERDICT_NEEDS_APPROVAL)
        self.assertIn("/storage/user/escape.txt", resp["out_of_scope"])
        self.assertIn("approval_token", resp)
        self.assertFalse(self.h.base_has("escape.txt"))

    def test_mixed_turn_commits_in_scope_holds_out_of_scope(self):
        self.h.write("Projects/proj-a/ok.txt", "ok")
        self.h.write("escape.txt", "no")
        resp = self.h.tc.finalize_turn()
        self.assertEqual(resp["verdict"], VERDICT_NEEDS_APPROVAL)
        self.assertTrue(self.h.base_has("Projects/proj-a/ok.txt"))  # in-scope
        self.assertFalse(self.h.base_has("escape.txt"))             # held
        self.assertIn("/storage/user/Projects/proj-a/ok.txt",
                      resp["committed"])

    def test_in_scope_permission_denied_turn_commits_others_and_keeps_blocked(self):
        readonly = os.path.join(self.h.base, "Projects", "proj-a", "readonly")
        os.makedirs(readonly, exist_ok=True)
        os.chmod(readonly, 0o555)
        try:
            self.h.write("Projects/proj-a/ok.txt", "ok")
            self.h.write("Projects/proj-a/readonly/no.txt", "blocked")

            resp = self.h.tc.finalize_turn()
            resp2 = self.h.tc.finalize_turn()
        finally:
            os.chmod(readonly, 0o755)

        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertIn("/storage/user/Projects/proj-a/ok.txt",
                      resp["committed"])
        self.assertEqual(resp["kept"],
                         ["/storage/user/Projects/proj-a/readonly/no.txt"])
        self.assertEqual(resp["permission_denied"], resp["kept"])
        self.assertTrue(self.h.base_has("Projects/proj-a/ok.txt"))
        self.assertFalse(self.h.base_has("Projects/proj-a/readonly/no.txt"))
        decisions = self.h.store.load(
            self.h.session.session_id).policy["turn_path_decisions"]
        self.assertEqual(
            decisions["/storage/user/Projects/proj-a/readonly/no.txt"],
            "kept")
        self.assertEqual(resp2.get("permission_denied", []), [])
        self.assertNotIn("/storage/user/Projects/proj-a/readonly/no.txt",
                         resp2.get("committed", []))

    def test_approve_yes_commits_the_out_of_scope_changes(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        resp = self.h.tc.approve_turn(token, "yes")
        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("escape.txt"))

    def test_approve_no_holds_and_does_not_reprompt(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        resp = self.h.tc.approve_turn(token, "no")
        self.assertEqual(resp["verdict"], VERDICT_HELD)
        self.assertFalse(self.h.base_has("escape.txt"))
        # the denied path lingers in the branch but must NOT be re-prompted
        self.h.write("Projects/proj-a/c.txt", "c")
        resp2 = self.h.tc.finalize_turn()
        self.assertEqual(resp2["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("Projects/proj-a/c.txt"))

    def test_approve_keep_holds_without_committing(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        resp = self.h.tc.approve_turn(token, "keep")
        self.assertEqual(resp["verdict"], VERDICT_HELD)
        self.assertFalse(self.h.base_has("escape.txt"))

    def test_default_keep_commits_in_scope_and_keeps_oos_without_prompt(self):
        self.h.write("Projects/proj-a/ok.txt", "ok")
        self.h.write("escape.txt", "x")

        resp = self.h.tc.finalize_turn(default_keep=True)

        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertIn("/storage/user/Projects/proj-a/ok.txt",
                      resp["committed"])
        self.assertEqual(resp["kept"], ["/storage/user/escape.txt"])
        self.assertNotIn("approval_token", resp)
        self.assertTrue(self.h.base_has("Projects/proj-a/ok.txt"))
        self.assertFalse(self.h.base_has("escape.txt"))
        persisted = self.h.store.load(self.h.session.session_id)
        self.assertEqual(
            persisted.policy["turn_path_decisions"]["/storage/user/escape.txt"],
            "kept")

    def test_first_hook_workspace_initializes_scope_for_server_session(self):
        self.h.session.workspace = None
        self.h.session.policy["workspace_scopes"] = []
        self.h.session.policy["allowed_scopes"] = []
        self.h.store.save(self.h.session)

        resp = self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                       hook_session="hook-a")

        self.assertEqual(resp["workspaces"], [
            "/storage/user/Projects/proj-b",
        ])
        persisted = self.h.store.load(self.h.session.session_id)
        self.assertEqual(persisted.workspace, "/storage/user/Projects/proj-b")
        self.assertEqual(persisted.policy["allowed_scopes"], [
            "/storage/user/Projects/proj-b",
        ])
        self.h.write("Projects/proj-b/b.txt", "two")
        self.assertEqual(self.h.tc.finalize_turn()["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("Projects/proj-b/b.txt"))

    def test_hook_session_add_workspace_allows_new_workspace_changes(self):
        resp = self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                       hook_session="hook-a")
        self.assertEqual(resp["verdict"], "workspace-updated")
        self.assertTrue(resp["added"])
        self.assertTrue(resp["owned"])
        self.assertEqual(resp["workspaces"], [
            "/storage/user/Projects/proj-a",
            "/storage/user/Projects/proj-b",
        ])

        self.h.write("Projects/proj-b/b.txt", "two")
        commit = self.h.tc.finalize_turn()

        self.assertEqual(commit["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("Projects/proj-b/b.txt"))

    def test_hook_session_remove_workspace_only_removes_scope_it_added(self):
        self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                hook_session="hook-a")
        resp = self.h.tc.remove_workspace("/storage/user/Projects/proj-b",
                                          hook_session="hook-a")
        self.assertEqual(resp["verdict"], "workspace-updated")
        self.assertTrue(resp["removed"])
        self.assertEqual(resp["workspaces"], ["/storage/user/Projects/proj-a"])

        self.h.write("Projects/proj-b/b.txt", "two")
        commit = self.h.tc.finalize_turn()

        self.assertEqual(commit["verdict"], VERDICT_NEEDS_APPROVAL)
        self.assertIn("/storage/user/Projects/proj-b/b.txt",
                      commit["out_of_scope"])
        self.assertFalse(self.h.base_has("Projects/proj-b/b.txt"))

    def test_hook_session_does_not_remove_preexisting_workspace(self):
        add = self.h.tc.add_workspace("/storage/user/Projects/proj-a",
                                      hook_session="hook-a")
        self.assertFalse(add["added"])
        self.assertFalse(add["owned"])

        remove = self.h.tc.remove_workspace("/storage/user/Projects/proj-a",
                                            hook_session="hook-a")

        self.assertFalse(remove["removed"])
        self.assertEqual(remove["workspaces"], ["/storage/user/Projects/proj-a"])
        self.h.write("Projects/proj-a/still-ok.txt", "ok")
        self.assertEqual(self.h.tc.finalize_turn()["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("Projects/proj-a/still-ok.txt"))

    def test_shared_hook_workspace_is_removed_after_last_owner_finishes(self):
        self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                hook_session="hook-a")
        add_b = self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                        hook_session="hook-b")
        self.assertFalse(add_b["added"])
        self.assertTrue(add_b["owned"])

        remove_a = self.h.tc.remove_workspace("/storage/user/Projects/proj-b",
                                              hook_session="hook-a")
        self.assertFalse(remove_a["removed"])
        self.assertIn("/storage/user/Projects/proj-b", remove_a["workspaces"])

        remove_b = self.h.tc.remove_workspace("/storage/user/Projects/proj-b",
                                              hook_session="hook-b")
        self.assertTrue(remove_b["removed"])
        self.assertNotIn("/storage/user/Projects/proj-b", remove_b["workspaces"])

    def test_agent_session_workspace_update_replaces_only_that_session_workspace(self):
        self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                hook_session="agent-session-a")
        update = self.h.tc.add_workspace("/storage/user/Projects/proj-c",
                                         hook_session="agent-session-a")

        self.assertIn("/storage/user/Projects/proj-c", update["workspaces"])
        self.assertNotIn("/storage/user/Projects/proj-b", update["workspaces"])

        self.h.write("Projects/proj-b/old.txt", "old")
        self.h.write("Projects/proj-c/new.txt", "new")
        commit = self.h.tc.finalize_turn()

        self.assertEqual(commit["verdict"], VERDICT_NEEDS_APPROVAL)
        self.assertIn("/storage/user/Projects/proj-b/old.txt",
                      commit["out_of_scope"])
        self.assertTrue(self.h.base_has("Projects/proj-c/new.txt"))
        self.assertFalse(self.h.base_has("Projects/proj-b/old.txt"))

    def test_turn_workspace_must_be_under_a_protected_root(self):
        with self.assertRaises(ValueError):
            self.h.tc.add_workspace("/tmp/not-protected", hook_session="hook-a")

    def test_turn_workspace_requires_hook_session_identifier(self):
        with self.assertRaises(ValueError):
            self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                    hook_session="")

    def test_reset_agent_workspaces_drops_stale_hook_owned_scopes_for_resume(self):
        self.h.tc.add_workspace("/storage/user/Projects/proj-b",
                                hook_session="agent-session-a")

        reset = self.h.tc.reset_agent_workspaces()

        self.assertEqual(reset["removed"], ["/storage/user/Projects/proj-b"])
        self.assertEqual(reset["workspaces"], ["/storage/user/Projects/proj-a"])
        self.h.write("Projects/proj-b/stale.txt", "stale")
        commit = self.h.tc.finalize_turn()
        self.assertEqual(commit["verdict"], VERDICT_NEEDS_APPROVAL)
        self.assertIn("/storage/user/Projects/proj-b/stale.txt",
                      commit["out_of_scope"])

    def test_keep_decision_is_persisted_and_not_reprompted(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        self.h.tc.approve_turn(token, "keep")

        persisted = self.h.store.load(self.h.session.session_id)
        self.assertEqual(
            persisted.policy["turn_path_decisions"]["/storage/user/escape.txt"],
            "kept")

        # Reconstruct the controller as a process/control-server restart would.
        tc2 = TurnController(persisted, self.h.store, self.h.backend,
                             self.h.alias)
        self.h.write("Projects/proj-a/c.txt", "c")
        resp2 = tc2.finalize_turn()
        self.assertEqual(resp2["verdict"], VERDICT_COMMITTED)
        self.assertNotIn("approval_token", resp2)
        self.assertTrue(self.h.base_has("Projects/proj-a/c.txt"))
        self.assertFalse(self.h.base_has("escape.txt"))

    def test_later_commit_of_kept_path_commits_and_stops_prompting(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        self.h.tc.approve_turn(token, "keep")

        persisted = self.h.store.load(self.h.session.session_id)
        tc2 = TurnController(persisted, self.h.store, self.h.backend,
                             self.h.alias)
        resp = tc2.resolve_turn("commit", ["/storage/user/escape.txt"])

        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("escape.txt"))
        persisted2 = self.h.store.load(self.h.session.session_id)
        self.assertEqual(
            persisted2.policy["turn_path_decisions"]["/storage/user/escape.txt"],
            "committed")
        self.assertIn("/storage/user/escape.txt",
                      persisted2.policy["allowed_scopes"])

    def test_later_discard_of_kept_path_removes_branch_delta(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        self.h.tc.approve_turn(token, "keep")

        persisted = self.h.store.load(self.h.session.session_id)
        tc2 = TurnController(persisted, self.h.store, self.h.backend,
                             self.h.alias)
        with self.assertRaises(ValueError):
            tc2.resolve_turn("commit", ["/storage/user/not-kept.txt"])

        resp = tc2.resolve_turn("discard", ["/storage/user/escape.txt"])

        self.assertEqual(resp["verdict"], "discarded")
        self.assertEqual(resp["discarded"], ["/storage/user/escape.txt"])
        self.assertEqual(tc2.kept_status()["kept"], [])
        self.assertNotIn("/storage/user/escape.txt",
                         {c.path for c in self.h.backend.status(self.h.root)})
        persisted2 = self.h.store.load(self.h.session.session_id)
        self.assertNotIn("/storage/user/escape.txt",
                         persisted2.policy.get("turn_path_decisions", {}))

    def test_later_discard_of_kept_delete_removes_tombstone(self):
        with open(os.path.join(self.h.base, "escape.txt"), "w") as fh:
            fh.write("base\n")
        self.h.backend.record_delete(self.h.root, "escape.txt")
        resp = self.h.tc.finalize_turn(default_keep=True)
        self.assertEqual(resp["kept"], ["/storage/user/escape.txt"])

        resp2 = self.h.tc.resolve_turn("discard", ["/storage/user/escape.txt"])

        self.assertEqual(resp2["verdict"], "discarded")
        self.assertEqual(resp2["discarded"], ["/storage/user/escape.txt"])
        self.assertEqual(self.h.backend.status(self.h.root), [])
        self.assertTrue(self.h.base_has("escape.txt"))

    def test_kept_status_lists_live_committed_and_kept_paths(self):
        self.h.write("Projects/proj-a/ok.txt", "ok")
        self.h.write("escape.txt", "x")
        resp = self.h.tc.finalize_turn(default_keep=True)
        self.assertEqual(resp["kept"], ["/storage/user/escape.txt"])

        status = self.h.tc.kept_status()

        self.assertEqual(status["verdict"], "kept-status")
        self.assertEqual(status["committed"],
                         ["/storage/user/Projects/proj-a/ok.txt"])
        self.assertEqual(status["kept"], ["/storage/user/escape.txt"])
        self.assertEqual(status["count"], 1)

    def test_review_kept_requests_user_decision_only_when_kept_paths_exist(self):
        self.assertEqual(self.h.tc.review_kept()["verdict"], VERDICT_NOOP)
        self.h.write("escape.txt", "x")
        self.h.tc.finalize_turn(default_keep=True)

        review = self.h.tc.review_kept()

        self.assertEqual(review["verdict"], "needs-kept-review")
        self.assertEqual(review["kept"], ["/storage/user/escape.txt"])
        self.assertIn("ccc_list_kept", review["message"])

    def test_granular_approval_can_commit_discard_and_keep_paths(self):
        self.h.write("commit-me.txt", "a")
        self.h.write("discard-me.txt", "b")
        self.h.write("keep-me.txt", "c")
        token = self.h.tc.finalize_turn()["approval_token"]

        resp = self.h.tc.approve_turn(
            token, "select",
            commit_paths=["/storage/user/commit-me.txt"],
            discard_paths=["/storage/user/discard-me.txt"],
            keep_paths=["/storage/user/keep-me.txt"])

        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("commit-me.txt"))
        self.assertFalse(self.h.base_has("discard-me.txt"))
        self.assertFalse(self.h.base_has("keep-me.txt"))
        self.assertEqual(resp["discarded"], ["/storage/user/discard-me.txt"])
        self.assertEqual(resp["kept"], ["/storage/user/keep-me.txt"])
        self.assertNotIn("/storage/user/discard-me.txt",
                         {c.path for c in self.h.backend.status(self.h.root)})
        decisions = self.h.store.load(
            self.h.session.session_id).policy["turn_path_decisions"]
        self.assertEqual(decisions["/storage/user/commit-me.txt"], "committed")
        self.assertNotIn("/storage/user/discard-me.txt", decisions)
        self.assertEqual(decisions["/storage/user/keep-me.txt"], "kept")

    def test_approve_revert_discards_from_branchfs(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        resp = self.h.tc.approve_turn(token, "revert")
        self.assertEqual(resp["verdict"], VERDICT_DISCARDED)
        self.assertIn("/storage/user/escape.txt", resp["discarded"])
        self.assertFalse(self.h.base_has("escape.txt"))
        self.assertEqual(self.h.backend.status(self.h.root), [])

    def test_approve_file_level_subset_commits_only_chosen(self):
        self.h.write("escape1.txt", "a")
        self.h.write("escape2.txt", "b")
        resp = self.h.tc.finalize_turn()
        token = resp["approval_token"]
        self.assertEqual(resp["verdict"], VERDICT_NEEDS_APPROVAL)
        out = self.h.tc.approve_turn(token, "select",
                                     paths=["/storage/user/escape1.txt"])
        self.assertEqual(out["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.h.base_has("escape1.txt"))     # chosen
        self.assertFalse(self.h.base_has("escape2.txt"))    # held
        self.assertIn("/storage/user/escape2.txt", out["held"])

    def test_unknown_token_raises(self):
        with self.assertRaises(KeyError):
            self.h.tc.approve_turn("bogus-token", "yes")

    def test_token_single_use(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        self.h.tc.approve_turn(token, "yes")
        with self.assertRaises(KeyError):
            self.h.tc.approve_turn(token, "yes")

    def test_ignored_paths_are_dropped(self):
        # cred-dir mountpoints (etc.) live under an ignore pattern and must be
        # neither flagged nor committed.
        self.h.session.policy["ignore_patterns"] = ["/storage/user/.codex"]
        self.h.write(".codex/config.toml", "x")
        resp = self.h.tc.finalize_turn()
        self.assertEqual(resp["verdict"], VERDICT_NOOP)
        self.assertFalse(self.h.base_has(".codex/config.toml"))

    def test_noop_turn(self):
        resp = self.h.tc.finalize_turn()
        self.assertEqual(resp["verdict"], VERDICT_NOOP)

    def test_delete_is_committed_in_scope(self):
        # seed a file in base, then tombstone it inside the workspace
        os.makedirs(os.path.join(self.h.base, "Projects", "proj-a"),
                    exist_ok=True)
        with open(os.path.join(self.h.base, "Projects", "proj-a", "old.txt"),
                  "w") as fh:
            fh.write("old")
        self.h.backend.record_delete(self.h.root, "Projects/proj-a/old.txt")
        resp = self.h.tc.finalize_turn()
        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertFalse(self.h.base_has("Projects/proj-a/old.txt"))

    def test_handle_dispatch(self):
        self.h.write("Projects/proj-a/a.txt", "one")
        resp = self.h.tc.handle({"op": "turn-finalize"})
        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        bad = self.h.tc.handle({"op": "nonsense"})
        self.assertFalse(bad["ok"])


if __name__ == "__main__":
    unittest.main()
