"""Per-logical-session trusted workspace authority tests."""

import json
import tempfile
import unittest

from ccc_agent.session import Session
from tests.test_turn import TurnHarness


class TestSessionWorkspaceState(unittest.TestCase):
    def test_round_trip_preserves_versioned_workspace_sessions_and_routes(self):
        session = Session(
            session_id="agent-outer", owner="domen", agent_kind="codex-remote",
            agent_command=["codex", "app-server"], workspace=None,
            policy={"mode": "workspace-auto", "allowed_scopes": []},
            protected_roots={},
            authenticated_workspace_sessions={
                "codex-app-server:" + "a" * 64: {
                    "source": "codex-app-server",
                    "logical_session_digest": "sha256:" + "a" * 64,
                    "generation": 4,
                    "roots": [],
                    "state": "active",
                    "updated_at": "2026-07-16T10:00:00Z",
                },
            },
            session_delta_routes={"route-" + "b" * 32: {"schema_version": 2}},
        )

        encoded = json.loads(json.dumps(session.to_dict()))
        restored = Session.from_dict(encoded)

        self.assertEqual(encoded["workspace_state_version"], 2)
        self.assertEqual(restored.authenticated_workspace_sessions,
                         session.authenticated_workspace_sessions)
        self.assertEqual(restored.session_delta_routes,
                         session.session_delta_routes)

    def test_legacy_session_loads_without_assigning_union_to_a_logical_session(self):
        session = Session(
            session_id="agent-outer", owner="domen", agent_kind="codex-remote",
            agent_command=["codex"], workspace=None,
            policy={"mode": "workspace-auto", "allowed_scopes": [],
                    "mcp_workspace_roots": ["/storage/user/legacy"]},
            protected_roots={},
        )
        legacy = session.to_dict()
        legacy.pop("workspace_state_version")
        legacy.pop("authenticated_workspace_sessions")
        legacy.pop("session_delta_routes")

        restored = Session.from_dict(legacy)

        self.assertEqual(restored.authenticated_workspace_sessions, {})
        self.assertEqual(restored.session_delta_routes, {})
        self.assertEqual(restored.policy["mcp_workspace_roots"],
                         ["/storage/user/legacy"])


class TestWorkspaceSessionReplacement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = TurnHarness(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def replace(self, logical_id, generation, paths, state="active"):
        return self.h.tc.replace_workspace_session(
            source="codex-app-server", logical_session_id=logical_id,
            generation=generation, paths=paths, state=state)

    def test_two_sessions_replace_independently_and_derive_union(self):
        a = self.replace("thread-a", 1, ["/storage/user/Projects/proj-a"])
        b = self.replace("thread-b", 2, ["/storage/user/Projects/proj-b"])

        self.assertNotEqual(a["workspace_session_key"],
                            b["workspace_session_key"])
        self.assertEqual(self.h.session.policy["mcp_workspace_roots"], [
            "/storage/user/Projects/proj-a",
            "/storage/user/Projects/proj-b",
        ])
        self.assertEqual(len(self.h.session.authenticated_workspace_sessions), 2)
        serialized = json.dumps(self.h.session.to_dict())
        self.assertNotIn("thread-a", serialized)
        self.assertNotIn("thread-b", serialized)

    def test_stale_generation_cannot_restore_ended_roots(self):
        self.replace("thread-a", 10, ["/storage/user/Projects/proj-a"])
        self.replace("thread-a", 12, [], state="ended")

        with self.assertRaisesRegex(ValueError, "stale generation"):
            self.replace("thread-a", 11, ["/storage/user/Projects/proj-b"])

        self.assertEqual(self.h.session.policy["mcp_workspace_roots"], [])
        record = next(iter(
            self.h.session.authenticated_workspace_sessions.values()))
        self.assertEqual(record["state"], "ended")
        self.assertEqual(record["roots"], [])

    def test_same_generation_is_only_idempotent_for_same_complete_replacement(self):
        first = self.replace("thread-a", 3,
                             ["/storage/user/Projects/proj-a"])
        second = self.replace("thread-a", 3,
                              ["/storage/user/Projects/proj-a"])
        self.assertEqual(first["workspace_session_key"],
                         second["workspace_session_key"])
        with self.assertRaisesRegex(ValueError, "generation collision"):
            self.replace("thread-a", 3,
                         ["/storage/user/Projects/proj-b"])

    def test_ending_a_does_not_remove_b(self):
        self.replace("a", 1, ["/storage/user/Projects/proj-a"])
        self.replace("b", 1, ["/storage/user/Projects/proj-b"])
        self.replace("a", 2, [], state="ended")

        self.assertEqual(self.h.session.policy["mcp_workspace_roots"],
                         ["/storage/user/Projects/proj-b"])

    def test_workspace_inode_swap_invalidates_authority_before_auto_apply(self):
        self.replace("a", 1, ["/storage/user/Projects/proj-a"])
        self.h.write("Projects/proj-a/unsafe.txt", "must-not-commit")
        original = self.h.base + "/Projects/proj-a"
        moved = self.h.base + "/Projects/proj-a-old"
        import os
        os.rename(original, moved)
        os.makedirs(original)

        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.h.tc.finalize_turn()

        self.assertFalse(self.h.base_has("Projects/proj-a/unsafe.txt"))
        record = next(iter(
            self.h.session.authenticated_workspace_sessions.values()))
        self.assertEqual(record["state"], "invalid")
        self.assertEqual(self.h.session.policy["mcp_workspace_roots"], [])


if __name__ == "__main__":
    unittest.main()
