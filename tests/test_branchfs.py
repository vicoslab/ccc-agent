"""Tests for ccc_agent.branchfs: CLI driver (with fake subprocess) and the
in-memory FakeBranchFS used by runner tests."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import ccc_agent.branchfs as branchfs_module
from ccc_agent.branchfs import BranchfsCli, BranchfsError, FakeBranchFS
from ccc_agent.session import ProtectedRoot


def make_root(tmp, branch="agent-20260611T000000Z-abc12345"):
    return ProtectedRoot(
        name="storage_user",
        base=os.path.join(tmp, "base"),
        store=os.path.join(tmp, "store"),
        branch=branch,
        mount=os.path.join(tmp, "mounts", "storage_user"),
        visible="/storage/user",
    )


STATUS_JSON = {
    "name": "agent-20260611T000000Z-abc12345",
    "parent": "main",
    "inheritance": "lazy",
    "state": "frozen",
    "parent_version_at_fork": 0,
    "commit_count": 0,
    "delta_entries": 4,
    "tombstones": 1,
    "diff": [
        {"op": "delta", "path": "Projects/proj-a/new.py", "kind": "file",
         "bytes": 12},
        {"op": "delta", "path": "Projects/proj-a/existing.py", "kind": "file",
         "bytes": 21},
        # Older BranchFS versions emitted structural parent directories.  The
        # ccc-agent review surface must still hide those and show only leaves.
        {"op": "delta", "path": "Projects/proj-a/sub", "kind": "dir",
         "bytes": 0},
        {"op": "delta", "path": "Projects/proj-a/sub/leaf.txt", "kind": "file",
         "bytes": 8},
        {"op": "delete", "path": "Projects/proj-a/old.txt",
         "kind": "tombstone", "bytes": 0},
    ],
}


class RecordingRunner(object):
    """Stands in for subprocess execution inside BranchfsCli."""

    def __init__(self, outputs=None, fail_on=None):
        self.calls = []
        self.outputs = outputs or {}
        self.fail_on = fail_on or set()

    def __call__(self, argv):
        self.calls.append(list(argv))
        subcommand = argv[1]
        if subcommand in self.fail_on:
            return 1, "", "daemon not running"
        return 0, self.outputs.get(subcommand, ""), ""


class TestBranchfsCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_daemon_argv(self):
        runner = RecordingRunner()
        cli = BranchfsCli(binary="branchfs", run=runner)
        cli.start_daemon(self.root)
        self.assertEqual(runner.calls[0][:2], ["branchfs", "start-daemon"])
        self.assertIn("--base", runner.calls[0])
        self.assertIn(self.root.base, runner.calls[0])
        self.assertIn("--storage", runner.calls[0])
        self.assertIn(self.root.store, runner.calls[0])

    def test_start_daemon_accepts_socket_that_becomes_ready_after_cli_timeout(self):
        class InternalTimeoutRunner(object):
            def __init__(self):
                self.calls = []

            def __call__(self, argv):
                self.calls.append(list(argv))
                return 1, "", "Error: Daemon failed to start"

        ready_checks = []

        def daemon_ready(path):
            ready_checks.append(path)
            return len(ready_checks) >= 3

        sleeps = []
        runner = InternalTimeoutRunner()
        cli = BranchfsCli(run=runner, timeout_seconds=10,
                          daemon_ready=daemon_ready,
                          sleep=lambda seconds: sleeps.append(seconds))

        cli.start_daemon(self.root)

        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(all(path.endswith("daemon.sock")
                            for path in ready_checks))
        self.assertTrue(sleeps)

    def test_start_daemon_reports_socket_not_ready_after_internal_timeout(self):
        class InternalTimeoutRunner(object):
            def __call__(self, argv):
                return 1, "", "Error: Daemon failed to start"

        cli = BranchfsCli(run=InternalTimeoutRunner(), timeout_seconds=0,
                          daemon_ready=lambda path: False,
                          sleep=lambda seconds: None)

        with self.assertRaises(BranchfsError) as ctx:
            cli.start_daemon(self.root)

        message = str(ctx.exception)
        self.assertIn("Daemon failed to start", message)
        self.assertIn("daemon.sock", message)
        self.assertIn("not reachable", message)

    def test_create_branch_argv_has_no_mountpoint(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.create_branch(self.root)
        # daemon-dependent ops re-ensure the daemon first
        self.assertEqual(runner.calls[0][1], "start-daemon")
        call = runner.calls[-1]
        self.assertEqual(call[1], "create")
        self.assertIn(self.root.branch, call)
        # supervisor creates branches without any mounted view
        self.assertNotIn(self.root.mount, call)

    def test_create_branch_passes_hide_paths(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        self.root.hide_paths = [".ssh", ".netrc"]
        cli.create_branch(self.root)
        call = runner.calls[-1]
        self.assertEqual(call[1], "create")
        self.assertEqual(call.count("--hide"), 2)
        self.assertIn(".ssh", call)
        self.assertIn(".netrc", call)

    def test_mount_agent_view(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=True)
        call = runner.calls[-1]
        self.assertEqual(call[1], "mount")
        self.assertIn("--agent", call)
        self.assertIn("--branch", call)
        self.assertIn(self.root.branch, call)
        self.assertEqual(call[-1], self.root.mount)

    def test_trusted_mount_keeps_control(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=False)
        self.assertNotIn("--agent", runner.calls[-1])

    def test_mount_allow_other_for_privilege_separated_agent(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=True, allow_other=True)
        call = runner.calls[-1]
        self.assertIn("--allow-other", call)
        # mountpoint stays the final positional argument
        self.assertEqual(call[-1], self.root.mount)

    def test_mount_omits_allow_other_by_default(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.mount(self.root, agent=True)
        self.assertNotIn("--allow-other", runner.calls[-1])

    def test_status_parses_changes_into_visible_namespace(self):
        os.makedirs(os.path.join(self.root.base, "Projects", "proj-a"),
                    exist_ok=True)
        with open(os.path.join(self.root.base, "Projects", "proj-a",
                               "existing.py"), "w") as fh:
            fh.write("old contents\n")
        runner = RecordingRunner(outputs={"status": json.dumps(STATUS_JSON)})
        cli = BranchfsCli(run=runner)
        changes = cli.status(self.root)
        self.assertIn("--json", runner.calls[-1])
        by_path = {c.path: c for c in changes}
        added = by_path["/storage/user/Projects/proj-a/new.py"]
        self.assertEqual(added.op, "A")
        self.assertEqual(added.kind, "file")
        self.assertEqual(added.bytes, 12)
        self.assertEqual(added.root, "storage_user")
        modified = by_path["/storage/user/Projects/proj-a/existing.py"]
        self.assertEqual(modified.op, "M")
        deleted = by_path["/storage/user/Projects/proj-a/old.txt"]
        self.assertEqual(deleted.op, "D")
        leaf = by_path["/storage/user/Projects/proj-a/sub/leaf.txt"]
        self.assertEqual(leaf.op, "A")
        self.assertNotIn("/storage/user/Projects/proj-a/sub", by_path)

    def test_status_nets_delete_then_rewrite_to_final_file_change(self):
        os.makedirs(os.path.join(self.root.base, "Projects", "proj-a"),
                    exist_ok=True)
        with open(os.path.join(self.root.base, "Projects", "proj-a",
                               "settings.json"), "w") as fh:
            fh.write("old\n")
        status = dict(STATUS_JSON)
        status["diff"] = [
            {"op": "delete", "path": "Projects/proj-a/settings.json",
             "kind": "tombstone", "bytes": 0},
            {"op": "delta", "path": "Projects/proj-a/settings.json",
             "kind": "file", "bytes": 9},
        ]
        runner = RecordingRunner(outputs={"status": json.dumps(status)})
        cli = BranchfsCli(run=runner)

        changes = cli.status(self.root)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].path,
                         "/storage/user/Projects/proj-a/settings.json")
        self.assertEqual(changes[0].op, "M")
        self.assertEqual(changes[0].kind, "file")

    def test_status_parsing_does_not_scan_entries_pairwise(self):
        status = dict(STATUS_JSON)
        status["diff"] = [
            {"op": "delete", "path": "bulk/file-%04d.txt" % idx,
             "kind": "tombstone", "bytes": 0}
            for idx in range(1200)
        ]
        runner = RecordingRunner(outputs={"status": json.dumps(status)})
        cli = BranchfsCli(run=runner)
        calls = []
        original = branchfs_module._is_descendant_path

        def counted(child, parent):
            calls.append((child, parent))
            return original(child, parent)

        with mock.patch("ccc_agent.branchfs._is_descendant_path",
                        side_effect=counted):
            changes = cli.status(self.root)

        self.assertEqual(len(changes), 1200)
        self.assertLess(len(calls), len(changes) * 10)

    def test_status_collapses_descendant_tombstones_under_tree_delete(self):
        status = dict(STATUS_JSON)
        status["diff"] = [
            {"op": "delete", "path": "Projects/proj-a/.ccc-storage",
             "kind": "tombstone", "bytes": 0},
            {"op": "delete", "path": "Projects/proj-a/.ccc-storage/locks",
             "kind": "tombstone", "bytes": 0},
            {"op": "delete",
             "path": "Projects/proj-a/.ccc-storage/locks/foo.lock",
             "kind": "tombstone", "bytes": 0},
            {"op": "delete", "path": "Projects/proj-a/.ccc-storage/packs",
             "kind": "tombstone", "bytes": 0},
        ]
        runner = RecordingRunner(outputs={"status": json.dumps(status)})
        cli = BranchfsCli(run=runner)

        changes = cli.status(self.root)

        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change.op, "D")
        self.assertEqual(change.path,
                         "/storage/user/Projects/proj-a/.ccc-storage")
        self.assertEqual(change.summary, "3 nested deletions hidden")

    def test_status_report_preserves_branchfs_warnings(self):
        status = dict(STATUS_JSON)
        status["warnings"] = [
            {"path": "/Projects/proj-a/unreadable",
             "message": "unreadable delta directory: Permission denied; commit may fail"},
        ]
        runner = RecordingRunner(outputs={"status": json.dumps(status)})
        cli = BranchfsCli(run=runner)

        report = cli.status_report(self.root)

        self.assertEqual(len(report.changes), 4)
        self.assertEqual(len(report.warnings), 1)
        warning = report.warnings[0]
        self.assertEqual(warning.path,
                         "/storage/user/Projects/proj-a/unreadable")
        self.assertEqual(warning.root, "storage_user")
        self.assertIn("commit may fail", warning.message)
        # Existing callers keep receiving just changes.
        self.assertEqual([c.path for c in cli.status(self.root)],
                         [c.path for c in report.changes])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "mode-000 permission checks are bypassed by root")
    def test_status_report_falls_back_to_store_when_branchfs_status_denied(self):
        class StatusDeniedRunner(object):
            def __init__(self):
                self.calls = []

            def __call__(self, argv):
                self.calls.append(list(argv))
                if argv[1] == "status":
                    return 1, "", "Error: io error: Permission denied (os error 13)"
                return 0, "", ""

        branch_dir = os.path.join(self.root.store, "branches", self.root.branch)
        files = os.path.join(branch_dir, "files")
        os.makedirs(os.path.join(files, "Projects", "proj-a"), exist_ok=True)
        with open(os.path.join(files, "Projects", "proj-a", "result.txt"), "w") as fh:
            fh.write("ok\n")
        unreadable = os.path.join(files, "Projects", "proj-a", "scratch", "work", "work")
        os.makedirs(unreadable)
        with open(os.path.join(unreadable, "hidden.txt"), "w") as fh:
            fh.write("hidden\n")
        with open(os.path.join(branch_dir, "tombstones"), "w") as fh:
            fh.write("/Projects/proj-a/deleted.txt\n")
        os.chmod(unreadable, 0)
        try:
            cli = BranchfsCli(run=StatusDeniedRunner())
            report = cli.status_report(self.root)
        finally:
            os.chmod(unreadable, 0o700)

        by_path = {change.path: change for change in report.changes}
        self.assertIn("/storage/user/Projects/proj-a/result.txt", by_path)
        self.assertIn("/storage/user/Projects/proj-a/scratch/work/work", by_path)
        self.assertNotIn("/storage/user/Projects/proj-a/scratch/work/work/hidden.txt", by_path)
        self.assertEqual(by_path["/storage/user/Projects/proj-a/deleted.txt"].op, "D")
        warning_text = "\n".join(w.message for w in report.warnings)
        self.assertIn("branchfs status failed", warning_text)
        self.assertIn("using direct store fallback", warning_text)
        self.assertIn("unreadable delta directory", warning_text)
        self.assertTrue(all(w.root == "storage_user" for w in report.warnings))

    def test_status_report_falls_back_to_store_when_branchfs_unavailable(self):
        class MissingBinaryRunner(object):
            def __call__(self, argv):
                raise FileNotFoundError(argv[0])

        files = os.path.join(self.root.store, "branches", self.root.branch,
                             "files")
        os.makedirs(os.path.join(files, "Projects", "proj-a"), exist_ok=True)
        with open(os.path.join(files, "Projects", "proj-a", "result.txt"), "w") as fh:
            fh.write("ok\n")

        cli = BranchfsCli(binary="/missing/branchfs", run=MissingBinaryRunner())
        report = cli.status_report(self.root)

        self.assertEqual([change.path for change in report.changes],
                         ["/storage/user/Projects/proj-a/result.txt"])
        self.assertIn("direct store fallback", report.warnings[0].message)
        self.assertIn("/missing/branchfs", report.warnings[0].message)

    def test_failure_raises_with_stderr(self):
        runner = RecordingRunner(fail_on={"freeze"})
        cli = BranchfsCli(run=runner)
        with self.assertRaises(BranchfsError) as ctx:
            cli.freeze(self.root)
        self.assertIn("daemon not running", str(ctx.exception))

    def test_hung_branchfs_command_times_out_with_context(self):
        def block_forever(argv, **kwargs):
            timeout = kwargs.get("timeout", 30)
            raise subprocess.TimeoutExpired(argv, timeout)

        with mock.patch("ccc_agent.branchfs.subprocess.run",
                        side_effect=block_forever) as run:
            cli = BranchfsCli(binary="branchfs")
            with self.assertRaises(BranchfsError) as ctx:
                cli.start_daemon(self.root)

        self.assertEqual(run.call_args.kwargs.get("timeout"), 30)
        message = str(ctx.exception)
        self.assertIn("branchfs start-daemon timed out after 30s", message)
        self.assertIn(self.root.store, message)

    def test_commit_and_abort_use_trusted_branch_commands(self):
        runner = RecordingRunner()
        cli = BranchfsCli(run=runner)
        cli.commit(self.root)
        cli.abort(self.root)
        subcommands = [c[1] for c in runner.calls]
        self.assertEqual(subcommands, ["start-daemon", "commit-branch",
                                       "start-daemon", "abort-branch"])

    def test_abort_treats_missing_branch_with_no_store_dir_as_idempotent(self):
        class MissingBranchRunner(object):
            def __init__(self, branch):
                self.calls = []
                self.branch = branch

            def __call__(self, argv):
                self.calls.append(list(argv))
                if argv[1] == "abort-branch":
                    return 1, "", "Error: branch not found: %s" % self.branch
                return 0, "", ""

        runner = MissingBranchRunner(self.root.branch)
        cli = BranchfsCli(run=runner)

        cli.abort(self.root)

        self.assertEqual([c[1] for c in runner.calls],
                         ["start-daemon", "abort-branch"])

    def test_abort_removes_empty_orphan_branch_dir_after_missing_branch(self):
        class MissingBranchRunner(object):
            def __init__(self, branch):
                self.calls = []
                self.branch = branch

            def __call__(self, argv):
                self.calls.append(list(argv))
                if argv[1] == "abort-branch":
                    return 1, "", "Error: branch not found: %s" % self.branch
                return 0, "", ""

        branch_dir = os.path.join(self.root.store, "branches", self.root.branch)
        os.makedirs(os.path.join(branch_dir, "files", "user"), exist_ok=True)
        os.makedirs(os.path.join(branch_dir, "inherited"), exist_ok=True)
        runner = MissingBranchRunner(self.root.branch)
        cli = BranchfsCli(run=runner)

        cli.abort(self.root)

        self.assertFalse(os.path.exists(branch_dir))

    def test_abort_preserves_non_empty_orphan_branch_dir(self):
        class MissingBranchRunner(object):
            def __init__(self, branch):
                self.calls = []
                self.branch = branch

            def __call__(self, argv):
                self.calls.append(list(argv))
                if argv[1] == "abort-branch":
                    return 1, "", "Error: branch not found: %s" % self.branch
                return 0, "", ""

        branch_dir = os.path.join(self.root.store, "branches", self.root.branch)
        os.makedirs(os.path.join(branch_dir, "files"), exist_ok=True)
        payload = os.path.join(branch_dir, "files", "still-open.txt")
        with open(payload, "w") as fh:
            fh.write("leftover\n")
        runner = MissingBranchRunner(self.root.branch)
        cli = BranchfsCli(run=runner)

        with self.assertRaises(BranchfsError):
            cli.abort(self.root)

        self.assertTrue(os.path.isfile(payload))


class TestFakeBranchFS(unittest.TestCase):
    """The fake must be behaviorally close enough to drive the runner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)
        os.makedirs(self.root.base)
        with open(os.path.join(self.root.base, "existing.txt"), "w") as fh:
            fh.write("base data\n")
        self.fake = FakeBranchFS()

    def tearDown(self):
        self.tmp.cleanup()

    def test_mount_creates_writable_view(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        self.assertTrue(os.path.isdir(self.root.mount))
        with open(os.path.join(self.root.mount, "agent.txt"), "w") as fh:
            fh.write("agent wrote this\n")

    def test_status_reports_writes_in_visible_namespace(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        os.makedirs(os.path.join(self.root.mount, "Projects/p"), exist_ok=True)
        with open(os.path.join(self.root.mount, "Projects/p/a.py"), "w") as fh:
            fh.write("x = 1\n")
        self.fake.record_delete(self.root, "existing.txt")
        changes = self.fake.status(self.root)
        ops = {c.path: c.op for c in changes}
        self.assertEqual(ops["/storage/user/Projects/p/a.py"], "A")
        self.assertEqual(ops["/storage/user/existing.txt"], "D")

    def test_freeze_blocks_status_free_write_simulation(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.freeze(self.root)
        self.assertEqual(self.fake.branch_state(self.root), "frozen")
        self.fake.thaw(self.root)
        self.assertEqual(self.fake.branch_state(self.root), "open")

    def test_commit_applies_deltas_and_tombstones_to_base(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        with open(os.path.join(self.root.mount, "new.txt"), "w") as fh:
            fh.write("delta\n")
        self.fake.record_delete(self.root, "existing.txt")
        self.fake.freeze(self.root)
        self.fake.commit(self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.root.base, "new.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root.base,
                                                     "existing.txt")))

    def test_abort_discards_deltas(self):
        self.fake.start_daemon(self.root)
        self.fake.create_branch(self.root)
        self.fake.mount(self.root)
        with open(os.path.join(self.root.mount, "junk.txt"), "w") as fh:
            fh.write("discard me\n")
        self.fake.abort(self.root)
        self.assertEqual(self.fake.status(self.root), [])
        self.assertFalse(os.path.exists(os.path.join(self.root.base,
                                                     "junk.txt")))


if __name__ == "__main__":
    unittest.main()
