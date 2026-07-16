import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest

from tests.user_facing_acceptance.harness import (
    AGENTS,
    TRANSPORTS,
    AcceptanceError,
    AcceptanceManifest,
    AcceptanceRunner,
    SessionRegistry,
    TmuxDriver,
    build_scenario,
    render_command,
    visible_to_underlay,
)


class TestCommandRendering(unittest.TestCase):
    def test_renders_literal_and_shell_quoted_placeholders(self):
        context = {"workspace": "/storage/user/a b", "prompt": "say 'hello'"}

        rendered = render_command(
            ["tool", "{workspace}", "sh -lc {prompt_shell}"], context)

        self.assertEqual(rendered[0:2], ["tool", "/storage/user/a b"])
        self.assertEqual(rendered[2], "sh -lc %s" % shlex.quote(context["prompt"]))

    def test_unknown_placeholder_is_rejected(self):
        with self.assertRaises(AcceptanceError):
            render_command(["tool", "{missing}"], {})

    def test_doubled_braces_preserve_vendor_shell_expansions(self):
        rendered = render_command(
            ["sh", "-lc", "PATH=${{HOME}}/.local/bin:$PATH; codex"], {})

        self.assertEqual(
            rendered[2], "PATH=${HOME}/.local/bin:$PATH; codex")


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "acceptance.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def complete_manifest(self):
        agents = {}
        for agent in AGENTS:
            agents[agent] = {
                "transports": {
                    "local-cli": {
                        "driver": "tmux",
                        "command": ["ccc-agent", "run", "--agent", agent,
                                    "--workspace", "{workspace}", "--", agent],
                        "expected_agent_kind": agent,
                    },
                    "ssh-cli": {
                        "driver": "tmux",
                        "command": ["ssh", "-tt", "host",
                                    "cd {workspace_shell} && %s" % agent],
                        "expected_agent_kind": agent + "-remote",
                    },
                    "remote-server": {
                        "driver": "external",
                        "command": ["driver", agent, "{scenario_file}"],
                        "expected_agent_kind": agent + "-remote",
                    },
                }
            }
        return {
            "ccc_agent": "/usr/bin/ccc-agent",
            "ccc_agent_config": "/etc/ccc-agent/config.json",
            "test_root": "/storage/user/ccc-agent-acceptance",
            "artifacts_dir": os.path.join(self.tmp.name, "artifacts"),
            "agents": agents,
        }

    def test_full_manifest_requires_every_agent_transport_pair(self):
        data = self.complete_manifest()
        self.write(data)

        manifest = AcceptanceManifest.load(self.path, level="full")

        self.assertEqual(set(manifest.agents), set(AGENTS))
        for agent in AGENTS:
            self.assertEqual(
                set(manifest.agent(agent)["transports"]), set(TRANSPORTS))

    def test_full_manifest_rejects_missing_server_driver(self):
        data = self.complete_manifest()
        del data["agents"]["hermes"]["transports"]["remote-server"]
        self.write(data)

        with self.assertRaisesRegex(AcceptanceError, "hermes.*remote-server"):
            AcceptanceManifest.load(self.path, level="full")

    def test_core_manifest_requires_only_local_cli(self):
        data = self.complete_manifest()
        for entry in data["agents"].values():
            entry["transports"] = {
                "local-cli": entry["transports"]["local-cli"]
            }
        self.write(data)

        manifest = AcceptanceManifest.load(self.path, level="core")

        self.assertEqual(manifest.required_transports, ("local-cli",))

    def test_repository_example_is_a_valid_full_manifest(self):
        example = os.path.join(
            os.path.dirname(__file__), "acceptance.example.json")

        manifest = AcceptanceManifest.load(example, level="full")

        self.assertEqual(manifest.required_transports, TRANSPORTS)
        scenario = build_scenario(
            manifest.test_root, "codex", "local-cli", run_id="render-check")
        context = {
            **scenario.to_dict(),
            "scenario_file": "/tmp/scenario.json",
            "ccc_agent": manifest.ccc_agent,
            "ccc_agent_config": manifest.ccc_agent_config,
        }
        for agent in AGENTS:
            self.assertEqual(
                set(manifest.agent(agent)["transports"]), set(TRANSPORTS))
            for transport in TRANSPORTS:
                rendered = render_command(
                    manifest.transport(agent, transport)["command"], context)
                self.assertTrue(rendered)
                self.assertFalse(any("{" in item or "}" in item
                                     for item in rendered))

    def test_manifest_rejects_unknown_final_review_action(self):
        data = self.complete_manifest()
        data["agents"]["codex"]["transports"]["local-cli"][
            "final_review_action"] = "delete-everything"
        self.write(data)

        with self.assertRaisesRegex(AcceptanceError, "final_review_action"):
            AcceptanceManifest.load(self.path, level="full")


class TestPreflight(unittest.TestCase):
    def test_rejects_unreplaced_driver_before_matrix_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugins = {}
            for agent in AGENTS:
                path = os.path.join(tmp, "plugin-" + agent)
                os.makedirs(path)
                plugins[agent] = {"src": path}
            config_path = os.path.join(tmp, "config.json")
            config = {
                "backend": "branchfs",
                "confinement": "bwrap",
                "branchfs_bin": "/bin/true",
                "bwrap_bin": "/bin/true",
                "state_dir": os.path.join(tmp, "state"),
                "agent_plugins": plugins,
                "roots": [
                    {
                        "name": "tmp",
                        "visible": tmp,
                        "base": os.path.join(tmp, "underlay"),
                        "store": os.path.join(tmp, "store"),
                    }
                ],
            }
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(config, handle)
            agents = {}
            for agent in AGENTS:
                agents[agent] = {"transports": {}}
                for transport in TRANSPORTS:
                    command = ["/bin/true"]
                    if agent == "codex" and transport == "remote-server":
                        command = ["REPLACE_WITH_CODEX_DRIVER"]
                    agents[agent]["transports"][transport] = {
                        "driver": "external" if transport == "remote-server"
                        else "tmux",
                        "command": command,
                        "expected_agent_kind": agent,
                    }
            manifest = AcceptanceManifest(
                path=os.path.join(tmp, "manifest.json"),
                ccc_agent="/bin/true",
                ccc_agent_config=config_path,
                test_root=os.path.join(tmp, "ccc-agent-acceptance"),
                artifacts_dir=os.path.join(tmp, "artifacts"),
                agents=agents,
                required_transports=TRANSPORTS,
                timeout_seconds=1,
                poll_seconds=0.01,
            )
            runner = AcceptanceRunner(manifest)

            with self.assertRaisesRegex(
                    AcceptanceError, "still contains REPLACE_WITH_"):
                runner.preflight()


class TestScenario(unittest.TestCase):
    def test_scenario_uses_separate_workspace_outside_and_deny_paths(self):
        scenario = build_scenario(
            test_root="/storage/user/acceptance", agent="codex",
            transport="local-cli", run_id="run-123")

        self.assertTrue(scenario.workspace.startswith("/storage/user/acceptance/"))
        self.assertFalse(scenario.outside_dir.startswith(scenario.workspace + os.sep))
        self.assertTrue(scenario.deny_file.startswith(scenario.workspace + os.sep))
        self.assertIn("commit", scenario.decision_prompt.lower())
        self.assertIn("discard", scenario.decision_prompt.lower())
        self.assertIn("keep", scenario.decision_prompt.lower())
        self.assertIn("Do not call ccc-agent turn-finalize", scenario.initial_prompt)
        self.assertNotIn(scenario.review_marker, scenario.initial_prompt)
        self.assertNotIn(scenario.status_marker, scenario.decision_prompt)


class TestPathMapping(unittest.TestCase):
    def test_maps_visible_path_to_real_underlay(self):
        roots = [{"visible": "/storage/user", "base": "/srv/nfs/user"}]

        actual = visible_to_underlay(
            "/storage/user/project/result.txt", roots)

        self.assertEqual(actual, "/srv/nfs/user/project/result.txt")

    def test_rejects_path_outside_protected_roots(self):
        with self.assertRaises(AcceptanceError):
            visible_to_underlay("/tmp/result.txt", [
                {"visible": "/storage/user", "base": "/srv/nfs/user"}
            ])


class TestSessionRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = SessionRegistry(self.tmp.name)

    def write_session(self, sid, **updates):
        data = {
            "session_id": sid,
            "owner": "domen",
            "agent_kind": "codex",
            "agent_command": ["codex"],
            "workspace": "/storage/user/project",
            "policy": {},
            "protected_roots": {},
            "state": "running",
            "created_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "exit_status": None,
            "completion": "process-exit",
            "events": [],
            "repair_attempts": 0,
        }
        data.update(updates)
        directory = os.path.join(self.tmp.name, sid, "session")
        os.makedirs(directory)
        with open(os.path.join(directory, "session.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_discovers_only_new_matching_session(self):
        self.write_session("old", agent_kind="claude")
        before = self.registry.ids()
        self.write_session("new", agent_kind="codex-remote")

        session = self.registry.find_new(
            before, expected_agent_kind="codex-remote",
            workspace="/storage/user/project")

        self.assertEqual(session["session_id"], "new")

    def test_event_helpers_use_persisted_event_names(self):
        self.write_session("s", events=[
            {"event": "turn-committed"},
            {"event": "turn-default-kept"},
        ])

        self.assertTrue(self.registry.has_events(
            self.registry.load("s"), {"turn-committed", "turn-default-kept"}))
        self.assertFalse(self.registry.has_events(
            self.registry.load("s"), {"turn-kept-review-requested"}))


class TestExternalDriverHandshake(unittest.TestCase):
    def runner(self):
        runner = object.__new__(AcceptanceRunner)
        runner.manifest = AcceptanceManifest(
            path="/tmp/manifest.json",
            ccc_agent="/bin/true",
            ccc_agent_config="/tmp/config.json",
            test_root="/tmp/ccc-agent-acceptance",
            artifacts_dir="/tmp/ccc-agent-acceptance-artifacts",
            agents={},
            required_transports=(),
            timeout_seconds=0.05,
            poll_seconds=0.005,
        )
        return runner

    def completed_process(self):
        proc = subprocess.Popen(["/bin/true"])
        proc.wait()
        return proc

    def test_accepts_only_the_requested_atomic_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "result.json")
            with open(result, "w", encoding="utf-8") as handle:
                json.dump({"phase": "first-turn-ready", "first_response": "ok"},
                          handle)

            value = self.runner()._wait_external_phase(
                result, "first-turn-ready", self.completed_process())

            self.assertEqual(value["first_response"], "ok")

    def test_rejects_driver_that_exits_before_requested_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "result.json")
            with open(result, "w", encoding="utf-8") as handle:
                json.dump({"phase": "complete"}, handle)

            with self.assertRaisesRegex(AcceptanceError, "first-turn-ready"):
                self.runner()._wait_external_phase(
                    result, "first-turn-ready", self.completed_process())


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TestTmuxDriver(unittest.TestCase):
    def test_starts_sends_captures_and_exits_a_real_tty_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = TmuxDriver(
                "ccc-harness-%d" % os.getpid(), ["/bin/sh"], tmp)
            driver.start()
            self.addCleanup(driver.kill)

            driver.send("printf 'CCC_TMUX_DRIVER_OK\\n'")
            deadline = time.monotonic() + 5
            output = ""
            while time.monotonic() < deadline:
                output = driver.capture()
                if "CCC_TMUX_DRIVER_OK" in output:
                    break
                time.sleep(0.05)
            self.assertIn("CCC_TMUX_DRIVER_OK", output)

            driver.send("exit")
            self.assertTrue(driver.wait_exit(5, 0.05))


if __name__ == "__main__":
    unittest.main()
