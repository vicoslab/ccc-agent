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
from ccc_agent.control import (VERDICT_COMMITTED, VERDICT_HELD,
                               VERDICT_NEEDS_APPROVAL, VERDICT_NOOP)
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

    def test_approve_revert_holds_and_asks_agent_to_undo(self):
        self.h.write("escape.txt", "x")
        token = self.h.tc.finalize_turn()["approval_token"]
        resp = self.h.tc.approve_turn(token, "revert")
        self.assertEqual(resp["verdict"], VERDICT_HELD)
        self.assertIn("/storage/user/escape.txt", resp["revert"])
        self.assertFalse(self.h.base_has("escape.txt"))

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
