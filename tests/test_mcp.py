"""Agent-facing stdio MCP protocol and fail-closed approval tests."""

import io
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

from ccc_agent import control as control_mod
from ccc_agent import mcp
from ccc_agent.control import ControlServer, MCPControlClient


class FakeControl(object):
    def __init__(self):
        self.calls = []
        self.kept = ["/storage/user/outside.txt"]

    def admit(self, client):
        self.calls.append(("admit", client))
        return {"admitted": True}

    def kept_status(self):
        self.calls.append(("status",))
        return {"verdict": "kept-status", "kept": list(self.kept),
                "count": len(self.kept), "committed_count": 2}

    def resolve_turn(self, decision, paths):
        self.calls.append(("resolve", decision, list(paths)))
        return {"verdict": decision, decision + "ted": list(paths)}

    def close(self):
        pass


def rpc(req_id, method, params=None):
    obj = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        obj["params"] = params
    return json.dumps(obj) + "\n"


class TestMCPProtocol(unittest.TestCase):
    def run_server(self, lines, client="claude"):
        reader = io.StringIO("".join(lines))
        writer = io.StringIO()
        control = FakeControl()
        server = mcp.MCPServer(reader, writer, control, client=client)
        server.run()
        return [json.loads(line) for line in writer.getvalue().splitlines()], control

    def initialize(self, capabilities=None):
        return rpc(1, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": capabilities or {},
            "clientInfo": {"name": "test", "version": "1"},
        })

    def test_initialize_and_tools_list(self):
        output, control = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/list", {}),
        ])
        self.assertEqual(output[0]["id"], 1)
        self.assertIn("tools", output[0]["result"]["capabilities"])
        self.assertEqual(control.calls[0], ("admit", "claude"))
        tools = {tool["name"]: tool for tool in output[1]["result"]["tools"]}
        self.assertEqual(set(tools), {"ccc_status", "ccc_list_kept",
                                      "ccc_commit_kept", "ccc_discard_kept",
                                      "ccc_keep_kept"})
        for name in ("ccc_commit_kept", "ccc_discard_kept"):
            self.assertTrue(tools[name]["_meta"][
                "anthropic/requiresUserInteraction"])

    def test_status_and_list_kept_are_compact_and_read_only(self):
        output, control = self.run_server([
            self.initialize(),
            rpc(2, "tools/call", {"name": "ccc_status", "arguments": {}}),
            rpc(3, "tools/call", {"name": "ccc_list_kept", "arguments": {}}),
        ])
        status = output[1]["result"]["structuredContent"]
        self.assertEqual(status, {"kept_count": 1, "committed_count": 2})
        listed = output[2]["result"]["structuredContent"]
        self.assertEqual(listed["kept"], ["/storage/user/outside.txt"])
        self.assertFalse(any(call[0] == "resolve" for call in control.calls))

    def test_destructive_call_uses_nested_form_elicitation_and_accepts(self):
        output, control = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_commit_kept",
                                   "arguments": {"paths": [
                                       "/storage/user/outside.txt"]}}),
            rpc("ccc-elicitation-1", "unused"),  # overwritten below
        ])
        # A JSON-RPC response, not a request, is needed for the nested request.
        # Re-run with the deterministic nested request id returned by the server.
        nested = output[1]
        self.assertEqual(nested["method"], "elicitation/create")
        self.assertEqual(nested["params"]["mode"], "form")

        accept = json.dumps({"jsonrpc": "2.0", "id": nested["id"],
                             "result": {"action": "accept",
                                        "content": {"confirm": True}}}) + "\n"
        output, control = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_commit_kept",
                                   "arguments": {"paths": [
                                       "/storage/user/outside.txt"]}}),
            accept,
        ])
        self.assertEqual(control.calls[-1],
                         ("resolve", "commit", ["/storage/user/outside.txt"]))
        self.assertFalse(output[-1]["result"].get("isError", False))

    def test_destructive_call_decline_and_no_capability_fail_closed(self):
        # Discover deterministic id and decline it.
        probe, _ = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_discard_kept",
                                   "arguments": {}}),
        ])
        decline = json.dumps({"jsonrpc": "2.0", "id": probe[1]["id"],
                              "result": {"action": "decline"}}) + "\n"
        output, control = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_discard_kept",
                                   "arguments": {}}),
            decline,
        ])
        self.assertTrue(output[-1]["result"]["isError"])
        self.assertFalse(any(call[0] == "resolve" for call in control.calls))

        cancel = json.dumps({"jsonrpc": "2.0", "id": probe[1]["id"],
                             "result": {"action": "cancel"}}) + "\n"
        output, control = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_discard_kept",
                                   "arguments": {}}),
            cancel,
        ])
        self.assertTrue(output[-1]["result"]["isError"])
        self.assertFalse(any(call[0] == "resolve" for call in control.calls))

        output, control = self.run_server([
            self.initialize(),
            rpc(2, "tools/call", {"name": "ccc_commit_kept",
                                   "arguments": {}}),
        ])
        self.assertTrue(output[-1]["result"]["isError"])
        self.assertFalse(any(call[0] == "resolve" for call in control.calls))


class TestMCPControlAdmission(unittest.TestCase):
    def test_so_peercred_is_used_and_ordinary_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "control.sock")
            calls = []
            server = ControlServer(path, lambda req: calls.append(req) or
                                   {"verdict": "ok"}, "token",
                                   expected_clients=("python",),
                                   require_launch_boundary=False)
            server.start()
            self.addCleanup(server.stop)
            client = MCPControlClient(path, "token")
            try:
                admitted = client.admit("codex")
                self.assertTrue(admitted["admitted"])
                response = client.kept_status()
                self.assertEqual(response["verdict"], "ok")
            finally:
                client.close()

            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            raw.connect(path)
            raw.sendall((json.dumps({"version": 1, "token": "token",
                                     "op": "turn-resolve", "decision": "commit",
                                     "paths": ["/x"]}) + "\n").encode())
            reply = json.loads(raw.makefile("r").readline())
            raw.close()
            self.assertFalse(reply["ok"])
            self.assertIn("MCP", reply["error"])
            self.assertEqual([req["op"] for req in calls], ["turn-kept-status"])

    def test_shell_parent_is_not_an_eligible_official_client(self):
        server = ControlServer("/unused", lambda req: {}, "token",
                               expected_clients=("codex",))
        server._launch_pid = 100
        server._launch_start_time = 1
        server._launch_supported = True
        identities = {
            10: {"pid": 10, "ppid": 20, "start_time": 3,
                 "argv": ["ccc-agent", "mcp-server"], "exe": "/bin/python"},
            20: {"pid": 20, "ppid": 100, "start_time": 2,
                 "argv": ["bash"], "exe": "/bin/bash"},
            100: {"pid": 100, "ppid": 1, "start_time": 1,
                  "argv": ["bwrap"], "exe": "/usr/bin/bwrap"},
        }
        with mock.patch.object(control_mod, "_proc_identity",
                               side_effect=lambda pid: identities.get(pid)):
            self.assertIsNone(server._eligible_mcp_peer(10))
            identities[20]["argv"] = ["codex"]
            identities[20]["exe"] = "/usr/bin/codex"
            with mock.patch.object(control_mod, "_is_descendant",
                                   return_value=True):
                self.assertEqual(server._eligible_mcp_peer(10),
                                 (10, 3, 20, 2))

    def test_peer_credentials_report_this_process(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        try:
            pid, uid, gid = mcp.peer_credentials(left)
        finally:
            left.close()
            right.close()
        self.assertEqual(pid, os.getpid())
        self.assertEqual(uid, os.getuid())
        self.assertEqual(gid, os.getgid())


class TestMCPPluginAssets(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(__file__).resolve().parents[1]
        self.plugins = self.repo / "ccc_agent" / "assets" / "plugins"

    def test_claude_and_codex_use_supported_plugin_mcp_config(self):
        for agent in ("claude", "codex"):
            root = self.plugins / (agent + "-ccc-containment")
            with open(root / ".mcp.json") as fh:
                config = json.load(fh)
            server = config["mcpServers"]["ccc"]
            self.assertEqual(server["command"], "ccc-agent")
            self.assertEqual(server["args"],
                             ["mcp-server", "--client", agent])

            skills = root / "skills"
            installed_skills = sorted(
                path.parent.name for path in skills.glob("*/SKILL.md"))
            self.assertEqual(installed_skills, ["ccc-containment"])
            text = (skills / "ccc-containment" / "SKILL.md").read_text()
            self.assertIn("ccc_status", text)
            self.assertIn("ccc_commit_kept", text)
            if agent == "codex":
                self.assertIn("per-tool", text)
                self.assertIn("prompt", text)

    def test_wheel_contains_mcp_module_configs_and_single_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.check_call([
                sys.executable, "setup.py", "build", "--build-base",
                os.path.join(tmp, "build"), "bdist_wheel", "--dist-dir", tmp,
            ], cwd=self.repo, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL)
            wheels = list(pathlib.Path(tmp).glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())
        self.assertIn("ccc_agent/mcp.py", names)
        for agent in ("claude", "codex"):
            prefix = "ccc_agent/assets/plugins/%s-ccc-containment/" % agent
            self.assertIn(prefix + ".mcp.json", names)
            self.assertIn(prefix + "skills/ccc-containment/SKILL.md", names)
            self.assertNotIn(prefix + "skills/ccc/SKILL.md", names)


if __name__ == "__main__":
    unittest.main()
