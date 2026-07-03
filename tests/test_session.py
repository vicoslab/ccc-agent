"""Tests for ccc_agent.session: schema, state machine, durable store."""

import json
import os
import tempfile
import unittest

from ccc_agent.session import (
    ProtectedRoot,
    Session,
    SessionStore,
    StateError,
    new_session_id,
)


def make_session(store):
    roots = {
        "storage_user": ProtectedRoot(
            name="storage_user",
            base="/__real/storage_user",
            store="/__branchfs_store/storage_user",
            branch="sessions/test-branch",
            mount="/__branchfs_mounts/storage_user",
            visible="/storage/user",
            home_subdir="",
        ),
    }
    return store.create(
        owner="domen",
        agent_kind="codex-cli",
        agent_command=["codex", "exec", "do thing"],
        workspace="/home/domen/Projects/proj-a",
        policy={"mode": "workspace-auto",
                "allowed_scopes": ["/home/domen/Projects/proj-a"]},
        protected_roots=roots,
    )


class TestSessionId(unittest.TestCase):
    def test_format_and_uniqueness(self):
        a = new_session_id()
        b = new_session_id()
        self.assertTrue(a.startswith("agent-"))
        self.assertNotEqual(a, b)
        # ids end up in filesystem paths: keep them path-safe
        self.assertNotIn("/", a)
        self.assertNotIn(":", a)


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.tmp.name)
        self.session = make_session(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_state(self):
        self.assertEqual(self.session.state, "created")

    def test_happy_path_to_auto_commit(self):
        for state in ("mounting", "running", "finalizing", "frozen",
                      "auto-committed"):
            self.session.transition(state)
        self.assertEqual(self.session.state, "auto-committed")

    def test_pending_review_then_commit(self):
        for state in ("mounting", "running", "finalizing", "frozen",
                      "pending-review", "committed"):
            self.session.transition(state)
        self.assertEqual(self.session.state, "committed")

    def test_illegal_transition_raises(self):
        with self.assertRaises(StateError):
            self.session.transition("frozen")  # created -> frozen is illegal

    def test_committed_terminal_state_does_not_reopen(self):
        for state in ("mounting", "running", "finalizing", "frozen",
                      "auto-committed"):
            self.session.transition(state)
        with self.assertRaises(StateError):
            self.session.transition("running")

    def test_aborted_can_reopen_to_running_for_resume(self):
        self.session.transition("mounting")
        self.session.transition("running")
        self.session.transition("aborted")
        self.session.finished_at = "2000-01-01T00:00:00Z"

        self.session.transition("running")

        self.assertEqual(self.session.state, "running")
        self.assertIsNone(self.session.finished_at)

    def test_failed_reachable_from_nonterminal(self):
        self.session.transition("mounting")
        self.session.transition("failed")
        self.assertEqual(self.session.state, "failed")

    def test_failed_can_reopen_to_running_for_explicit_resume(self):
        self.session.transition("mounting")
        self.session.transition("running")
        self.session.transition("failed")
        self.session.finished_at = "2000-01-01T00:00:00Z"

        self.session.transition("running")

        self.assertEqual(self.session.state, "running")
        self.assertIsNone(self.session.finished_at)

    def test_transition_records_event(self):
        self.session.transition("mounting")
        events = [e["event"] for e in self.session.events]
        self.assertIn("state:mounting", events)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_persists_immediately(self):
        session = make_session(self.store)
        path = self.store.session_file(session.session_id)
        self.assertTrue(os.path.isfile(path))
        with open(path) as fh:
            data = json.load(fh)
        self.assertEqual(data["state"], "created")
        self.assertEqual(data["owner"], "domen")

    def test_roundtrip(self):
        session = make_session(self.store)
        session.transition("mounting")
        self.store.save(session)
        loaded = self.store.load(session.session_id)
        self.assertEqual(loaded.state, "mounting")
        self.assertEqual(loaded.agent_command, ["codex", "exec", "do thing"])
        self.assertEqual(loaded.protected_roots["storage_user"].visible,
                         "/storage/user")
        self.assertEqual(loaded.policy["mode"], "workspace-auto")

    def test_list_sessions(self):
        first = make_session(self.store)
        second = make_session(self.store)
        ids = [s.session_id for s in self.store.list()]
        self.assertIn(first.session_id, ids)
        self.assertIn(second.session_id, ids)

    def test_load_missing_raises(self):
        with self.assertRaises(KeyError):
            self.store.load("agent-nope")

    def test_no_stray_tempfiles_after_save(self):
        session = make_session(self.store)
        self.store.save(session)
        session_dir = os.path.dirname(self.store.session_file(session.session_id))
        leftovers = [n for n in os.listdir(session_dir) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_per_session_bundle_layout(self):
        session = make_session(self.store)
        bundle = os.path.join(self.tmp.name, session.session_id)

        self.assertEqual(self.store.session_dir(session.session_id),
                         os.path.join(bundle, "session"))
        self.assertEqual(self.store.session_file(session.session_id),
                         os.path.join(bundle, "session", "session.json"))
        self.assertEqual(self.store.review_dir(session.session_id),
                         os.path.join(bundle, "reviews"))
        self.assertEqual(self.store.mount_dir(session.session_id),
                         os.path.join(bundle, "mounts"))
        self.assertEqual(self.store.control_dir(session.session_id),
                         os.path.join(bundle, "control"))
        self.assertEqual(self.store.control_socket(session.session_id),
                         os.path.join(bundle, "control", "control.sock"))

        # New sessions should not recreate the old type-first top-level layout.
        for old_top in ("sessions", "reviews", "mounts", "control"):
            self.assertFalse(os.path.exists(os.path.join(self.tmp.name,
                                                         old_top)))

    def test_remove_deletes_session_bundle_and_legacy_artifacts(self):
        session = make_session(self.store)
        sid = session.session_id
        legacy_paths = [
            os.path.join(self.tmp.name, "sessions", sid),
            os.path.join(self.tmp.name, "reviews", sid),
            os.path.join(self.tmp.name, "mounts", sid),
            os.path.join(self.tmp.name, "control", sid),
        ]
        for path in legacy_paths:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "artifact"), "w") as fh:
                fh.write("legacy\n")

        removed = self.store.remove(sid)

        self.assertTrue(removed)
        self.assertFalse(os.path.exists(self.store.bundle_dir(sid)))
        for path in legacy_paths:
            self.assertFalse(os.path.exists(path))
        with self.assertRaises(KeyError):
            self.store.load(sid)


if __name__ == "__main__":
    unittest.main()
