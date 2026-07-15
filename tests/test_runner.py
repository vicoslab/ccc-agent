"""End-to-end (non-FUSE) tests for ccc_agent.runner using FakeBranchFS.

These mirror the Phase 2 validation list from the accepted design:
- agent writing inside the workspace auto-commits;
- agent writing outside the workspace becomes pending-review;
- agent deleting global config is recoverable;
- a no-op run closes cleanly.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ccc_agent.branchfs import FakeBranchFS, StatusReport, StatusWarning
from ccc_agent.paths import AliasMap
from ccc_agent.runner import (BWRAP_AGENT_RUNNER, BWRAP_AGENT_RUNNER_ARG0,
                              ENV_CONTROL_TOKEN, ENV_STATE_DIR, ResumeError,
                              RootSpec, RunnerConfig, resume_session,
                              run_session)
from ccc_agent.session import SessionStore


class RunnerHarness(object):
    def __init__(self, tmp, mode="workspace-auto"):
        self.tmp = tmp
        self.state_dir = os.path.join(tmp, "state")
        self.base = os.path.join(tmp, "real", "storage_user")
        os.makedirs(os.path.join(self.base, "Projects", "proj-a"),
                    exist_ok=True)
        with open(os.path.join(self.base, ".bashrc"), "w") as fh:
            fh.write("export PS1=x\n")
        self.backend = FakeBranchFS()
        self.store = SessionStore(self.state_dir)
        self.mode = mode

    def config(self, argv, mode=None, hide_patterns=(), agent_kind="fake-agent", **extra):
        return RunnerConfig(
            store=self.store,
            backend=self.backend,
            alias_map=AliasMap.for_home("domen", home_subdir=""),
            owner="domen",
            agent_kind=agent_kind,
            agent_command=list(argv),
            workspace="/home/domen/Projects/proj-a",
            policy={
                "mode": mode or self.mode,
                "allowed_scopes": ["/home/domen/Projects/proj-a"],
                "hide_patterns": list(hide_patterns),
            },
            roots=[RootSpec(name="storage_user", base=self.base,
                            store=os.path.join(self.tmp, "stores",
                                               "storage_user"),
                            visible="/storage/user", home_subdir="")],
            **extra
        )


class TestRunSession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = RunnerHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def running_session(self, session_id="agent-resume", command=None,
                        mode="workspace-auto"):
        spec = RootSpec(name="storage_user", base=self.h.base,
                        store=os.path.join(self.h.tmp, "stores",
                                           "storage_user"),
                        visible="/storage/user", home_subdir="")
        root = spec.materialize(session_id, self.h.store.state_dir,
                                mount_dir=self.h.store.mount_dir(session_id))
        os.makedirs(os.path.join(root.store, "branches", root.branch, "files"),
                    exist_ok=True)
        session = self.h.store.create(
            owner="domen", agent_kind="codex",
            agent_command=command or ["sh", "-c", "echo resumed > resumed.txt"],
            workspace="/home/domen/Projects/proj-a",
            policy={"mode": mode,
                    "allowed_scopes": ["/home/domen/Projects/proj-a"]},
            protected_roots={"storage_user": root}, completion="process-exit",
            session_id=session_id)
        session.transition("mounting")
        session.transition("running")
        self.h.store.save(session)
        return session

    def failed_session(self, session_id="agent-resume-failed", command=None,
                       mode="workspace-auto"):
        session = self.running_session(session_id=session_id, command=command,
                                       mode=mode)
        session.transition("failed")
        session.finished_at = "2000-01-01T00:00:00Z"
        self.h.store.save(session)
        return session

    def test_workspace_write_auto_commits(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo done > result.txt"]))
        self.assertEqual(session.state, "auto-committed")
        self.assertEqual(session.exit_status, 0)
        committed = os.path.join(self.h.base, "Projects", "proj-a",
                                 "result.txt")
        self.assertTrue(os.path.isfile(committed))

    def test_out_of_scope_write_pends_review_and_underlay_untouched(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo hacked > ../../escape.txt"]))
        self.assertEqual(session.state, "pending-review")
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "escape.txt")))

    def test_global_config_delete_is_recoverable(self):
        config = self.h.config(["sh", "-c", "true"])
        session = run_session(config, before_finalize=lambda s: (
            self.h.backend.record_delete(s.protected_roots["storage_user"],
                                         ".bashrc")))
        self.assertEqual(session.state, "pending-review")
        # underlay untouched until a human commits
        self.assertTrue(os.path.isfile(os.path.join(self.h.base, ".bashrc")))

    def test_noop_run_closes_cleanly(self):
        session = run_session(self.h.config(["true"]))
        self.assertEqual(session.state, "auto-committed")
        self.assertTrue(any("no changes" in (e.get("detail") or "")
                            for e in session.events))

    def test_branchfs_status_warnings_force_pending_review(self):
        class WarningStatusBranchFS(FakeBranchFS):
            def status_report(self, root):
                base = FakeBranchFS.status_report(self, root)
                return StatusReport(
                    changes=base.changes,
                    warnings=[StatusWarning(
                        path="/storage/user/Projects/proj-a/unreadable",
                        message="unreadable delta directory; commit may fail",
                        root=root.name,
                    )],
                )

        self.h.backend = WarningStatusBranchFS()
        session = run_session(self.h.config(
            ["sh", "-c", "echo done > result.txt"]))

        self.assertEqual(session.state, "pending-review")
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, "Projects", "proj-a", "result.txt")))
        review = self.h.store.review_dir(session.session_id)
        with open(os.path.join(review, "warnings.storage_user.json")) as fh:
            warnings = json.load(fh)
        self.assertEqual(warnings, [{
            "path": "/storage/user/Projects/proj-a/unreadable",
            "message": "unreadable delta directory; commit may fail",
            "root": "storage_user",
        }])
        with open(os.path.join(review, "policy-decision.json")) as fh:
            decision = json.load(fh)
        self.assertEqual(decision["decision"], "pending-review")
        self.assertTrue(any("BranchFS status warning" in reason
                            for reason in decision["reasons"]))
        with open(os.path.join(review, "summary.md")) as fh:
            summary = fh.read()
        self.assertIn("## BranchFS status warnings", summary)
        self.assertIn("commit may fail", summary)

    def test_shell_history_temp_noise_is_discarded_as_no_change(self):
        session = run_session(
            self.h.config(["sh", "-c", "true"]),
            before_finalize=lambda s: self.h.backend.record_delete(
                s.protected_roots["storage_user"],
                "domen-cuda10/.bash_history-00002.tmp"))

        self.assertEqual(session.state, "auto-committed")
        review_status = os.path.join(
            self.h.store.review_dir(session.session_id),
            "status.storage_user.json")
        with open(review_status) as fh:
            self.assertEqual(json.load(fh), [])
        root = session.protected_roots["storage_user"]
        self.assertEqual(self.h.backend.status(root), [])

    def test_throwaway_mode_aborts_even_clean_writes(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo x > t.txt"], mode="throwaway"))
        self.assertEqual(session.state, "aborted")
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, "Projects", "proj-a", "t.txt")))

    def test_agent_env_carries_session_id(self):
        session = run_session(self.h.config(
            ["sh", "-c", "printf %s \"$CCC_AGENT_SESSION\" > sid.txt"]))
        committed = os.path.join(self.h.base, "Projects", "proj-a", "sid.txt")
        with open(committed) as fh:
            self.assertEqual(fh.read(), session.session_id)

    def test_nonzero_exit_still_finalizes(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo partial > p.txt; exit 3"]))
        self.assertEqual(session.exit_status, 3)
        self.assertEqual(session.state, "auto-committed")

    def test_auto_commit_permission_denied_keeps_only_blocked_paths_for_review(self):
        readonly = os.path.join(self.h.base, "Projects", "proj-a", "readonly")
        os.makedirs(readonly, exist_ok=True)
        os.chmod(readonly, 0o555)
        try:
            session = run_session(self.h.config([
                "sh", "-c",
                "echo ok > ok.txt; mkdir -p readonly; "
                "echo blocked > readonly/no.txt",
            ]))
        finally:
            os.chmod(readonly, 0o755)

        self.assertEqual(session.state, "pending-review")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "ok.txt")))
        self.assertFalse(os.path.exists(os.path.join(readonly, "no.txt")))

        root = session.protected_roots["storage_user"]
        remaining = sorted(change.path for change in self.h.backend.status(root))
        self.assertEqual(remaining,
                         ["/storage/user/Projects/proj-a/readonly/no.txt"])
        failures = session.policy.get("commit_permission_denied", [])
        self.assertEqual([f["path"] for f in failures],
                         ["/storage/user/Projects/proj-a/readonly/no.txt"])
        self.assertTrue(any(e["event"] == "commit-permission-denied"
                            for e in session.events))

        review = self.h.store.review_dir(session.session_id)
        with open(os.path.join(review, "policy-decision.json")) as fh:
            decision = json.load(fh)
        self.assertEqual(decision["decision"], "pending-review")
        self.assertTrue(any("permission denied" in reason.lower()
                            for reason in decision["reasons"]))
        with open(os.path.join(review, "summary.md")) as fh:
            summary = fh.read()
        self.assertIn("Permission denied", summary)
        self.assertIn("readonly/no.txt", summary)

    def test_resume_running_session_reuses_existing_branch(self):
        class NoCreateOnResume(FakeBranchFS):
            def __init__(self):
                super(NoCreateOnResume, self).__init__()
                self.create_calls = 0

            def create_branch(self, root, parent="main"):
                self.create_calls += 1
                raise AssertionError("resume must not create a new branch")

        self.h.backend = NoCreateOnResume()
        session = self.running_session(
            command=["sh", "-c", "echo resumed > resumed.txt"])

        resumed = resume_session(session.session_id,
                                 self.h.config(session.agent_command))

        self.assertEqual(resumed.state, "auto-committed")
        self.assertEqual(self.h.backend.create_calls, 0)
        committed = os.path.join(self.h.base, "Projects", "proj-a",
                                 "resumed.txt")
        self.assertTrue(os.path.isfile(committed))

    def test_resume_clears_stale_mount_before_remounting(self):
        class RequiresStaleCleanup(FakeBranchFS):
            def __init__(self):
                super(RequiresStaleCleanup, self).__init__()
                self.cleanup_calls = []
                self.cleaned = False

            def cleanup_stale_mount(self, root):
                self.cleanup_calls.append(root.mount)
                self.cleaned = True

            def mount(self, root, agent=True, allow_other=False):
                if not self.cleaned:
                    raise RuntimeError("File exists (os error 17)")
                return super(RequiresStaleCleanup, self).mount(
                    root, agent=agent, allow_other=allow_other)

        self.h.backend = RequiresStaleCleanup()
        session = self.running_session(
            session_id="agent-resume-stale-mount",
            command=["sh", "-c", "echo resumed > resumed.txt"])
        root = session.protected_roots["storage_user"]

        resumed = resume_session(session.session_id,
                                 self.h.config(session.agent_command))

        self.assertEqual(resumed.state, "auto-committed")
        self.assertEqual(self.h.backend.cleanup_calls, [root.mount])
        committed = os.path.join(self.h.base, "Projects", "proj-a",
                                 "resumed.txt")
        self.assertTrue(os.path.isfile(committed))

    def test_resume_stale_cleanup_does_not_stat_mountpoint_first(self):
        class RecordsStaleCleanup(FakeBranchFS):
            def __init__(self):
                super(RecordsStaleCleanup, self).__init__()
                self.cleanup_calls = []

            def cleanup_stale_mount(self, root):
                self.cleanup_calls.append(root.mount)

        self.h.backend = RecordsStaleCleanup()
        session = self.running_session(
            session_id="agent-resume-no-mountpoint-probe",
            command=["sh", "-c", "echo resumed > resumed.txt"])
        root = session.protected_roots["storage_user"]

        with mock.patch("ccc_agent.runner.os.path.ismount",
                        side_effect=AssertionError(
                            "resume must not stat stale FUSE mountpoints")):
            resumed = resume_session(session.session_id,
                                     self.h.config(session.agent_command))

        self.assertEqual(resumed.state, "auto-committed")
        self.assertEqual(self.h.backend.cleanup_calls, [root.mount])

    def test_resume_failed_session_requires_explicit_allow_failed(self):
        session = self.failed_session(
            command=["sh", "-c", "echo recovered > recovered.txt"])

        with self.assertRaisesRegex(ResumeError, "--allow-failed"):
            resume_session(session.session_id,
                           self.h.config(session.agent_command))

    def test_resume_failed_session_when_allowed_reopens_and_finalizes(self):
        class ThawBeforeMountFake(FakeBranchFS):
            def __init__(self):
                super(ThawBeforeMountFake, self).__init__()
                self.thaw_calls = 0

            def thaw(self, root):
                self.thaw_calls += 1
                return super(ThawBeforeMountFake, self).thaw(root)

            def mount(self, root, agent=True, allow_other=False):
                if self.branch_state(root) != "open":
                    raise AssertionError("failed resume must thaw before mount")
                return super(ThawBeforeMountFake, self).mount(
                    root, agent=agent, allow_other=allow_other)

        self.h.backend = ThawBeforeMountFake()
        session = self.failed_session(
            command=["sh", "-c", "echo recovered > recovered.txt"])
        root = session.protected_roots["storage_user"]
        self.h.backend.freeze(root)

        resumed = resume_session(session.session_id,
                                 self.h.config(session.agent_command),
                                 allow_failed=True)

        self.assertEqual(resumed.state, "auto-committed")
        self.assertEqual(self.h.backend.thaw_calls, 1)
        self.assertNotEqual(resumed.finished_at, "2000-01-01T00:00:00Z")
        committed = os.path.join(self.h.base, "Projects", "proj-a",
                                 "recovered.txt")
        self.assertTrue(os.path.isfile(committed))
        persisted = self.h.store.load(session.session_id)
        self.assertTrue(any(e["event"] == "resume-from-failed"
                            for e in persisted.events))

    def test_resume_pending_review_session_thaws_existing_branch(self):
        class ThawBeforeMountFake(FakeBranchFS):
            def __init__(self):
                super(ThawBeforeMountFake, self).__init__()
                self.thaw_calls = 0

            def thaw(self, root):
                self.thaw_calls += 1
                return super(ThawBeforeMountFake, self).thaw(root)

            def mount(self, root, agent=True, allow_other=False):
                if self.branch_state(root) != "open":
                    raise AssertionError(
                        "pending-review resume must thaw before mount")
                return super(ThawBeforeMountFake, self).mount(
                    root, agent=agent, allow_other=allow_other)

        self.h.backend = ThawBeforeMountFake()
        session = run_session(self.h.config(
            ["sh", "-c", "echo kept > kept.txt"], mode="manual"))
        self.assertEqual(session.state, "pending-review")
        root = session.protected_roots["storage_user"]

        resumed = resume_session(
            session.session_id,
            self.h.config(["sh", "-c", "echo more > more.txt"]))

        self.assertEqual(resumed.state, "pending-review")
        self.assertEqual(self.h.backend.thaw_calls, 1)
        paths = sorted(change.path for change in self.h.backend.status(root))
        self.assertIn("/storage/user/Projects/proj-a/kept.txt", paths)
        self.assertIn("/storage/user/Projects/proj-a/more.txt", paths)
        persisted = self.h.store.load(session.session_id)
        self.assertTrue(any(e["event"] == "resume-from-pending-review"
                            for e in persisted.events))

    def test_resume_aborted_session_recreates_branch_and_finalizes(self):
        class CreateBeforeMountFake(FakeBranchFS):
            def __init__(self):
                super(CreateBeforeMountFake, self).__init__()
                self.create_calls = 0

            def create_branch(self, root, parent="main"):
                self.create_calls += 1
                return super(CreateBeforeMountFake, self).create_branch(
                    root, parent=parent)

            def mount(self, root, agent=True, allow_other=False):
                if self._key(root) not in self._state:
                    raise AssertionError(
                        "aborted resume must recreate branch before mount")
                return super(CreateBeforeMountFake, self).mount(
                    root, agent=agent, allow_other=allow_other)

        self.h.backend = CreateBeforeMountFake()
        session = self.running_session(
            session_id="agent-resume-aborted",
            command=["sh", "-c", "echo rerun > rerun.txt"])
        root = session.protected_roots["storage_user"]
        self.h.backend.abort(root)
        session.transition("aborted")
        session.finished_at = "2000-01-01T00:00:00Z"
        self.h.store.save(session)

        resumed = resume_session(session.session_id,
                                 self.h.config(session.agent_command))

        self.assertEqual(resumed.state, "auto-committed")
        self.assertEqual(self.h.backend.create_calls, 1)
        self.assertNotEqual(resumed.finished_at, "2000-01-01T00:00:00Z")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "rerun.txt")))
        persisted = self.h.store.load(session.session_id)
        self.assertTrue(any(e["event"] == "resume-from-aborted"
                            for e in persisted.events))

    def test_resume_custom_command_is_one_shot_and_preserves_original_exec(self):
        original = ["sh", "-c", "echo original > original.txt"]
        session = self.running_session(session_id="agent-resume-custom",
                                       command=original)

        resumed = resume_session(
            session.session_id,
            self.h.config(["sh", "-c", "echo custom > custom.txt"],
                          agent_kind="command"))

        self.assertEqual(resumed.state, "auto-committed")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "custom.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, "Projects", "proj-a", "original.txt")))
        persisted = self.h.store.load(session.session_id)
        self.assertEqual(persisted.agent_command, original)
        self.assertTrue(any(e["event"] == "resume-command"
                            and "custom.txt" in e.get("detail", "")
                            for e in persisted.events))

    def test_review_artifacts_written(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo x > ../../oops.txt"]))
        review = self.h.store.review_dir(session.session_id)
        for name in ("session.json", "status.storage_user.json",
                     "policy-decision.json", "summary.md"):
            self.assertTrue(os.path.isfile(os.path.join(review, name)),
                            "missing artifact %s" % name)
        with open(os.path.join(review, "policy-decision.json")) as fh:
            decision = json.load(fh)
        self.assertEqual(decision["decision"], "pending-review")
        self.assertEqual(decision["out_of_scope"], ["/storage/user/oops.txt"])
        with open(os.path.join(review, "summary.md")) as fh:
            summary = fh.read()
        self.assertIn(session.session_id, summary)
        self.assertIn("ccc-agent commit", summary)

    def test_review_artifacts_record_ignored_policy_changes(self):
        session = run_session(self.h.config([
            "sh", "-c",
            "echo keep > result.txt; mkdir -p ../../.cache/pip; "
            "echo wheel > ../../.cache/pip/wheel.txt",
        ], mode="manual"))
        self.assertEqual(session.state, "pending-review")

        review = self.h.store.review_dir(session.session_id)
        with open(os.path.join(review, "status.storage_user.json")) as fh:
            visible = json.load(fh)
        with open(os.path.join(review, "ignored.storage_user.json")) as fh:
            ignored = json.load(fh)

        self.assertEqual([c["path"] for c in visible],
                         ["/storage/user/Projects/proj-a/result.txt"])
        self.assertEqual([c["path"] for c in ignored],
                         ["/storage/user/.cache/pip/wheel.txt"])
        self.assertEqual(ignored[0]["ignore_pattern"], ".cache")

        with open(os.path.join(review, "summary.md")) as fh:
            summary = fh.read()
        self.assertIn("## Ignored by policy (not committed)", summary)
        self.assertIn(".cache", summary)
        self.assertIn("--include-ignored", summary)

    def test_mounts_and_reviews_live_under_session_bundle(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo x > ../../outside.txt"]))
        bundle = os.path.join(self.h.state_dir, session.session_id)
        root = session.protected_roots["storage_user"]

        self.assertEqual(root.mount,
                         os.path.join(bundle, "mounts", "storage_user"))
        self.assertEqual(self.h.store.review_dir(session.session_id),
                         os.path.join(bundle, "reviews"))
        self.assertTrue(os.path.isfile(os.path.join(bundle, "session",
                                                    "session.json")))
        self.assertFalse(os.path.exists(os.path.join(self.h.state_dir,
                                                     "mounts")))
        self.assertFalse(os.path.exists(os.path.join(self.h.state_dir,
                                                     "reviews")))

    def test_mount_failure_marks_session_failed(self):
        class FailingMount(FakeBranchFS):
            def mount(self, root, agent=True):
                raise RuntimeError("no fuse for you")

        self.h.backend = FailingMount()
        session = run_session(self.h.config(["true"]))
        self.assertEqual(session.state, "failed")
        persisted = self.h.store.load(session.session_id)
        self.assertEqual(persisted.state, "failed")

    def test_auto_commit_unmounts_before_committing(self):
        # The real branchfs binary fails commit-branch with ENOTEMPTY if the
        # branch is still mounted (the store dir is busy).  The supervisor must
        # unmount the bundle before applying the commit decision.
        class MountedCommitFails(FakeBranchFS):
            def commit(self, root):
                if root.mount in self._mounted:
                    raise RuntimeError("Directory not empty (os error 39)")
                super(MountedCommitFails, self).commit(root)

        self.h.backend = MountedCommitFails()
        session = run_session(self.h.config(
            ["sh", "-c", "echo done > result.txt"]))
        self.assertEqual(session.state, "auto-committed")
        committed = os.path.join(self.h.base, "Projects", "proj-a",
                                 "result.txt")
        self.assertTrue(os.path.isfile(committed))

    def test_nested_invocation_reuses_session(self):
        outer = run_session(self.h.config(
            ["sh", "-c", "echo outer > outer.txt"]))
        before = len(self.h.store.list())
        nested_config = self.h.config(["true"])
        nested = run_session(nested_config,
                             env={"CCC_AGENT_SESSION": outer.session_id})
        self.assertEqual(nested.session_id, outer.session_id)
        self.assertEqual(len(self.h.store.list()), before)

    def test_frozen_branches_left_frozen_on_pending_review(self):
        session = run_session(self.h.config(
            ["sh", "-c", "echo x > ../../outside.txt"]))
        root = session.protected_roots["storage_user"]
        self.assertEqual(self.h.backend.branch_state(root), "frozen")


class TestConfinementModes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = RunnerHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_confinement_rejected(self):
        with self.assertRaises(ValueError):
            self.h.config(["true"], confinement="jail")

    def test_chroot_confinement_no_longer_supported(self):
        # chroot was removed (needed a privileged container); it must now be
        # rejected like any other unknown mode.
        with self.assertRaises(ValueError):
            self.h.config(["true"], confinement="chroot")

    def test_none_confinement_runs_command_directly(self):
        # Regression: default mode must not wrap the command.
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self.h.config(["my-agent", "--flag"]))
        self.assertEqual(seen["argv"], ["my-agent", "--flag"])


class TestBwrapConfinement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = RunnerHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _bwrap_config(self, argv, **kw):
        return self.h.config(argv, confinement="bwrap",
                             bwrap_bin="/opt/ccc-agent/bin/bwrap", **kw)

    def _wrapped_agent_command(self, argv):
        sep = argv.index("--")
        self.assertEqual(argv[sep + 1:sep + 3], ["/usr/bin/python3", "-c"])
        self.assertIn("child = subprocess.Popen", argv[sep + 3])
        self.assertEqual(argv[sep + 4], "ccc-agent-runner")
        return argv[sep + 5:]

    def _fake_bwrap(self):
        path = os.path.join(self._tmp.name, "fake-bwrap.py")
        with open(path, "w") as fh:
            fh.write(r'''#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
env = dict(os.environ)
binds = {}
i = 0
while i < len(args) and args[i] != "--":
    token = args[i]
    if token == "--setenv":
        env[args[i + 1]] = args[i + 2]
        i += 3
    elif token in ("--bind", "--ro-bind", "--dev-bind"):
        binds[args[i + 2]] = args[i + 1]
        i += 3
    elif token in ("--uid", "--gid", "--chdir", "--proc", "--dev",
                   "--tmpfs", "--dir", "--symlink"):
        i += 2 if token not in ("--symlink",) else 3
    else:
        i += 1
command = args[i + 1:]
command = [binds.get(part, part) for part in command]
for key, value in list(env.items()):
    env[key] = binds.get(value, value)
os.execvpe(command[0], command, env)
''')
        os.chmod(path, 0o755)
        return path

    def _wait_terminal(self, session_id, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            session = self.h.store.load(session_id)
            if session.state in ("auto-committed", "committed",
                                 "pending-review", "aborted", "failed"):
                return session
            time.sleep(0.01)
        self.fail("adaptive supervisor did not finalize session %s" % session_id)

    def test_adaptive_requires_bwrap(self):
        with self.assertRaisesRegex(ValueError, "requires bwrap"):
            self.h.config(["true"], lifecycle="adaptive")

    def test_adaptive_default_allows_slow_remote_login_shell_bootstrap(self):
        config = self._bwrap_config(["true"], lifecycle="adaptive")

        self.assertEqual(config.adaptive_bootstrap_seconds, 10.0)

    def test_adaptive_tty_run_uses_foreground_lifecycle(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        config = self._bwrap_config(["true"], lifecycle="adaptive")
        with mock.patch.object(os, "isatty", return_value=True), \
                mock.patch.object(os, "fork",
                                  side_effect=AssertionError("must not detach TTY")), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(config)

        self.assertEqual(session.state, "auto-committed")
        self.assertIn(BWRAP_AGENT_RUNNER, seen["argv"])

    def test_adaptive_path_inherits_environment_and_applies_explicit_removals(self):
        output = os.path.join(self._tmp.name, "adaptive-env.json")
        code = r'''
import json, os, sys
names = ["CONTAINER_NAME", "CCC_FUSE_SIDECAR_SOCKET", "EXTERNAL_API_TOKEN",
         "DROP_ME", "CCC_AGENT_STATE_DIR", "CCC_AGENT_CONTROL_TOKEN"]
with open(sys.argv[1], "w") as fh:
    json.dump({name: os.environ.get(name) for name in names}, fh)
'''
        config = self.h.config(
            [sys.executable, "-c", code, output], confinement="bwrap",
            bwrap_bin=self._fake_bwrap(), lifecycle="adaptive",
            adaptive_bootstrap_seconds=0.5,
            adaptive_stability_seconds=0.03,
            adaptive_detach_seconds=0.2,
            bwrap_unsetenv=["DROP_ME"], per_turn=False)
        session = run_session(config, env={
            "PATH": os.environ["PATH"],
            "SHELL": "/bin/bash",
            "CONTAINER_NAME": "domen-cuda10",
            "CCC_FUSE_SIDECAR_SOCKET": "/run/ccc-fuse-sidecar/fuse.sock",
            "EXTERNAL_API_TOKEN": "keep-me",
            "DROP_ME": "remove-me",
            ENV_STATE_DIR: "/stale/state",
            ENV_CONTROL_TOKEN: "stale-token",
        })

        self.assertEqual(session.state, "auto-committed")
        with open(output) as fh:
            observed = json.load(fh)
        self.assertEqual(observed["CONTAINER_NAME"], "domen-cuda10")
        self.assertEqual(observed["CCC_FUSE_SIDECAR_SOCKET"],
                         "/run/ccc-fuse-sidecar/fuse.sock")
        self.assertEqual(observed["EXTERNAL_API_TOKEN"], "keep-me")
        self.assertIsNone(observed["DROP_ME"])
        self.assertIsNone(observed[ENV_STATE_DIR])
        self.assertIsNone(observed[ENV_CONTROL_TOKEN])

    def test_adaptive_clean_detach_returns_running_then_finalizes(self):
        code = r'''
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(0.35)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, start_new_session=True)
'''
        config = self.h.config(
            [sys.executable, "-c", code], confinement="bwrap",
            bwrap_bin=self._fake_bwrap(), lifecycle="adaptive",
            adaptive_bootstrap_seconds=0.5,
            adaptive_stability_seconds=0.03,
            adaptive_detach_seconds=0.2,
            per_turn=False)

        started = time.monotonic()
        session = run_session(config)
        elapsed = time.monotonic() - started

        self.assertEqual(session.state, "running")
        self.assertLess(elapsed, 0.3)
        finished = self._wait_terminal(session.session_id)
        self.assertEqual(finished.state, "auto-committed")
        self.assertEqual(finished.exit_status, 0)
        self.assertTrue(any(e["event"] == "adaptive-handoff"
                            for e in finished.events))

    def test_adaptive_services_use_independent_concurrent_sessions(self):
        code = r'''
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(0.3)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, start_new_session=True)
'''
        def config():
            return self.h.config(
                [sys.executable, "-c", code], confinement="bwrap",
                bwrap_bin=self._fake_bwrap(), lifecycle="adaptive",
                adaptive_bootstrap_seconds=0.5,
                adaptive_stability_seconds=0.02,
                adaptive_detach_seconds=0.15,
                per_turn=False)

        first = run_session(config())
        second = run_session(config())

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(first.state, "running")
        self.assertEqual(second.state, "running")
        self.assertEqual(self._wait_terminal(first.session_id).state,
                         "auto-committed")
        self.assertEqual(self._wait_terminal(second.session_id).state,
                         "auto-committed")

    def test_adaptive_foreground_exit_does_not_wait_for_leaked_helper(self):
        code = r'''
import subprocess, sys, time
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, start_new_session=True)
time.sleep(0.18)
'''
        config = self.h.config(
            [sys.executable, "-c", code], confinement="bwrap",
            bwrap_bin=self._fake_bwrap(), lifecycle="adaptive",
            adaptive_bootstrap_seconds=0.05,
            adaptive_stability_seconds=0.02,
            adaptive_detach_seconds=0.08,
            per_turn=False)

        started = time.monotonic()
        session = run_session(config)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        self.assertEqual(session.state, "auto-committed")
        self.assertTrue(any(e["event"] == "adaptive-foreground-locked"
                            for e in session.events))

    def test_adaptive_remote_bridges_are_hidden_aborted_and_removed(self):
        for agent in ("claude", "codex", "hermes"):
            with self.subTest(agent=agent):
                def seed_delta(running_session):
                    root = running_session.protected_roots["storage_user"]
                    path = os.path.join(
                        root.store, "branches", root.branch, "files",
                        "Projects", "proj-a", "bridge-only.txt")
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as fh:
                        fh.write("discard me\n")

                self.h.backend = FakeBranchFS()
                config = self.h.config(
                    [sys.executable, "-c", "import time; time.sleep(0.08)"],
                    agent_kind=agent + "-remote", server_mode=True,
                    confinement="bwrap", bwrap_bin=self._fake_bwrap(),
                    lifecycle="adaptive", adaptive_bootstrap_seconds=0.02,
                    adaptive_stability_seconds=0.01,
                    adaptive_detach_seconds=0.05, per_turn=False,
                    on_session_start=seed_delta)

                session = run_session(
                    config,
                    before_finalize=lambda _session: self.fail(
                        "remote bridge must be discarded, not finalized"))

                self.assertEqual(session.agent_kind, agent + "-remote-bridge")
                self.assertEqual(session.state, "aborted")
                self.assertEqual(self.h.store.list(), [])
                with self.assertRaises(KeyError):
                    self.h.store.load(session.session_id)
                self.assertFalse(os.path.exists(
                    self.h.store.bundle_dir(session.session_id)))
                self.assertFalse(os.path.exists(os.path.join(
                    self.h.base, "Projects", "proj-a", "bridge-only.txt")))
                root = session.protected_roots["storage_user"]
                self.assertEqual(self.h.backend.status(root), [])

    def test_bwrap_needs_no_script_or_uid(self):
        # Unlike chroot, bwrap is rootless: it must not require uid/gid/script.
        cfg = self.h.config(["true"], confinement="bwrap")
        self.assertEqual(cfg.confinement, "bwrap")

    def test_bwrap_rejects_bad_proc_mode(self):
        with self.assertRaises(ValueError):
            self.h.config(["true"], confinement="bwrap",
                          bwrap_proc_mode="magic")

    def test_bwrap_uses_pid1_lifecycle_wrapper_for_agent_command(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["my-agent", "--flag"]))

        argv = seen["argv"]
        self.assertIn("--as-pid-1", argv)
        sep = argv.index("--")
        self.assertEqual(argv[sep + 1:sep + 3], ["/usr/bin/python3", "-c"])
        self.assertIn("child = subprocess.Popen", argv[sep + 3])
        self.assertIn("returncode = child.wait()", argv[sep + 3])
        self.assertEqual(argv[sep + 4], "ccc-agent-runner")
        self.assertEqual(self._wrapped_agent_command(argv),
                         ["my-agent", "--flag"])

    def test_bwrap_lifecycle_wrapper_preserves_agent_exit_status(self):
        proc = subprocess.run(
            [sys.executable, "-c", BWRAP_AGENT_RUNNER,
             BWRAP_AGENT_RUNNER_ARG0, "sh", "-c", "exit 7"])

        self.assertEqual(proc.returncode, 7)

    def test_bwrap_uses_unshimmed_path_export_for_agent_lookup(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        path = "/tmp/conda-agent-bin:/usr/bin:/bin"
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["codex"], agent_kind="codex"),
                        env={"CCC_AGENT_SHIM_UNDERLYING_PATH": path})

        self.assertEqual(seen["env"].get("PATH"), path)

    def test_bwrap_preserves_invoking_login_shell(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"]),
                        env={"SHELL": "/bin/bash"})

        self.assertEqual(seen["env"].get("SHELL"), "/bin/bash")

    def test_bwrap_mode_builds_sandbox_and_wraps_command(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(argv, 0)

        # bwrap is same-uid, so the view must NOT be mounted allow_other.
        orig_mount = self.h.backend.mount
        mounts = []

        def spy_mount(root, agent=True, allow_other=False):
            mounts.append(allow_other)
            return orig_mount(root, agent=agent, allow_other=allow_other)

        self.h.backend.mount = spy_mount

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(["my-agent", "--flag"]))

        self.assertTrue(mounts and not any(mounts),
                        "bwrap is same-uid; allow_other must stay off")
        argv = seen["argv"]
        self.assertEqual(argv[0], "/opt/ccc-agent/bin/bwrap")
        self.assertIn("--unshare-user", argv)
        self.assertIn("--unshare-pid", argv)
        # OS exposed read-only (first --ro-bind is /usr)
        i = argv.index("--ro-bind")
        self.assertEqual(argv[i + 1], "/usr")
        # the view is bound rw at its visible path and at $HOME
        self.assertIn("/storage/user", argv)
        self.assertIn("/home/domen", argv)
        # workspace is the sandbox cwd via --chdir; keep the user-visible alias
        # from the launch directory instead of canonicalizing to /storage.
        ci = argv.index("--chdir")
        self.assertEqual(argv[ci + 1], "/home/domen/Projects/proj-a")
        # the real agent command is run by the lifecycle wrapper after --
        self.assertEqual(self._wrapped_agent_command(argv),
                         ["my-agent", "--flag"])
        # no host-side cwd is forced (bwrap --chdir handles it)
        self.assertIsNone(seen["cwd"])
        self.assertTrue(any(e.get("kind") == "bwrap-launch"
                            or e.get("event") == "bwrap-launch"
                            for e in session.events))

    def test_bwrap_exposes_container_run_var_and_dev_by_default(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"]))

        argv = seen["argv"]
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        pairs = [(argv[k], argv[k + 1])
                 for k in range(len(argv) - 1)]
        self.assertIn(("--bind", "/run", "/run"), triples)
        self.assertIn(("--ro-bind", "/var", "/var"), triples)
        self.assertNotIn(("--dir", "/var"), pairs)
        self.assertNotIn(("--symlink", "/run", "/var/run"), triples)
        self.assertIn(("--dev-bind", "/dev", "/dev"), triples)
        self.assertNotIn(("--bind", "/dev", "/dev"), triples)
        self.assertNotIn("--dev", argv)

    def test_bwrap_uses_accessible_docker_socket_gid(self):
        seen = {}
        real_stat = os.stat
        real_access = os.access

        def docker_socket_stat():
            values = list(real_stat("/run"))
            values[stat.ST_MODE] = stat.S_IFSOCK | 0o660
            values[stat.ST_UID] = 0
            values[stat.ST_GID] = 998
            return os.stat_result(values)

        def fake_stat(path, *args, **kwargs):
            if os.fspath(path) == "/var/run/docker.sock":
                return docker_socket_stat()
            return real_stat(path, *args, **kwargs)

        def fake_access(path, mode, *args, **kwargs):
            if os.fspath(path) == "/var/run/docker.sock":
                return True
            return real_access(path, mode, *args, **kwargs)

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        patches = (
            mock.patch.object(os, "stat", side_effect=fake_stat),
            mock.patch.object(os, "access", side_effect=fake_access),
            mock.patch.object(os, "getgid", return_value=2094),
            mock.patch.object(os, "getgroups", return_value=[2094, 998]),
            mock.patch.object(subprocess, "run", side_effect=fake_run),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            run_session(self._bwrap_config(["true"]))

        argv = seen["argv"]
        gi = argv.index("--gid")
        self.assertEqual(argv[gi + 1], "998")

    def test_bwrap_explicit_gid_overrides_accessible_docker_socket_gid(self):
        seen = {}
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            if os.fspath(path) == "/var/run/docker.sock":
                values = list(real_stat("/run"))
                values[stat.ST_MODE] = stat.S_IFSOCK | 0o660
                values[stat.ST_UID] = 0
                values[stat.ST_GID] = 998
                return os.stat_result(values)
            return real_stat(path, *args, **kwargs)

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(os, "stat", side_effect=fake_stat), \
                mock.patch.object(os, "access", return_value=True), \
                mock.patch.object(os, "getgid", return_value=2094), \
                mock.patch.object(os, "getgroups", return_value=[2094, 998]), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"], bwrap_gid=2094))

        argv = seen["argv"]
        gi = argv.index("--gid")
        self.assertEqual(argv[gi + 1], "2094")

    def test_bwrap_full_isolation_omits_container_run_and_uses_minimal_dev(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"], container_run_access=False))

        argv = seen["argv"]
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertNotIn(("--bind", "/run", "/run"), triples)
        self.assertNotIn(("--bind", "/dev", "/dev"), triples)
        self.assertNotIn(("--ro-bind", "/var", "/var"), triples)
        self.assertNotIn(("--symlink", "/run", "/var/run"), triples)
        self.assertNotIn(("--dir", "/var"), [(argv[k], argv[k + 1])
                                              for k in range(len(argv) - 1)])
        self.assertIn(("--dev", "/dev"), [(argv[k], argv[k + 1])
                                           for k in range(len(argv) - 1)])

    def test_bwrap_full_isolation_keeps_primary_gid_even_if_docker_socket_accessible(self):
        seen = {}
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            if os.fspath(path) == "/var/run/docker.sock":
                values = list(real_stat("/run"))
                values[stat.ST_MODE] = stat.S_IFSOCK | 0o660
                values[stat.ST_UID] = 0
                values[stat.ST_GID] = 998
                return os.stat_result(values)
            return real_stat(path, *args, **kwargs)

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(os, "stat", side_effect=fake_stat), \
                mock.patch.object(os, "access", return_value=True), \
                mock.patch.object(os, "getgid", return_value=2094), \
                mock.patch.object(os, "getgroups", return_value=[2094, 998]), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"], container_run_access=False))

        argv = seen["argv"]
        gi = argv.index("--gid")
        self.assertEqual(argv[gi + 1], "2094")

    def test_bwrap_rejects_invalid_environment_removal_names(self):
        for name in ("", "BAD=NAME", "BAD\x00NAME"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError,
                                            "invalid bwrap_unsetenv"):
                    self._bwrap_config(["true"], bwrap_unsetenv=[name])

    def test_bwrap_inherits_full_invocation_environment_with_explicit_removals(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        invoking_env = {
            "PATH": "/opt/ccc-agent/shims:/usr/bin",
            "SHELL": "/bin/bash",
            "CONTAINER_NAME": "domen-cuda10",
            "CONTAINER_NODE": "donbot",
            "CCC_FUSE_SIDECAR_SOCKET": "/run/ccc-fuse-sidecar/fuse.sock",
            "NVIDIA_VISIBLE_DEVICES": "void",
            "EXTERNAL_API_TOKEN": "keep-me",
            "SOURCE_CRED": "source-secret",
            "DROP_ME": "remove-me",
            "OVERRIDE_ME": "old-value",
            ENV_STATE_DIR: "/stale/supervisor/state",
            ENV_CONTROL_TOKEN: "stale-control-token",
        }
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(
                ["true"], per_turn=False,
                bwrap_unsetenv=["DROP_ME", "OVERRIDE_ME"],
                bwrap_setenv={"OVERRIDE_ME": "trusted-value",
                              "TERM": "trusted-term"},
                cred_env={"CRED_FROM_ENV": {"env": "SOURCE_CRED"}}),
                env=invoking_env)

        self.assertNotIn("--clearenv", seen["argv"])
        self.assertEqual(seen["env"]["CONTAINER_NAME"], "domen-cuda10")
        self.assertEqual(seen["env"]["CONTAINER_NODE"], "donbot")
        self.assertEqual(seen["env"]["CCC_FUSE_SIDECAR_SOCKET"],
                         "/run/ccc-fuse-sidecar/fuse.sock")
        self.assertEqual(seen["env"]["NVIDIA_VISIBLE_DEVICES"], "void")
        self.assertEqual(seen["env"]["EXTERNAL_API_TOKEN"], "keep-me")
        self.assertNotIn("DROP_ME", seen["env"])
        self.assertEqual(seen["env"].get("OVERRIDE_ME"), "trusted-value")
        self.assertEqual(seen["env"].get("TERM"), "trusted-term")
        self.assertEqual(seen["env"].get("CRED_FROM_ENV"), "source-secret")
        self.assertNotIn("trusted-value", seen["argv"])
        self.assertNotIn("source-secret", seen["argv"])
        self.assertNotIn(ENV_STATE_DIR, seen["env"])
        self.assertNotIn(ENV_CONTROL_TOKEN, seen["env"])

    def test_bwrap_fresh_control_token_is_environment_only_not_argv(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"], per_turn=True), env={})

        token = seen["env"][ENV_CONTROL_TOKEN]
        self.assertTrue(token)
        self.assertNotIn(token, seen["argv"])

    def test_bwrap_ro_binds_and_setenv_after_view(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        runtime = self.h.base
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(
                ["true"],
                bwrap_ro_binds=[runtime, runtime + ":/ccc-agent", "/no/such/path"],
                bwrap_setenv={"OPENAI_API_KEY": "sek-test"}))
        argv = seen["argv"]
        # Existing runtime paths are re-exposed read-only; missing optional paths
        # are skipped before invoking bwrap.
        self.assertIn(runtime, argv)
        self.assertNotIn("/no/such/path", argv)
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--ro-bind", runtime, "/ccc-agent"), triples)
        # the ro-bind for the runtime must come AFTER the view bind so it wins
        view_i = argv.index("/storage/user")
        ro_i = max(k for k in range(len(argv) - 1)
                   if argv[k] == "--ro-bind" and argv[k + 1] == runtime)
        self.assertGreater(ro_i, view_i)
        # Value-bearing overrides use the process environment, not argv.
        self.assertEqual(seen["env"].get("OPENAI_API_KEY"), "sek-test")
        self.assertNotIn("sek-test", argv)

    def test_bwrap_ro_bind_resolves_symlink_to_existing_target(self):
        target = os.path.join(self._tmp.name, "real-storage", "domen", ".claude")
        os.makedirs(target)
        home = os.path.join(self._tmp.name, "home", "domen")
        os.makedirs(home)
        link = os.path.join(home, ".claude")
        os.symlink(target, link)

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"], bwrap_ro_binds=[link]))

        argv = seen["argv"]
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--ro-bind", target, target), triples)
        self.assertNotIn(link, argv)

    def test_bwrap_ro_bind_skips_broken_symlink(self):
        home = os.path.join(self._tmp.name, "home", "domen")
        os.makedirs(home)
        broken_target = os.path.join(self._tmp.name, "missing", ".claude")
        link = os.path.join(home, ".claude")
        os.symlink(broken_target, link)

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(["true"], bwrap_ro_binds=[link]))

        argv = seen["argv"]
        self.assertNotIn(link, argv)
        self.assertNotIn(broken_target, argv)

    def _make_plugin(self, name):
        """Create a fake trusted plugin source dir (must exist on the host)."""
        path = os.path.join(self._tmp.name, name)
        os.makedirs(os.path.join(path, "hooks"), exist_ok=True)
        return path

    def _capture_argv(self, command, agent_kind, agent_plugins,
                      return_env=False, **extra):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(
                command, agent_kind=agent_kind, agent_plugins=agent_plugins,
                **extra))
        return ((seen["argv"], seen["env"])
                if return_env else seen["argv"])

    def _agent_state_binds(self):
        paths = {}
        binds = []
        for name in ("codex", "claude", "hermes"):
            path = os.path.join(self._tmp.name, "real-agent-state", name)
            os.makedirs(path)
            paths[name] = path
        binds.append(paths["codex"] + ":/home/domen/.codex")
        binds.append(paths["claude"] + ":/home/domen/.claude")
        binds.append(paths["hermes"] + ":/home/domen/.hermes")
        return paths, binds

    def test_bwrap_mounts_claude_plugin_without_launch_args_for_direct_claude(self):
        src = self._make_plugin("claude-ccc-containment")
        sandbox = "/ccc-agent/plugins/claude-ccc-containment"
        plugins = {"claude": {"src": src, "sandbox_path": sandbox,
                              "setenv": {"CLAUDE_CODE_PLUGIN_SEED_DIR": sandbox}}}

        argv, env = self._capture_argv(
            ["claude", "-p", "x"], "claude", plugins, return_env=True)
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--ro-bind", src, sandbox), triples)
        self.assertEqual(env.get("CLAUDE_CODE_PLUGIN_SEED_DIR"), sandbox)
        self.assertNotIn(sandbox, [argv[k + 2] for k in range(len(argv) - 2)
                                  if argv[k] == "--setenv" and
                                  argv[k + 1] == "CLAUDE_CODE_PLUGIN_SEED_DIR"])
        self.assertEqual(self._wrapped_agent_command(argv),
                         ["claude", "-p", "x"])
        self.assertNotIn("--plugin-dir", argv)

        # a non-claude command never receives the claude plugin by inference
        other = self._capture_argv(["bash", "-lc", "claude"], "command", plugins)
        self.assertNotIn(src, other)
        self.assertNotIn("--plugin-dir", other)

    def test_bwrap_does_not_inject_claude_plugin_into_serve_direct_agent(self):
        src = self._make_plugin("claude-ccc-containment")
        sandbox = "/ccc-agent/plugins/claude-ccc-containment"
        plugins = {"claude": {"src": src, "sandbox_path": sandbox,
                              "argv": ["--plugin-dir", sandbox]}}
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0)

        cfg = self._bwrap_config(["claude", "-p", "x"], agent_kind="claude",
                                 agent_plugins=plugins, server_mode=True)
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(cfg)

        argv = seen["argv"]
        self.assertIn(src, argv)
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(self._wrapped_agent_command(argv), ["claude", "-p", "x"])

    def test_bwrap_mounts_claude_plugin_without_launch_args_for_ssh_server_shell(self):
        src = self._make_plugin("claude-ccc-containment")
        sandbox = "/ccc-agent/plugins/claude-ccc-containment"
        plugins = {"claude": {"src": src, "sandbox_path": sandbox,
                              "setenv": {"CLAUDE_CODE_PLUGIN_SEED_DIR": sandbox}}}

        argv, env = self._capture_argv(
            ["/bin/bash", "-c",
             "'/home/domen/.claude/remote/srv/hash/server' --version"],
            "claude", plugins, return_env=True, server_mode=True)

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--ro-bind", src, sandbox), triples)
        self.assertEqual(env.get("CLAUDE_CODE_PLUGIN_SEED_DIR"), sandbox)
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(
            self._wrapped_agent_command(argv),
            ["/bin/bash", "-c",
             "'/home/domen/.claude/remote/srv/hash/server' --version"])

    def test_bwrap_injects_codex_plugin_with_ensure_dirs(self):
        src = self._make_plugin("codex-ccc-containment")
        sandbox = "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0"
        plugins = {"codex": {"src": src, "sandbox_path": sandbox,
                             "ensure_dirs": ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"],
                             "argv": ["--dangerously-bypass-approvals-and-sandbox"]}}

        argv = self._capture_argv(["codex"], "codex", plugins)
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--ro-bind", src, sandbox), triples)
        self.assertTrue(any(argv[k] == "--dir" and
                            argv[k + 1] == "/home/domen/.codex/plugins/cache/ccc-agent/ccc"
                            for k in range(len(argv) - 1)))
        self.assertEqual(
            self._wrapped_agent_command(argv),
            ["codex", "--dangerously-bypass-approvals-and-sandbox"])

    def test_bwrap_mounts_codex_plugin_without_launch_args_for_ssh_payload_shell(self):
        src = self._make_plugin("codex-ccc-containment")
        sandbox = "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0"
        plugins = {"codex": {"src": src, "sandbox_path": sandbox,
                             "ensure_dirs": ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"]}}

        command = [
            "/bin/bash", "-c",
            "sh -c 'CODEX_REMOTE_PAYLOAD=\"$1\"; exec /bin/sh -c \"$CODEX_REMOTE_PAYLOAD\"' "
            "sh 'PATH=\"${CODEX_INSTALL_DIR:-$HOME/.local/bin}:$PATH\"; export PATH; codex --version'",
        ]
        argv = self._capture_argv(command, "codex", plugins)
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]

        self.assertIn(("--ro-bind", src, sandbox), triples)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(self._wrapped_agent_command(argv), command)

    def test_bwrap_shared_agent_state_dirs_are_rw_binds_by_default(self):
        paths, binds = self._agent_state_binds()
        src = self._make_plugin("codex-ccc-containment")
        sandbox = "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0"
        plugins = {"codex": {"src": src, "sandbox_path": sandbox,
                             "ensure_dirs": ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"],
                             "argv": []}}

        argv = self._capture_argv(["codex"], "codex", plugins,
                                  agent_state_binds=binds)

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--bind", paths["codex"], "/home/domen/.codex"), triples)
        self.assertIn(("--bind", paths["claude"], "/home/domen/.claude"), triples)
        self.assertIn(("--bind", paths["hermes"], "/home/domen/.hermes"), triples)
        self.assertNotIn(("--ro-bind", paths["codex"], "/home/domen/.codex"), triples)

        home_view = next(k for k in range(len(argv) - 2)
                         if argv[k] == "--bind" and argv[k + 2] == "/home/domen")
        codex_state = next(k for k in range(len(argv) - 2)
                           if argv[k] == "--bind" and argv[k + 2] == "/home/domen/.codex")
        plugin_bind = next(k for k in range(len(argv) - 2)
                           if argv[k] == "--ro-bind" and argv[k + 2] == sandbox)
        self.assertGreater(codex_state, home_view)
        self.assertGreater(plugin_bind, codex_state)

    def test_default_agent_state_binds_include_claude_code_runtime_paths(self):
        cfg = self._bwrap_config(["claude"], agent_kind="claude")

        self.assertIn("/home/domen/.codex", cfg.agent_state_binds)
        self.assertIn("/home/domen/.claude", cfg.agent_state_binds)
        self.assertIn("/home/domen/.hermes", cfg.agent_state_binds)
        self.assertIn("/home/domen/.claude.json", cfg.agent_state_binds)
        self.assertIn("/home/domen/.local/bin/codex",
                      cfg.agent_state_binds)
        self.assertIn("/home/domen/.local/bin/claude",
                      cfg.agent_state_binds)
        self.assertIn("/home/domen/.local/share/claude",
                      cfg.agent_state_binds)
        self.assertIn("/home/domen/.local/state/claude",
                      cfg.agent_state_binds)
        self.assertIn("/home/domen/.cache/claude-cli-nodejs",
                      cfg.agent_state_binds)

    def test_ensure_agent_state_dirs_skips_claude_json_file_path(self):
        home = os.path.join(self._tmp.name, "home", "domen")
        binds = [
            os.path.join(home, ".claude"),
            os.path.join(home, ".claude.json"),
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(home, ".local", "share", "claude"),
            os.path.join(home, ".local", "state", "claude"),
            os.path.join(home, ".cache", "claude-cli-nodejs"),
        ]

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(
                ["claude"], agent_kind="claude", agent_state_binds=binds,
                ensure_agent_state_dirs=True))

        self.assertTrue(os.path.isdir(os.path.join(home, ".claude")))
        self.assertFalse(os.path.exists(os.path.join(home, ".claude.json")))
        self.assertFalse(os.path.exists(os.path.join(home, ".local", "bin",
                                                     "claude")))
        self.assertTrue(os.path.isdir(os.path.join(home, ".local", "share",
                                                  "claude")))
        self.assertTrue(os.path.isdir(os.path.join(home, ".local", "state",
                                                  "claude")))
        self.assertTrue(os.path.isdir(os.path.join(home, ".cache",
                                                  "claude-cli-nodejs")))

    def test_bwrap_shared_agent_state_binds_claude_file_and_runtime_dirs(self):
        state = os.path.join(self._tmp.name, "real-agent-state")
        claude_home = os.path.join(state, ".claude")
        claude_json = os.path.join(state, ".claude.json")
        local_bin_claude = os.path.join(state, ".local", "bin", "claude")
        share = os.path.join(state, ".local", "share", "claude")
        local_state = os.path.join(state, ".local", "state", "claude")
        cache = os.path.join(state, ".cache", "claude-cli-nodejs")
        for path in (claude_home, share, local_state, cache):
            os.makedirs(path)
        os.makedirs(os.path.dirname(local_bin_claude))
        with open(claude_json, "w") as fh:
            fh.write("{}\n")
        with open(local_bin_claude, "w") as fh:
            fh.write("#!/bin/sh\n")

        binds = [
            claude_home + ":/home/domen/.claude",
            claude_json + ":/home/domen/.claude.json",
            local_bin_claude + ":/home/domen/.local/bin/claude",
            share + ":/home/domen/.local/share/claude",
            local_state + ":/home/domen/.local/state/claude",
            cache + ":/home/domen/.cache/claude-cli-nodejs",
            os.path.join(state, "missing.json") + ":/home/domen/.missing.json",
        ]

        argv = self._capture_argv(["claude"], "claude", {},
                                  agent_state_binds=binds)

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--bind", claude_home, "/home/domen/.claude"), triples)
        self.assertIn(("--bind", claude_json, "/home/domen/.claude.json"), triples)
        local_bin_dest = os.path.realpath("/home/domen/.local/bin/claude")
        self.assertIn(("--bind", local_bin_claude,
                       local_bin_dest), triples)
        self.assertIn(("--bind", share, "/home/domen/.local/share/claude"), triples)
        self.assertIn(("--bind", local_state, "/home/domen/.local/state/claude"), triples)
        cache_dest = os.path.realpath("/home/domen/.cache/claude-cli-nodejs")
        self.assertIn(("--bind", cache, cache_dest), triples)
        self.assertNotIn("/home/domen/.missing.json", argv)

    def test_claude_runtime_deltas_are_ignored_when_optional_binds_missing(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        def claude_runtime_delta(session):
            root = session.protected_roots["storage_user"]
            files = {
                os.path.join(".claude", "settings.json"): "{}\n",
                ".claude.json": "{}\n",
                os.path.join(".local", "bin", "claude"): "#!/bin/sh\n",
                os.path.join(".local", "share", "claude", "versions",
                             "v1", "node"): "runtime\n",
                os.path.join(".local", "state", "claude", "locks",
                             "pid.lock"): "lock\n",
                os.path.join(".cache", "claude-cli-nodejs", "npm.log"):
                    "log\n",
            }
            for rel, data in files.items():
                path = os.path.join(root.mount, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as fh:
                    fh.write(data)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["claude"], agent_kind="claude", agent_state_binds=[]),
                before_finalize=claude_runtime_delta)

        self.assertEqual(session.state, "auto-committed")
        self.assertIn("/storage/user/.claude",
                      session.policy["ignore_patterns"])
        self.assertIn("/storage/user/.claude.json",
                      session.policy["ignore_patterns"])
        self.assertIn("/storage/user/.local/bin/claude",
                      session.policy["ignore_patterns"])
        self.assertIn("/storage/user/.local/share/claude",
                      session.policy["ignore_patterns"])
        self.assertIn("/storage/user/.local/state/claude",
                      session.policy["ignore_patterns"])
        self.assertIn("/storage/user/.cache/claude-cli-nodejs",
                      session.policy["ignore_patterns"])
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     ".claude")))
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     ".claude.json")))
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     ".local", "bin",
                                                     "claude")))
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     ".local", "share",
                                                     "claude")))

    def test_codex_local_bin_delta_is_ignored_when_optional_bind_missing(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        def codex_runtime_delta(session):
            root = session.protected_roots["storage_user"]
            path = os.path.join(root.mount, ".local", "bin", "codex")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["codex"], agent_kind="codex", agent_state_binds=[]),
                before_finalize=codex_runtime_delta)

        self.assertEqual(session.state, "auto-committed")
        self.assertIn("/storage/user/.local/bin/codex",
                      session.policy["ignore_patterns"])
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     ".local", "bin",
                                                     "codex")))

    def test_claude_runtime_ignores_do_not_hide_neighbor_local_data(self):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        def neighbor_delta(session):
            root = session.protected_roots["storage_user"]
            path = os.path.join(root.mount, ".local", "share", "project",
                                "data.txt")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("deliverable\n")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["claude"], agent_kind="claude", agent_state_binds=[]),
                before_finalize=neighbor_delta)

        self.assertEqual(session.state, "pending-review")
        with open(os.path.join(self.h.store.review_dir(session.session_id),
                               "policy-decision.json")) as fh:
            decision = json.load(fh)
        self.assertIn("/storage/user/.local/share/project/data.txt",
                      decision["out_of_scope"])

    def test_bwrap_shared_agent_state_bind_resolves_symlink_destination(self):
        target = os.path.join(self._tmp.name, "real-storage", "domen", ".claude")
        os.makedirs(target)
        home = os.path.join(self._tmp.name, "home", "domen")
        os.makedirs(home)
        link = os.path.join(home, ".claude")
        os.symlink(target, link)

        argv = self._capture_argv(["claude"], "claude", {},
                                  agent_state_binds=[link])

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--bind", target, target), triples)
        self.assertNotIn(("--bind", target, link), triples)

    def test_bwrap_shared_agent_state_binds_symlink_target_agent_dir(self):
        # Regression for ~/.codex/config.toml -> /storage/user/.codex/config.toml:
        # binding ~/.codex alone leaves the absolute symlink target inside the
        # BranchFS /storage view, so bind the target .codex dir as shared state too.
        codex_home = os.path.join(self._tmp.name, "real-agent-state", "codex")
        os.makedirs(codex_home)
        target_dir = os.path.join(self._tmp.name, "storage", "user", ".codex")
        os.makedirs(target_dir)
        target = os.path.join(target_dir, "config.toml")
        with open(target, "w") as fh:
            fh.write("model = 'test'\n")
        os.symlink(target, os.path.join(codex_home, "config.toml"))

        argv = self._capture_argv(["codex"], "codex", {},
                                  agent_state_binds=[codex_home +
                                                     ":/home/domen/.codex"])

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--bind", codex_home, "/home/domen/.codex"), triples)
        self.assertIn(("--bind", target_dir, target_dir), triples)

    def test_shared_agent_state_symlink_detection_is_shallow(self):
        # Unit tests must not recursively walk the developer's live
        # ~/.codex/~/.claude caches. The known compatibility case is a top-level
        # config symlink such as ~/.codex/config.toml.
        codex_home = os.path.join(self._tmp.name, "real-agent-state", "codex")
        os.makedirs(codex_home)
        target_dir = os.path.join(self._tmp.name, "storage", "user", ".codex")
        os.makedirs(target_dir)
        target = os.path.join(target_dir, "config.toml")
        with open(target, "w") as fh:
            fh.write("model = 'test'\n")
        os.symlink(target, os.path.join(codex_home, "config.toml"))

        with mock.patch("ccc_agent.runner.os.walk",
                        side_effect=AssertionError("must not recurse")):
            argv = self._capture_argv(["codex"], "codex", {},
                                      agent_state_binds=[codex_home +
                                                         ":/home/domen/.codex"])

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--bind", target_dir, target_dir), triples)

    def test_bwrap_shared_agent_state_does_not_bind_arbitrary_symlink_target(self):
        codex_home = os.path.join(self._tmp.name, "real-agent-state", "codex")
        os.makedirs(codex_home)
        project_dir = os.path.join(self._tmp.name, "storage", "user", "project")
        os.makedirs(project_dir)
        target = os.path.join(project_dir, "config.toml")
        with open(target, "w") as fh:
            fh.write("project-owned\n")
        os.symlink(target, os.path.join(codex_home, "project-config.toml"))

        argv = self._capture_argv(["codex"], "codex", {},
                                  agent_state_binds=[codex_home +
                                                     ":/home/domen/.codex"])

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertNotIn(("--bind", project_dir, project_dir), triples)

    def test_bwrap_protect_agent_state_omits_shared_agent_state_binds(self):
        paths, binds = self._agent_state_binds()

        argv = self._capture_argv(["true"], "command", {},
                                  agent_state_binds=binds,
                                  protect_agent_state=True)

        self.assertNotIn(paths["codex"], argv)
        self.assertNotIn(paths["claude"], argv)
        self.assertNotIn(paths["hermes"], argv)

    def test_shared_agent_state_skips_agent_home_policy_ignores(self):
        _paths, binds = self._agent_state_binds()
        src = self._make_plugin("codex-ccc-containment")
        sandbox = "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0"
        plugins = {"codex": {"src": src, "sandbox_path": sandbox,
                             "ensure_dirs": ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"],
                             "argv": []}}

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["codex"], agent_kind="codex", agent_plugins=plugins,
                agent_state_binds=binds))

        self.assertNotIn("/storage/user/.codex", session.policy["ignore_patterns"])
        self.assertNotIn("/storage/user/.codex/plugins", session.policy["ignore_patterns"])

    def test_protected_agent_state_ignores_only_codex_plugin_subpaths(self):
        _paths, binds = self._agent_state_binds()
        src = self._make_plugin("codex-ccc-containment")
        sandbox = "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0"
        plugins = {"codex": {"src": src, "sandbox_path": sandbox,
                             "ensure_dirs": ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"],
                             "argv": []}}

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["codex"], agent_kind="codex", agent_plugins=plugins,
                agent_state_binds=binds, protect_agent_state=True))

        self.assertNotIn("/storage/user/.codex", session.policy["ignore_patterns"])
        self.assertIn("/storage/user/.codex/plugins/cache/ccc-agent/ccc",
                      session.policy["ignore_patterns"])
        self.assertIn("/storage/user/.codex/plugins/cache/ccc-agent/ccc/0.2.0",
                      session.policy["ignore_patterns"])

    def test_bwrap_plugin_mount_paths_are_ignored_even_without_policy_default(self):
        src = self._make_plugin("codex-ccc-containment")
        sandbox = "/home/domen/.ccc-system/plugins/ccc-agent"
        plugins = {"codex": {"src": src, "sandbox_path": sandbox,
                             "ensure_dirs": ["/home/domen/.ccc-system/plugins"],
                             "argv": []}}

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        def plugin_mountpoint_delta(session):
            root = session.protected_roots["storage_user"]
            path = os.path.join(root.mount, ".ccc-system", "plugins",
                                "ccc-agent", "hooks.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("{}\n")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["codex"], agent_kind="codex", agent_plugins=plugins),
                before_finalize=plugin_mountpoint_delta)

        self.assertEqual(session.state, "auto-committed")
        self.assertIn("/storage/user/.ccc-system", session.policy["ignore_patterns"])
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, ".ccc-system", "plugins", "ccc-agent", "hooks.json")))

    def test_bwrap_ro_bind_destinations_under_home_are_ignored(self):
        runtime = os.path.join(self._tmp.name, "runtime")
        os.makedirs(runtime)

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        def mountpoint_delta(session):
            root = session.protected_roots["storage_user"]
            path = os.path.join(root.mount, ".ccc-runtime", "marker")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("mounted\n")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["true"], bwrap_ro_binds=[runtime + ":/home/domen/.ccc-runtime"]),
                before_finalize=mountpoint_delta)

        self.assertEqual(session.state, "auto-committed")
        self.assertIn("/storage/user/.ccc-runtime",
                      session.policy["ignore_patterns"])
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     ".ccc-runtime", "marker")))

    def test_missing_bwrap_ro_bind_source_does_not_ignore_destination(self):
        missing = os.path.join(self._tmp.name, "missing-runtime")

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0)

        def workspace_delta(session):
            root = session.protected_roots["storage_user"]
            path = os.path.join(root.mount, "Projects", "proj-a", "result.txt")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("agent work\n")

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(
                ["true"],
                bwrap_ro_binds=[missing + ":/home/domen/Projects/proj-a"]),
                before_finalize=workspace_delta)

        self.assertEqual(session.state, "auto-committed")
        self.assertNotIn("/storage/user/Projects/proj-a",
                         session.policy["ignore_patterns"])
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, "Projects", "proj-a", "result.txt")))

    def test_bwrap_auto_detects_plugin_from_absolute_executable_path(self):
        src = self._make_plugin("codex-ccc-containment")
        sandbox = "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0"
        plugins = {"codex": {"src": src, "sandbox_path": sandbox,
                             "ensure_dirs": ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"],
                             "argv": []}}
        absolute_codex = os.path.join(self._tmp.name, "bin", "codex")

        argv = self._capture_argv([absolute_codex, "exec", "x"],
                                  "command", plugins)

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--ro-bind", src, sandbox), triples)
        self.assertEqual(self._wrapped_agent_command(argv),
                         [absolute_codex, "exec", "x"])

    def test_explicit_agent_kind_can_mount_only_without_decorating_different_executable(self):
        codex_src = self._make_plugin("codex-ccc-containment")
        claude_src = self._make_plugin("claude-ccc-containment")
        codex_sandbox = "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0"
        claude_sandbox = "/ccc-agent/plugins/claude-ccc-containment"
        plugins = {
            "codex": {"src": codex_src, "sandbox_path": codex_sandbox,
                      "ensure_dirs": ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"]},
            "claude": {"src": claude_src, "sandbox_path": claude_sandbox,
                       "argv": ["--plugin-dir", claude_sandbox]},
        }
        misleading_claude_path = os.path.join(self._tmp.name, "bin", "claude")

        argv = self._capture_argv([misleading_claude_path, "-p", "x"],
                                  "codex", plugins)

        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        self.assertIn(("--ro-bind", codex_src, codex_sandbox), triples)
        self.assertNotIn(("--ro-bind", claude_src, claude_sandbox), triples)
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(self._wrapped_agent_command(argv),
                         [misleading_claude_path, "-p", "x"])

    def test_bwrap_sets_plugin_env_for_hermes(self):
        src = self._make_plugin("hermes-ccc-containment")
        plugins = {"hermes": {
            "src": src,
            "sandbox_path": "/ccc-agent/plugins/hermes/ccc-agent",
            "setenv": {"HERMES_BUNDLED_PLUGINS": "/ccc-agent/plugins/hermes",
                       "HERMES_ACCEPT_HOOKS": "1"}}}

        argv, process_env = self._capture_argv(
            ["hermes", "chat"], "hermes", plugins, return_env=True)
        self.assertEqual(process_env.get("HERMES_BUNDLED_PLUGINS"),
                         "/ccc-agent/plugins/hermes")
        self.assertEqual(process_env.get("HERMES_ACCEPT_HOOKS"), "1")
        self.assertNotIn("/ccc-agent/plugins/hermes", argv)

    def test_bwrap_skips_plugin_when_source_missing(self):
        # Graceful degradation: a missing trusted plugin dir must not be mounted
        # and must not alter the command (process-exit review still finalizes).
        missing = os.path.join(self._tmp.name, "does-not-exist")
        sandbox = "/ccc-agent/plugins/claude-ccc-containment"
        plugins = {"claude": {"src": missing, "sandbox_path": sandbox,
                              "argv": ["--plugin-dir", sandbox]}}

        argv = self._capture_argv(["claude", "-p", "x"], "claude", plugins)
        self.assertNotIn(missing, argv)
        self.assertNotIn("--plugin-dir", argv)
        self.assertEqual(self._wrapped_agent_command(argv), ["claude", "-p", "x"])

    def test_bwrap_skips_plugin_for_bare_agent(self):
        # --bare disables Claude hooks/plugins, so injection would be a no-op;
        # skip it rather than mount a plugin that will not load.
        src = self._make_plugin("claude-ccc-containment")
        sandbox = "/ccc-agent/plugins/claude-ccc-containment"
        plugins = {"claude": {"src": src, "sandbox_path": sandbox,
                              "argv": ["--plugin-dir", sandbox]}}

        argv = self._capture_argv(["claude", "--bare", "-p", "x"],
                                  "claude", plugins)
        self.assertNotIn(src, argv)
        self.assertNotIn("--plugin-dir", argv)

    def test_bwrap_binds_control_socket_and_sets_env(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["process_env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self._bwrap_config(["my-agent"]))
        argv = seen["argv"]
        sock = "/tmp/ccc-agent/control.sock"
        # the host socket is bind-mounted to a fixed in-sandbox path under
        # private /tmp, not under /run.  The default container /run bind may be
        # root-owned, so bwrap cannot mkdir /run/ccc-agent there.
        bind_dests = [argv[k + 2] for k in range(len(argv) - 2)
                      if argv[k] == "--bind"]
        self.assertIn(sock, bind_dests)
        self.assertNotIn("/run/ccc-agent/control.sock", bind_dests)
        # The socket path is remapped on argv. Fresh tokens are inherited through
        # bwrap's process environment so they are not exposed in /proc cmdline.
        setenv = {argv[k + 1]: argv[k + 2] for k in range(len(argv) - 2)
                  if argv[k] == "--setenv"}
        self.assertEqual(setenv.get("CCC_AGENT_CONTROL_SOCK"), sock)
        token = seen["process_env"].get("CCC_AGENT_CONTROL_TOKEN")
        self.assertTrue(token)
        self.assertNotIn(token, argv)
        expected_host_sock = os.path.join(self.h.state_dir, session.session_id,
                                          "control", "control.sock")
        control_events = [e for e in session.events
                          if e.get("event") == "control-server"]
        self.assertEqual(control_events[-1].get("detail"), expected_host_sock)
        # everything is before the -- command separator except the supervised command
        self.assertEqual(self._wrapped_agent_command(argv), ["my-agent"])
        self.assertTrue(any(e.get("kind") == "control-server"
                            or e.get("event") == "control-server"
                            for e in session.events))

    def test_bwrap_credentials_mount_mask_and_env(self):
        cred_dir = os.path.join(self._tmp.name, "home", ".codex")
        os.makedirs(cred_dir)
        auth = os.path.join(cred_dir, "auth.json")
        with open(auth, "w") as fh:
            json.dump({"tokens": {"access": "sek-xyz"}}, fh)

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self._bwrap_config(
                ["a"], cred_mounts=[cred_dir], cred_mask=[auth],
                cred_env={"OPENAI_API_KEY":
                          {"file": auth, "json_key": "tokens.access"}}))
        argv = seen["argv"]
        triples = [(argv[k], argv[k + 1], argv[k + 2])
                   for k in range(len(argv) - 2)]
        # cred dir re-exposed read-only; missing optional cred dirs are skipped
        # before invoking bwrap.
        self.assertIn(("--ro-bind", cred_dir, cred_dir), triples)
        # secret file masked with /dev/null
        self.assertIn(("--ro-bind", "/dev/null", auth), triples)
        # Credential extracted from the host auth file is inherited via the
        # process environment and never exposed in bwrap argv.
        self.assertEqual(seen["env"].get("OPENAI_API_KEY"), "sek-xyz")
        self.assertNotIn("sek-xyz", argv)

    def test_per_turn_off_starts_no_control_server(self):
        # none mode (per_turn defaults off): no control env injected.
        seen = {}

        def fake_run(argv, **kwargs):
            seen["env"] = dict(kwargs.get("env") or {})
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            session = run_session(self.h.config(["true"]))
        self.assertNotIn("CCC_AGENT_CONTROL_SOCK", seen["env"])
        self.assertFalse(any(e.get("kind") == "control-server"
                             for e in session.events))

    def test_per_turn_opt_in_for_none_sets_host_socket_env(self):
        # `none` debug mode can opt into per-turn; the hook reaches the host
        # socket directly (no sandbox remap).
        seen = {}

        def fake_run(argv, **kwargs):
            seen["env"] = dict(kwargs.get("env") or {})
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            run_session(self.h.config(["true"], per_turn=True))
        self.assertIn("CCC_AGENT_CONTROL_SOCK", seen["env"])
        self.assertTrue(seen["env"].get("CCC_AGENT_CONTROL_TOKEN"))

    def test_bwrap_proc_mode_selects_flag(self):
        cases = {"bind": "--bind", "ro": "--ro-bind", "fresh": "--proc"}
        for mode, flag in cases.items():
            seen = {}

            def fake_run(argv, **kwargs):
                seen["argv"] = list(argv)
                return subprocess.CompletedProcess(argv, 0)

            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                run_session(self._bwrap_config(["true"], bwrap_proc_mode=mode))
            argv = seen["argv"]
            self.assertIn("/proc", argv, "proc not mounted in mode %s" % mode)
            found = any(argv[k] == flag and argv[k + 1] == "/proc"
                        for k in range(len(argv) - 1))
            self.assertTrue(found, "mode %s missing %s /proc" % (mode, flag))


if __name__ == "__main__":
    unittest.main()
