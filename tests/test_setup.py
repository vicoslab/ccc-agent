"""Tests for ccc-agent setup installation wiring."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ccc_agent import setup as setup_mod


class TestCondaShimActivation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.home = os.path.join(self.tmp, "home")
        self.shimdir = os.path.join(self.tmp, "ccc-agent-shims")
        self.conda = os.path.join(self.tmp, "conda-env")
        self.conda_bin = os.path.join(self.conda, "bin")
        os.makedirs(self.home)
        os.makedirs(self.conda_bin)
        self.launcher = os.path.join(self.tmp, "ccc-agent")
        with open(self.launcher, "w") as fh:
            fh.write("#!/bin/sh\necho LAUNCH:$*\n")
        os.chmod(self.launcher, 0o755)
        for agent in ("codex", "claude"):
            real = os.path.join(self.conda_bin, agent)
            with open(real, "w") as fh:
                fh.write("#!/bin/sh\necho CONDA-REAL:%s:$*\n" % agent)
            os.chmod(real, 0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def test_conda_activation_hook_puts_shims_before_env_bin(self):
        config = os.path.join(self.tmp, "config.json")
        with mock.patch.dict(os.environ, {"HOME": self.home}, clear=False):
            rc = setup_mod.main([
                "--user",
                "--config", config,
                "--state-dir", os.path.join(self.tmp, "state"),
                "--no-hooks",
                "--enable-shims",
                "--link-dir", self.shimdir,
                "--conda-prefix", self.conda,
                "--conda-activate-shims",
            ])
        self.assertEqual(rc, 0)
        activate = os.path.join(
            self.conda, "etc", "conda", "activate.d", "ccc-agent-shims.sh")
        deactivate = os.path.join(
            self.conda, "etc", "conda", "deactivate.d", "ccc-agent-shims.sh")
        self.assertTrue(os.path.isfile(activate))
        self.assertTrue(os.path.isfile(deactivate))

        for agent in ("codex", "claude"):
            proc = subprocess.run(
                ["sh", "-c", ". \"$ACTIVATE\" && command -v \"$AGENT\" && \"$AGENT\" do thing"],
                env={
                    "PATH": "%s:/usr/bin:/bin" % self.conda_bin,
                    "HOME": self.home,
                    "ACTIVATE": activate,
                    "AGENT": agent,
                    "CCC_AGENT_CLI": self.launcher,
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.splitlines()
            self.assertEqual(lines[0], os.path.join(self.shimdir, agent))
            self.assertIn(
                "LAUNCH:run --agent %s -- %s do thing" % (agent, agent),
                proc.stdout)

        proc = subprocess.run(
            ["sh", "-c", ". \"$ACTIVATE\" && . \"$DEACTIVATE\" && command -v codex"],
            env={
                "PATH": "%s:/usr/bin:/bin" % self.conda_bin,
                "HOME": self.home,
                "ACTIVATE": activate,
                "DEACTIVATE": deactivate,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), os.path.join(self.conda_bin, "codex"))

    def test_shell_path_hook_moves_dedicated_shim_dir_to_front(self):
        config = os.path.join(self.tmp, "config.json")
        hook = os.path.join(self.tmp, "ccc-agent-shim-path.sh")
        with mock.patch.dict(os.environ, {"HOME": self.home}, clear=False):
            rc = setup_mod.main([
                "--user",
                "--config", config,
                "--state-dir", os.path.join(self.tmp, "state"),
                "--no-hooks",
                "--enable-shims",
                "--link-dir", self.shimdir,
                "--shell-path-hook", hook,
            ])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(hook))

        original_path = "%s:%s:%s:/usr/bin:/bin" % (
            self.local_bin_for_test(), self.shimdir, self.conda_bin)
        proc = subprocess.run(
            ["sh", "-c", ". \"$HOOK\" && printf '%s\\n%s\\n' \"$PATH\" \"$CCC_AGENT_SHIM_DIR\""],
            env={"PATH": original_path, "HOOK": hook},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertEqual(lines[0], "%s:%s:%s:/usr/bin:/bin" % (
            self.shimdir, self.local_bin_for_test(), self.conda_bin))
        self.assertEqual(lines[1], self.shimdir)

    def test_setup_links_ssh_shell_router_when_requested(self):
        config = os.path.join(self.tmp, "config.json")
        router = os.path.join(self.tmp, "install", "bin", "ccc-ssh-shell-router")
        with mock.patch.dict(os.environ, {"HOME": self.home}, clear=False):
            rc = setup_mod.main([
                "--user",
                "--config", config,
                "--state-dir", os.path.join(self.tmp, "state"),
                "--no-hooks",
                "--enable-shims",
                "--link-dir", self.shimdir,
                "--ssh-shell-router", router,
            ])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.islink(router), router)
        target = os.readlink(router)
        self.assertTrue(target.endswith("ccc-agent-ssh-shell-router.sh"), target)
        self.assertTrue(os.access(router, os.X_OK), router)

    def local_bin_for_test(self):
        local_bin = os.path.join(self.home, ".local", "bin")
        os.makedirs(local_bin, exist_ok=True)
        return local_bin


class TestSetupConfig(unittest.TestCase):
    def test_setup_prefers_packaged_vicoslab_branchfs_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            bundled = os.path.join(tmp, "branchfs")
            with open(bundled, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(bundled, 0o755)
            config_path = os.path.join(tmp, "config.json")
            with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False), \
                    mock.patch("ccc_agent.setup.branchfs_runtime.packaged_branchfs_bin",
                               return_value=bundled), \
                    mock.patch("ccc_agent.setup.branchfs_runtime.libfuse3_status",
                               return_value=(True, "libfuse3.so.3")):
                rc = setup_mod.main([
                    "--user",
                    "--config", config_path,
                    "--state-dir", os.path.join(tmp, "state"),
                    "--no-hooks",
                ])
            self.assertEqual(rc, 0)
            with open(config_path) as fh:
                cfg = json.load(fh)
            self.assertEqual(cfg["branchfs_bin"], bundled)

    def test_setup_warns_when_packaged_branchfs_needs_libfuse3(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            bundled = os.path.join(tmp, "branchfs")
            with open(bundled, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(bundled, 0o755)
            config_path = os.path.join(tmp, "config.json")
            stderr = tempfile.TemporaryFile(mode="w+")
            with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False), \
                    mock.patch("ccc_agent.setup.branchfs_runtime.packaged_branchfs_bin",
                               return_value=bundled), \
                    mock.patch("ccc_agent.setup.branchfs_runtime.libfuse3_status",
                               return_value=(False, "libfuse3.so.3 not found")), \
                    mock.patch("sys.stderr", stderr):
                rc = setup_mod.main([
                    "--user",
                    "--config", config_path,
                    "--state-dir", os.path.join(tmp, "state"),
                    "--no-hooks",
                ])
            self.assertEqual(rc, 0)
            stderr.seek(0)
            self.assertIn("WARNING packaged BranchFS requires libfuse3",
                          stderr.read())
            stderr.close()

    def test_bundled_codex_hook_uses_plugin_root_command_path(self):
        hooks_json = os.path.join(
            setup_mod.plugins_dir(), "codex-ccc-containment", "hooks", "hooks.json")
        with open(hooks_json) as fh:
            hooks = json.load(fh)
        stop_groups = hooks["hooks"]["Stop"]
        self.assertEqual(len(stop_groups), 1)
        command = stop_groups[0]["hooks"][0]["command"]
        self.assertEqual(command, "${PLUGIN_ROOT}/hooks/ccc-stop-hook.sh")

    def test_system_config_keeps_agent_state_writable_by_default(self):
        cfg = setup_mod.build_config(
            mode="system",
            user="domen",
            home="/home/domen",
            branchfs_bin="/usr/local/bin/branchfs",
            bwrap_bin="/usr/bin/bwrap",
            state_dir="/storage/user/.ccc-agent",
            storage_root="/storage",
            branch_store="/opt/branchfs_branches",
            container_name="domen-cuda10",
        )

        self.assertEqual(cfg["cred_mounts"], [])
        # Default runtime plugin wiring is mount/config only. ccc-agent setup
        # persists tool config where needed; ccc-agent run must not append
        # interactive CLI args such as Codex YOLO flags or Claude --plugin-dir.
        self.assertEqual(cfg["agent_hook_mode"], "plugins")
        plugins = cfg["agent_plugins"]
        self.assertEqual(sorted(plugins), ["codex"])
        self.assertEqual(plugins["codex"]["sandbox_path"],
                         "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0")
        self.assertEqual(plugins["codex"]["ensure_dirs"],
                         ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"])
        self.assertNotIn("argv", plugins["codex"])
        self.assertEqual(plugins["codex"]["plugin_id"],
                         "ccc@ccc-agent")
        for spec in plugins.values():
            self.assertNotIn("settings.json", spec.get("sandbox_path", ""))
        ignore = cfg["policy"]["ignore_patterns"]
        self.assertNotIn("/storage/user/domen-cuda10/.codex*", ignore)
        self.assertNotIn("/storage/user/domen-cuda10/.claude*", ignore)
        self.assertEqual(cfg["protect_agent_state"], False)
        self.assertTrue(cfg["ensure_agent_state_dirs"])
        self.assertIn("/home/domen/.codex", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.hermes", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.claude.json", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/bin/codex", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/bin/claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/share/claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/state/claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.cache/claude-cli-nodejs", cfg["agent_state_binds"])
        self.assertEqual(cfg["roots"][0]["visible"], "/storage")
        self.assertEqual(cfg["roots"][0]["home_subdir"], "user/domen-cuda10")
        self.assertEqual(cfg["branchfs_timeout_seconds"], 30)
        self.assertNotIn("workspace", cfg)

    def test_user_config_keeps_agent_state_writable_by_default(self):
        cfg = setup_mod.build_config(
            mode="user",
            user="domen",
            home="/home/domen",
            branchfs_bin="branchfs",
            bwrap_bin="bwrap",
            state_dir="/home/domen/.ccc-agent",
        )

        self.assertEqual(cfg["cred_mounts"], [])
        plugins = cfg["agent_plugins"]
        self.assertEqual(sorted(plugins), ["codex"])
        self.assertEqual(plugins["codex"]["sandbox_path"],
                         "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0")
        self.assertEqual(plugins["codex"]["plugin_id"],
                         "ccc@ccc-agent")
        self.assertNotIn("argv", plugins["codex"])
        ignore = cfg["policy"]["ignore_patterns"]
        self.assertNotIn("/home/domen/.codex*", ignore)
        self.assertNotIn("/home/domen/.claude*", ignore)
        self.assertIn("/home/domen/.codex", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.hermes", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.claude.json", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/bin/codex", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/bin/claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/share/claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.local/state/claude", cfg["agent_state_binds"])
        self.assertIn("/home/domen/.cache/claude-cli-nodejs", cfg["agent_state_binds"])
        self.assertFalse(cfg["protect_agent_state"])
        self.assertNotIn("workspace", cfg)

    def test_setup_enables_codex_plugin_and_claude_hooks_with_persistent_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            config_path = os.path.join(tmp, "config.json")
            state_dir = os.path.join(tmp, "state")
            with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False):
                rc = setup_mod.main([
                    "--user",
                    "--config", config_path,
                    "--state-dir", state_dir,
                ])
            self.assertEqual(rc, 0)
            # Codex loads enabled/trusted plugins from config; user-mode setup
            # writes the managed block to ~/.codex/config.toml.
            codex_config = os.path.join(home, ".codex", "config.toml")
            self.assertTrue(os.path.isfile(codex_config))
            with open(codex_config) as fh:
                codex_toml = fh.read()
            self.assertIn("BEGIN ccc-agent Codex plugin", codex_toml)
            self.assertIn("contained `ccc-agent run -- codex` sessions", codex_toml)
            self.assertIn("safe to leave enabled", codex_toml)
            self.assertIn('plugins."ccc@ccc-agent".enabled = true',
                          codex_toml)
            self.assertIn("Trust only the bundled CCC hooks", codex_toml)
            for key, trusted_hash in setup_mod.CODEX_HOOK_TRUSTED_HASHES:
                self.assertIn('hooks.state."%s".trusted_hash = "%s"'
                              % (key, trusted_hash), codex_toml)

            # Claude uses standalone persistent settings instead of --plugin-dir.
            claude_settings = os.path.join(home, ".claude", "settings.json")
            self.assertTrue(os.path.isfile(claude_settings))
            with open(claude_settings) as fh:
                claude = json.load(fh)
            hooks = claude["hooks"]
            self.assertIn("SessionStart", hooks)
            self.assertIn("Stop", hooks)
            hook_commands = json.dumps(hooks)
            self.assertIn("claude-ccc-containment/hooks/ccc-context-hook.sh",
                          hook_commands)
            self.assertIn("claude-ccc-containment/hooks/ccc-stop-hook.sh",
                          hook_commands)
            self.assertNotIn("--plugin-dir", hook_commands)

            with open(config_path) as fh:
                cfg = json.load(fh)
            self.assertEqual(cfg["agent_hook_mode"], "plugins")
            self.assertEqual(sorted(cfg["agent_plugins"]), ["codex"])
            src = cfg["agent_plugins"]["codex"]["src"]
            self.assertTrue(os.path.isdir(src), src)
            self.assertNotIn("argv", cfg["agent_plugins"]["codex"])

    def test_system_setup_can_write_tool_managed_config_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            config_path = os.path.join(tmp, "ccc-agent.json")
            codex_config = os.path.join(tmp, "etc", "codex", "config.toml")
            claude_settings = os.path.join(
                tmp, "etc", "claude-code", "managed-settings.d",
                "50-ccc-agent.json")
            with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False):
                rc = setup_mod.main([
                    "--system",
                    "--config", config_path,
                    "--state-dir", os.path.join(tmp, "state"),
                    "--storage-root", os.path.join(tmp, "storage"),
                    "--branch-store", os.path.join(tmp, "branches"),
                    "--codex-config", codex_config,
                    "--claude-settings", claude_settings,
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(codex_config))
            self.assertTrue(os.path.isfile(claude_settings))
            self.assertFalse(os.path.exists(
                os.path.join(home, ".codex", "config.toml")))
            self.assertFalse(os.path.exists(
                os.path.join(home, ".claude", "settings.json")))
            with open(codex_config) as fh:
                self.assertIn('plugins."ccc@ccc-agent".enabled = true',
                              fh.read())
            with open(claude_settings) as fh:
                hook_json = fh.read()
            self.assertIn("claude-ccc-containment/hooks/ccc-context-hook.sh",
                          hook_json)
            self.assertIn("claude-ccc-containment/hooks/ccc-stop-hook.sh",
                          hook_json)
            self.assertNotIn("--plugin-dir", hook_json)

    def test_setup_preserves_existing_codex_config_when_adding_managed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            codex_dir = os.path.join(home, ".codex")
            os.makedirs(codex_dir)
            codex_config = os.path.join(codex_dir, "config.toml")
            with open(codex_config, "w") as fh:
                fh.write(
                    'model = "gpt-5.5"\n'
                    '\n[hooks.state]\n'
                    '\n[hooks.state."ccc@ccc-agent:hooks/hooks.json:stop:0:0"]\n'
                    'trusted_hash = "sha256:old"\n'
                    '\n[hooks.state."ccc-agent@ccc-agent:hooks/hooks.json:stop:0:0"]\n'
                    'trusted_hash = "sha256:older"\n'
                    '\n[hooks.state."other@plugin:hooks/hooks.json:stop:0:0"]\n'
                    'trusted_hash = "sha256:keep"\n'
                    '\n[projects."/"]\ntrust_level = "trusted"\n')
            config_path = os.path.join(tmp, "config.json")
            with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False):
                rc = setup_mod.main([
                    "--user", "--config", config_path,
                    "--state-dir", os.path.join(tmp, "state")])
            self.assertEqual(rc, 0)
            with open(codex_config) as fh:
                codex_toml = fh.read()
            self.assertIn('model = "gpt-5.5"', codex_toml)
            self.assertIn('[projects."/"]', codex_toml)
            self.assertEqual(codex_toml.count("BEGIN ccc-agent Codex plugin"), 1)
            self.assertTrue(codex_toml.startswith("# BEGIN ccc-agent Codex plugin"))
            self.assertNotIn('trusted_hash = "sha256:old"', codex_toml)
            self.assertNotIn('trusted_hash = "sha256:older"', codex_toml)
            self.assertIn('trusted_hash = "sha256:keep"', codex_toml)
            for key, _trusted_hash in setup_mod.CODEX_HOOK_TRUSTED_HASHES:
                self.assertIn('hooks.state."%s".trusted_hash' % key,
                              codex_toml)

    def test_no_agent_plugins_flag_disables_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            config_path = os.path.join(tmp, "config.json")
            for flag in ("--no-agent-plugins", "--no-hooks"):
                with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False):
                    rc = setup_mod.main([
                        "--user", "--config", config_path,
                        "--state-dir", os.path.join(tmp, "state"), flag])
                self.assertEqual(rc, 0)
                with open(config_path) as fh:
                    cfg = json.load(fh)
                self.assertEqual(cfg["agent_plugins"], {})
                self.assertEqual(cfg["agent_hook_mode"], "disabled")
                self.assertFalse(os.path.exists(
                    os.path.join(home, ".codex", "config.toml")))
                self.assertFalse(os.path.exists(
                    os.path.join(home, ".claude", "settings.json")))

    def test_setup_protect_agent_state_flag_sets_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            config_path = os.path.join(tmp, "config.json")
            with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False):
                rc = setup_mod.main([
                    "--user", "--config", config_path,
                    "--state-dir", os.path.join(tmp, "state"),
                    "--protect-agent-state"])
            self.assertEqual(rc, 0)
            with open(config_path) as fh:
                cfg = json.load(fh)
            self.assertTrue(cfg["protect_agent_state"])


if __name__ == "__main__":
    unittest.main()
