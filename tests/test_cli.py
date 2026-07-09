"""Tests for the ccc-agent run / ccc-agent command-line layer."""

import contextlib
import errno
import io
import json
import os
import signal
import subprocess
import termios
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ccc_agent import cli as cli_mod
from ccc_agent.branchfs import BranchfsCli
from ccc_agent.cli import build_runtime, load_config, main, main_ctl, main_run
from ccc_agent.ctl import Controller
from ccc_agent.session import ProtectedRoot, SessionStore


class CliHarness(object):
    def __init__(self, tmp):
        self.tmp = tmp
        self.base = os.path.join(tmp, "real", "storage_user")
        self.workspace_rel = "Projects/proj-a"
        os.makedirs(os.path.join(self.base, self.workspace_rel))
        self.config_path = os.path.join(tmp, "config.json")
        with open(self.config_path, "w") as fh:
            json.dump({
                "state_dir": os.path.join(tmp, "state"),
                "backend": "fake",
                "user": "domen",
                "home_subdir": "",
                # the fake backend exercises the pipeline without a real
                # sandbox; "none" runs the command directly (debug mode).
                "confinement": "none",
                "roots": [{
                    "name": "storage_user",
                    "base": self.base,
                    "store": os.path.join(tmp, "stores", "storage_user"),
                    "visible": "/storage/user",
                    "home_subdir": "",
                }],
            }, fh)

    def sessions(self):
        store = SessionStore(os.path.join(self.tmp, "state"))
        return sorted(s.session_id for s in store.list())


class TestLoadConfig(unittest.TestCase):
    def test_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as fh:
                json.dump({"state_dir": "/x", "roots": []}, fh)
            config = load_config(path, env={})
            self.assertEqual(config["state_dir"], "/x")

    def test_env_var_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json")
            with open(path, "w") as fh:
                json.dump({"state_dir": "/y", "roots": []}, fh)
            config = load_config(env={"CCC_AGENT_CONFIG": path})
            self.assertEqual(config["state_dir"], "/y")

    def test_missing_config_exits(self):
        with self.assertRaises(SystemExit):
            load_config(env={})


class TestBuildRuntime(unittest.TestCase):
    def test_branchfs_timeout_seconds_configures_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "state_dir": os.path.join(tmp, "state"),
                "backend": "branchfs",
                "branchfs_bin": "/bin/branchfs-test",
                "branchfs_timeout_seconds": 42,
                "user": "domen",
                "home_subdir": "",
                "roots": [{
                    "name": "storage_user",
                    "base": os.path.join(tmp, "base"),
                    "store": os.path.join(tmp, "store"),
                    "visible": "/storage/user",
                }],
            }

            _store, backend, _alias_map, _user, _roots = build_runtime(config)

            self.assertIsInstance(backend, BranchfsCli)
            self.assertEqual(backend.binary, "/bin/branchfs-test")
            self.assertEqual(backend.timeout_seconds, 42)

    def test_missing_branchfs_bin_uses_packaged_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "state_dir": os.path.join(tmp, "state"),
                "backend": "branchfs",
                "branchfs_timeout_seconds": 42,
                "user": "domen",
                "home_subdir": "",
                "roots": [{
                    "name": "storage_user",
                    "base": os.path.join(tmp, "base"),
                    "store": os.path.join(tmp, "store"),
                    "visible": "/storage/user",
                }],
            }

            with mock.patch("ccc_agent.cli.branchfs_runtime.default_branchfs_bin",
                            return_value="/opt/vicoslab/branchfs"):
                _store, backend, _alias_map, _user, _roots = build_runtime(config)

            self.assertIsInstance(backend, BranchfsCli)
            self.assertEqual(backend.binary, "/opt/vicoslab/branchfs")
            self.assertEqual(backend.timeout_seconds, 42)


class TestShellDefault(unittest.TestCase):
    def test_default_shell_prefers_current_parent_over_login_shell_env(self):
        with mock.patch("ccc_agent.cli._parent_shell_command",
                        return_value=["sh"]):
            self.assertEqual(
                getattr(cli_mod, "_current_shell_command")(
                    {"SHELL": "/bin/zsh"}),
                ["sh"])

    def test_default_shell_falls_back_to_shell_env(self):
        with mock.patch("ccc_agent.cli._parent_shell_command",
                        return_value=None):
            self.assertEqual(
                getattr(cli_mod, "_current_shell_command")(
                    {"SHELL": "/bin/zsh"}),
                ["/bin/zsh"])

    def test_default_shell_falls_back_to_bin_sh(self):
        with mock.patch("ccc_agent.cli._parent_shell_command",
                        return_value=None):
            self.assertEqual(getattr(cli_mod, "_current_shell_command")({}),
                             ["/bin/sh"])

    def test_parent_shell_preserves_sh_cmdline_spelling(self):
        def fake_open(path, mode="rb"):
            self.assertEqual(path, "/proc/123/cmdline")
            self.assertEqual(mode, "rb")
            return io.BytesIO(b"sh\0")

        with mock.patch("ccc_agent.cli.os.getppid", return_value=123):
            with mock.patch("builtins.open", side_effect=fake_open):
                self.assertEqual(getattr(cli_mod, "_parent_shell_command")(),
                                 ["sh"])

    def test_parent_shell_strips_login_shell_prefix(self):
        def fake_open(path, mode="rb"):
            return io.BytesIO(b"-zsh\0")

        with mock.patch("ccc_agent.cli.os.getppid", return_value=123):
            with mock.patch("builtins.open", side_effect=fake_open):
                self.assertEqual(getattr(cli_mod, "_parent_shell_command")(),
                                 ["zsh"])


class TestMainRun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_running_session(self, session_id="agent-resume-cli", command=None,
                             mode="workspace-auto"):
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        root = ProtectedRoot(
            name="storage_user", base=self.h.base,
            store=os.path.join(self.h.tmp, "stores", "storage_user"),
            branch=session_id,
            mount=os.path.join(self.h.tmp, "state", session_id, "mounts",
                               "storage_user"),
            visible="/storage/user", home_subdir="")
        os.makedirs(os.path.join(root.store, "branches", root.branch, "files"),
                    exist_ok=True)
        session = store.create(
            owner="domen", agent_kind="codex",
            agent_command=command or ["sh", "-c", "echo default > default.txt"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": mode,
                    "allowed_scopes": ["/storage/user/Projects/proj-a"]},
            protected_roots={"storage_user": root}, completion="process-exit",
            session_id=session_id)
        session.transition("mounting")
        session.transition("running")
        store.save(session)
        return session

    def test_contained_run_auto_commits(self):
        code = main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--agent", "fake",
            "--", "sh", "-c", "echo out > artifact.txt",
        ], env={})
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        self.assertEqual(len(self.h.sessions()), 1)

    def test_auto_commit_finish_line_summarizes_no_changes(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_run([
                "--config", self.h.config_path,
                "--workspace", "/storage/user/Projects/proj-a",
                "--agent", "fake",
                "--", "true",
            ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        self.assertIn("finished: auto-committed (no changes)", output)

    def test_auto_commit_finish_line_summarizes_workspace_updates(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_run([
                "--config", self.h.config_path,
                "--workspace", "/storage/user/Projects/proj-a",
                "--agent", "fake",
                "--", "sh", "-c", "echo out > artifact.txt",
            ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        self.assertIn("finished: auto-committed (1 update in workspace)",
                      output)

    def test_serve_run_is_quiet_and_default_keeps_out_of_scope(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_run([
                "--config", self.h.config_path,
                "--workspace", "/storage/user/Projects/proj-a",
                "--serve", "codex",
                "--", "sh", "-c",
                "echo workspace > artifact.txt; echo outside > ../../escape.txt",
            ], env={})

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.h.base,
                                                     "escape.txt")))
        sid = self.h.sessions()[0]
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        session = store.load(sid)
        self.assertEqual(session.agent_kind, "codex")
        self.assertEqual(session.state, "pending-review")
        decisions = session.policy.get("turn_path_decisions", {})
        self.assertEqual(decisions.get("/storage/user/Projects/proj-a/artifact.txt"),
                         "committed")
        self.assertEqual(decisions.get("/storage/user/escape.txt"), "kept")

    def test_serve_and_adaptive_lifecycle_are_orthogonal(self):
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["confinement"] = "bwrap"
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)
        seen = []

        def fake_run_session(config, env=None, before_finalize=None):
            seen.append(config)
            return SimpleNamespace(
                session_id="agent-lifecycle", workspace=config.workspace,
                protected_roots={}, state="auto-committed", events=[],
                exit_status=0, agent_kind=config.agent_kind, policy={})

        with mock.patch("ccc_agent.cli.run_session", side_effect=fake_run_session):
            self.assertEqual(main_run([
                "--config", self.h.config_path,
                "--serve", "claude", "--lifecycle", "adaptive",
                "--", "tool", "--lifecycle", "child-value",
            ], env={}), 0)
            self.assertEqual(main_run([
                "--config", self.h.config_path,
                "--serve", "claude", "--", "true",
            ], env={}), 0)

        self.assertEqual(seen[0].lifecycle, "adaptive")
        self.assertEqual(seen[0].agent_command,
                         ["tool", "--lifecycle", "child-value"])
        self.assertEqual(seen[1].lifecycle, "foreground")
        self.assertEqual(seen[1].adaptive_bootstrap_seconds, 2.0)

    def test_serve_run_suppresses_banner_finish_and_review_prompt(self):
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["confinement"] = "bwrap"
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)

        def fake_run_session(config, env=None, before_finalize=None):
            root = config.roots[0].materialize(
                "agent-serve", config.store.state_dir,
                mount_dir=config.store.mount_dir("agent-serve"))
            session = SimpleNamespace(
                session_id="agent-serve",
                workspace="/storage/user/Projects/proj-a",
                protected_roots={"storage_user": root},
                state="pending-review",
                events=[],
                exit_status=0,
                agent_kind=config.agent_kind,
                policy={},
            )
            callback = getattr(config, "on_session_start", None)
            if callback is not None:
                callback(session)
            return session

        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli.run_session", side_effect=fake_run_session):
            with mock.patch("ccc_agent.cli._handle_pending_review_finish",
                            side_effect=AssertionError(
                                "server-mode run must not review/prompt")):
                with contextlib.redirect_stderr(stderr):
                    code = main_run([
                        "--config", self.h.config_path,
                        "--workspace", "/storage/user/Projects/proj-a",
                        "--serve", "claude",
                        "--", "true",
                    ], env={})

        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_workspace_defaults_to_calling_directory_not_config_home(self):
        # System configs generated by ccc-agent setup historically contained a
        # broad default workspace such as /home/domen, which aliases to
        # /storage/user/<container> on CCC.  A bare `ccc-agent run codex` must
        # ignore that broad config value and use the directory where the user
        # invoked ccc-agent instead.
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["workspace"] = "/storage/user"
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)

        with mock.patch("ccc_agent.cli.os.getcwd",
                        return_value="/storage/user/Projects/proj-a"):
            code = main_run([
                "--config", self.h.config_path,
                "--agent", "fake",
                "--", "sh", "-c", "echo out > artifact.txt",
            ], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, "artifact.txt")))

    def test_ignore_patterns_from_config_reach_the_session(self):
        # Regression: cli must carry policy.ignore_patterns from config (an
        # ignored out-of-scope write must not block the in-scope auto-commit).
        cfg = os.path.join(self._tmp.name, "cfg-ignore.json")
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["policy"] = {"mode": "workspace-auto",
                          "allowed_scopes": ["/storage/user/Projects/proj-a"],
                          "ignore_patterns": ["/storage/user/.codex"]}
        with open(cfg, "w") as fh:
            json.dump(data, fh)
        code = main_run([
            "--config", cfg,
            "--workspace", "/storage/user/Projects/proj-a",
            "--agent", "fake",
            "--", "sh", "-c",
            "mkdir -p ../../.codex && echo x > ../../.codex/foo; "
            "echo y > artifact.txt",
        ], env={})
        self.assertEqual(code, 0)
        # in-scope file committed -> auto-commit happened, i.e. the ignored
        # .codex write did NOT flag the session into pending-review
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))

    def test_pending_review_finish_line_shows_changes_without_line_diff(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_run([
                "--config", self.h.config_path,
                "--workspace", "/storage/user/Projects/proj-a",
                "--policy", "manual",
                "--agent", "fake",
                "--", "sh", "-c", "printf 'hello\\n' > review.txt",
            ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        self.assertIn("finished: pending-review (1 change needs review)", output)
        self.assertIn("Pending changes for", output)
        self.assertIn("A /storage/user/Projects/proj-a/review.txt", output)
        self.assertNotIn("Diff:", output)
        self.assertNotIn("--- a/Projects/proj-a/review.txt", output)
        self.assertNotIn("+hello", output)
        self.assertIn("review with: ccc-agent review", output)
        self.assertIn("scripted: ccc-agent commit", output)
        self.assertNotIn("Accept changes?", output)

    def test_pending_review_lists_all_paths_without_file_hunks(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main_run([
                "--config", self.h.config_path,
                "--workspace", "/storage/user/Projects/proj-a",
                "--policy", "manual",
                "--agent", "fake",
                "--", "sh", "-c",
                "printf 'one\\n' > a.txt; "
                "mkdir -p sub; printf 'two\\n' > sub/b.txt",
            ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        self.assertIn("finished: pending-review (2 changes need review)", output)
        changed_section = output[output.index("Changed paths:"):]
        self.assertIn("A /storage/user/Projects/proj-a/a.txt", changed_section)
        self.assertIn("A /storage/user/Projects/proj-a/sub/b.txt", changed_section)
        self.assertNotIn("Diff:", changed_section)
        self.assertNotIn("--- a/Projects/proj-a/a.txt", changed_section)
        self.assertNotIn("--- a/Projects/proj-a/sub/b.txt", changed_section)

    def test_pending_review_quick_yes_commits(self):
        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli._is_interactive_review", return_value=True):
            with mock.patch("builtins.input", return_value="yes"):
                with contextlib.redirect_stderr(stderr):
                    code = main_run([
                        "--config", self.h.config_path,
                        "--workspace", "/storage/user/Projects/proj-a",
                        "--policy", "manual",
                        "--agent", "fake",
                        "--", "sh", "-c", "echo out > artifact.txt",
                    ], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        sid = self.h.sessions()[0]
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        self.assertEqual(store.load(sid).state, "committed")
        self.assertIn("Accept changes?", stderr.getvalue())
        self.assertIn("[c] commit all changes", stderr.getvalue())
        self.assertIn("aliases: y, yes, commit", stderr.getvalue())
        self.assertIn("committed session", stderr.getvalue())

    def test_pending_review_quick_no_discards(self):
        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli._is_interactive_review", return_value=True):
            with mock.patch("builtins.input", return_value="no"):
                with contextlib.redirect_stderr(stderr):
                    code = main_run([
                        "--config", self.h.config_path,
                        "--workspace", "/storage/user/Projects/proj-a",
                        "--policy", "manual",
                        "--agent", "fake",
                        "--", "sh", "-c", "echo out > artifact.txt",
                    ], env={})

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        sid = self.h.sessions()[0]
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        self.assertEqual(store.load(sid).state, "aborted")
        self.assertIn("discarded session", stderr.getvalue())

    def test_pending_review_quick_later_keeps_review(self):
        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli._is_interactive_review", return_value=True):
            with mock.patch("builtins.input", return_value="later"):
                with contextlib.redirect_stderr(stderr):
                    code = main_run([
                        "--config", self.h.config_path,
                        "--workspace", "/storage/user/Projects/proj-a",
                        "--policy", "manual",
                        "--agent", "fake",
                        "--", "sh", "-c", "echo out > artifact.txt",
                    ], env={})

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "artifact.txt")))
        sid = self.h.sessions()[0]
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        self.assertEqual(store.load(sid).state, "pending-review")
        self.assertIn("kept for later review", stderr.getvalue())

    def test_pending_review_escape_keeps_review_for_later(self):
        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli._is_interactive_review", return_value=True):
            with mock.patch("builtins.input", return_value="\x1b"):
                with contextlib.redirect_stderr(stderr):
                    code = main_run([
                        "--config", self.h.config_path,
                        "--workspace", "/storage/user/Projects/proj-a",
                        "--policy", "manual",
                        "--agent", "fake",
                        "--", "sh", "-c", "echo out > artifact.txt",
                    ], env={})

        self.assertEqual(code, 0)
        sid = self.h.sessions()[0]
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        self.assertEqual(store.load(sid).state, "pending-review")
        self.assertIn("[l] keep for later review", stderr.getvalue())
        self.assertIn("aliases: Enter, Esc, later", stderr.getvalue())

    def test_review_choice_reads_single_escape_key_from_tty(self):
        class FakeStdin(object):
            def isatty(self):
                return True

            def fileno(self):
                return 7

            def read(self, size):
                self.size = size
                return "\x1b"

        fake_stdin = FakeStdin()
        with mock.patch("ccc_agent.cli.sys.stdin", fake_stdin):
            with mock.patch("ccc_agent.cli.termios.tcgetattr",
                            return_value=["old"]):
                with mock.patch("ccc_agent.cli.tty.setcbreak") as setcbreak:
                    with mock.patch("ccc_agent.cli.termios.tcsetattr") as restore:
                        choice = getattr(cli_mod, "_read_review_choice")()

        self.assertEqual(choice, "\x1b")
        self.assertEqual(fake_stdin.size, 1)
        setcbreak.assert_called_once_with(7)
        restore.assert_called_once_with(7, termios.TCSADRAIN, ["old"])

    def test_prompt_reclaims_terminal_foreground_group_before_reading(self):
        class FakeStdin(object):
            def fileno(self):
                return 7

        with mock.patch("ccc_agent.cli.sys.stdin", FakeStdin()):
            with mock.patch("ccc_agent.cli.os.getpgrp", return_value=456):
                with mock.patch("ccc_agent.cli.os.tcgetpgrp", return_value=123):
                    with mock.patch("ccc_agent.cli.os.tcsetpgrp") as setpgrp:
                        with mock.patch("ccc_agent.cli.signal.signal",
                                        side_effect=["old-ttou", None]) as sig:
                            ok = getattr(cli_mod,
                                         "_ensure_foreground_for_prompt")()

        self.assertTrue(ok)
        setpgrp.assert_called_once_with(7, 456)
        sig.assert_has_calls([
            mock.call(signal.SIGTTOU, signal.SIG_IGN),
            mock.call(signal.SIGTTOU, "old-ttou"),
        ])

    def test_nested_run_does_not_prompt_for_pending_review(self):
        def fake_run_session(config, env=None):
            return SimpleNamespace(session_id="agent-parent",
                                   state="pending-review", events=[],
                                   exit_status=0)

        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            with mock.patch("ccc_agent.cli._handle_pending_review_finish",
                            side_effect=AssertionError(
                                "nested run must not review/prompt")):
                with contextlib.redirect_stderr(stderr):
                    code = main_run(["--config", self.h.config_path,
                                     "--", "true"],
                                    env={"CCC_AGENT_SESSION": "agent-parent"})

        self.assertEqual(code, 0)
        self.assertNotIn("Accept changes?", stderr.getvalue())

    def test_large_pending_review_text_uses_less_after_reclaiming_foreground(self):
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        stream = TtyBuffer()
        with mock.patch("ccc_agent.cli._terminal_lines", return_value=2):
            with mock.patch("ccc_agent.cli.shutil.which", return_value="less"):
                with mock.patch("ccc_agent.cli._ensure_foreground_for_prompt",
                                return_value=True) as foreground:
                    with mock.patch("ccc_agent.cli.subprocess.run") as run:
                        getattr(cli_mod, "_display_or_page")(
                            "one\ntwo\nthree\n", stream=stream)

        foreground.assert_called_once_with()
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ["less", "-R"])
        self.assertEqual(run.call_args[1]["input"], "one\ntwo\nthree\n")
        self.assertIn("opening change review in less", stream.getvalue())

    def test_pending_review_text_prints_directly_when_foreground_not_reclaimed(self):
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        stream = TtyBuffer()
        with mock.patch("ccc_agent.cli._terminal_lines", return_value=2):
            with mock.patch("ccc_agent.cli.shutil.which", return_value="less"):
                with mock.patch("ccc_agent.cli._ensure_foreground_for_prompt",
                                return_value=False):
                    with mock.patch("ccc_agent.cli.subprocess.run") as run:
                        getattr(cli_mod, "_display_or_page")(
                            "one\ntwo\nthree\n", stream=stream)

        run.assert_not_called()
        self.assertEqual(stream.getvalue(), "one\ntwo\nthree\n")

    def test_pending_review_pager_writes_to_review_stream_not_stdout(self):
        class TtyStream(object):
            def __init__(self):
                self.parts = []

            def isatty(self):
                return True

            def fileno(self):
                return 8

            def write(self, value):
                self.parts.append(value)

            def flush(self):
                pass

        stream = TtyStream()
        with mock.patch("ccc_agent.cli._terminal_lines", return_value=2):
            with mock.patch("ccc_agent.cli.shutil.which", return_value="less"):
                with mock.patch("ccc_agent.cli._ensure_foreground_for_prompt",
                                return_value=True):
                    with mock.patch("ccc_agent.cli.subprocess.run") as run:
                        getattr(cli_mod, "_display_or_page")(
                            "one\ntwo\nthree\n", stream=stream)

        run.assert_called_once()
        self.assertIs(run.call_args[1]["stdout"], stream)

    def test_run_displays_containment_banner_at_session_start(self):
        """A newly-created contained session should be obvious to the user."""
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["confinement"] = "bwrap"
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)

        def fake_run_session(config, env=None):
            root = config.roots[0].materialize(
                "agent-banner", config.store.state_dir,
                mount_dir=config.store.mount_dir("agent-banner"))
            session = SimpleNamespace(
                session_id="agent-banner",
                workspace="/storage/user/Projects/proj-a",
                protected_roots={"storage_user": root},
                state="auto-committed",
                events=[],
                exit_status=0,
            )
            callback = getattr(config, "on_session_start", None)
            if callback is not None:
                callback(session)
            return session

        stderr = io.StringIO()
        with mock.patch("ccc_agent.cli.run_session", side_effect=fake_run_session):
            with contextlib.redirect_stderr(stderr):
                code = main_run([
                    "--config", self.h.config_path,
                    "--workspace", "/storage/user/Projects/proj-a",
                    "--agent", "fake",
                    "--", "true",
                ], env={})

        self.assertEqual(code, 0)
        output = stderr.getvalue()
        mount = os.path.join(self._tmp.name, "state", "agent-banner",
                             "mounts", "storage_user")
        self.assertIn("dropped into new contained BranchFS environment", output)
        self.assertIn("session: agent-banner", output)
        self.assertIn("serving visible /storage/user from BranchFS view %s"
                      % mount, output)
        self.assertNotIn("visible workspace:", output)
        self.assertNotIn("backend data path:", output)

    def test_agent_exit_code_propagates(self):
        code = main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "exit 7",
        ], env={})
        self.assertEqual(code, 7)

    def test_run_without_command_defaults_to_current_shell(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-shell", state="auto-committed",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            with mock.patch("ccc_agent.cli._parent_shell_command",
                            return_value=["/bin/zsh"]):
                code = main_run(["--config", self.h.config_path, "--"],
                                env={"SHELL": "/bin/bash"})

        self.assertEqual(code, 0)
        self.assertEqual(seen["config"].agent_command, ["/bin/zsh"])

    def test_resume_uses_stored_command_by_default(self):
        session = self.make_running_session(
            session_id="agent-resume-default",
            command=["sh", "-c", "echo default > default.txt"])

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["resume", "--config", self.h.config_path,
                         session.session_id], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "default.txt")))
        self.assertIn("resumed", stderr.getvalue())
        self.assertIn(session.session_id, stderr.getvalue())

    def test_resume_accepts_custom_command_after_separator(self):
        original = ["sh", "-c", "echo original > original.txt"]
        session = self.make_running_session(
            session_id="agent-resume-custom-cli", command=original)

        code = main(["resume", "--config", self.h.config_path,
                     session.session_id, "--", "sh", "-c",
                     "echo custom > custom.txt"], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "custom.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "original.txt")))
        persisted = SessionStore(os.path.join(self.h.tmp, "state")).load(
            session.session_id)
        self.assertEqual(persisted.agent_command, original)

    def test_resume_separator_command_can_pass_its_own_cmd_option(self):
        session = self.make_running_session(
            session_id="agent-resume-cmd-passthrough-cli",
            command=["sh", "-c", "echo original > original.txt"])

        code = main(["resume", "--config", self.h.config_path,
                     session.session_id, "--", "sh", "-c",
                     "printf '%s' \"$1\" > passthrough.txt", "sh",
                     "--cmd"], env={})

        self.assertEqual(code, 0)
        with open(os.path.join(self.h.base, self.h.workspace_rel,
                               "passthrough.txt")) as fh:
            self.assertEqual(fh.read(), "--cmd")

    def test_resume_cmd_option_overrides_stored_command_once(self):
        original = ["sh", "-c", "echo original > original.txt"]
        session = self.make_running_session(
            session_id="agent-resume-cmd-option-cli", command=original)

        code = main(["resume", "--config", self.h.config_path,
                     session.session_id, "--cmd",
                     "sh -c 'echo cmdflag > cmdflag.txt'"], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "cmdflag.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "original.txt")))
        persisted = SessionStore(os.path.join(self.h.tmp, "state")).load(
            session.session_id)
        self.assertEqual(persisted.agent_command, original)

    def test_resume_failed_session_requires_allow_failed_flag(self):
        session = self.make_running_session(
            session_id="agent-resume-failed-cli",
            command=["sh", "-c", "echo recovered > recovered.txt"])
        session.transition("failed")
        SessionStore(os.path.join(self.h.tmp, "state")).save(session)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["resume", "--config", self.h.config_path,
                         session.session_id], env={})

        self.assertEqual(code, 1)
        self.assertIn("--allow-failed", stderr.getvalue())
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "recovered.txt")))

    def test_resume_allow_failed_flag_retries_failed_session(self):
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        session = self.make_running_session(
            session_id="agent-resume-allowed-failed-cli",
            command=["sh", "-c", "echo recovered > recovered.txt"])
        session.transition("failed")
        session.finished_at = "2000-01-01T00:00:00Z"
        store.save(session)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["resume", "--config", self.h.config_path,
                         "--allow-failed", session.session_id], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "recovered.txt")))
        persisted = store.load(session.session_id)
        self.assertEqual(persisted.state, "auto-committed")
        self.assertNotEqual(persisted.finished_at, "2000-01-01T00:00:00Z")
        self.assertIn("resumed", stderr.getvalue())

    def test_resume_pending_review_session_runs_without_extra_flag(self):
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        session = self.make_running_session(
            session_id="agent-resume-pending-cli",
            command=["sh", "-c", "echo pending > pending.txt"])
        session.transition("finalizing")
        session.transition("frozen")
        session.transition("pending-review")
        store.save(session)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["resume", "--config", self.h.config_path,
                         session.session_id], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "pending.txt")))
        self.assertIn("resumed", stderr.getvalue())

    def test_resume_aborted_session_runs_without_extra_flag(self):
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        session = self.make_running_session(
            session_id="agent-resume-aborted-cli",
            command=["sh", "-c", "echo aborted > aborted.txt"])
        session.transition("aborted")
        session.finished_at = "2000-01-01T00:00:00Z"
        store.save(session)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["resume", "--config", self.h.config_path,
                         session.session_id], env={})

        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "aborted.txt")))
        persisted = store.load(session.session_id)
        self.assertEqual(persisted.state, "auto-committed")
        self.assertNotEqual(persisted.finished_at, "2000-01-01T00:00:00Z")
        self.assertIn("resumed", stderr.getvalue())

    def test_agent_option_sets_explicit_agent_kind(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(
                session_id="agent-test",
                state="aborted",
                events=[],
                exit_status=0,
            )

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run([
                "--config", self.h.config_path,
                "--agent", "codex",
                "/opt/agents/claude", "-p", "hello",
            ], env={})

        self.assertEqual(code, 0)
        self.assertEqual(seen["config"].agent_kind, "codex")
        self.assertEqual(seen["config"].agent_command,
                         ["/opt/agents/claude", "-p", "hello"])

    def test_agent_state_binds_from_config_reach_runner(self):
        seen = {}
        binds = ["/real/codex:/home/domen/.codex",
                 "/real/claude:/home/domen/.claude",
                 "/real/hermes:/home/domen/.hermes"]
        with open(self.h.config_path) as fh:
            data = json.load(fh)
        data["agent_state_binds"] = binds
        data["ensure_agent_state_dirs"] = True
        with open(self.h.config_path, "w") as fh:
            json.dump(data, fh)

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-test", state="aborted",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run(["--config", self.h.config_path, "--", "true"],
                            env={})

        self.assertEqual(code, 0)
        self.assertEqual(seen["config"].agent_state_binds, binds)
        self.assertTrue(seen["config"].ensure_agent_state_dirs)
        self.assertFalse(seen["config"].protect_agent_state)

    def test_protect_agent_state_flag_overrides_shared_default(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-test", state="aborted",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run(["--config", self.h.config_path,
                             "--protect-agent-state", "--", "true"], env={})

        self.assertEqual(code, 0)
        self.assertTrue(seen["config"].protect_agent_state)

    def test_container_run_access_defaults_on(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-test", state="aborted",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run(["--config", self.h.config_path, "--", "true"],
                            env={})

        self.assertEqual(code, 0)
        self.assertTrue(seen["config"].container_run_access)

    def test_full_isolation_flag_disables_container_run_access(self):
        seen = {}

        def fake_run_session(config, env=None):
            seen["config"] = config
            return SimpleNamespace(session_id="agent-test", state="aborted",
                                   events=[], exit_status=0)

        with mock.patch("ccc_agent.cli.run_session",
                        side_effect=fake_run_session):
            code = main_run(["--config", self.h.config_path,
                             "--full-isolation", "--", "true"], env={})

        self.assertEqual(code, 0)
        self.assertFalse(seen["config"].container_run_access)

    def test_shortcut_agent_flags_are_not_supported(self):
        for flag in ("--codex", "--claude", "--hermes"):
            with self.subTest(flag=flag):
                with mock.patch("ccc_agent.cli.run_session",
                                side_effect=AssertionError(
                                    "%s should not parse" % flag)):
                    with self.assertRaises(SystemExit) as cm:
                        main_run(["--config", self.h.config_path,
                                  flag, "codex"], env={})
                self.assertEqual(cm.exception.code, 2)


class TestMainCtl(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def store(self):
        return SessionStore(os.path.join(self.h.tmp, "state"))

    def make_pending_session(self, relpath):
        before = set(self.h.sessions())
        self.assertEqual(main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--policy", "manual",
            "--", "sh", "-c", "printf 'x\\n' > %s" % relpath,
        ], env={}), 0)
        created = sorted(set(self.h.sessions()) - before)
        self.assertEqual(len(created), 1)
        return created[0]

    def make_pending_cache_session(self):
        before = set(self.h.sessions())
        self.assertEqual(main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--policy", "manual",
            "--", "sh", "-c",
            "printf 'keep\\n' > result.txt; mkdir -p ../../.cache/pip; "
            "printf 'wheel\\n' > ../../.cache/pip/wheel.txt",
        ], env={}), 0)
        created = sorted(set(self.h.sessions()) - before)
        self.assertEqual(len(created), 1)
        return created[0]

    def make_running_session(self, session_id, dirty_rel=None):
        store = self.store()
        branch_store = os.path.join(self.h.tmp, "stores", "storage_user")
        files = os.path.join(branch_store, "branches", session_id, "files")
        os.makedirs(files, exist_ok=True)
        root = ProtectedRoot(
            name="storage_user", base=self.h.base, store=branch_store,
            branch=session_id, mount=os.path.join(self.h.tmp, "mounts", session_id),
            visible="/storage/user", home_subdir="")
        session = store.create(
            owner="domen", agent_kind="codex", agent_command=["codex"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "manual",
                    "allowed_scopes": ["/storage/user/Projects/proj-a"]},
            protected_roots={"storage_user": root}, completion="manual",
            session_id=session_id)
        session.transition("mounting")
        session.transition("running")
        store.save(session)
        if dirty_rel:
            path = os.path.join(files, dirty_rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("x\n")
        return session_id

    def make_auto_committed_session(self, relpath="closed.txt"):
        before = set(self.h.sessions())
        self.assertEqual(main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "printf 'x\\n' > %s" % relpath,
        ], env={}), 0)
        created = sorted(set(self.h.sessions()) - before)
        self.assertEqual(len(created), 1)
        return created[0]

    def set_session_time(self, session_id, timestamp):
        store = self.store()
        session = store.load(session_id)
        session.created_at = timestamp
        session.finished_at = timestamp
        store.save(session)

    def make_session_in_state(self, session_id, state):
        store = self.store()
        root = ProtectedRoot(
            name="storage", base=self.h.base,
            store=os.path.join(self.h.tmp, "stores", "storage"),
            branch=session_id,
            mount=os.path.join(self.h.tmp, "state", session_id, "mounts",
                               "storage"),
            visible="/storage/user", home_subdir="")
        session = store.create(
            owner="domen", agent_kind="codex", agent_command=["codex"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "manual"},
            protected_roots={"storage": root}, session_id=session_id)
        paths = {
            "created": (),
            "mounting": ("mounting",),
            "running": ("mounting", "running"),
            "finalizing": ("mounting", "running", "finalizing"),
            "frozen": ("mounting", "running", "finalizing", "frozen"),
            "auto-committed": ("mounting", "running", "finalizing",
                               "frozen", "auto-committed"),
            "pending-review": ("mounting", "running", "finalizing",
                               "frozen", "pending-review"),
            "committed": ("mounting", "running", "finalizing", "frozen",
                          "committed"),
            "aborted": ("aborted",),
            "failed": ("mounting", "running", "failed"),
        }
        for next_state in paths[state]:
            session.transition(next_state)
        store.save(session)
        return session_id

    def test_list_and_show_roundtrip(self):
        main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "echo x > f.txt",
        ], env={})
        self.assertEqual(main_ctl(["--config", self.h.config_path, "list"],
                                  env={}), 0)
        sid = self.h.sessions()[0]
        self.assertEqual(main_ctl(["--config", self.h.config_path, "show",
                                   sid], env={}), 0)

    def test_ls_alias_lists_sessions(self):
        main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "echo x > f.txt",
        ], env={})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "ls"], env={})

        self.assertEqual(code, 0)
        self.assertIn(self.h.sessions()[0], out.getvalue())

    def test_control_help_describes_subcommands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                main_ctl(["--help"], env={})

        self.assertEqual(cm.exception.code, 0)
        text = out.getvalue()
        self.assertIn("list session records", text)
        self.assertIn("ls", text)
        self.assertIn("finalize the current turn", text)
        self.assertIn("inside-session plugin op: answer a pending turn", text)
        self.assertIn("resolve remembered", text)
        self.assertIn("kept/discarded turn paths", text)
        self.assertIn("turn-kept-status", text)
        self.assertIn("show remembered kept", text)
        self.assertIn("turn-review-kept", text)
        self.assertIn("ask about remembered kept", text)
        self.assertIn("approval token", text)

    def test_turn_finalize_cli_can_default_keep_for_nonblocking_loops(self):
        seen = {}

        class FakeControlClient(object):
            def __init__(self, sock, token):
                seen["init"] = (sock, token)

            def finalize_turn(self, default_keep=False):
                seen["default_keep"] = default_keep
                return {"verdict": "committed", "committed": ["/workspace/ok"],
                        "kept": ["/storage/user/outside.txt"]}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        out = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stdout(out):
                code = main_ctl(["turn-finalize", "--default-keep"], env=env)

        self.assertEqual(code, 0)
        self.assertTrue(seen["default_keep"])
        self.assertIn("committed (1), kept local (1)", out.getvalue())
        self.assertNotIn("outside.txt", out.getvalue())

    def test_turn_workspace_cli_relays_hook_session_add_remove_commands(self):
        seen = []

        class FakeControlClient(object):
            def __init__(self, sock, token, hook_token=None):
                seen.append(("init", sock, token, hook_token))

            def add_workspace(self, path, hook_session):
                seen.append(("add", path, hook_session))
                return {"verdict": "workspace-updated", "action": "add",
                        "workspace": path, "workspaces": [path],
                        "added": True, "owned": True}

            def remove_workspace(self, path, hook_session):
                seen.append(("remove", path, hook_session))
                return {"verdict": "workspace-updated", "action": "remove",
                        "workspace": path, "workspaces": [],
                        "removed": True}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok",
               cli_mod.ENV_HOOK_TOKEN: "hook-tok",
               cli_mod.ENV_HOOK_SESSION: "hook-session-1"}
        out = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with mock.patch("ccc_agent.cli.os.getcwd",
                            return_value="/storage/user/Projects/proj-c"):
                with contextlib.redirect_stdout(out):
                    code1 = main_ctl(["turn-add-workspace"], env=env)
                    code2 = main_ctl(["turn-remove-workspace",
                                      "/storage/user/Projects/proj-c"], env=env)

        self.assertEqual((code1, code2), (0, 0))
        self.assertIn(("init", "/tmp/ccc.sock", "tok", "hook-tok"), seen)
        self.assertIn(("add", "/storage/user/Projects/proj-c",
                       "hook-session-1"), seen)
        self.assertIn(("remove", "/storage/user/Projects/proj-c",
                       "hook-session-1"), seen)
        self.assertIn("workspace added", out.getvalue())
        self.assertIn("workspace removed", out.getvalue())

    def test_turn_workspace_cli_rejects_missing_hook_token(self):
        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok",
               cli_mod.ENV_HOOK_SESSION: "hook-session-1"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main_ctl(["turn-add-workspace", "/storage/user/Projects/proj-c"],
                            env=env)

        self.assertEqual(code, 1)
        self.assertIn("hook-only", err.getvalue())

    def test_turn_finalize_cli_prompts_then_keeps_after_timeout(self):
        seen = {}

        class FakeControlClient(object):
            def __init__(self, sock, token):
                seen["init"] = (sock, token)

            def finalize_turn(self, default_keep=False):
                seen["default_keep"] = default_keep
                return {"verdict": "needs-approval",
                        "approval_token": "appr-1",
                        "out_of_scope": ["/storage/user/outside.txt"],
                        "committed": ["/storage/user/Projects/proj-a/ok.txt"]}

            def approve_turn(self, approval_token, decision, paths=None,
                             commit_paths=None, keep_paths=None,
                             discard_paths=None):
                seen["approve"] = (approval_token, decision)
                return {"verdict": "committed",
                        "committed": [],
                        "kept": ["/storage/user/outside.txt"]}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with mock.patch("ccc_agent.cli.time.sleep") as sleep:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main_ctl(["turn-finalize", "--default-keep-after", "0"],
                                    env=env)

        self.assertEqual(code, 0)
        self.assertFalse(seen["default_keep"])
        self.assertEqual(seen["approve"], ("appr-1", "keep"))
        sleep.assert_not_called()
        self.assertIn("ask the user", err.getvalue())
        self.assertIn("outside.txt", err.getvalue())
        self.assertIn("kept 1 in branch", out.getvalue())
        self.assertNotIn("resolve later with", out.getvalue())

    def test_turn_approve_cli_accepts_granular_file_decisions(self):
        seen = {}

        class FakeControlClient(object):
            def __init__(self, sock, token):
                seen["init"] = (sock, token)

            def approve_turn(self, approval_token, decision, paths=None,
                             commit_paths=None, keep_paths=None,
                             discard_paths=None):
                seen["approve"] = {
                    "approval_token": approval_token,
                    "decision": decision,
                    "paths": paths,
                    "commit_paths": commit_paths,
                    "keep_paths": keep_paths,
                    "discard_paths": discard_paths,
                }
                return {"verdict": "committed", "committed": ["/a"],
                        "kept": ["/b"], "discarded": ["/c"]}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        out = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stdout(out):
                code = main_ctl([
                    "turn-approve", "appr-1", "select",
                    "--commit", "/a", "--keep", "/b", "--discard", "/c",
                ], env=env)

        self.assertEqual(code, 0)
        self.assertEqual(seen["init"], ("/tmp/ccc.sock", "tok"))
        self.assertEqual(seen["approve"], {
            "approval_token": "appr-1",
            "decision": "select",
            "paths": None,
            "commit_paths": ["/a"],
            "keep_paths": ["/b"],
            "discard_paths": ["/c"],
        })
        self.assertIn("committed 1 change(s)", out.getvalue())
        self.assertIn("kept 1", out.getvalue())
        self.assertIn("discarded 1 path(s) from BranchFS", out.getvalue())

    def test_turn_resolve_cli_sends_later_kept_path_decision(self):
        seen = {}

        class FakeControlClient(object):
            def __init__(self, sock, token):
                seen["init"] = (sock, token)

            def resolve_turn(self, decision, paths):
                seen["resolve"] = (decision, paths)
                return {"verdict": "discarded", "discarded": ["/b"]}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        out = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stdout(out):
                code = main_ctl(["turn-resolve", "discard", "--paths", "/b"],
                                env=env)

        self.assertEqual(code, 0)
        self.assertEqual(seen["resolve"], ("discard", ["/b"]))
        self.assertIn("discarded 1 path(s) from BranchFS", out.getvalue())
        self.assertNotIn("/b", out.getvalue())

    def test_turn_resolve_cli_can_apply_to_all_kept_paths_compactly(self):
        seen = {}

        class FakeControlClient(object):
            def __init__(self, sock, token):
                seen["init"] = (sock, token)

            def kept_status(self):
                seen["status"] = True
                return {"verdict": "kept-status",
                        "kept": ["/storage/user/a", "/storage/user/b"]}

            def resolve_turn(self, decision, paths):
                seen["resolve"] = (decision, paths)
                return {"verdict": "held", "kept": list(paths)}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        out = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stdout(out):
                code = main_ctl(["turn-resolve", "keep", "--all-kept"],
                                env=env)

        self.assertEqual(code, 0)
        self.assertTrue(seen["status"])
        self.assertEqual(seen["resolve"],
                         ("keep", ["/storage/user/a", "/storage/user/b"]))
        self.assertIn("kept 2 path(s)", out.getvalue())
        self.assertNotIn("/storage/user/a", out.getvalue())

    def test_turn_kept_status_cli_is_compact_by_default(self):
        class FakeControlClient(object):
            def __init__(self, sock, token):
                pass

            def kept_status(self):
                return {"verdict": "kept-status",
                        "committed": ["/storage/user/Projects/proj-a/ok.txt"],
                        "kept": ["/storage/user/outside.txt"],
                        "stale": [], "count": 1}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        out = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stdout(out):
                code = main_ctl(["turn-kept-status"], env=env)

        self.assertEqual(code, 0)
        self.assertIn("committed=1 kept=1 stale=0", out.getvalue())
        self.assertIn("turn-resolve <commit|discard|keep> --all-kept",
                      out.getvalue())
        self.assertIn("turn-kept-status --details", out.getvalue())
        self.assertNotIn("outside.txt", out.getvalue())
        self.assertNotIn("ok.txt", out.getvalue())

    def test_turn_kept_status_details_lists_remembered_kept_paths(self):
        class FakeControlClient(object):
            def __init__(self, sock, token):
                pass

            def kept_status(self):
                return {"verdict": "kept-status",
                        "committed": ["/storage/user/Projects/proj-a/ok.txt"],
                        "kept": ["/storage/user/outside.txt"],
                        "stale": [], "count": 1}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        out = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stdout(out):
                code = main_ctl(["turn-kept-status", "--details"], env=env)

        self.assertEqual(code, 0)
        self.assertIn("committed this live BranchFS session", out.getvalue())
        self.assertIn("ok.txt", out.getvalue())
        self.assertIn("kept non-workspace paths", out.getvalue())
        self.assertIn("outside.txt", out.getvalue())
        self.assertIn("turn-resolve commit --paths", out.getvalue())

    def test_turn_review_kept_cli_exits_two_when_user_decision_needed(self):
        class FakeControlClient(object):
            def __init__(self, sock, token):
                pass

            def review_kept(self):
                return {"verdict": "needs-kept-review",
                        "kept": ["/storage/user/outside.txt"],
                        "stale": [], "message": "ask user"}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        err = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stderr(err):
                code = main_ctl(["turn-review-kept"], env=env)

        self.assertEqual(code, 2)
        self.assertIn("1 kept non-workspace path(s) pending", err.getvalue())
        self.assertIn("ask user: commit, discard, or keep pending", err.getvalue())
        self.assertIn("turn-resolve <commit|discard|keep> --all-kept",
                      err.getvalue())
        self.assertIn("turn-kept-status --details", err.getvalue())
        self.assertNotIn("outside.txt", err.getvalue())

    def test_turn_review_kept_details_lists_exact_paths(self):
        class FakeControlClient(object):
            def __init__(self, sock, token):
                pass

            def review_kept(self):
                return {"verdict": "needs-kept-review",
                        "kept": ["/storage/user/outside.txt"],
                        "stale": [], "message": "ask user"}

        env = {cli_mod.ENV_CONTROL_SOCK: "/tmp/ccc.sock",
               cli_mod.ENV_CONTROL_TOKEN: "tok"}
        err = io.StringIO()
        with mock.patch("ccc_agent.cli.ControlClient", FakeControlClient):
            with contextlib.redirect_stderr(err):
                code = main_ctl(["turn-review-kept", "--details"], env=env)

        self.assertEqual(code, 2)
        self.assertIn("outside.txt", err.getvalue())
        self.assertIn("turn-resolve commit --paths", err.getvalue())

    def test_diff_accepts_optional_path(self):
        base_path = os.path.join(self.h.base, self.h.workspace_rel, "cli.txt")
        with open(base_path, "w") as fh:
            fh.write("old\n")
        self.assertEqual(main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--policy", "manual",
            "--", "sh", "-c", "printf 'old\\nnew\\n' > cli.txt",
        ], env={}), 0)
        sid = self.h.sessions()[0]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "diff", sid,
                             "cli.txt"], env={})

        self.assertEqual(code, 0)
        self.assertIn("--- a/Projects/proj-a/cli.txt", out.getvalue())
        self.assertIn("+new", out.getvalue())

    def test_diff_show_ignored_cli_flag_lists_ignored_policy_changes(self):
        sid = self.make_pending_cache_session()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "diff",
                             "--show-ignored", sid], env={})

        self.assertEqual(code, 0)
        self.assertIn("/storage/user/Projects/proj-a/result.txt", out.getvalue())
        self.assertIn("/storage/user/.cache/pip/wheel.txt", out.getvalue())

    def test_diff_show_file_diffs_cli_flag_adds_text_file_hunks(self):
        base_path = os.path.join(self.h.base, self.h.workspace_rel, "cli-diff.txt")
        with open(base_path, "w") as fh:
            fh.write("old\n")
        self.assertEqual(main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--policy", "manual",
            "--", "sh", "-c", "printf 'old\\nnew\\n' > cli-diff.txt",
        ], env={}), 0)
        sid = self.h.sessions()[0]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "diff",
                             "--show-file-diffs", sid], env={})

        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("M /storage/user/Projects/proj-a/cli-diff.txt", text)
        self.assertIn("Text file diffs:", text)
        self.assertIn("--- a/Projects/proj-a/cli-diff.txt", text)
        self.assertIn("+new", text)

    def test_review_accept_include_ignored_cli_flag_commits_ignored_changes(self):
        sid = self.make_pending_cache_session()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main_ctl(["--config", self.h.config_path, "review", sid,
                             "--accept", "--include-ignored"], env={})

        self.assertEqual(code, 0)
        self.assertIn("now committed", err.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "result.txt")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, ".cache", "pip", "wheel.txt")))

    def test_review_interactive_yes_commits_after_displaying_changes(self):
        sid = self.make_pending_session("review-yes.txt")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("ccc_agent.cli._is_interactive_review", return_value=True):
            with mock.patch("builtins.input", return_value="yes"):
                with contextlib.redirect_stdout(out):
                    with contextlib.redirect_stderr(err):
                        code = main_ctl(["--config", self.h.config_path,
                                         "review", sid], env={})

        self.assertEqual(code, 0)
        self.assertIn("A /storage/user/Projects/proj-a/review-yes.txt",
                      out.getvalue())
        self.assertIn("Accept changes?", err.getvalue())
        self.assertIn("committed session", err.getvalue())
        self.assertEqual(self.store().load(sid).state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "review-yes.txt")))

    def test_review_interactive_selective_commits_only_selected_paths(self):
        before = set(self.h.sessions())
        self.assertEqual(main_run([
            "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--policy", "manual",
            "--", "sh", "-c",
            "printf 'keep\\n' > keep.txt; printf 'drop\\n' > drop.txt",
        ], env={}), 0)
        created = sorted(set(self.h.sessions()) - before)
        self.assertEqual(len(created), 1)
        sid = created[0]

        err = io.StringIO()
        with mock.patch("ccc_agent.cli._is_interactive_review", return_value=True):
            with mock.patch("builtins.input", return_value="select"):
                with mock.patch(
                    "ccc_agent.cli._select_review_paths_interactive",
                    return_value=["/storage/user/Projects/proj-a/keep.txt"]):
                    with contextlib.redirect_stderr(err):
                        code = main_ctl(["--config", self.h.config_path,
                                         "review", sid], env={})

        self.assertEqual(code, 0)
        self.assertIn("selective accept", err.getvalue())
        self.assertEqual(self.store().load(sid).state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "keep.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "drop.txt")))

    def test_abort_accepts_multiple_session_ids_and_reports_each_ok(self):
        sid1 = self.make_pending_session("abort-one.txt")
        sid2 = self.make_pending_session("abort-two.txt")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main_ctl(["--config", self.h.config_path, "abort",
                             sid1, sid2], env={})

        self.assertEqual(code, 0)
        self.assertIn("%s: ok\n" % sid1, err.getvalue())
        self.assertIn("%s: ok\n" % sid2, err.getvalue())
        self.assertEqual(self.store().load(sid1).state, "aborted")
        self.assertEqual(self.store().load(sid2).state, "aborted")
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "abort-one.txt")))
        self.assertFalse(os.path.exists(os.path.join(
            self.h.base, self.h.workspace_rel, "abort-two.txt")))

    def test_commit_accepts_multiple_session_ids_and_reports_each_ok(self):
        sid1 = self.make_pending_session("commit-one.txt")
        sid2 = self.make_pending_session("commit-two.txt")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main_ctl(["--config", self.h.config_path, "commit",
                             sid1, sid2], env={})

        self.assertEqual(code, 0)
        self.assertIn("%s: ok\n" % sid1, err.getvalue())
        self.assertIn("%s: ok\n" % sid2, err.getvalue())
        self.assertEqual(self.store().load(sid1).state, "committed")
        self.assertEqual(self.store().load(sid2).state, "committed")
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "commit-one.txt")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "commit-two.txt")))

    def test_batch_session_op_continues_after_error(self):
        sid = self.make_pending_session("abort-after-error.txt")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main_ctl(["--config", self.h.config_path, "abort",
                             "agent-missing", sid], env={})

        self.assertEqual(code, 1)
        self.assertIn("agent-missing: error: no such session", err.getvalue())
        self.assertIn("%s: ok\n" % sid, err.getvalue())
        self.assertEqual(self.store().load(sid).state, "aborted")

    def test_thaw_finish_and_finish_turn_accept_multiple_session_ids(self):
        thaw1 = self.make_pending_session("thaw-one.txt")
        thaw2 = self.make_pending_session("thaw-two.txt")
        run1 = self.make_running_session("agent-finish-one", "finish-one.txt")
        run2 = self.make_running_session("agent-finish-two", "finish-two.txt")
        turn1 = self.make_pending_session("turn-one.txt")
        turn2 = self.make_pending_session("turn-two.txt")

        for op, ids in (("thaw", (thaw1, thaw2)),
                        ("finish", (run1, run2)),
                        ("turn-record", (turn1, turn2))):
            with self.subTest(op=op):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = main_ctl(["--config", self.h.config_path, op]
                                    + list(ids), env={})
                self.assertEqual(code, 0)
                for sid in ids:
                    self.assertIn("%s: ok" % sid, err.getvalue())

        self.assertEqual(self.store().load(thaw1).state, "running")
        self.assertEqual(self.store().load(thaw2).state, "running")
        self.assertEqual(self.store().load(run1).state, "pending-review")
        self.assertEqual(self.store().load(run2).state, "pending-review")
        for sid in (turn1, turn2):
            self.assertTrue(any(e["event"] == "turn-finished"
                                for e in self.store().load(sid).events))

    def test_cleanup_removes_only_old_closed_session_bundles(self):
        old_closed = self.make_auto_committed_session("old-closed.txt")
        old_pending = self.make_pending_session("old-pending.txt")
        old_failed = self.make_session_in_state("agent-old-failed", "failed")
        recent_closed = self.make_auto_committed_session("recent-closed.txt")
        self.set_session_time(old_closed, "2000-01-01T00:00:00Z")
        self.set_session_time(old_pending, "2000-01-01T00:00:00Z")
        self.set_session_time(old_failed, "2000-01-01T00:00:00Z")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "cleanup",
                             "--older-than", "7"], env={})

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("%s: removed" % old_closed, text)
        self.assertIn("removed 1 old session", text)
        self.assertNotIn(old_pending, text)
        self.assertNotIn(old_failed, text)
        with self.assertRaises(KeyError):
            self.store().load(old_closed)
        self.assertFalse(os.path.exists(self.store().bundle_dir(old_closed)))
        self.assertEqual(self.store().load(old_pending).state, "pending-review")
        self.assertEqual(self.store().load(old_failed).state, "failed")
        self.assertEqual(self.store().load(recent_closed).state,
                         "auto-committed")

    def test_cleanup_all_type_includes_failed_and_pending_sessions(self):
        old_failed = self.make_session_in_state("agent-alltype-failed", "failed")
        old_pending = self.make_session_in_state("agent-alltype-pending",
                                                 "pending-review")
        for sid in (old_failed, old_pending):
            self.set_session_time(sid, "2000-01-01T00:00:00Z")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "cleanup",
                             "--all-type", "--older-than", "7"], env={})

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("%s: removed" % old_failed, text)
        self.assertIn("%s: removed" % old_pending, text)
        self.assertIn("removed 2 old session(s)", text)
        for sid in (old_failed, old_pending):
            with self.assertRaises(KeyError):
                self.store().load(sid)

    def test_cleanup_short_flags_select_all_types_and_age(self):
        old_failed = self.make_session_in_state("agent-short-old-failed",
                                                "failed")
        recent_failed = self.make_session_in_state("agent-short-recent-failed",
                                                   "failed")
        self.set_session_time(old_failed, "2000-01-01T00:00:00Z")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "cleanup",
                             "-a", "-o", "20"], env={})

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("%s: removed" % old_failed, text)
        self.assertNotIn(recent_failed, text)
        with self.assertRaises(KeyError):
            self.store().load(old_failed)
        self.assertEqual(self.store().load(recent_failed).state, "failed")

    def test_cleanup_dry_run_keeps_matching_sessions(self):
        old_closed = self.make_auto_committed_session("dry-run-closed.txt")
        self.set_session_time(old_closed, "2000-01-01T00:00:00Z")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path, "cleanup",
                             "--older-than", "7", "--dry-run"], env={})

        self.assertEqual(code, 0)
        self.assertIn("%s: would remove" % old_closed, out.getvalue())
        self.assertEqual(self.store().load(old_closed).state, "auto-committed")

    def test_cleanup_detects_active_mounts_from_mountinfo(self):
        sid = "agent-cleanup-active-mount"
        store = self.store()
        mount = os.path.join(self.h.tmp, "state", sid, "mounts", "storage")
        os.makedirs(mount, exist_ok=True)
        root = ProtectedRoot(
            name="storage", base=self.h.base,
            store=os.path.join(self.h.tmp, "stores", "storage"),
            branch=sid, mount=mount, visible="/storage/user",
            home_subdir="")
        session = store.create(
            owner="domen", agent_kind="codex", agent_command=["codex"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "manual"},
            protected_roots={"storage": root}, session_id=sid)
        for state in ("mounting", "running", "finalizing", "frozen",
                      "auto-committed"):
            session.transition(state)
        store.save(session)
        self.set_session_time(sid, "2000-01-01T00:00:00Z")
        mountinfo = os.path.join(self.h.tmp, "mountinfo")
        escaped_mount = mount.replace(" ", "\\040")
        with open(mountinfo, "w") as fh:
            fh.write("123 1 0:42 / %s rw,relatime - "
                     "fuse.branchfs branchfs rw\n" % escaped_mount)

        out = io.StringIO()
        controller = Controller(store, SimpleNamespace(_mountinfo_path=mountinfo),
                                None)
        matched = controller.cleanup(older_than_days=7, out=out)

        self.assertEqual(matched, [])
        self.assertIn("%s: skipped (active mount: %s)" % (sid, mount),
                      out.getvalue())
        self.assertTrue(os.path.isdir(store.bundle_dir(sid)))
        self.assertEqual(store.load(sid).state, "auto-committed")

    def test_cleanup_continues_after_remove_error_and_reports_failures(self):
        store = self.store()

        def make_closed(session_id):
            root = ProtectedRoot(
                name="storage", base=self.h.base,
                store=os.path.join(self.h.tmp, "stores", "storage"),
                branch=session_id,
                mount=os.path.join(self.h.tmp, "state", session_id, "mounts",
                                   "storage"),
                visible="/storage/user", home_subdir="")
            session = store.create(
                owner="domen", agent_kind="codex", agent_command=["codex"],
                workspace="/storage/user/Projects/proj-a",
                policy={"mode": "manual"},
                protected_roots={"storage": root}, session_id=session_id)
            for state in ("mounting", "running", "finalizing", "frozen",
                          "auto-committed"):
                session.transition(state)
            store.save(session)
            self.set_session_time(session_id, "2000-01-01T00:00:00Z")

        first = "agent-cleanup-a"
        failing = "agent-cleanup-b"
        last = "agent-cleanup-c"
        for sid in (first, failing, last):
            make_closed(sid)

        original_remove = SessionStore.remove

        def flaky_remove(store_obj, session_id):
            if session_id == failing:
                raise OSError(errno.EBUSY, os.strerror(errno.EBUSY), "storage")
            return original_remove(store_obj, session_id)

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(SessionStore, "remove", flaky_remove):
            with contextlib.redirect_stdout(out):
                with contextlib.redirect_stderr(err):
                    code = main_ctl(["--config", self.h.config_path, "cleanup",
                                     "--older-than", "7"], env={})

        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertIn("%s: removed" % first, text)
        self.assertIn("%s: failed ([Errno 16] Device or resource busy: "
                      "'storage')" % failing, text)
        self.assertIn("%s: removed" % last, text)
        self.assertIn("removed 2 old session(s); failed 1 session(s)", text)
        self.assertIn("ccc-agent: cleanup failed for 1 session(s)",
                      err.getvalue())
        with self.assertRaises(KeyError):
            store.load(first)
        self.assertEqual(store.load(failing).state, "auto-committed")
        with self.assertRaises(KeyError):
            store.load(last)

    def test_ctl_error_returns_nonzero(self):
        self.assertEqual(
            main_ctl(["--config", self.h.config_path, "commit",
                      "agent-missing"], env={}), 1)


class TestShellCompletion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)
        self._make_session("agent-alpha")
        self._make_session("agent-beta")

    def tearDown(self):
        self._tmp.cleanup()

    def _make_session(self, session_id):
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        root = ProtectedRoot(
            name="storage_user", base=self.h.base,
            store=os.path.join(self.h.tmp, "stores", "storage_user"),
            branch=session_id,
            mount=os.path.join(self.h.tmp, "mounts", session_id),
            visible="/storage/user", home_subdir="")
        return store.create(
            owner="domen", agent_kind="codex", agent_command=["codex"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "manual"}, protected_roots={"storage_user": root},
            session_id=session_id)

    def complete(self, words, cword=None, env=None):
        if cword is None:
            cword = len(words) - 1
        out = io.StringIO()
        env = {"CCC_AGENT_CONFIG": self.h.config_path} if env is None else env
        with contextlib.redirect_stdout(out):
            code = main(["__complete", "bash", str(cword)] + list(words),
                        env=env)
        self.assertEqual(code, 0)
        return out.getvalue().splitlines()

    def test_session_id_ops_complete_session_ids(self):
        ops = (list(getattr(cli_mod, "_SESSION_ID_CTL_OPS"))
               + ["diff", "review", "list", "ls"])
        for op in ops:
            with self.subTest(op=op):
                self.assertEqual(
                    self.complete(["ccc-agent", op, "agent-a"]),
                    ["agent-alpha"])

    def test_resume_completes_session_ids_and_options(self):
        self.assertEqual(
            self.complete(["ccc-agent", "resume", "agent-a"]),
            ["agent-alpha"])
        matches = self.complete(["ccc-agent", "resume", "--"])
        self.assertIn("--force", matches)
        self.assertIn("--allow-failed", matches)
        self.assertIn("--agent", matches)
        self.assertIn("--cmd", matches)
        self.assertIn("--full-isolation", matches)
        self.assertEqual(self.complete(["ccc-agent", "resume", "agent-alpha",
                                        "--cmd", ""]), [])

    def test_run_completion_lists_full_isolation_option(self):
        matches = self.complete(["ccc-agent", "run", "--"])
        self.assertIn("--full-isolation", matches)
        self.assertIn("--protect-agent-state", matches)
        self.assertIn("--serve", matches)
        self.assertIn("--lifecycle", matches)
        self.assertIn("--adaptive-bootstrap-seconds", matches)
        self.assertIn("--adaptive-stability-seconds", matches)
        self.assertIn("--adaptive-detach-seconds", matches)

    def test_review_completion_lists_ignored_policy_options(self):
        matches = self.complete(["ccc-agent", "review", "--"])
        self.assertIn("--show-ignored", matches)
        self.assertIn("--include-ignored", matches)
        self.assertIn("--show-file-diffs", matches)

    def test_diff_completion_lists_file_diff_option(self):
        matches = self.complete(["ccc-agent", "diff", "--"])
        self.assertIn("--show-file-diffs", matches)

    def test_session_completion_uses_config_flag_after_op(self):
        self.assertEqual(
            self.complete(["ccc-agent", "show", "--config", self.h.config_path,
                           "agent-b"]),
            ["agent-beta"])

    def test_session_completion_stops_after_session_positional(self):
        self.assertEqual(
            self.complete(["ccc-agent", "diff", "agent-alpha", ""]),
            [])

    def test_multi_session_ops_complete_additional_session_ids(self):
        self.assertEqual(
            self.complete(["ccc-agent", "abort", "agent-alpha", "agent-b"]),
            ["agent-beta"])

    def test_list_accepts_completed_session_prefix(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--config", self.h.config_path, "list", "agent-a"],
                        env={})
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("agent-alpha", text)
        self.assertNotIn("agent-beta", text)

    def test_ls_alias_accepts_completed_session_prefix(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--config", self.h.config_path, "ls", "agent-a"],
                        env={})
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("agent-alpha", text)
        self.assertNotIn("agent-beta", text)

    def test_top_level_completion_lists_matching_ops(self):
        matches = self.complete(["ccc-agent", "st"])
        self.assertIn("status", matches)
        self.assertIn("cleanup", self.complete(["ccc-agent", "cl"]))
        list_matches = self.complete(["ccc-agent", "l"])
        self.assertIn("list", list_matches)
        self.assertIn("ls", list_matches)

    def test_cleanup_completion_lists_options_not_session_ids(self):
        matches = self.complete(["ccc-agent", "cleanup", "--"])
        self.assertIn("--older-than", matches)
        self.assertIn("--all-type", matches)
        self.assertIn("--all-types", matches)
        self.assertIn("--dry-run", matches)
        self.assertNotIn("agent-alpha", matches)
        short_matches = self.complete(["ccc-agent", "cleanup", "-"])
        self.assertIn("-a", short_matches)
        self.assertIn("-o", short_matches)

    def test_public_completion_command_prints_bash_hook(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["completion", "bash"], env={})
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("ccc-agent __complete bash", text)
        self.assertIn("complete -o default -F _ccc_agent_complete ccc-agent",
                      text)


class TestUnifiedMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_op_dispatches_to_contained_run(self):
        code = main([
            "run", "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "echo unified > u.txt",
        ], env={})
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(
            self.h.base, self.h.workspace_rel, "u.txt")))

    def test_control_ops_are_direct(self):
        main([
            "run", "--config", self.h.config_path,
            "--workspace", "/storage/user/Projects/proj-a",
            "--", "sh", "-c", "echo x > f.txt",
        ], env={})
        self.assertEqual(main(["--config", self.h.config_path, "list"],
                              env={}), 0)
        self.assertEqual(main(["list", "--config", self.h.config_path],
                              env={}), 0)

    def test_version_flag_reports_release_and_git_commit_without_config(self):
        expected_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            text=True).strip()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--version"], env={})

        self.assertEqual(code, 0)
        self.assertEqual(
            out.getvalue(),
            "ccc-agent v0.4 (git %s)\n" % expected_commit)

    def test_version_flag_omits_git_commit_when_unknown(self):
        out = io.StringIO()
        with mock.patch("ccc_agent.version.git_commit", return_value=None):
            with contextlib.redirect_stdout(out):
                code = main(["--version"], env={})

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "ccc-agent v0.4\n")

    def test_top_level_help_groups_commands_by_audience(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--help"], env={})

        self.assertEqual(code, 0)
        text = out.getvalue()
        primary = text.index("Primary user ops:")
        session = text.index("Session/control ops")
        plugin = text.index("Plugin/hook ops")
        auxiliary = text.index("Auxiliary setup/debug ops:")
        self.assertLess(primary, session)
        self.assertLess(session, plugin)
        self.assertLess(plugin, auxiliary)
        primary_text = text[primary:session]
        self.assertIn("  run", primary_text)
        self.assertIn("  resume", primary_text)
        self.assertNotIn("setup", primary_text)
        session_text = text[session:plugin]
        self.assertIn("list, ls", session_text)
        self.assertIn("review", session_text)
        self.assertIn("commit", session_text)
        plugin_text = text[plugin:auxiliary]
        self.assertIn("turn-finalize", plugin_text)
        self.assertIn("turn-approve", plugin_text)
        self.assertIn("turn-resolve", plugin_text)
        auxiliary_text = text[auxiliary:]
        self.assertIn("setup", auxiliary_text)
        self.assertIn("completion", auxiliary_text)
        self.assertIn("softsandbox", auxiliary_text)


class TestMainCtlCheckBeforeFinal(unittest.TestCase):
    """Exit-code contract for the hook: 2 = repair, 0 = allow/exhausted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = CliHarness(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_running_session(self, dirty_rel=None, max_repair_attempts=1):
        # Park a session in `running` against the same state dir the CLI
        # uses; FakeBranchFS status only needs the on-disk branch delta dir.
        store = SessionStore(os.path.join(self.h.tmp, "state"))
        branch_store = os.path.join(self.h.tmp, "stores", "storage_user")
        files = os.path.join(branch_store, "branches", "agent-live", "files")
        os.makedirs(files, exist_ok=True)
        root = ProtectedRoot(
            name="storage_user", base=self.h.base, store=branch_store,
            branch="agent-live",
            mount=os.path.join(self.h.tmp, "mounts", "agent-live"),
            visible="/storage/user", home_subdir="")
        session = store.create(
            owner="domen", agent_kind="claude", agent_command=["claude"],
            workspace="/storage/user/Projects/proj-a",
            policy={"mode": "workspace-auto",
                    "allowed_scopes": ["/storage/user/Projects/proj-a"],
                    "max_policy_repair_attempts": max_repair_attempts},
            protected_roots={"storage_user": root}, completion="manual")
        session.transition("mounting")
        session.transition("running")
        store.save(session)
        if dirty_rel:
            with open(os.path.join(files, dirty_rel), "w") as fh:
                fh.write("x\n")
        return session.session_id

    def check(self, sid):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main_ctl(["--config", self.h.config_path,
                             "turn-check", sid], env={})
        return code, out.getvalue()

    def test_repair_then_exhausted_exit_codes(self):
        sid = self.make_running_session(dirty_rel="outside.txt")
        code, text = self.check(sid)
        self.assertEqual(code, 2)        # dirty + budget left: ask for repair
        self.assertIn("/storage/user/outside.txt", text)
        code, _text = self.check(sid)
        self.assertEqual(code, 0)        # budget exhausted: never loop

    def test_clean_running_session_exits_zero(self):
        sid = self.make_running_session()
        self.assertEqual(self.check(sid)[0], 0)

    def test_control_error_exits_one(self):
        self.assertEqual(self.check("agent-missing")[0], 1)


if __name__ == "__main__":
    unittest.main()
