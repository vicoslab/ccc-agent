"""Tests for durable, opaque nested-delta routing records."""

import json
import os
import tempfile
import unittest

from ccc_agent.delta_routing import (
    DeltaRoute,
    NestedRoot,
    RouteStateError,
    new_route_id,
)
from ccc_agent.session import ProtectedRoot


class TestDeltaRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.outer = ProtectedRoot(
            name="storage_user",
            base=os.path.join(self.tmp.name, "base"),
            store=os.path.join(self.tmp.name, "store"),
            branch="agent-outer",
            mount=os.path.join(self.tmp.name, "outer-mount"),
            visible="/storage/user",
            home_subdir="domen",
            hide_paths=(".ssh",),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_route_and_nested_branch_ids_are_opaque_to_vendor_id(self):
        vendor_id = "vendor-thread/raw:id-123"

        route = DeltaRoute.create(
            provider="codex",
            vendor_session_id=vendor_id,
            parent_session_id="agent-outer",
            parent_roots={"storage_user": self.outer},
            mount_dir=os.path.join(self.tmp.name, "nested-mounts"),
        )

        self.assertTrue(route.route_id.startswith("route-"))
        self.assertNotIn(vendor_id, route.route_id)
        self.assertNotIn(vendor_id, json.dumps(route.to_dict()))
        self.assertTrue(route.logical_session_digest.startswith("sha256:"))
        nested = route.roots["storage_user"]
        self.assertNotIn(vendor_id, nested.branch)
        self.assertEqual(nested.parent_branch, self.outer.branch)
        self.assertEqual(nested.store, self.outer.store)
        self.assertEqual(nested.base, self.outer.base)
        self.assertEqual(nested.visible, self.outer.visible)
        self.assertEqual(nested.home_subdir, "domen")
        self.assertEqual(nested.hide_paths, [".ssh"])

    def test_route_id_generation_does_not_interpolate_hint(self):
        raw_vendor_id = "thread_very_secret_42"

        route_id = new_route_id(raw_vendor_id)

        self.assertTrue(route_id.startswith("route-"))
        self.assertNotIn(raw_vendor_id, route_id)
        self.assertNotIn("thread", route_id)

    def test_round_trip_is_json_serializable_and_versioned(self):
        nested = NestedRoot.from_parent(
            self.outer,
            route_id="route-0123456789abcdef0123456789abcdef",
            mount_dir=os.path.join(self.tmp.name, "mounts"),
        )
        route = DeltaRoute(
            route_id="route-0123456789abcdef0123456789abcdef",
            provider="claude",
            vendor_session_id="session/raw-77",
            parent_session_id="agent-outer",
            roots={nested.name: nested},
            state="active",
            created_at="2026-07-16T10:00:00Z",
            updated_at="2026-07-16T10:01:00Z",
        )

        encoded = json.loads(json.dumps(route.to_dict()))
        restored = DeltaRoute.from_dict(encoded)

        self.assertEqual(encoded["schema_version"], 2)
        self.assertEqual(restored.to_dict(), encoded)
        self.assertIsInstance(restored.roots["storage_user"], NestedRoot)
        self.assertNotIn("vendor_session_id", encoded)
        self.assertNotIn("session/raw-77", json.dumps(encoded))
        self.assertTrue(encoded["logical_session_digest"].startswith("sha256:"))

    def test_route_persists_coverage_without_command_or_environment_data(self):
        route = DeltaRoute.create(
            provider="codex",
            vendor_session_id="thread-77",
            parent_session_id="agent-outer",
            parent_roots={"storage_user": self.outer},
            mount_dir=os.path.join(self.tmp.name, "nested-mounts"),
        )

        route.record_coverage("routed")
        route.record_coverage("bypassed", warning="full-write-bypass")

        self.assertEqual(route.coverage, {
            "routed_bwrap_calls": 1,
            "bypassed_or_unattributed_calls": 1,
            "last_warning": "full-write-bypass",
            "capability": "unknown",
        })
        self.assertNotIn("command", json.dumps(route.to_dict()))
        self.assertNotIn("environment", json.dumps(route.to_dict()))

    def test_explicit_state_transitions_reject_skips_and_terminal_reopen(self):
        route = DeltaRoute(
            route_id="route-0123456789abcdef0123456789abcdef",
            provider="codex",
            vendor_session_id="raw-id",
            parent_session_id="agent-outer",
            roots={},
            created_at="2026-07-16T10:00:00Z",
            updated_at="2026-07-16T10:00:00Z",
        )

        with self.assertRaises(RouteStateError):
            route.transition("merged")
        route.transition("active", at="2026-07-16T10:01:00Z")
        route.transition("quiescing", at="2026-07-16T10:02:00Z")
        route.transition("frozen", at="2026-07-16T10:03:00Z")
        route.transition("merged", at="2026-07-16T10:04:00Z")
        self.assertEqual(route.updated_at, "2026-07-16T10:04:00Z")
        with self.assertRaises(RouteStateError):
            route.transition("active")

    def test_conflicted_frozen_route_can_be_held_for_review(self):
        route = DeltaRoute(
            route_id="route-0123456789abcdef0123456789abcdef",
            provider="codex",
            vendor_session_id="raw-id",
            parent_session_id="agent-outer",
            roots={},
        )
        route.transition("active")
        route.transition("quiescing")
        route.transition("frozen")
        route.transition("pending-review", detail={"conflicts": ["a.txt"]})

        self.assertEqual(route.state, "pending-review")
        self.assertEqual(route.events[-1]["detail"], {"conflicts": ["a.txt"]})

    def test_deserialization_rejects_unknown_schema_or_state(self):
        payload = {
            "schema_version": 99,
            "route_id": "route-0123456789abcdef0123456789abcdef",
            "provider": "codex",
            "logical_session_digest": "sha256:" + "0" * 64,
            "parent_session_id": "agent-outer",
            "state": "provisioning",
            "created_at": "2026-07-16T10:00:00Z",
            "updated_at": "2026-07-16T10:00:00Z",
            "roots": {},
        }
        with self.assertRaises(ValueError):
            DeltaRoute.from_dict(payload)
        payload["schema_version"] = 2
        payload["state"] = "mystery"
        with self.assertRaises(ValueError):
            DeltaRoute.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
