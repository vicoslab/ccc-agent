"""Per-logical-session trusted workspace authority tests."""

import json
import os
import tempfile
import unittest

from ccc_agent.runner import finalize_session
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
        os.rename(original, moved)
        os.makedirs(original)

        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.h.tc.finalize_turn()

        self.assertFalse(self.h.base_has("Projects/proj-a/unsafe.txt"))
        record = next(iter(
            self.h.session.authenticated_workspace_sessions.values()))
        self.assertEqual(record["state"], "invalid")
        self.assertEqual(self.h.session.policy["mcp_workspace_roots"], [])

    def test_codex_workspace_lifecycle_provisions_and_merges_route(self):
        self.h.session.policy["session_delta_routing"] = True
        self.h.session.policy["session_delta_routing_vendors"] = ["codex"]
        active = self.replace(
            "thread-route", 1, ["/storage/user/Projects/proj-a"])
        route = self.h.tc.route_manager.get(active["route"]["route_id"])
        child = route.roots["r"]
        routed = os.path.join(
            child.mount, "Projects", "proj-a", "from-route.txt")
        os.makedirs(os.path.dirname(routed), exist_ok=True)
        with open(routed, "w") as fh:
            fh.write("attributed")

        ended = self.replace("thread-route", 2, [], state="ended")

        self.assertEqual(ended["route"]["state"], "merged")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.root.mount, "Projects", "proj-a", "from-route.txt")))
        self.assertFalse(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "from-route.txt")))

    def _ended_route_change(self, content="attributed", relative="safe.txt"):
        self.h.session.policy["allowed_scopes"] = []
        self.h.session.policy["workspace_scope_ceiling"] = []
        self.h.session.policy["session_delta_routing"] = True
        self.h.session.policy["session_delta_routing_vendors"] = ["codex"]
        active = self.replace(
            "thread-safe-apply", 1, ["/storage/user/Projects/proj-a"])
        route = self.h.tc.route_manager.get(active["route"]["route_id"])
        routed = os.path.join(
            route.roots["r"].mount, "Projects", "proj-a", relative)
        os.makedirs(os.path.dirname(routed), exist_ok=True)
        with open(routed, "w") as fh:
            fh.write(content)
        self.replace("thread-safe-apply", 2, [], state="ended")
        return route

    def test_fingerprint_checked_route_path_is_exactly_auto_applied(self):
        self._ended_route_change()
        self.h.session.transition("mounting")
        self.h.session.transition("running")
        self.h.session.transition("finalizing")

        decision = finalize_session(
            self.h.session, self.h.store, self.h.backend, self.h.alias)

        self.assertEqual(decision.decision, "auto-commit")
        self.assertEqual(self.h.session.state, "auto-committed")
        with open(os.path.join(
                self.h.base, "Projects", "proj-a", "safe.txt")) as fh:
            self.assertEqual(fh.read(), "attributed")
        reconciliation = os.path.join(
            self.h.store.review_dir(self.h.session.session_id),
            "route-reconciliation.json")
        self.assertTrue(os.path.isfile(reconciliation))

    def test_post_merge_mutation_forces_pending_review_not_apply(self):
        self._ended_route_change()
        with open(os.path.join(
                self.h.root.mount, "Projects", "proj-a", "safe.txt"),
                "w") as fh:
            fh.write("changed-after-merge")
        self.h.session.transition("mounting")
        self.h.session.transition("running")
        self.h.session.transition("finalizing")

        decision = finalize_session(
            self.h.session, self.h.store, self.h.backend, self.h.alias)

        self.assertEqual(decision.decision, "pending-review")
        self.assertEqual(self.h.session.state, "pending-review")
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, "Projects", "proj-a", "safe.txt")))
        blocker = " ".join(
            self.h.session.policy["route_reconciliation"]["blockers"])
        self.assertIn("changed after its route merge", blocker)

    def test_symlinked_underlay_parent_fails_preflight_without_escape(self):
        self._ended_route_change(relative="redirected/file.txt")
        outside = os.path.join(os.path.dirname(self.h.base), "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(
            self.h.base, "Projects", "proj-a", "redirected"))
        self.h.session.transition("mounting")
        self.h.session.transition("running")
        self.h.session.transition("finalizing")

        decision = finalize_session(
            self.h.session, self.h.store, self.h.backend, self.h.alias)

        self.assertEqual(decision.decision, "pending-review")
        self.assertEqual(self.h.session.state, "pending-review")
        self.assertFalse(os.path.exists(os.path.join(outside, "file.txt")))
        self.assertIn("apply parent is a symlink",
                      " ".join(decision.reasons))


if __name__ == "__main__":
    unittest.main()
