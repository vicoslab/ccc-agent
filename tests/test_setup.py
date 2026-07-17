"""Tests for ccc-agent setup installation wiring."""

import json
import os
import stat
import subprocess
import sys
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
    def test_client_hardening_source_builds_and_blocks_parent_fd_theft(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = os.path.join(tmp, "libccc-client-hardening.so")
            built = setup_mod.build_mcp_client_hardening(library)
            self.assertEqual(built, library)
            self.assertTrue(os.path.isfile(library))
            self.assertTrue(os.path.isfile(library + ".sha256"))

            probe = """import os, subprocess, sys
r, w = os.pipe()
child = '''import os, sys
try:
    os.open('/proc/%d/fd/%d' % (os.getppid(), int(sys.argv[1])), os.O_WRONLY)
except OSError as exc:
    print(exc.errno)
    raise SystemExit(0)
raise SystemExit(2)
'''
proc = subprocess.run([sys.executable, '-c', child, str(w)],
                      stdout=subprocess.PIPE, text=True)
print('inheritable=%s child_rc=%d errno=%s' %
      (os.get_inheritable(w), proc.returncode, proc.stdout.strip()))
raise SystemExit(proc.returncode)
"""
            env = dict(os.environ, CCC_AGENT_HARDEN_CLIENT="1",
                       LD_PRELOAD=library)
            proc = subprocess.run([sys.executable, "-c", probe], env=env,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("inheritable=False", proc.stdout)
            self.assertIn("errno=13", proc.stdout)

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

    def test_codex_config_trusts_every_bundled_lifecycle_hook(self):
        # Captured from Codex 0.143.0 hooks/list for the packaged 0.2.0 plugin.
        # A hook can be discovered and enabled while still not executable in a
        # protocol-clean remote session unless its exact definition is trusted.
        expected = {
            "ccc@ccc-agent:hooks/hooks.json:session_start:0:0":
                "sha256:755b1b9d145a87b94a388f7566c56064acd92ad81dc9b221de795d5604e2625a",
            "ccc@ccc-agent:hooks/hooks.json:subagent_start:0:0":
                "sha256:110513c459ef92d4852bdf801e6be8c2fae9def56da9496775b408701d08a609",
            "ccc@ccc-agent:hooks/hooks.json:subagent_stop:0:0":
                "sha256:bf2126876905c70422c93361befd22ae53bbfa868b653587a10ba146af01554f",
            "ccc@ccc-agent:hooks/hooks.json:stop:0:0":
                "sha256:72d22a4a83b82ca16b8a5bcc60f519bc6b75773cc17f103e9acb18a028dc6998",
        }

        self.assertEqual(dict(setup_mod.CODEX_HOOK_TRUSTED_HASHES), expected)
        block = setup_mod.codex_plugin_config_block()
        for key, trusted_hash in expected.items():
            self.assertIn('hooks.state."%s".trusted_hash = "%s"'
                          % (key, trusted_hash), block)

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
        self.assertEqual(cfg["bwrap_unsetenv"], [])
        self.assertEqual(cfg["bwrap_setenv"], {})
        self.assertIn("full invocation/container environment",
                      cfg["_runtime_comment"])
        # Default runtime plugin wiring is plugin/seed based. Setup persists
        # Codex config; ccc-agent run does not append Codex YOLO flags or
        # Claude --plugin-dir.
        self.assertEqual(cfg["agent_hook_mode"], "plugins")
        plugins = cfg["agent_plugins"]
        self.assertEqual(sorted(plugins), ["claude", "codex"])
        seed = "/opt/claude-seed"
        self.assertEqual(plugins["claude"]["src"], seed)
        self.assertEqual(plugins["claude"]["sandbox_path"], seed)
        self.assertEqual(plugins["claude"]["setenv"],
                         {"CLAUDE_CODE_PLUGIN_SEED_DIR": seed})
        self.assertEqual(plugins["claude"]["plugin_id"],
                         "ccc@ccc-agent")
        self.assertNotIn("argv", plugins["claude"])
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
        self.assertEqual(cfg["bwrap_unsetenv"], [])
        self.assertEqual(cfg["bwrap_setenv"], {})
        plugins = cfg["agent_plugins"]
        self.assertEqual(sorted(plugins), ["claude", "codex"])
        seed = "/home/domen/.local/share/ccc-agent/claude-seed"
        self.assertEqual(plugins["claude"]["src"], seed)
        self.assertEqual(plugins["claude"]["sandbox_path"], seed)
        self.assertEqual(plugins["claude"]["setenv"],
                         {"CLAUDE_CODE_PLUGIN_SEED_DIR": seed})
        self.assertEqual(plugins["claude"]["plugin_id"],
                         "ccc@ccc-agent")
        self.assertNotIn("argv", plugins["claude"])
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

    def test_user_setup_materializes_and_enables_claude_seed(self):
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
            claude_seed = os.path.join(
                home, ".local", "share", "ccc-agent", "claude-seed")
            codex_config = os.path.join(home, ".codex", "config.toml")
            self.assertTrue(os.path.isfile(codex_config))
            with open(codex_config) as fh:
                codex_toml = fh.read()
            self.assertIn("BEGIN ccc-agent Codex plugin", codex_toml)
            self.assertIn("contained `ccc-agent run -- codex` sessions", codex_toml)
            self.assertIn("safe to leave enabled", codex_toml)
            self.assertIn('plugins."ccc@ccc-agent".enabled = true',
                          codex_toml)
            for tool in ("ccc_commit_kept", "ccc_discard_kept",
                         "ccc_abort_session"):
                self.assertIn(
                    'plugins."ccc@ccc-agent".mcp_servers.ccc.tools.%s.approval_mode = "prompt"'
                    % tool, codex_toml)
            for tool in ("ccc_status", "ccc_list_kept", "ccc_keep_kept"):
                self.assertIn(
                    'plugins."ccc@ccc-agent".mcp_servers.ccc.tools.%s.approval_mode = "approve"'
                    % tool, codex_toml)
            self.assertIn("Trust only the bundled CCC hooks", codex_toml)
            for key, trusted_hash in setup_mod.CODEX_HOOK_TRUSTED_HASHES:
                self.assertIn('hooks.state."%s".trusted_hash = "%s"'
                              % (key, trusted_hash), codex_toml)

            # Setup creates a complete seed from package data. It does not need
            # Docker or invoke Claude's marketplace installer.
            claude_settings = os.path.join(home, ".claude", "settings.json")
            with open(claude_settings) as fh:
                claude = json.load(fh)
            self.assertEqual(claude,
                             {"enabledPlugins": {"ccc@ccc-agent": True}})
            self.assertTrue(os.path.isfile(os.path.join(
                claude_seed, "marketplaces", "ccc-agent", ".claude-plugin",
                "marketplace.json")))
            self.assertTrue(os.path.isfile(os.path.join(
                claude_seed, "cache", "ccc-agent", "ccc", "0.2.0",
                ".claude-plugin", "plugin.json")))
            self.assertTrue(os.path.isfile(os.path.join(
                claude_seed, "cache", "ccc-agent", "ccc", "0.2.0",
                ".mcp.json")))
            with open(os.path.join(
                    claude_seed, "cache", "ccc-agent", "ccc", "0.2.0",
                    ".mcp.json")) as fh:
                self.assertEqual(json.load(fh)["mcpServers"]["ccc"]["args"],
                                 ["mcp-server", "--client", "claude"])
            self.assertTrue(os.path.isfile(os.path.join(
                claude_seed, "installed_plugins.json")))

            with open(config_path) as fh:
                cfg = json.load(fh)
            self.assertNotIn("mcp_client_hardening_library", cfg)
            self.assertEqual(cfg["agent_hook_mode"], "plugins")
            self.assertEqual(sorted(cfg["agent_plugins"]), ["claude", "codex"])
            src = cfg["agent_plugins"]["codex"]["src"]
            self.assertTrue(os.path.isdir(src), src)
            self.assertNotIn("argv", cfg["agent_plugins"]["codex"])
            self.assertNotIn("argv", cfg["agent_plugins"]["claude"])
            self.assertEqual(cfg["agent_plugins"]["claude"]["src"], claude_seed)
            self.assertEqual(cfg["agent_plugins"]["claude"]["sandbox_path"], claude_seed)
            self.assertEqual(
                cfg["agent_plugins"]["claude"]["setenv"],
                {"CLAUDE_CODE_PLUGIN_SEED_DIR": claude_seed})

    def test_system_setup_can_write_codex_config_and_claude_seed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            config_path = os.path.join(tmp, "ccc-agent.json")
            codex_config = os.path.join(tmp, "etc", "codex", "config.toml")
            claude_settings = os.path.join(
                tmp, "etc", "claude-code", "managed-settings.d",
                "50-ccc-agent.json")
            claude_seed = os.path.join(tmp, "opt", "claude-seed")
            with mock.patch.dict(os.environ, {"HOME": home, "USER": "domen"}, clear=False):
                rc = setup_mod.main([
                    "--system",
                    "--home", home,
                    "--config", config_path,
                    "--state-dir", os.path.join(tmp, "state"),
                    "--storage-root", os.path.join(tmp, "storage"),
                    "--branch-store", os.path.join(tmp, "branches"),
                    "--codex-config", codex_config,
                    "--claude-settings", claude_settings,
                    "--claude-plugin-seed-dir", claude_seed,
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(codex_config))
            self.assertTrue(os.path.isfile(claude_settings))
            self.assertFalse(os.path.exists(
                os.path.join(home, ".codex", "config.toml")))
            self.assertFalse(os.path.exists(
                os.path.join(home, ".claude", "settings.json")))
            self.assertTrue(os.path.isfile(os.path.join(
                home, ".claude", "plugins", "installed_plugins.json")))
            self.assertTrue(os.path.isfile(os.path.join(
                claude_seed, "installed_plugins.json")))
            with open(codex_config) as fh:
                self.assertIn('plugins."ccc@ccc-agent".enabled = true',
                              fh.read())
            with open(claude_settings) as fh:
                self.assertEqual(
                    json.load(fh),
                    {"enabledPlugins": {"ccc@ccc-agent": True}})
            with open(config_path) as fh:
                cfg = json.load(fh)
            self.assertTrue(os.path.isfile(
                cfg["mcp_client_hardening_library"]))
            self.assertEqual(cfg["agent_plugins"]["claude"]["src"], claude_seed)
            self.assertEqual(
                cfg["agent_plugins"]["claude"]["setenv"],
                {"CLAUDE_CODE_PLUGIN_SEED_DIR": claude_seed})

    def test_claude_seed_enablement_removes_only_legacy_ccc_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            settings_path = os.path.join(home, ".claude", "settings.json")
            os.makedirs(os.path.dirname(settings_path))
            legacy = {
                "extraKnownMarketplaces": {
                    "ccc-agent": {"source": {"source": "directory",
                                               "path": "/old/ccc"}},
                    "keep-market": {"source": {"source": "github",
                                                 "repo": "org/plugins"}},
                },
                "enabledPlugins": {"other@keep-market": False},
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command":
                                     "/old/claude-ccc-containment/hooks/ccc-stop-hook.sh"}]},
                        {"hooks": [{"type": "command", "command":
                                     "/keep/user-hook.sh"}]},
                    ],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command":
                                     "/old/claude-ccc-containment/hooks/ccc-context-hook.sh"}]},
                    ],
                },
            }
            with open(settings_path, "w") as fh:
                json.dump(legacy, fh)

            setup_mod.ensure_claude_seed_plugin_enabled(
                home, settings_path=settings_path)

            with open(settings_path) as fh:
                settings = json.load(fh)
            self.assertNotIn("ccc-agent", settings["extraKnownMarketplaces"])
            self.assertIn("keep-market", settings["extraKnownMarketplaces"])
            self.assertEqual(settings["enabledPlugins"], {
                "ccc@ccc-agent": True,
                "other@keep-market": False,
            })
            self.assertEqual(len(settings["hooks"]["Stop"]), 1)
            self.assertIn("/keep/user-hook.sh", json.dumps(settings["hooks"]))
            self.assertNotIn("SessionStart", settings["hooks"])
            self.assertNotIn("claude-ccc-containment/hooks/ccc-",
                             json.dumps(settings))

    def test_claude_seed_registry_initialization_preserves_other_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            seed = os.path.join(tmp, "seed")
            marketplace = os.path.join(seed, "marketplaces", "ccc-agent")
            cache = os.path.join(seed, "cache", "ccc-agent", "ccc", "0.2.0")
            os.makedirs(os.path.join(marketplace, ".claude-plugin"))
            os.makedirs(os.path.join(cache, ".claude-plugin"))
            with open(os.path.join(marketplace, ".claude-plugin",
                                   "marketplace.json"), "w") as fh:
                json.dump({"name": "ccc-agent", "plugins": []}, fh)
            with open(os.path.join(cache, ".claude-plugin", "plugin.json"), "w") as fh:
                json.dump({"name": "ccc", "version": "0.2.0"}, fh)
            with open(os.path.join(seed, "known_marketplaces.json"), "w") as fh:
                json.dump({"ccc-agent": {
                    "source": {"source": "directory", "path": "/build/path"},
                    "installLocation": "/build/path",
                }}, fh)
            with open(os.path.join(seed, "installed_plugins.json"), "w") as fh:
                json.dump({"version": 2, "plugins": {"ccc@ccc-agent": [{
                    "scope": "user", "installPath": "/build/cache",
                    "version": "0.2.0", "installedAt": "then",
                    "lastUpdated": "then",
                }]}}, fh)

            plugin_state = os.path.join(home, ".claude", "plugins")
            os.makedirs(plugin_state)
            os.chmod(os.path.join(home, ".claude"), 0o700)
            with open(os.path.join(plugin_state, "known_marketplaces.json"), "w") as fh:
                json.dump({"keep-market": {"source": {"source": "github",
                                                        "repo": "org/keep"}}}, fh)
            with open(os.path.join(plugin_state, "installed_plugins.json"), "w") as fh:
                json.dump({"version": 2, "plugins": {
                    "keep@keep-market": [{"scope": "user", "version": "1.0.0"}]
                }}, fh)

            paths = setup_mod.ensure_claude_seed_plugin_registry(home, seed)
            self.assertIsNotNone(paths)
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.join(home, ".claude")).st_mode),
                0o700)
            with open(paths[0]) as fh:
                known = json.load(fh)
            with open(paths[1]) as fh:
                installed = json.load(fh)
            self.assertIn("keep-market", known)
            self.assertEqual(
                known["ccc-agent"]["installLocation"],
                os.path.realpath(marketplace))
            self.assertIn("keep@keep-market", installed["plugins"])
            ccc_entry = installed["plugins"]["ccc@ccc-agent"][0]
            self.assertEqual(ccc_entry["installPath"], os.path.realpath(cache))
            self.assertEqual(ccc_entry["version"], "0.2.0")

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
