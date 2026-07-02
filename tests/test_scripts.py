"""Tests for the shell scaffolding: launch shim and hook adapters. All run
unprivileged (syntax and behavior checks only)."""

import json
import os
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(HERE)
ASSETS = os.path.join(AGENT_DIR, "ccc_agent", "assets")
PLUGINS = os.path.join(ASSETS, "plugins")
SHIM_SH = os.path.join(ASSETS, "shims", "ccc-agent-shim.sh")
SOFTSANDBOX_SH = os.path.join(ASSETS, "scripts", "softsandbox.sh")
HOOKS = [os.path.join(ASSETS, "hooks", name)
         for name in ("claude-stop-hook.sh", "codex-stop-hook.sh",
                      "hermes-finish-turn.sh")]
# hooks with blocking stop semantics (check-before-final self-repair)
STOP_HOOKS = [os.path.join(ASSETS, "hooks", name)
              for name in ("claude-stop-hook.sh", "codex-stop-hook.sh")]
# plugin-bundled stop hooks (the auto-injected per-contained-run path)
PLUGIN_STOP_HOOKS = [
    os.path.join(PLUGINS, "claude-ccc-containment", "hooks", "ccc-stop-hook.sh"),
    os.path.join(PLUGINS, "codex-ccc-containment", "hooks", "ccc-stop-hook.sh"),
]


class TestShellSyntax(unittest.TestCase):
    def test_all_scripts_parse(self):
        for script in [SHIM_SH, SOFTSANDBOX_SH] + HOOKS + PLUGIN_STOP_HOOKS:
            proc = subprocess.run(["bash", "-n", script],
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0,
                             "%s: %s" % (script, proc.stderr))


class TestPluginAssets(unittest.TestCase):
    """The packaged native plugins ccc-agent run injects per contained run."""

    def test_claude_plugin_layout(self):
        root = os.path.join(PLUGINS, "claude-ccc-containment")
        self.assertTrue(os.path.isfile(
            os.path.join(root, ".claude-plugin", "plugin.json")))
        with open(os.path.join(root, "hooks", "hooks.json")) as fh:
            hooks = json.load(fh)
        self.assertIn("Stop", hooks["hooks"])
        cmd = hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn("ccc-stop-hook.sh", cmd)
        self.assertTrue(os.path.isfile(
            os.path.join(root, "hooks", "ccc-stop-hook.sh")))

    def test_codex_plugin_layout(self):
        root = os.path.join(PLUGINS, "codex-ccc-containment")
        self.assertTrue(os.path.isfile(
            os.path.join(root, ".codex-plugin", "plugin.json")))
        with open(os.path.join(root, "hooks", "hooks.json")) as fh:
            hooks = json.load(fh)
        self.assertIn("Stop", hooks["hooks"])
        self.assertTrue(os.path.isfile(
            os.path.join(root, "hooks", "ccc-stop-hook.sh")))

    def test_hermes_plugin_layout(self):
        root = os.path.join(PLUGINS, "hermes-ccc-containment")
        self.assertTrue(os.path.isfile(os.path.join(root, "plugin.yaml")))
        with open(os.path.join(root, "__init__.py")) as fh:
            src = fh.read()
        self.assertIn("def register", src)
        self.assertIn("finalize-turn", src)

    def test_bundled_stop_hooks_match(self):
        # the claude/codex plugin stop hooks are the same adapter; guard drift
        bodies = set()
        for path in PLUGIN_STOP_HOOKS:
            with open(path) as fh:
                bodies.add(fh.read())
        self.assertEqual(len(bodies), 1, "plugin stop hooks have drifted")


class TestShim(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.shimdir = os.path.join(tmp, "shims")
        self.realdir = os.path.join(tmp, "real")
        self.home = os.path.join(tmp, "home")
        self.localbin = os.path.join(self.home, ".local", "bin")
        os.makedirs(self.shimdir)
        os.makedirs(self.realdir)
        os.makedirs(self.localbin)
        os.symlink(SHIM_SH, os.path.join(self.shimdir, "codex"))
        os.symlink(SHIM_SH, os.path.join(self.shimdir, "claude"))
        self.real = os.path.join(self.realdir, "codex")
        with open(self.real, "w") as fh:
            fh.write("#!/bin/sh\necho REAL:$0:$*\n")
        os.chmod(self.real, 0o755)
        self.launcher = os.path.join(tmp, "ccc-agent")
        with open(self.launcher, "w") as fh:
            fh.write("#!/bin/sh\necho LAUNCH:$*\n")
        os.chmod(self.launcher, 0o755)
        self.env = {
            "PATH": "%s:%s:/usr/bin:/bin" % (self.shimdir, self.realdir),
            "HOME": self.home,
            "CCC_AGENT_CLI": self.launcher,
        }

    def tearDown(self):
        self._tmp.cleanup()

    def run_shim(self, env_extra=None, args=("do", "thing")):
        env = dict(self.env)
        env.update(env_extra or {})
        return subprocess.run(["codex", *args], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)

    def test_shim_wraps_with_launcher(self):
        proc = self.run_shim()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LAUNCH:run --agent codex -- %s do thing" % self.real,
                      proc.stdout)

    def test_finds_user_local_bin_when_not_on_path(self):
        for agent in ("codex", "claude"):
            local_real = os.path.join(self.localbin, agent)
            with open(local_real, "w") as fh:
                fh.write("#!/bin/sh\necho LOCAL:$0:$*\n")
            os.chmod(local_real, 0o755)
            env = dict(self.env)
            env["PATH"] = "%s:/usr/bin:/bin" % self.shimdir
            proc = subprocess.run([agent, "do", "thing"], env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("LAUNCH:run --agent %s -- %s do thing" % (agent, local_real),
                          proc.stdout)

    def test_nested_session_runs_real_binary_directly(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SESSION": "agent-x"})
        self.assertIn("REAL:", proc.stdout)
        self.assertNotIn("LAUNCH:", proc.stdout)

    def test_bypass_env(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SHIM_BYPASS": "1"})
        self.assertIn("REAL:", proc.stdout)
        self.assertIn("bypass", proc.stderr)

    def test_missing_launcher_refuses_unprotected_run(self):
        env = dict(self.env)
        env["CCC_AGENT_CLI"] = "/nonexistent/launcher"
        proc = subprocess.run(["codex", "x"], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("REAL:", proc.stdout)
        self.assertIn("refusing", proc.stderr)


class TestStopHookSelfRepair(unittest.TestCase):
    """check-before-final wiring: exit 2 blocks the stop so the agent can
    repair; every other ctl outcome degrades to report-only (never blocks)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctl = os.path.join(self._tmp.name, "ccc-agent")

    def tearDown(self):
        self._tmp.cleanup()

    def fake_ctl(self, check_rc):
        with open(self.ctl, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "echo \"CALLED $1\"\n"
                     "if [ \"$1\" = check-before-final ]; then exit %d; fi\n"
                     "exit 0\n" % check_rc)
        os.chmod(self.ctl, 0o755)

    def run_hook(self, hook):
        env = {"PATH": "/usr/bin:/bin",
               "CCC_AGENT_SESSION": "agent-x",
               "CCC_AGENT_CLI": self.ctl}
        return subprocess.run(["sh", hook], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)

    def test_repair_exit_blocks_stop_without_reporting_turn(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(2)
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 2, "%s: %s" % (hook, proc.stderr))
            # repair instructions must reach the harness on stderr
            self.assertIn("check-before-final", proc.stderr)
            self.assertNotIn("finish-turn", proc.stdout + proc.stderr)

    def test_clean_check_reports_turn_and_exits_zero(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(0)
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (hook, proc.stderr))
            self.assertIn("CALLED finish-turn", proc.stdout)

    def test_ctl_failure_never_blocks_stop(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(1)  # e.g. ControlError from a racing finalize
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (hook, proc.stderr))
            self.assertIn("CALLED finish-turn", proc.stdout)


class TestStopHookControlSocket(unittest.TestCase):
    """When CCC_AGENT_CONTROL_SOCK is set the hook signals the supervisor via
    `ccc-agent finalize-turn` and propagates its exit code, instead of the
    store-based self-repair path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctl = os.path.join(self._tmp.name, "ccc-agent")

    def tearDown(self):
        self._tmp.cleanup()

    def fake_ctl(self, finalize_rc):
        with open(self.ctl, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "echo \"CALLED $1\" 1>&2\n"
                     "if [ \"$1\" = finalize-turn ]; then exit %d; fi\n"
                     "exit 0\n" % finalize_rc)
        os.chmod(self.ctl, 0o755)

    def run_hook(self, hook):
        env = {"PATH": "/usr/bin:/bin",
               "CCC_AGENT_SESSION": "agent-x",
               "CCC_AGENT_CLI": self.ctl,
               "CCC_AGENT_CONTROL_SOCK": "/tmp/ccc-agent/control.sock"}
        return subprocess.run(["sh", hook], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)

    def test_committed_turn_lets_stop_proceed(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(0)
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (hook, proc.stderr))
            self.assertIn("CALLED finalize-turn", proc.stderr)
            self.assertNotIn("check-before-final", proc.stderr)

    def test_needs_approval_blocks_stop(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(2)
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 2, "%s: %s" % (hook, proc.stderr))
            self.assertIn("CALLED finalize-turn", proc.stderr)


class TestHooksAreNoopsOutsideSessions(unittest.TestCase):
    def test_hooks_exit_zero_without_session(self):
        for hook in HOOKS:
            proc = subprocess.run(["sh", hook], env={"PATH": "/usr/bin:/bin"},
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0,
                             "%s: %s" % (hook, proc.stderr))


if __name__ == "__main__":
    unittest.main()
