"""Nested BranchFS route lifecycle and recovery tests."""

import json
import os
import tempfile
import unittest

from ccc_agent.branchfs import FakeBranchFS
from ccc_agent.route_manager import DeltaRouteManager
from tests.test_turn import TurnHarness


class TestDeltaRouteManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = TurnHarness(self.tmp.name)
        self.h.session.policy["session_delta_routing"] = True
        self.h.session.policy["session_delta_routing_vendors"] = ["codex"]
        self.manager = DeltaRouteManager(
            self.h.session, self.h.store, self.h.backend)
        admitted = self.h.tc.workspace_admission_policy.admit(
            "/storage/user/Projects/proj-a", require_existing=True)
        self.admitted_roots = [admitted]
        self.workspace_key = "codex-app-server:current:" + "a" * 64

    def tearDown(self):
        self.tmp.cleanup()

    def provision(self, generation=1):
        return self.manager.provision(
            provider="codex", logical_session_id="thread-secret",
            workspace_session_key=self.workspace_key,
            workspace_generation=generation,
            admitted_roots=self.admitted_roots,
        )

    def test_provision_creates_opaque_nested_branches_and_lookup_bindings(self):
        route = self.provision()

        self.assertEqual(route.state, "active")
        self.assertNotIn("thread-secret", json.dumps(route.to_dict()))
        self.assertIn(route.route_id, self.h.session.session_delta_routes)
        child = route.roots["r"]
        self.assertEqual(child.parent_branch, self.h.root.branch)
        self.assertTrue(os.path.islink(child.mount))

        lookup = self.manager.lookup("codex", "thread-secret")
        self.assertTrue(lookup["ok"])
        self.assertEqual(lookup["route_id"], route.route_id)
        self.assertIn({
            "source": "/run/ccc-agent/routes/%s/r" % route.route_id,
            "destination": "/storage/user",
        }, lookup["bindings"])

    def test_generation_update_reuses_route_but_replaces_exact_admission(self):
        first = self.provision(1)
        admitted = self.h.tc.workspace_admission_policy.admit(
            "/storage/user/Projects/proj-b", require_existing=True)

        second = self.manager.provision(
            provider="codex", logical_session_id="thread-secret",
            workspace_session_key=self.workspace_key,
            workspace_generation=2, admitted_roots=[admitted])

        self.assertEqual(second.route_id, first.route_id)
        self.assertEqual(second.workspace_generation, 2)
        self.assertEqual(second.admitted_roots[0]["canonical_path"],
                         "/storage/user/Projects/proj-b")

    def test_end_freezes_snapshots_and_merges_into_outer_never_base(self):
        route = self.provision()
        child = route.roots["r"]
        path = os.path.join(child.mount, "Projects", "proj-a", "routed.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("routed")

        ended = self.manager.end_workspace_session(self.workspace_key)

        self.assertEqual(ended.state, "merged")
        self.assertFalse(os.path.lexists(child.mount))
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.root.mount, "Projects", "proj-a", "routed.txt")))
        self.assertFalse(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "routed.txt")))
        attribution = self.h.session.policy["route_path_attribution"][
            "/storage/user/Projects/proj-a/routed.txt"]
        self.assertEqual(attribution["category"], "attributed")
        self.assertTrue(attribution["authorized"])
        self.assertEqual(attribution["route_ids"], [route.route_id])
        self.assertIn("sha256", attribution["fingerprint"])
        artifact = os.path.join(
            self.h.store.review_dir(self.h.session.session_id),
            "routes", route.route_id, "status.json")
        self.assertTrue(os.path.isfile(artifact))

    def test_lookup_wrong_provider_or_unknown_hint_is_unattributed(self):
        self.provision()
        self.assertEqual(self.manager.lookup("claude", "thread-secret"),
                         {"ok": False, "reason": "no-active-route"})
        self.assertEqual(self.manager.lookup("codex", "other"),
                         {"ok": False, "reason": "no-active-route"})

    def test_disabled_vendor_never_creates_child_branch(self):
        self.h.session.policy["session_delta_routing_vendors"] = ["claude"]
        manager = DeltaRouteManager(self.h.session, self.h.store, self.h.backend)

        route = manager.provision(
            provider="codex", logical_session_id="thread",
            workspace_session_key=self.workspace_key,
            workspace_generation=1, admitted_roots=self.admitted_roots)

        self.assertIsNone(route)
        self.assertEqual(self.h.session.session_delta_routes, {})

    def test_outer_baseline_marks_same_path_as_multiply_influenced(self):
        shared = os.path.join(
            self.h.root.mount, "Projects", "proj-a", "shared.txt")
        os.makedirs(os.path.dirname(shared), exist_ok=True)
        with open(shared, "w") as fh:
            fh.write("outer")
        route = self.manager.provision(
            provider="codex", logical_session_id="thread-shared",
            workspace_session_key="key-shared", workspace_generation=1,
            admitted_roots=self.admitted_roots)
        routed = os.path.join(
            route.roots["r"].mount, "Projects", "proj-a", "shared.txt")
        os.makedirs(os.path.dirname(routed), exist_ok=True)
        with open(routed, "w") as fh:
            fh.write("child")

        ended = self.manager.end_workspace_session("key-shared")

        record = self.h.session.policy["route_path_attribution"][
            "/storage/user/Projects/proj-a/shared.txt"]
        self.assertEqual(ended.state, "merged")
        self.assertEqual(record["category"], "multiply-influenced")
        self.assertFalse(record["authorized"])
        self.assertTrue(record["shared_outer_influence"])
        self.assertIn("/storage/user/Projects/proj-a/shared.txt",
                      route.outer_baseline["r"])

    def test_recovery_quiesces_and_merges_an_active_route(self):
        route = self.provision()
        child = route.roots["r"]
        path = os.path.join(child.mount, "Projects", "proj-a", "recovered.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("recovered")
        restarted = DeltaRouteManager(
            self.h.session, self.h.store, self.h.backend)

        outcomes = restarted.recover()

        self.assertEqual(outcomes[route.route_id], "merged")
        self.assertEqual(restarted.get(route.route_id).state, "merged")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.root.mount, "Projects", "proj-a", "recovered.txt")))


if __name__ == "__main__":
    unittest.main()
