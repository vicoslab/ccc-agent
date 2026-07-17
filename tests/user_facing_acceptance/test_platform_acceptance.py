import json
import os
import tempfile
import unittest
from unittest import mock

from tests.user_facing_acceptance.platform import (
    PlatformAcceptanceError,
    PlatformAcceptanceRunner,
    _deployment_integration_problems,
    load_platform_manifest,
    main,
)


class TestPlatformManifest(unittest.TestCase):
    def test_loads_dedicated_test_root_and_required_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "platform.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "ccc_agent": "/usr/local/bin/ccc-agent",
                    "ccc_agent_config": "/etc/ccc-agent/config.json",
                    "codex_command": "/opt/codex/bin/codex",
                    "test_root": "/storage/user/ccc-agent-acceptance-platform",
                    "artifacts_dir": os.path.join(tmp, "artifacts"),
                }, handle)

            manifest = load_platform_manifest(path)

            self.assertEqual(
                manifest.test_root,
                "/storage/user/ccc-agent-acceptance-platform")
            self.assertEqual(manifest.ccc_agent, "/usr/local/bin/ccc-agent")

    def test_rejects_manifest_without_codex_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "platform.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "ccc_agent": "/usr/local/bin/ccc-agent",
                    "ccc_agent_config": "/etc/ccc-agent/config.json",
                    "test_root": "/storage/user/ccc-agent-acceptance-platform",
                }, handle)

            with self.assertRaisesRegex(
                    PlatformAcceptanceError, "codex_command"):
                load_platform_manifest(path)

    def test_rejects_broad_test_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "platform.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "ccc_agent": "/usr/local/bin/ccc-agent",
                    "ccc_agent_config": "/etc/ccc-agent/config.json",
                    "codex_command": "/opt/codex/bin/codex",
                    "test_root": "/storage/user",
                }, handle)

            with self.assertRaisesRegex(
                    PlatformAcceptanceError, "dedicated.*acceptance"):
                load_platform_manifest(path)


class TestDeploymentIntegration(unittest.TestCase):
    def test_requires_configured_hardening_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.so")
            self.assertTrue(_deployment_integration_problems({
                "mcp_client_hardening_library": missing,
            }))

    def test_accepts_readable_executable_hardening_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = os.path.join(tmp, "hardening.so")
            with open(library, "wb") as handle:
                handle.write(b"test")
            os.chmod(library, 0o555)
            self.assertEqual(_deployment_integration_problems({
                "mcp_client_hardening_library": library,
            }), [])


class TestPlatformMain(unittest.TestCase):
    def test_main_runs_manifest_and_prints_json_result(self):
        manifest = object()
        runner = mock.Mock()
        runner.run.return_value = {"ok": True, "run_id": "r"}
        with mock.patch(
                "tests.user_facing_acceptance.platform.load_platform_manifest",
                return_value=manifest) as load, mock.patch(
                    "tests.user_facing_acceptance.platform.PlatformAcceptanceRunner",
                    return_value=runner), mock.patch("builtins.print") as output:
            rc = main(["/tmp/platform.json"])

        self.assertEqual(rc, 0)
        load.assert_called_once_with("/tmp/platform.json")
        runner.run.assert_called_once_with()
        self.assertTrue(output.called)


class TestPlatformAcceptanceContract(unittest.TestCase):
    def test_ccc_places_config_before_command_separator(self):
        runner = object.__new__(PlatformAcceptanceRunner)
        runner.manifest = mock.Mock(
            ccc_agent="/usr/local/bin/ccc-agent",
            ccc_agent_config="/etc/ccc-agent/config.json")

        command = runner._ccc(
            "run", "--workspace", "/storage/user/w", "--", "bash", "-lc", "true")

        self.assertEqual(command[:4], [
            "/usr/local/bin/ccc-agent", "run", "--config",
            "/etc/ccc-agent/config.json"])
        self.assertEqual(command[-3:], ["bash", "-lc", "true"])

    def test_serve_places_agent_before_config(self):
        runner = object.__new__(PlatformAcceptanceRunner)
        runner.manifest = mock.Mock(
            ccc_agent="/usr/local/bin/ccc-agent",
            ccc_agent_config="/etc/ccc-agent/config.json")

        command = runner._serve(
            "codex", "--workspace", "/storage/user/w",
            "--", "codex", "app-server", "--stdio")

        self.assertEqual(command[:5], [
            "/usr/local/bin/ccc-agent", "serve", "codex", "--config",
            "/etc/ccc-agent/config.json"])

    def test_expected_checks_cover_branch_functionality(self):
        expected = set(PlatformAcceptanceRunner.CHECKS)

        self.assertTrue({
            "foreground-review-boundary",
            "serve-protocol-cleanliness",
            "bound-proc-routing-fallback",
            "review-accept",
            "review-abort",
            "session-cleanup",
            "package-assets",
            "codex-app-server-mcp",
            "claude-plugin-mcp",
        }.issubset(expected))

    def test_codex_protocol_probe_requires_initialized_ccc_tool_inventory(self):
        tools = {name: {"name": name} for name in
                 PlatformAcceptanceRunner.CCC_MCP_TOOLS}
        good = {
            "id": 2,
            "result": {"data": [{
                "name": "ccc",
                "serverInfo": {"name": "ccc-agent", "version": "1"},
                "tools": tools,
            }]},
        }
        self.assertIsNone(
            PlatformAcceptanceRunner._codex_inventory_problem(good, ""))
        missing_tools = json.loads(json.dumps(good))
        missing_tools["result"]["data"][0]["tools"].pop("ccc_status")
        problem = PlatformAcceptanceRunner._codex_inventory_problem(
            missing_tools, "")
        self.assertIn("missing tools", problem or "")
        static_listing = "Name Command Args\nccc ccc-agent mcp-server --client codex\n"
        problem = PlatformAcceptanceRunner._codex_inventory_problem(
            static_listing, "")
        self.assertIn("protocol response", problem or "")

    def test_codex_status_call_requires_successful_structured_result(self):
        good = {"id": 4, "result": {"structuredContent": {
            "kept_count": 0, "committed_count": 0}}}
        self.assertIsNone(
            PlatformAcceptanceRunner._codex_status_problem(good))
        error = {"id": 4, "error": {"code": -32001,
                                      "message": "already pinned"}}
        self.assertIn("already pinned",
                      PlatformAcceptanceRunner._codex_status_problem(error) or "")

    def test_claude_probe_requires_connected_plugin_mcp(self):
        good = ("Checking MCP server health…\n"
                "plugin:ccc:ccc: ccc-agent mcp-server --client claude - "
                "✔ Connected\n")
        self.assertIsNone(PlatformAcceptanceRunner._claude_probe_problem(good, ""))
        registration = PlatformAcceptanceRunner._claude_probe_problem(
            good, "initial client/workspace registration unavailable")
        self.assertIn("registration failed", registration or "")
        disconnected = good.replace("✔ Connected", "✘ Failed to connect")
        problem = PlatformAcceptanceRunner._claude_probe_problem(disconnected, "")
        self.assertIn("did not connect", problem or "")

    def test_session_selection_requires_one_new_expected_kind(self):
        before = {"old"}
        sessions = [
            {"session_id": "old", "agent_kind": "command"},
            {"session_id": "new", "agent_kind": "codex-remote"},
        ]

        selected = PlatformAcceptanceRunner.select_new_session(
            before, sessions, "codex-remote")

        self.assertEqual(selected["session_id"], "new")

    def test_session_selection_rejects_ambiguous_matches(self):
        sessions = [
            {"session_id": "a", "agent_kind": "codex-remote"},
            {"session_id": "b", "agent_kind": "codex-remote"},
        ]

        with self.assertRaisesRegex(PlatformAcceptanceError, "exactly one"):
            PlatformAcceptanceRunner.select_new_session(
                set(), sessions, "codex-remote")


if __name__ == "__main__":
    unittest.main()
