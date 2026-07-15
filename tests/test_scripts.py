"""Tests for the shell scaffolding: launch shim and hook adapters. All run
unprivileged (syntax and behavior checks only)."""

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest

import ccc_agent.claude_plugin as claude_plugin

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(HERE)
ASSETS = os.path.join(AGENT_DIR, "ccc_agent", "assets")
PLUGINS = os.path.join(ASSETS, "plugins")
SHIM_SH = os.path.join(ASSETS, "shims", "ccc-agent-shim.sh")
SSH_ROUTER_SH = os.path.join(ASSETS, "shims", "ccc-agent-ssh-shell-router.sh")
SOFTSANDBOX_SH = os.path.join(ASSETS, "scripts", "softsandbox.sh")
HOOKS = [os.path.join(ASSETS, "hooks", name)
         for name in ("claude-stop-hook.sh", "codex-stop-hook.sh",
                      "hermes-turn-record.sh")]
# hooks with blocking stop semantics (turn-check self-repair)
STOP_HOOKS = [os.path.join(ASSETS, "hooks", name)
              for name in ("claude-stop-hook.sh", "codex-stop-hook.sh")]
# plugin-bundled stop hooks (the auto-injected per-contained-run path)
PLUGIN_STOP_HOOKS = [
    os.path.join(PLUGINS, "claude-ccc-containment", "hooks", "ccc-stop-hook.sh"),
    os.path.join(PLUGINS, "codex-ccc-containment", "hooks", "ccc-stop-hook.sh"),
]
CODEX_WORKSPACE_HOOK = os.path.join(
    PLUGINS, "codex-ccc-containment", "hooks", "ccc-workspace-hook.sh")
CLAUDE_CONTEXT_HOOK = os.path.join(
    PLUGINS, "claude-ccc-containment", "hooks", "ccc-context-hook.sh")
HERMES_PLUGIN_INIT = os.path.join(
    PLUGINS, "hermes-ccc-containment", "__init__.py")


class TestShellSyntax(unittest.TestCase):
    def test_all_scripts_parse(self):
        for script in ([SHIM_SH, SSH_ROUTER_SH, SOFTSANDBOX_SH,
                       CLAUDE_CONTEXT_HOOK, CODEX_WORKSPACE_HOOK]
                       + HOOKS + PLUGIN_STOP_HOOKS):
            proc = subprocess.run(["bash", "-n", script],
                                  stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 0,
                             "%s: %s" % (script, proc.stderr))


class TestPluginAssets(unittest.TestCase):
    """The packaged native plugins ccc-agent run injects per contained run."""

    def test_claude_plugin_layout(self):
        marketplace_path = os.path.join(PLUGINS, ".claude-plugin", "marketplace.json")
        self.assertTrue(os.path.isfile(marketplace_path))
        with open(marketplace_path) as fh:
            marketplace = json.load(fh)
        self.assertEqual(marketplace["name"], "ccc-agent")
        self.assertEqual(marketplace["plugins"][0]["name"], "ccc")
        self.assertEqual(marketplace["plugins"][0]["source"],
                         "./claude-ccc-containment")

        root = os.path.join(PLUGINS, "claude-ccc-containment")
        manifest_path = os.path.join(root, ".claude-plugin", "plugin.json")
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["name"], "ccc")
        with open(os.path.join(root, "hooks", "hooks.json")) as fh:
            hooks = json.load(fh)
        self.assertIn("SessionStart", hooks["hooks"])
        self.assertIn("SessionEnd", hooks["hooks"])
        self.assertIn("UserPromptSubmit", hooks["hooks"])
        self.assertIn("Stop", hooks["hooks"])
        start_cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        end_cmd = hooks["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
        prompt_cmd = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        stop_cmds = [hook["command"]
                     for hook in hooks["hooks"]["Stop"][0]["hooks"]]
        self.assertIn("ccc-context-hook.sh", start_cmd)
        self.assertIn("ccc-context-hook.sh", end_cmd)
        self.assertIn("ccc-context-hook.sh", prompt_cmd)
        self.assertTrue(any("ccc-stop-hook.sh" in cmd for cmd in stop_cmds))
        self.assertTrue(any("ccc-context-hook.sh" in cmd for cmd in stop_cmds))
        self.assertTrue(os.path.isfile(
            os.path.join(root, "hooks", "ccc-stop-hook.sh")))
        self.assertTrue(os.path.isfile(
            os.path.join(root, "hooks", "ccc-context-hook.sh")))
        with open(os.path.join(root, "hooks", "ccc-context-hook.sh")) as fh:
            context_body = fh.read()
        self.assertIn("turn-add-workspace", context_body)
        self.assertIn("turn-remove-workspace", context_body)
        self.assertIn("--agent-session", context_body)

    def test_claude_plugin_materializer_writes_local_marketplace_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "ccc-claude-plugin")
            written = claude_plugin.materialize_marketplace(dest)
            self.assertEqual(written, dest)
            marketplace_path = os.path.join(dest, ".claude-plugin",
                                            "marketplace.json")
            self.assertTrue(os.path.isfile(marketplace_path))
            with open(marketplace_path) as fh:
                marketplace = json.load(fh)
            self.assertEqual(marketplace["name"], "ccc-agent")
            self.assertEqual(marketplace["plugins"][0]["source"],
                             "./claude-ccc-containment")
            self.assertTrue(os.path.isfile(os.path.join(
                dest, "claude-ccc-containment", ".claude-plugin",
                "plugin.json")))

    def test_claude_plugin_materializer_does_not_delete_existing_dest_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "existing")
            os.makedirs(dest)
            sentinel = os.path.join(dest, "keep.txt")
            with open(sentinel, "w") as fh:
                fh.write("keep")
            with self.assertRaises(FileExistsError):
                claude_plugin.materialize_marketplace(dest)
            self.assertTrue(os.path.isfile(sentinel))

    def test_claude_seed_materializer_is_self_contained_and_preserves_other_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "claude-seed")
            os.makedirs(seed)
            with open(os.path.join(seed, "known_marketplaces.json"), "w") as fh:
                json.dump({"keep-market": {"source": {"source": "github",
                                                        "repo": "org/keep"}}}, fh)
            with open(os.path.join(seed, "installed_plugins.json"), "w") as fh:
                json.dump({"version": 2, "plugins": {
                    "keep@keep-market": [{"scope": "user", "version": "1.0.0"}]
                }}, fh)

            written = claude_plugin.materialize_seed(seed)

            self.assertEqual(written, seed)
            marketplace = os.path.join(
                seed, "marketplaces", "ccc-agent", ".claude-plugin",
                "marketplace.json")
            cached_plugin = os.path.join(
                seed, "cache", "ccc-agent", "ccc", "0.2.0",
                ".claude-plugin", "plugin.json")
            self.assertTrue(os.path.isfile(marketplace))
            self.assertTrue(os.path.isfile(cached_plugin))
            with open(os.path.join(seed, "known_marketplaces.json")) as fh:
                known = json.load(fh)
            with open(os.path.join(seed, "installed_plugins.json")) as fh:
                installed = json.load(fh)
            self.assertIn("keep-market", known)
            self.assertEqual(
                known["ccc-agent"]["installLocation"],
                os.path.join(seed, "marketplaces", "ccc-agent"))
            self.assertIn("keep@keep-market", installed["plugins"])
            entry = installed["plugins"]["ccc@ccc-agent"][0]
            self.assertEqual(entry["version"], "0.2.0")
            self.assertEqual(
                entry["installPath"],
                os.path.join(seed, "cache", "ccc-agent", "ccc", "0.2.0"))

    def test_claude_plugin_cli_can_materialize_complete_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = os.path.join(tmp, "seed")
            self.assertEqual(claude_plugin.main(["--seed-dir", seed]), 0)
            self.assertTrue(os.path.isfile(os.path.join(
                seed, "installed_plugins.json")))
            self.assertTrue(os.path.isfile(os.path.join(
                seed, "marketplaces", "ccc-agent", ".claude-plugin",
                "marketplace.json")))

    def test_codex_plugin_layout(self):
        root = os.path.join(PLUGINS, "codex-ccc-containment")
        manifest_path = os.path.join(root, ".codex-plugin", "plugin.json")
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path) as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["name"], "ccc")
        with open(os.path.join(root, "hooks", "hooks.json")) as fh:
            hooks = json.load(fh)
        self.assertIn("SessionStart", hooks["hooks"])
        self.assertIn("SubagentStart", hooks["hooks"])
        self.assertIn("SubagentStop", hooks["hooks"])
        self.assertIn("Stop", hooks["hooks"])
        start_cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        subagent_start_cmd = hooks["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
        subagent_stop_cmd = hooks["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
        stop_groups = hooks["hooks"]["Stop"]
        self.assertEqual(len(stop_groups), 1)
        stop_cmd = stop_groups[0]["hooks"][0]["command"]
        self.assertEqual(stop_cmd, "${PLUGIN_ROOT}/hooks/ccc-stop-hook.sh")
        self.assertEqual(start_cmd, "${PLUGIN_ROOT}/hooks/ccc-workspace-hook.sh")
        self.assertEqual(subagent_start_cmd, "${PLUGIN_ROOT}/hooks/ccc-workspace-hook.sh")
        self.assertEqual(subagent_stop_cmd, "${PLUGIN_ROOT}/hooks/ccc-workspace-hook.sh")
        self.assertTrue(os.path.isfile(
            os.path.join(root, "hooks", "ccc-stop-hook.sh")))
        self.assertTrue(os.path.isfile(
            os.path.join(root, "hooks", "ccc-workspace-hook.sh")))
        with open(os.path.join(root, "hooks", "ccc-workspace-hook.sh")) as fh:
            workspace_body = fh.read()
        self.assertIn("turn-add-workspace", workspace_body)
        self.assertIn("turn-remove-workspace", workspace_body)
        self.assertIn("--agent-session", workspace_body)

    def test_ccc_containment_skill_is_bundled_for_claude_codex_and_hermes(self):
        bodies = []
        for plugin in ("claude-ccc-containment", "codex-ccc-containment",
                       "hermes-ccc-containment"):
            for old_name in ("branchfs-commit", "contained-commit"):
                old_path = os.path.join(PLUGINS, plugin, "skills", old_name)
                self.assertFalse(os.path.exists(old_path), old_path)
            path = os.path.join(PLUGINS, plugin, "skills", "ccc-containment",
                                "SKILL.md")
            self.assertTrue(os.path.isfile(path), path)
            with open(path) as fh:
                bodies.append(fh.read())
        self.assertEqual(len(set(bodies)), 1)
        self.assertIn("name: ccc-containment", bodies[0])
        self.assertIn("Always use this skill", bodies[0])
        self.assertIn("contained filesystem", bodies[0])
        self.assertIn("ccc_status", bodies[0])
        self.assertIn("ccc_list_kept", bodies[0])
        self.assertIn("ccc_commit_kept", bodies[0])
        self.assertIn("per-tool", bodies[0])
        self.assertIn("Do not stop active loops/goals", bodies[0])
        self.assertIn("Process-exit finalization", bodies[0])

    def test_protected_user_command_skills_are_replaced_by_mcp_instructions(self):
        removed = ("ccc-status", "ccc-commit", "ccc-discard", "ccc")
        for plugin in ("claude-ccc-containment", "codex-ccc-containment",
                       "hermes-ccc-containment"):
            for name in removed:
                path = os.path.join(PLUGINS, plugin, "skills", name,
                                    "SKILL.md")
                self.assertFalse(os.path.isfile(path), path)

    def test_hermes_plugin_layout(self):
        root = os.path.join(PLUGINS, "hermes-ccc-containment")
        self.assertTrue(os.path.isfile(os.path.join(root, "plugin.yaml")))
        with open(os.path.join(root, "plugin.yaml")) as fh:
            manifest = fh.read()
        self.assertIn("pre_llm_call", manifest)
        self.assertIn("transform_llm_output", manifest)
        self.assertIn("post_llm_call", manifest)
        self.assertIn("on_session_end", manifest)
        with open(os.path.join(root, "__init__.py")) as fh:
            src = fh.read()
        self.assertIn("def register", src)
        self.assertIn("pre_llm_call", src)
        self.assertIn("transform_llm_output", src)
        self.assertIn("turn-finalize", src)
        self.assertIn("turn-review-kept", src)
        self.assertIn("turn-add-workspace", src)
        self.assertIn("turn-remove-workspace", src)
        self.assertIn("--agent-session", src)
        self.assertIn("ccc-containment", src)
        self.assertTrue(os.path.isfile(
            os.path.join(root, "skills", "ccc-containment", "SKILL.md")))

    def test_bundled_stop_hooks_are_agent_specific(self):
        with open(PLUGIN_STOP_HOOKS[0]) as fh:
            claude_body = fh.read()
        with open(PLUGIN_STOP_HOOKS[1]) as fh:
            codex_body = fh.read()
        self.assertIn("turn-finalize --default-keep", claude_body)
        self.assertNotIn("block_once_for_kept_review", claude_body)
        self.assertIn("block_once_for_kept_review", codex_body)
        self.assertIn("turn-review-kept", codex_body)

    def test_bundled_stop_hook_keeps_codex_stdout_json_clean(self):
        # Codex treats command-hook stdout as JSON. ccc-agent's human-readable
        # text must therefore go to stderr, while exit status still carries
        # block/allow semantics.
        with open(PLUGIN_STOP_HOOKS[1]) as fh:
            body = fh.read()
        self.assertIn('"$CTL" turn-finalize --default-keep 1>&2 || rc=$?', body)
        self.assertIn('REVIEW=$("$CTL" turn-review-kept 2>&1) || review_rc=$?',
                      body)
        self.assertIn("printf '%s\\n' \"$REVIEW\" >&2", body)

    def test_codex_stop_hook_blocks_once_for_kept_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = os.path.join(tmp, "calls")
            ctl = os.path.join(tmp, "ccc-agent")
            with open(ctl, "w") as fh:
                fh.write("#!/bin/sh\n"
                         "echo \"$*\" >> \"$CCC_AGENT_TEST_CALLS\"\n"
                         "if [ \"$1\" = turn-finalize ]; then\n"
                         "  echo 'kept in branch only' 1>&2\n"
                         "  exit 0\n"
                         "fi\n"
                         "if [ \"$1\" = turn-review-kept ]; then\n"
                         "  echo 'ccc-agent: ask the user what to do' 1>&2\n"
                         "  echo '  - /storage/user/outside.txt' 1>&2\n"
                         "  exit 2\n"
                         "fi\n"
                         "exit 0\n")
            os.chmod(ctl, 0o755)
            env = {
                "PATH": "/usr/bin:/bin",
                "CCC_AGENT_SESSION": "agent-x",
                "CCC_AGENT_CONTROL_SOCK": os.path.join(tmp, "sock"),
                "CCC_AGENT_CLI": ctl,
                "CCC_AGENT_TEST_CALLS": calls,
                "TMPDIR": tmp,
            }
            proc = subprocess.run(
                ["sh", PLUGIN_STOP_HOOKS[1]], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertIn("kept in branch only", proc.stderr)
            self.assertIn("ask the user", proc.stderr)
            self.assertIn("outside.txt", proc.stderr)
            proc2 = subprocess.run(
                ["sh", PLUGIN_STOP_HOOKS[1]], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertEqual(proc2.stdout, "")
            with open(calls) as fh:
                call_log = fh.read()
            self.assertIn("turn-finalize --default-keep", call_log)
            self.assertIn("turn-review-kept", call_log)

    def test_codex_workspace_hook_adds_and_removes_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = os.path.join(tmp, "calls")
            ctl = os.path.join(tmp, "ccc-agent")
            with open(ctl, "w") as fh:
                fh.write("#!/bin/sh\n"
                         "echo \"$*\" >> \"$CCC_AGENT_TEST_CALLS\"\n"
                         "exit 0\n")
            os.chmod(ctl, 0o755)
            env = {
                "PATH": "/usr/bin:/bin",
                "CCC_AGENT_SESSION": "outer-session",
                "CCC_AGENT_CONTROL_SOCK": os.path.join(tmp, "sock"),
                "CCC_AGENT_HOOK_TOKEN": "hook-token",
                "CCC_AGENT_CLI": ctl,
                "CCC_AGENT_TEST_CALLS": calls,
            }

            start = subprocess.run(
                ["sh", CODEX_WORKSPACE_HOOK], env=env,
                input=json.dumps({"hook_event_name": "SessionStart",
                                  "session_id": "codex-inner-1",
                                  "cwd": "/storage/user/Projects/proj-a"}),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            sub_start = subprocess.run(
                ["sh", CODEX_WORKSPACE_HOOK], env=env,
                input=json.dumps({"hook_event_name": "SubagentStart",
                                  "session_id": "codex-parent",
                                  "agent_id": "sub-1",
                                  "cwd": "/storage/user/Projects/proj-b"}),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            sub_end = subprocess.run(
                ["sh", CODEX_WORKSPACE_HOOK], env=env,
                input=json.dumps({"hook_event_name": "SubagentStop",
                                  "session_id": "codex-parent",
                                  "agent_id": "sub-1",
                                  "cwd": "/storage/user/Projects/proj-b"}),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            self.assertEqual(start.returncode, 0, start.stderr)
            self.assertEqual(sub_start.returncode, 0, sub_start.stderr)
            self.assertEqual(sub_end.returncode, 0, sub_end.stderr)
            self.assertEqual(start.stdout, "")
            self.assertEqual(sub_start.stdout, "")
            self.assertEqual(sub_end.stdout, "")
            with open(calls) as fh:
                call_log = fh.read()
            self.assertIn("turn-add-workspace --agent-session codex-inner-1 /storage/user/Projects/proj-a", call_log)
            self.assertIn("turn-add-workspace --agent-session codex-parent/sub-1 /storage/user/Projects/proj-b", call_log)
            self.assertIn("turn-remove-workspace --agent-session codex-parent/sub-1 /storage/user/Projects/proj-b", call_log)

    def test_codex_workspace_hook_degrades_safe_outside_contained_session(self):
        proc = subprocess.run(
            ["sh", CODEX_WORKSPACE_HOOK],
            input=json.dumps({"hook_event_name": "SessionStart"}),
            env={"PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")


class TestClaudeContextHook(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctl = os.path.join(self._tmp.name, "ccc-agent")
        self.plugin_root = os.path.join(PLUGINS, "claude-ccc-containment")

    def tearDown(self):
        self._tmp.cleanup()

    def run_hook(self, payload, extra_env=None):
        env = {
            "PATH": "/usr/bin:/bin",
            "CCC_AGENT_SESSION": "agent-x",
            "CCC_AGENT_CLI": self.ctl,
            "CLAUDE_PLUGIN_ROOT": self.plugin_root,
        }
        env.update(extra_env or {})
        return subprocess.run(
            ["sh", CLAUDE_CONTEXT_HOOK],
            input=json.dumps(payload),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True)

    def fake_ctl_review(self, rc, text):
        with open(self.ctl, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "echo \"$*\" >> \"$CCC_AGENT_TEST_CALLS\"\n"
                     "if [ \"$1\" = turn-review-kept ]; then\n"
                     "  printf '%s\\n' %r 1>&2\n"
                     "  exit %d\n"
                     "fi\n"
                     "exit 0\n" % ("%s", text, rc))
        os.chmod(self.ctl, 0o755)

    def test_session_start_injects_ccc_commit_skill_context(self):
        proc = self.run_hook({"hook_event_name": "SessionStart",
                              "source": "startup"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        out = data["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("ccc-containment", out["additionalContext"])
        self.assertIn("ccc_status", out["additionalContext"])
        self.assertIn("contained filesystem", out["additionalContext"])

    def test_session_start_restores_stripped_outer_session_for_bash_tools(self):
        handoff = os.path.join(self._tmp.name, "session-env.json")
        claude_env = os.path.join(self._tmp.name, "claude-env.sh")
        with open(handoff, "w") as fh:
            json.dump({"CCC_AGENT_SESSION": "agent-remote-claude"}, fh)

        env = {
            "PATH": "/usr/bin:/bin",
            "CLAUDE_PLUGIN_ROOT": self.plugin_root,
            "CLAUDE_ENV_FILE": claude_env,
            "CCC_AGENT_SESSION_ENV_FILE": handoff,
        }
        proc = subprocess.run(
            ["sh", CLAUDE_CONTEXT_HOOK],
            input=json.dumps({"hook_event_name": "SessionStart",
                              "source": "startup"}),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertIn("ccc-containment",
                      data["hookSpecificOutput"]["additionalContext"])
        with open(claude_env) as fh:
            persisted = fh.read()
        self.assertIn("CCC_AGENT_SESSION=agent-remote-claude", persisted)

    def test_session_handoff_json_cannot_inject_shell_commands_or_names(self):
        handoff = os.path.join(self._tmp.name, "session-env.json")
        claude_env = os.path.join(self._tmp.name, "claude-env.sh")
        marker = os.path.join(self._tmp.name, "must-not-exist")
        session_value = "agent-x; touch %s" % marker
        with open(handoff, "w") as fh:
            json.dump({"CCC_AGENT_SESSION": session_value,
                       "UNRELATED_INJECTED_NAME": "bad"}, fh)

        proc = subprocess.run(
            ["sh", CLAUDE_CONTEXT_HOOK],
            input=json.dumps({"hook_event_name": "SessionStart",
                              "source": "startup"}),
            env={"PATH": "/usr/bin:/bin",
                 "CLAUDE_PLUGIN_ROOT": self.plugin_root,
                 "CLAUDE_ENV_FILE": claude_env,
                 "CCC_AGENT_SESSION_ENV_FILE": handoff},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(marker))
        with open(claude_env) as fh:
            persisted = fh.read()
        self.assertNotIn("UNRELATED_INJECTED_NAME", persisted)
        sourced = subprocess.run(
            ["sh", "-c", '. "$1"; printf %s "$CCC_AGENT_SESSION"',
             "sh", claude_env],
            env={"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.assertEqual(sourced.returncode, 0, sourced.stderr)
        self.assertEqual(sourced.stdout, session_value)
        self.assertFalse(os.path.exists(marker))

    def test_session_start_and_end_update_workspace_scope_with_hook_token(self):
        calls = os.path.join(self._tmp.name, "calls")
        with open(self.ctl, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "echo \"$*\" >> \"$CCC_AGENT_TEST_CALLS\"\n"
                     "exit 0\n")
        os.chmod(self.ctl, 0o755)
        env = {
            "CCC_AGENT_CONTROL_SOCK": os.path.join(self._tmp.name, "sock"),
            "CCC_AGENT_HOOK_TOKEN": "hook-token",
            "CCC_AGENT_TEST_CALLS": calls,
        }

        start = self.run_hook(
            {"hook_event_name": "SessionStart",
             "session_id": "claude-inner-1",
             "cwd": "/storage/user/Projects/proj-a"},
            extra_env=env)
        end = self.run_hook(
            {"hook_event_name": "SessionEnd",
             "session_id": "claude-inner-1",
             "cwd": "/storage/user/Projects/proj-a"},
            extra_env=env)

        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertEqual(end.returncode, 0, end.stderr)
        self.assertEqual(end.stdout, "")
        with open(calls) as fh:
            call_log = fh.read()
        self.assertIn("turn-add-workspace --agent-session claude-inner-1 /storage/user/Projects/proj-a", call_log)
        self.assertIn("turn-remove-workspace --agent-session claude-inner-1 /storage/user/Projects/proj-a", call_log)

    def test_user_prompt_submit_injects_turn_reminder(self):
        proc = self.run_hook({"hook_event_name": "UserPromptSubmit",
                              "prompt": "do work"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        out = data["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn("CCC contained-session reminder", out["additionalContext"])
        self.assertIn("ccc_status", out["additionalContext"])
        self.assertIn("human elicitation", out["additionalContext"])

    def test_stop_review_kept_continues_with_user_decision_context(self):
        calls = os.path.join(self._tmp.name, "calls")
        prompt = ("ccc-agent: 1 kept non-workspace path(s) pending.\n"
                  "ccc-agent: ask user: commit, discard, or keep pending; then run "
                  "ccc-agent turn-resolve <commit|discard|keep> --all-kept\n"
                  "ccc-agent: list paths only if needed: ccc-agent turn-kept-status --details")
        self.fake_ctl_review(2, prompt)

        proc = self.run_hook(
            {"hook_event_name": "Stop", "stop_hook_active": False},
            extra_env={"CCC_AGENT_TEST_CALLS": calls})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        out = data["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "Stop")
        self.assertIn("review is pending", out["additionalContext"])
        self.assertIn("--all-kept", out["additionalContext"])
        self.assertNotIn("outside.txt", out["additionalContext"])
        with open(calls) as fh:
            self.assertIn("turn-review-kept", fh.read())

    def test_stop_context_does_not_loop_when_stop_hook_already_active(self):
        calls = os.path.join(self._tmp.name, "calls")
        self.fake_ctl_review(2, "should not be called")

        proc = self.run_hook(
            {"hook_event_name": "Stop", "stop_hook_active": True},
            extra_env={"CCC_AGENT_TEST_CALLS": calls})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(os.path.exists(calls))


class TestHermesContainmentPlugin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctl = os.path.join(self._tmp.name, "ccc-agent")
        self.calls = os.path.join(self._tmp.name, "calls")

    def tearDown(self):
        self._tmp.cleanup()

    def load_plugin(self):
        spec = importlib.util.spec_from_file_location(
            "ccc_agent_test_hermes_plugin", HERMES_PLUGIN_INIT)
        if spec is None or spec.loader is None:
            raise AssertionError("could not load Hermes plugin")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def set_contained_env(self):
        old = dict(os.environ)
        os.environ.update({
            "CCC_AGENT_SESSION": "agent-x",
            "CCC_AGENT_CONTROL_SOCK": os.path.join(self._tmp.name, "sock"),
            "CCC_AGENT_CLI": self.ctl,
        })
        return old

    def restore_env(self, old):
        os.environ.clear()
        os.environ.update(old)

    def fake_ctl_review(self, rc=2, text=None):
        text = text or (
            "ccc-agent: 1 kept non-workspace path(s) pending.\n"
            "ccc-agent: ask user: commit, discard, or keep pending; then run "
            "ccc-agent turn-resolve <commit|discard|keep> --all-kept\n"
            "ccc-agent: list paths only if needed: ccc-agent turn-kept-status --details")
        with open(self.ctl, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "echo \"$*\" >> %r\n"
                     "if [ \"$1\" = turn-finalize ]; then\n"
                     "  exit 0\n"
                     "fi\n"
                     "if [ \"$1\" = turn-review-kept ]; then\n"
                     "  printf '%%s\\n' %r 1>&2\n"
                     "  exit %d\n"
                     "fi\n"
                     "exit 0\n" % (self.calls, text, rc))
        os.chmod(self.ctl, 0o755)

    def test_pre_llm_call_injects_first_turn_skill_context(self):
        mod = self.load_plugin()
        old = self.set_contained_env()
        try:
            self.fake_ctl_review(rc=0, text="")
            result = mod._pre_llm_context(is_first_turn=True)
        finally:
            self.restore_env(old)
        self.assertIsInstance(result, dict)
        context = result["context"]
        self.assertIn("ccc-containment", context)
        self.assertIn("ccc_status", context)
        self.assertIn("contained filesystem", context)
        self.assertIn("Hermes would otherwise idle", context)
        self.assertIn("external session review", context)

    def test_pre_llm_and_session_end_update_workspace_scope_with_hook_token(self):
        mod = self.load_plugin()
        old = self.set_contained_env()
        try:
            os.environ["CCC_AGENT_HOOK_TOKEN"] = "hook-token"
            self.fake_ctl_review(rc=0, text="")
            result = mod._pre_llm_context(
                is_first_turn=True,
                session_id="hermes-inner-1",
                cwd="/storage/user/Projects/proj-a")
            mod._signal_workspace_end(
                session_id="hermes-inner-1",
                cwd="/storage/user/Projects/proj-a")
        finally:
            self.restore_env(old)
        self.assertIsInstance(result, dict)
        with open(self.calls) as fh:
            call_log = fh.read()
        self.assertIn("turn-add-workspace --agent-session hermes-inner-1 /storage/user/Projects/proj-a", call_log)
        self.assertIn("turn-remove-workspace --agent-session hermes-inner-1 /storage/user/Projects/proj-a", call_log)

    def test_pre_llm_call_is_inert_outside_contained_session(self):
        mod = self.load_plugin()
        old = dict(os.environ)
        try:
            os.environ.pop("CCC_AGENT_SESSION", None)
            os.environ.pop("CCC_AGENT_CONTROL_SOCK", None)
            self.assertIsNone(mod._pre_llm_context(is_first_turn=True))
        finally:
            self.restore_env(old)

    def test_transform_llm_output_appends_kept_review_prompt(self):
        mod = self.load_plugin()
        old = self.set_contained_env()
        try:
            self.fake_ctl_review()
            result = mod._append_review_to_response("Done.", turn_id="turn-1")
        finally:
            self.restore_env(old)
        self.assertIn("Done.", result)
        self.assertIn("CCC contained-session review is pending", result)
        self.assertIn("--all-kept", result)
        self.assertIn("turn-kept-status --details", result)
        self.assertNotIn("outside.txt", result)
        with open(self.calls) as fh:
            call_log = fh.read()
        self.assertIn("turn-finalize --default-keep", call_log)
        self.assertIn("turn-review-kept", call_log)

    def test_transform_llm_output_does_not_repeat_same_review(self):
        mod = self.load_plugin()
        old = self.set_contained_env()
        try:
            self.fake_ctl_review()
            first = mod._append_review_to_response("Done.", turn_id="turn-1")
            second = mod._append_review_to_response("Still done.", turn_id="turn-2")
        finally:
            self.restore_env(old)
        self.assertIn("CCC contained-session review is pending", first)
        self.assertIsNone(second)

    def test_registers_hermes_context_and_idle_hooks(self):
        class FakeCtx(object):
            def __init__(self):
                self.hooks = []
                self.injected = []

            def register_hook(self, name, callback):
                self.hooks.append((name, callback))

            def inject_message(self, content, role="user"):
                self.injected.append((role, content))
                return True

        mod = self.load_plugin()
        ctx = FakeCtx()
        mod.register(ctx)
        self.assertEqual(
            [name for name, _cb in ctx.hooks],
            ["pre_llm_call", "transform_llm_output", "post_llm_call",
             "on_session_end"])


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
            fh.write("#!/bin/sh\necho LAUNCH:$*\necho UNDERLYING_PATH:${CCC_AGENT_SHIM_UNDERLYING_PATH:-}\n")
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
        self.assertIn("LAUNCH:run --agent codex -- codex do thing",
                      proc.stdout)
        self.assertIn("redirect active", proc.stderr)

    def test_shim_does_not_preflight_missing_underlying_agent(self):
        os.unlink(self.real)

        proc = self.run_shim()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LAUNCH:run --agent codex -- codex do thing",
                      proc.stdout)
        self.assertNotIn("no real", proc.stderr)

    def test_missing_underlying_agent_fails_from_contained_exec(self):
        os.unlink(self.real)
        with open(self.launcher, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "while [ \"$1\" != -- ]; do shift; done\n"
                     "shift\n"
                     "CCC_AGENT_SESSION=agent-x\n"
                     "export CCC_AGENT_SESSION\n"
                     "PATH=\"$CCC_AGENT_SHIM_UNDERLYING_PATH\"\n"
                     "export PATH\n"
                     "exec \"$@\"\n")
        os.chmod(self.launcher, 0o755)

        proc = self.run_shim()

        self.assertEqual(proc.returncode, 127)
        self.assertNotIn("no real", proc.stderr)
        self.assertNotIn("LAUNCH:", proc.stdout)

    def test_shim_exports_path_without_itself_for_contained_agent_lookup(self):
        proc = self.run_shim()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("redirect active", proc.stderr)
        self.assertIn("UNDERLYING_PATH:%s:/usr/bin:/bin" % self.realdir,
                      proc.stdout)

    def test_redirect_does_not_hardcode_user_local_bin_when_not_on_path(self):
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
            self.assertIn("LAUNCH:run --agent %s -- %s do thing" % (agent, agent),
                          proc.stdout)

    def test_nested_session_runs_real_binary_directly(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SESSION": "agent-x"})
        self.assertIn("REAL:", proc.stdout)
        self.assertIn("do thing", proc.stdout)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                         proc.stdout)
        self.assertNotIn("disabling Codex inner sandbox", proc.stderr)
        self.assertNotIn("LAUNCH:", proc.stdout)

    def test_nested_session_respects_explicit_codex_no_sandbox_arg(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SESSION": "agent-x"},
                             args=("--sandbox", "danger-full-access", "do"))
        self.assertIn("REAL:", proc.stdout)
        self.assertIn("--sandbox danger-full-access do", proc.stdout)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                         proc.stdout)

    def test_nested_session_preserves_explicit_codex_sandbox_arg(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SESSION": "agent-x"},
                             args=("--sandbox", "workspace-write", "do"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("REAL:", proc.stdout)
        self.assertIn("--sandbox workspace-write do", proc.stdout)

    def test_nested_session_respects_explicit_codex_yolo_arg(self):
        proc = self.run_shim(env_extra={"CCC_AGENT_SESSION": "agent-x"},
                             args=("--yolo", "do"))
        self.assertIn("REAL:", proc.stdout)
        self.assertIn("--yolo do", proc.stdout)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                         proc.stdout)

    def test_nested_non_codex_agent_runs_without_codex_sandbox_arg(self):
        real_claude = os.path.join(self.realdir, "claude")
        with open(real_claude, "w") as fh:
            fh.write("#!/bin/sh\necho CLAUDE-REAL:$0:$*\n")
        os.chmod(real_claude, 0o755)
        proc = subprocess.run(["claude", "do", "thing"],
                              env=dict(self.env, CCC_AGENT_SESSION="agent-x"),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CLAUDE-REAL:", proc.stdout)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox",
                         proc.stdout)
        self.assertNotIn("LAUNCH:", proc.stdout)

    def test_nested_session_uses_exported_unshimmed_conda_path(self):
        conda = os.path.join(self._tmp.name, "conda", "bin")
        os.makedirs(conda)
        conda_codex = os.path.join(conda, "codex")
        with open(conda_codex, "w") as fh:
            fh.write("#!/bin/sh\necho CONDA-REAL:$0:$*\n")
        os.chmod(conda_codex, 0o755)

        env = dict(self.env)
        env.update({
            "CCC_AGENT_SESSION": "agent-x",
            "CCC_AGENT_SHIM_UNDERLYING_PATH": "%s:/usr/bin:/bin" % conda,
        })
        proc = subprocess.run(["codex", "do", "thing"], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CONDA-REAL:%s:" % conda_codex, proc.stdout)
        self.assertNotIn("REAL:%s:" % self.real, proc.stdout)

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


class TestSshShellRouter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(self.bin)
        self.home = os.path.join(tmp, "home")
        os.makedirs(self.home)
        self.launcher = os.path.join(self.bin, "ccc-agent")
        with open(self.launcher, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "i=0\n"
                     "for arg in \"$@\"; do\n"
                     "  i=$((i + 1))\n"
                     "  printf 'ARG%d:%s\\n' \"$i\" \"$arg\"\n"
                     "done\n"
                     "printf 'ORIG:%s\\n' \"${CCC_AGENT_SSH_ORIGINAL_COMMAND:-}\"\n"
                     "printf 'UNDERLYING:%s\\n' \"${CCC_AGENT_SHIM_UNDERLYING_PATH:-}\"\n")
        os.chmod(self.launcher, 0o755)
        self.shimdir = os.path.join(self._tmp.name, "shims")
        self.realdir = os.path.join(self._tmp.name, "real")
        os.makedirs(self.shimdir)
        os.makedirs(self.realdir)
        self.real_shell = os.path.join(self.bin, "real-shell")
        with open(self.real_shell, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "i=0\n"
                     "for arg in \"$@\"; do\n"
                     "  i=$((i + 1))\n"
                     "  printf 'SHELLARG%d:%s\\n' \"$i\" \"$arg\"\n"
                     "done\n")
        os.chmod(self.real_shell, 0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def run_router(self, command, enabled=True, extra_env=None):
        env = {
            "PATH": "%s:/usr/bin:/bin" % self.bin,
            "HOME": self.home,
            "CCC_AGENT_CLI": self.launcher,
            "CCC_AGENT_REAL_SHELL": self.real_shell,
            "CCC_AGENT_ENABLE_SHIMS": "1" if enabled else "0",
        }
        env.update(extra_env or {})
        return subprocess.run([SSH_ROUTER_SH, "-c", command], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              text=True)

    def assert_routed(self, command, agent):
        proc = self.run_router(command)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("ARG1:run", proc.stdout)
        self.assertIn("ARG2:--serve", proc.stdout)
        self.assertIn("ARG3:%s" % agent, proc.stdout)
        self.assertIn("ARG4:--lifecycle", proc.stdout)
        self.assertIn("ARG5:adaptive", proc.stdout)
        self.assertIn("ARG6:--", proc.stdout)
        self.assertIn("ARG7:%s" % self.real_shell, proc.stdout)
        self.assertIn("ARG8:-c", proc.stdout)
        self.assertIn("ARG9:%s" % command, proc.stdout)
        self.assertIn("ORIG:%s" % command, proc.stdout)

    def test_routes_direct_claude_codex_and_hermes_commands(self):
        self.assert_routed("claude --app", "claude")
        self.assert_routed("codex exec task", "codex")
        self.assert_routed("hermes chat", "hermes")

    def test_routes_absolute_agent_paths(self):
        self.assert_routed("/home/domen/.local/bin/claude --version", "claude")
        self.assert_routed("/storage/user/conda-envs/codex/bin/codex --help", "codex")

    def test_routed_commands_export_unshimmed_path_for_contained_lookup(self):
        proc = self.run_router(
            "claude --app",
            extra_env={
                "PATH": "%s:%s:%s:/usr/bin:/bin" % (
                    self.shimdir, self.realdir, self.bin),
                "CCC_AGENT_SHIM_DIR": self.shimdir,
            },
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ARG1:run", proc.stdout)
        self.assertIn("UNDERLYING:%s:%s:/usr/bin:/bin" %
                      (self.realdir, self.bin), proc.stdout)
        self.assertNotIn("UNDERLYING:%s:" % self.shimdir, proc.stdout)

    def test_routes_claude_remote_server_and_cli_paths(self):
        for operation in (
                "--version",
                "--install --cli-dir /home/domen/.claude/remote/ccd-cli",
                "--stop --socket /home/domen/.claude/remote/run/x/rpc.sock",
                "--serve --socket /home/domen/.claude/remote/run/x/rpc.sock",
                "--bridge --socket /home/domen/.claude/remote/run/x/rpc.sock"):
            self.assert_routed(
                "/home/domen/.claude/remote/srv/abc123/server %s" % operation,
                "claude")
        self.assert_routed(
            "bash -lc '/home/domen/.claude/remote/ccd-cli/2.1.202 --continue'",
            "claude")

    def test_routes_absolute_hermes_path(self):
        self.assert_routed("/home/domen/.local/bin/hermes --version", "hermes")

    def test_routes_codex_state_executables(self):
        self.assert_routed(
            "/home/domen/.codex/remote/exec-server --stdio", "codex")

    def test_routes_codex_app_server_after_path_prepending_snippet(self):
        command = 'PATH="${CODEX_INSTALL_PATH:-$HOME/.local/bin}:$PATH"; export PATH; codex app-server proxy'
        self.assert_routed(command, "codex")

    def test_routes_codex_app_server_inside_shell_after_path_prepending_snippet(self):
        command = "bash -lc 'PATH=\"${CODEX_INSTALL_PATH:-$HOME/.local/bin}:$PATH\"; export PATH; codex app-server proxy'"
        self.assert_routed(command, "codex")

    def test_routes_codex_app_server_in_shell_positional_payload(self):
        command = (
            "sh -c 'CODEX_REMOTE_PAYLOAD=\"$1\"; export CODEX_REMOTE_PAYLOAD; "
            "exec \"$SHELL\" -l -i -c '\"'\"'exec /bin/sh -c \"$CODEX_REMOTE_PAYLOAD\"'\"'\"'' "
            "sh 'PATH=\"${CODEX_INSTALL_PATH:-$HOME/.local/bin}:$PATH\"; export PATH; codex app-server proxy'"
        )
        self.assert_routed(command, "codex")

    def test_does_not_route_mentions_that_are_not_executables(self):
        for command in (
                "grep claude ~/.claude/remote/run/log",
                "echo codex app-server proxy",
                "printf '%s\\n' hermes"):
            proc = self.run_router(command)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("SHELLARG1:-c", proc.stdout)
            self.assertIn("SHELLARG2:%s" % command, proc.stdout)
            self.assertNotIn("ARG1:run", proc.stdout)

    def test_disabled_or_nested_sessions_pass_through(self):
        proc = self.run_router("claude --app", enabled=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SHELLARG1:-c", proc.stdout)
        self.assertIn("SHELLARG2:claude --app", proc.stdout)
        self.assertNotIn("ARG1:run", proc.stdout)

        proc = self.run_router("claude --app", extra_env={"CCC_AGENT_SESSION": "agent-x"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SHELLARG1:-c", proc.stdout)
        self.assertIn("SHELLARG2:claude --app", proc.stdout)
        self.assertNotIn("ARG1:run", proc.stdout)


class TestStopHookSelfRepair(unittest.TestCase):
    """turn-check wiring: exit 2 blocks the stop so the agent can
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
                     "if [ \"$1\" = turn-check ]; then exit %d; fi\n"
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
            self.assertIn("turn-check", proc.stderr)
            self.assertNotIn("turn-record", proc.stdout + proc.stderr)

    def test_clean_check_reports_turn_and_exits_zero(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(0)
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (hook, proc.stderr))
            self.assertIn("CALLED turn-record", proc.stdout)

    def test_ctl_failure_never_blocks_stop(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(1)  # e.g. ControlError from a racing finalize
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (hook, proc.stderr))
            self.assertIn("CALLED turn-record", proc.stdout)


class TestStopHookControlSocket(unittest.TestCase):
    """When CCC_AGENT_CONTROL_SOCK is set the hook signals the supervisor via
    `ccc-agent turn-finalize` and propagates its exit code, instead of the
    store-based self-repair path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctl = os.path.join(self._tmp.name, "ccc-agent")

    def tearDown(self):
        self._tmp.cleanup()

    def fake_ctl(self, finalize_rc):
        with open(self.ctl, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "echo \"CALLED $*\" 1>&2\n"
                     "if [ \"$1\" = turn-finalize ]; then\n"
                     "  if [ \"${2:-}\" = --default-keep ]; then exit 0; fi\n"
                     "  exit %d\n"
                     "fi\n"
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
            self.assertIn("CALLED turn-finalize --default-keep", proc.stderr)
            self.assertNotIn("turn-check", proc.stderr)

    def test_default_keep_lets_stop_continue_instead_of_blocking(self):
        for hook in STOP_HOOKS:
            self.fake_ctl(2)
            proc = self.run_hook(hook)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (hook, proc.stderr))
            self.assertIn("CALLED turn-finalize --default-keep", proc.stderr)


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
