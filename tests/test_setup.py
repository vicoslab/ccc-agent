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

    def local_bin_for_test(self):
        local_bin = os.path.join(self.home, ".local", "bin")
        os.makedirs(local_bin, exist_ok=True)
        return local_bin


class TestSetupConfig(unittest.TestCase):
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
        # Native plugin injection: Codex needs its plugin to appear as an
        # installed/enabled plugin, not merely as a raw ~/.codex/plugins dir.
        self.assertEqual(cfg["agent_hook_mode"], "plugins")
        plugins = cfg["agent_plugins"]
        self.assertEqual(plugins["claude"]["argv"],
                         ["--plugin-dir",
                          "/ccc-agent/plugins/claude-ccc-containment"])
        self.assertTrue(plugins["claude"]["src"].endswith(
            "/plugins/claude-ccc-containment"))
        self.assertEqual(plugins["codex"]["sandbox_path"],
                         "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0")
        self.assertEqual(plugins["codex"]["ensure_dirs"],
                         ["/home/domen/.codex/plugins/cache/ccc-agent/ccc"])
        self.assertEqual(plugins["codex"]["argv"],
                         [setup_mod.CODEX_DISABLE_INNER_SANDBOX_ARG])
        self.assertEqual(plugins["codex"]["plugin_id"],
                         "ccc@ccc-agent")
        self.assertEqual(
            plugins["hermes"]["setenv"]["HERMES_BUNDLED_PLUGINS"],
            "/ccc-agent/plugins/hermes")
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
        self.assertEqual(plugins["codex"]["sandbox_path"],
                         "/home/domen/.codex/plugins/cache/ccc-agent/ccc/0.2.0")
        self.assertEqual(plugins["codex"]["plugin_id"],
                         "ccc@ccc-agent")
        self.assertEqual(plugins["codex"]["argv"],
                         [setup_mod.CODEX_DISABLE_INNER_SANDBOX_ARG])
        self.assertEqual(plugins["claude"]["argv"],
                         ["--plugin-dir",
                          "/ccc-agent/plugins/claude-ccc-containment"])
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

    def test_setup_enables_codex_plugin_with_explanatory_config_comment(self):
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
            # Codex 0.136+ loads plugin hooks only from enabled plugins. Keep a
            # small managed config entry so contained ccc-agent Codex sessions
            # see the read-only plugin cache bind. The comments tell users why
            # this entry should remain even when the cache is absent outside
            # ccc-agent.
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
            self.assertFalse(os.path.exists(os.path.join(home, ".claude", "settings.json")))

            with open(config_path) as fh:
                cfg = json.load(fh)
            self.assertEqual(cfg["agent_hook_mode"], "plugins")
            # the plugin sources are the bundled package assets and exist on disk
            for agent in ("codex", "claude", "hermes"):
                src = cfg["agent_plugins"][agent]["src"]
                self.assertTrue(os.path.isdir(src), src)

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
