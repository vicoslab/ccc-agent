"""Tests for ccc_agent.ctl: the human/operator session control surface."""

import io
import json
import os
import tempfile
import unittest

from ccc_agent import ctl
from ccc_agent.branchfs import (BranchfsError, FakeBranchFS, StatusReport,
                                StatusWarning)
from ccc_agent.paths import AliasMap
from ccc_agent.runner import RootSpec, RunnerConfig, run_session
from ccc_agent.session import ProtectedRoot, SessionStore


class CtlHarness(object):
    def __init__(self, tmp):
        self.tmp = tmp
        self.state_dir = os.path.join(tmp, "state")
        self.base = os.path.join(tmp, "real", "storage_user")
        os.makedirs(os.path.join(self.base, "Projects", "proj-a"),
                    exist_ok=True)
        self.backend = FakeBranchFS()
        self.store = SessionStore(self.state_dir)
        self.alias_map = AliasMap.for_home("domen", home_subdir="")

    def run_agent(self, argv, mode="workspace-auto", ignore_patterns=()):
        return run_session(RunnerConfig(
            store=self.store, backend=self.backend, alias_map=self.alias_map,
            owner="domen", agent_kind="fake", agent_command=list(argv),
            workspace="/home/domen/Projects/proj-a",
            policy={"mode": mode,
                    "allowed_scopes": ["/home/domen/Projects/proj-a"],
                    "ignore_patterns": list(ignore_patterns)},
            roots=[RootSpec(name="storage_user", base=self.base,
                            store=os.path.join(self.tmp, "stores",
                                               "storage_user"),
                            visible="/storage/user", home_subdir="")],
        ))

    def controller(self):
        return ctl.Controller(store=self.store, backend=self.backend,
                              alias_map=self.alias_map)

    def running_session(self, mode="workspace-auto", branch="agent-live",
                        max_repair_attempts=2):
        """A mounted session parked in `running`, like a launcher mid-run."""
        root = ProtectedRoot(
            name="storage_user", base=self.base,
            store=os.path.join(self.tmp, "stores", "storage_user"),
            branch=branch, mount=os.path.join(self.tmp, "mounts", branch),
            visible="/storage/user", home_subdir="")
        session = self.store.create(
            owner="domen", agent_kind="hermes-gateway",
            agent_command=["hermes", "serve"],
            workspace="/home/domen/Projects/proj-a",
            policy={"mode": mode,
                    "allowed_scopes": ["/home/domen/Projects/proj-a"],
                    "max_policy_repair_attempts": max_repair_attempts},
            protected_roots={"storage_user": root}, completion="manual")
        self.backend.start_daemon(root)
        self.backend.create_branch(root)
        self.backend.mount(root)
        session.transition("mounting")
        session.transition("running")
        self.store.save(session)
        return session, root


class TestController(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CtlHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def pending_session(self):
        # out-of-scope write => pending-review with frozen branch
        return self.h.run_agent(["sh", "-c", "echo x > ../../outside.txt"])

    def test_list_renders_sessions(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().list(out=out)
        text = out.getvalue()
        self.assertIn(session.session_id, text)
        self.assertIn("pending-review", text)

    def test_show_dumps_json(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().show(session.session_id, out=out)
        data = json.loads(out.getvalue())
        self.assertEqual(data["session_id"], session.session_id)

    def test_diff_prints_stored_changes(self):
        session = self.pending_session()
        out = io.StringIO()
        self.h.controller().diff(session.session_id, out=out)
        self.assertIn("/storage/user/outside.txt", out.getvalue())

    def test_diff_with_empty_review_status_does_not_fallback_to_live_branch(self):
        session = self.h.run_agent(["true"])
        self.assertEqual(session.state, "auto-committed")

        class StatusWouldBeWrong(FakeBranchFS):
            def status(self, root):
                raise AssertionError("diff should use the empty stored review")

        controller = ctl.Controller(store=self.h.store,
                                    backend=StatusWouldBeWrong(),
                                    alias_map=self.h.alias_map)
        out = io.StringIO()
        controller.diff(session.session_id, out=out)
        self.assertEqual(out.getvalue(), "")

    def test_diff_live_status_failure_is_actionable_control_error(self):
        session, _root = self.h.running_session(branch="agent-stale-running")

        class BrokenStatus(FakeBranchFS):
            def status_report(self, root):
                raise BranchfsError(
                    "/usr/local/bin/branchfs start-daemon failed (1): "
                    "Error: Daemon failed to start")

        controller = ctl.Controller(store=self.h.store,
                                    backend=BrokenStatus(),
                                    alias_map=self.h.alias_map)

        with self.assertRaises(ctl.ControlError) as cm:
            controller.diff(session.session_id, out=io.StringIO())

        message = str(cm.exception)
        self.assertIn("could not read live BranchFS status", message)
        self.assertIn("ccc-agent resume %s" % session.session_id, message)
        self.assertIn("Daemon failed to start", message)

    def test_diff_live_status_prints_branchfs_warnings(self):
        session, root = self.h.running_session(branch="agent-live-warning")
        os.makedirs(os.path.join(root.mount, "Projects", "proj-a"), exist_ok=True)
        with open(os.path.join(root.mount, "Projects", "proj-a", "result.txt"), "w") as fh:
            fh.write("ok\n")

        class WarningStatus(FakeBranchFS):
            def status_report(self, root):
                base = FakeBranchFS.status_report(self, root)
                return StatusReport(
                    changes=base.changes,
                    warnings=[StatusWarning(
                        path="/storage/user/Projects/proj-a/unreadable",
                        message="branchfs status failed; using direct store fallback",
                        root=root.name,
                    )],
                )

        controller = ctl.Controller(store=self.h.store,
                                    backend=WarningStatus(),
                                    alias_map=self.h.alias_map)
        # Reuse the already-created branch/mount from the harness with the new
        # backend by copying FakeBranchFS' simulated state.
        controller.backend._state = self.h.backend._state
        controller.backend._deletes = self.h.backend._deletes
        controller.backend._mounted = self.h.backend._mounted

        out = io.StringIO()
        controller.diff(session.session_id, out=out)

        text = out.getvalue()
        self.assertIn("/storage/user/Projects/proj-a/result.txt", text)
        self.assertIn("WARNING", text)
        self.assertLess(text.index("WARNING"),
                        text.index("/storage/user/Projects/proj-a/result.txt"))
        self.assertIn("/storage/user/Projects/proj-a/unreadable", text)
        self.assertIn("direct store fallback", text)

    def test_diff_path_prints_unified_base_delta_diff(self):
        base_path = os.path.join(self.h.base, "Projects", "proj-a",
                                 "notes.txt")
        with open(base_path, "w") as fh:
            fh.write("old\n")
        session = self.h.run_agent([
            "sh", "-c", "printf 'old\\nnew\\n' > notes.txt",
        ], mode="manual")
        self.assertEqual(session.state, "pending-review")

        out = io.StringIO()
        self.h.controller().diff(session.session_id, "notes.txt", out=out)

        text = out.getvalue()
        self.assertIn("--- a/Projects/proj-a/notes.txt", text)
        self.assertIn("+++ b/Projects/proj-a/notes.txt", text)
        self.assertIn("+new", text)
        self.assertNotIn("/storage/user/outside.txt", text)

    def test_commit_pending_session(self):
        session = self.pending_session()
        self.h.controller().commit(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(self.h.base,
                                                    "outside.txt")))

    def test_commit_pending_session_does_not_apply_ignored_infra(self):
        session = self.h.run_agent([
            "sh", "-c",
            "mkdir -p ../../.codex/plugins/ccc-agent; "
            "echo hook > ../../.codex/plugins/ccc-agent/hooks.json; "
            "echo x > ../../outside.txt",
        ], ignore_patterns=["/storage/user/.codex"])
        self.assertEqual(session.state, "pending-review")

        self.h.controller().commit(session.session_id)

        self.assertTrue(os.path.isfile(os.path.join(self.h.base,
                                                    "outside.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, ".codex", "plugins", "ccc-agent", "hooks.json")))

    def test_abort_pending_session(self):
        session = self.pending_session()
        self.h.controller().abort(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "aborted")
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "outside.txt")))

    def test_abort_unmounts_running_session_before_discarding_branch(self):
        # Real BranchFS can fail abort-branch with ENOTEMPTY/.nfs leftovers if
        # the branch is still mounted.  Operator abort must quiesce the FUSE
        # view before discarding the delta.
        class MountedAbortFails(FakeBranchFS):
            def __init__(self):
                super(MountedAbortFails, self).__init__()
                self.calls = []

            def unmount(self, root):
                self.calls.append("unmount")
                super(MountedAbortFails, self).unmount(root)

            def abort(self, root):
                self.calls.append("abort")
                if root.mount in self._mounted:
                    raise RuntimeError("Directory not empty (os error 39)")
                super(MountedAbortFails, self).abort(root)

        self.h.backend = MountedAbortFails()
        session, root = self.h.running_session(branch="agent-running-abort")
        with open(os.path.join(root.mount, "junk.txt"), "w") as fh:
            fh.write("discard me\n")

        self.h.controller().abort(session.session_id)

        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "aborted")
        self.assertEqual(self.h.backend.calls[-2:], ["unmount", "abort"])
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "junk.txt")))

    def test_commit_terminal_session_rejected(self):
        session = self.h.run_agent(["sh", "-c", "echo ok > fine.txt"])
        self.assertEqual(session.state, "auto-committed")
        with self.assertRaises(ctl.ControlError):
            self.h.controller().commit(session.session_id)

    def test_finish_turn_records_event(self):
        session = self.pending_session()
        self.h.controller().finish_turn(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertTrue(any(e["event"] == "turn-finished"
                            for e in reloaded.events))

    def test_finish_finalizes_running_session(self):
        # simulate a long-running session that a hook/human finishes
        session, root = self.h.running_session(branch="agent-longrun")
        with open(os.path.join(root.mount, "served.txt"), "w") as fh:
            fh.write("output\n")

        self.h.controller().finish(session.session_id)
        reloaded = self.h.store.load(session.session_id)
        self.assertEqual(reloaded.state, "pending-review")

    def test_unknown_session_raises(self):
        with self.assertRaises(ctl.ControlError):
            self.h.controller().show("agent-missing")


class TestCheckBeforeFinal(unittest.TestCase):
    """Hook-driven bounded self-repair: live cleanliness check, no freeze."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CtlHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, root, relpath, content="x\n"):
        path = os.path.join(root.mount, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def check(self, session_id):
        out = io.StringIO()
        result = self.h.controller().check_before_final(session_id, out=out)
        return result, out.getvalue(), self.h.store.load(session_id)

    def test_clean_in_scope_change_allows(self):
        session, root = self.h.running_session()
        self.touch(root, "Projects/proj-a/result.txt")
        result, _text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_ALLOW)
        self.assertEqual(reloaded.state, "running")
        self.assertEqual(reloaded.repair_attempts, 0)
        self.assertTrue(any(e["event"] == "check-clean"
                            for e in reloaded.events))

    def test_clean_check_ignores_mode(self):
        # manual/read-only-review gate the *commit* at finalize; the hook
        # cleanliness check still lets the agent finish its turn.
        for mode in ("manual", "read-only-review"):
            with self.subTest(mode=mode):
                session, root = self.h.running_session(
                    mode=mode, branch="agent-%s" % mode)
                self.touch(root, "Projects/proj-a/result.txt")
                result, _text, reloaded = self.check(session.session_id)
                self.assertEqual(result, ctl.CHECK_ALLOW)
                self.assertEqual(reloaded.state, "running")

    def test_out_of_scope_change_requests_repair(self):
        session, root = self.h.running_session()
        self.touch(root, "outside.txt")
        result, text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_REPAIR)
        self.assertEqual(reloaded.repair_attempts, 1)
        self.assertEqual(reloaded.state, "running")
        self.assertEqual(self.h.backend.branch_state(root), "open")
        self.assertTrue(any(e["event"] == "repair-requested"
                            for e in reloaded.events))
        self.assertIn("/storage/user/outside.txt", text)
        self.assertIn("revert", text)

    def test_deny_match_requests_repair(self):
        session, root = self.h.running_session()
        self.touch(root, "Projects/proj-a/.env", "SECRET=1\n")
        result, text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_REPAIR)
        self.assertEqual(reloaded.repair_attempts, 1)
        self.assertIn(".env", text)

    def test_check_before_final_ignores_infra_patterns(self):
        session, root = self.h.running_session()
        session.policy["ignore_patterns"] = ["/storage/user/.codex"]
        self.h.store.save(session)

        self.touch(root, ".codex/plugins/ccc-agent/hooks.json", "{}\n")

        result, text, reloaded = self.check(session.session_id)
        self.assertEqual(result, ctl.CHECK_ALLOW)
        self.assertEqual(reloaded.repair_attempts, 0)
        self.assertIn("clean", text)

    def test_exhausted_budget_defers_to_review(self):
        session, root = self.h.running_session(max_repair_attempts=2)
        self.touch(root, "outside.txt")
        sid = session.session_id
        self.assertEqual(self.check(sid)[0], ctl.CHECK_REPAIR)
        self.assertEqual(self.check(sid)[0], ctl.CHECK_REPAIR)
        result, text, reloaded = self.check(sid)
        self.assertEqual(result, ctl.CHECK_EXHAUSTED)
        self.assertEqual(reloaded.repair_attempts, 2)  # no further increment
        self.assertEqual(reloaded.state, "running")
        self.assertTrue(any(e["event"] == "repair-budget-exhausted"
                            for e in reloaded.events))
        self.assertIn("review", text)

    def test_non_running_sessions_rejected(self):
        pending = self.h.run_agent(["sh", "-c", "echo x > ../../outside.txt"])
        self.assertEqual(pending.state, "pending-review")
        with self.assertRaises(ctl.ControlError):
            self.h.controller().check_before_final(pending.session_id)

        terminal = self.h.run_agent(["sh", "-c", "echo ok > fine.txt"])
        self.assertEqual(terminal.state, "auto-committed")
        with self.assertRaises(ctl.ControlError):
            self.h.controller().check_before_final(terminal.session_id)


if __name__ == "__main__":
    unittest.main()
