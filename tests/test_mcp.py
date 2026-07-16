"""Agent-facing stdio MCP protocol and fail-closed approval tests."""

import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from ccc_agent import control as control_mod
from ccc_agent import mcp
from ccc_agent import setup as setup_mod
from ccc_agent.control import ControlServer, MCPControlClient


class FakeControl(object):
    def __init__(self, destructive_authorized=True):
        self.calls = []
        self.kept = ["/storage/user/outside.txt"]
        self.destructive_authorized = destructive_authorized

    def admit(self, client):
        self.calls.append(("admit", client))
        return {"admitted": True,
                "destructive_authorized": self.destructive_authorized,
                "authorization_reason": (None if self.destructive_authorized else
                                         "trusted client transport is not hardened")}

    def kept_status(self):
        self.calls.append(("status",))
        return {"verdict": "kept-status", "kept": list(self.kept),
                "count": len(self.kept), "committed_count": 2}

    def resolve_turn(self, decision, paths):
        self.calls.append(("resolve", decision, list(paths)))
        return {"verdict": decision, decision + "ted": list(paths)}

    def request_abort(self):
        self.calls.append(("abort",))
        return {"verdict": "abort-requested", "apply": "process-exit"}

    def confirm_workspace_roots(self, paths):
        self.calls.append(("confirm-workspace-roots", list(paths)))
        return {"verdict": "workspace-updated", "confirmed": list(paths)}

    def replace_workspace_session(self, logical_session_id, generation, paths,
                                  state="active"):
        self.calls.append(("replace-workspace-session", logical_session_id,
                           generation, list(paths), state))
        return {"verdict": "workspace-updated", "confirmed": list(paths)}

    def close(self):
        pass


def rpc(req_id, method, params=None):
    obj = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        obj["params"] = params
    return json.dumps(obj) + "\n"


class TestMCPProtocol(unittest.TestCase):
    def run_server(self, lines, client="claude", destructive_authorized=True):
        reader = io.StringIO("".join(lines))
        writer = io.StringIO()
        control = FakeControl(destructive_authorized=destructive_authorized)
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
                                      "ccc_keep_kept", "ccc_abort_session"})
        for name in ("ccc_commit_kept", "ccc_discard_kept",
                     "ccc_abort_session"):
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

    def test_unhardened_client_returns_external_review_without_elicitation(self):
        output, control = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_commit_kept",
                                   "arguments": {}}),
        ], destructive_authorized=False)

        self.assertEqual(len(output), 2)
        result = output[-1]["result"]["structuredContent"]
        self.assertEqual(result["verdict"], "pending-external-approval")
        self.assertEqual(result["paths"], ["/storage/user/outside.txt"])
        self.assertIn("not hardened", result["reason"])
        self.assertFalse(any(call[0] == "resolve" for call in control.calls))

    def test_abort_requires_elicitation_and_records_abort_on_process_exit(self):
        probe, _ = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_abort_session",
                                   "arguments": {}}),
        ])
        accept = json.dumps({"jsonrpc": "2.0", "id": probe[1]["id"],
                             "result": {"action": "accept",
                                        "content": {"confirm": True}}}) + "\n"
        output, control = self.run_server([
            self.initialize({"elicitation": {"form": {}}}),
            rpc(2, "tools/call", {"name": "ccc_abort_session",
                                   "arguments": {}}),
            accept,
        ])

        self.assertEqual(control.calls[-1], ("abort",))
        result = output[-1]["result"]["structuredContent"]
        self.assertEqual(result["verdict"], "abort-requested")
        self.assertEqual(result["apply"], "process-exit")

    def test_pinned_client_roots_confirm_workspace_without_human_prompt(self):
        roots_response = json.dumps({
            "jsonrpc": "2.0", "id": "ccc-roots-1",
            "result": {"roots": [
                {"uri": "file:///storage/user/Projects/new-project",
                 "name": "new-project"},
            ]},
        }) + "\n"
        output, control = self.run_server([
            self.initialize({"roots": {"listChanged": True}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}) + "\n",
            roots_response,
        ])

        self.assertEqual(output[1]["method"], "roots/list")
        self.assertEqual(output[1]["id"], "ccc-roots-1")
        self.assertEqual(control.calls[-1],
                         ("replace-workspace-session", "mcp-root-set", 1,
                          ["/storage/user/Projects/new-project"], "active"))
        self.assertFalse(any(item.get("method") == "elicitation/create"
                             for item in output))

    def test_roots_change_notification_requeries_authoritative_client(self):
        first = json.dumps({
            "jsonrpc": "2.0", "id": "ccc-roots-1",
            "result": {"roots": [{"uri": "file:///storage/user/Projects/a"}]},
        }) + "\n"
        second = json.dumps({
            "jsonrpc": "2.0", "id": "ccc-roots-2",
            "result": {"roots": [{"uri": "file:///storage/user/Projects/b"}]},
        }) + "\n"
        output, control = self.run_server([
            self.initialize({"roots": {"listChanged": True}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}) + "\n",
            first,
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/roots/list_changed"}) + "\n",
            second,
        ])

        requests = [item for item in output if item.get("method") == "roots/list"]
        self.assertEqual([item["id"] for item in requests],
                         ["ccc-roots-1", "ccc-roots-2"])
        confirmations = [call for call in control.calls
                         if call[0] == "replace-workspace-session"]
        self.assertEqual(confirmations, [
            ("replace-workspace-session", "mcp-root-set", 1,
             ["/storage/user/Projects/a"], "active"),
            ("replace-workspace-session", "mcp-root-set", 2, [], "active"),
            ("replace-workspace-session", "mcp-root-set", 3,
             ["/storage/user/Projects/b"], "active"),
        ])

    def test_roots_change_while_request_pending_forces_followup_refresh(self):
        first = json.dumps({
            "jsonrpc": "2.0", "id": "ccc-roots-1",
            "result": {"roots": [{"uri": "file:///storage/user/Projects/a"}]},
        }) + "\n"
        second = json.dumps({
            "jsonrpc": "2.0", "id": "ccc-roots-2",
            "result": {"roots": [{"uri": "file:///storage/user/Projects/b"}]},
        }) + "\n"
        changed = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/roots/list_changed",
        }) + "\n"
        output, control = self.run_server([
            self.initialize({"roots": {"listChanged": True}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}) + "\n",
            changed,
            first,
            second,
        ])

        requests = [item for item in output if item.get("method") == "roots/list"]
        self.assertEqual([item["id"] for item in requests],
                         ["ccc-roots-1", "ccc-roots-2"])
        confirmations = [call for call in control.calls
                         if call[0] == "replace-workspace-session"]
        self.assertEqual(confirmations, [
            ("replace-workspace-session", "mcp-root-set", 1, [], "active"),
            ("replace-workspace-session", "mcp-root-set", 2,
             ["/storage/user/Projects/b"], "active"),
        ])

    def test_roots_reject_non_file_and_remote_file_uris(self):
        roots_response = json.dumps({
            "jsonrpc": "2.0", "id": "ccc-roots-1",
            "result": {"roots": [
                {"uri": "https://example.test/project"},
                {"uri": "file://remote/storage/user/Projects/x"},
                {"uri": "file:///storage/user/Projects/good%20name"},
            ]},
        }) + "\n"
        _output, control = self.run_server([
            self.initialize({"roots": {"listChanged": False}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}) + "\n",
            roots_response,
        ])

        self.assertEqual(control.calls[-1],
                         ("replace-workspace-session", "mcp-root-set", 1,
                          ["/storage/user/Projects/good name"], "active"))

    def test_unhardened_client_does_not_confirm_roots(self):
        output, control = self.run_server([
            self.initialize({"roots": {"listChanged": True}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}) + "\n",
        ], destructive_authorized=False)

        self.assertEqual(len(output), 1)
        self.assertFalse(any(call[0] == "replace-workspace-session"
                             for call in control.calls))

    def test_malformed_roots_response_is_ignored_without_killing_server(self):
        malformed = json.dumps({
            "jsonrpc": "2.0", "id": "ccc-roots-1",
            "result": {"roots": "not-an-array"},
        }) + "\n"
        output, control = self.run_server([
            self.initialize({"roots": {"listChanged": True}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}) + "\n",
            malformed,
            rpc(9, "ping", {}),
        ])

        self.assertEqual(output[-1], {"jsonrpc": "2.0", "id": 9,
                                      "result": {}})
        self.assertFalse(any(call[0] == "replace-workspace-session"
                             for call in control.calls))


class TestMCPControlAdmission(unittest.TestCase):
    def test_so_peercred_is_used_and_ordinary_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "control.sock")
            calls = []
            server = ControlServer(path, lambda req: calls.append(req) or
                                   {"verdict": "ok"}, "token",
                                   expected_clients=("python",),
                                   require_launch_boundary=False,
                                   transport_revoke_grace=0)
            server.start()
            self.addCleanup(server.stop)
            client = MCPControlClient(path, "token")
            try:
                admitted = client.admit("codex")
                self.assertTrue(admitted["admitted"])
                self.assertFalse(admitted["destructive_authorized"])
                with self.assertRaisesRegex(Exception, "hardened client transport"):
                    client.resolve_turn("commit", ["/x"])
                with self.assertRaisesRegex(Exception, "pinned hardened client"):
                    client.confirm_workspace_roots(
                        ["/storage/user/Projects/new"])
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
            self.assertEqual([req["op"] for req in calls], [
                "turn-kept-status"])

    def test_raw_control_token_cannot_confirm_workspace_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "control.sock")
            calls = []
            server = ControlServer(path, lambda req: calls.append(req) or
                                   {"verdict": "workspace-updated"}, "token")
            server.start()
            self.addCleanup(server.stop)
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                raw.connect(path)
                raw.sendall((json.dumps({
                    "version": 1, "token": "token",
                    "op": "turn-confirm-workspace-roots",
                    "paths": ["/storage/user"],
                }) + "\n").encode())
                reply = json.loads(raw.makefile("r").readline())
            finally:
                raw.close()

        self.assertFalse(reply["ok"])
        self.assertIn("pinned hardened client", reply["error"])
        self.assertEqual(calls, [])

    def test_raw_control_token_cannot_replace_logical_workspace_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "control.sock")
            calls = []
            server = ControlServer(path, lambda req: calls.append(req) or
                                   {"verdict": "workspace-updated"}, "token")
            server.start()
            self.addCleanup(server.stop)
            raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                raw.connect(path)
                raw.sendall((json.dumps({
                    "version": 1, "token": "token",
                    "op": "workspace-session-replace",
                    "source": "codex-app-server",
                    "logical_session_id": "forged-thread",
                    "generation": 1,
                    "paths": ["/storage/user/Projects/forged"],
                    "state": "active",
                }) + "\n").encode())
                reply = json.loads(raw.makefile("r").readline())
            finally:
                raw.close()

        self.assertFalse(reply["ok"])
        self.assertIn("pinned official client", reply["error"])
        self.assertEqual(calls, [])

    def test_hardened_parent_and_mcp_child_receive_destructive_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "control.sock")
            library = setup_mod.build_mcp_client_hardening(
                os.path.join(tmp, "hardening.so"))
            server = ControlServer(path, lambda req: {"verdict": "ok"},
                                   "token", expected_clients=("python",),
                                   require_launch_boundary=False)
            server.start()
            self.addCleanup(server.stop)
            child = """import json, sys
from ccc_agent.control import MCPControlClient
client = MCPControlClient(sys.argv[1], 'token')
try:
    admission = client.admit('codex')
    resolved = client.resolve_turn('commit', ['/x'])
    print(json.dumps({'admission': admission, 'resolved': resolved}))
finally:
    client.close()
"""
            parent = """import subprocess, sys
proc = subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
raise SystemExit(proc.returncode)
"""
            env = dict(os.environ, CCC_AGENT_HARDEN_CLIENT="1",
                       LD_PRELOAD=library)
            proc = subprocess.run([sys.executable, "-c", parent, child, path],
                                  cwd=pathlib.Path(__file__).resolve().parents[1],
                                  env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertTrue(result["admission"]["destructive_authorized"])
            self.assertEqual(result["resolved"]["verdict"], "ok")

    def test_only_registered_initial_client_can_parent_production_mcp(self):
        server = ControlServer("/unused", lambda req: {}, "token",
                               expected_clients=("codex",))
        server._launch_pid = 100
        server._launch_start_time = 1
        server._launch_supported = True
        identities = {
            10: {"pid": 10, "ppid": 20, "start_time": 3,
                 "argv": ["ccc-agent", "mcp-server"], "exe": "/bin/python"},
            20: {"pid": 20, "ppid": 50, "start_time": 2,
                 "argv": ["codex"], "exe": "/usr/bin/codex"},
            30: {"pid": 30, "ppid": 40, "start_time": 5,
                 "argv": ["ccc-agent", "mcp-server"], "exe": "/bin/python"},
            40: {"pid": 40, "ppid": 50, "start_time": 4,
                 "argv": ["codex"], "exe": "/tmp/codex"},
            50: {"pid": 50, "ppid": 100, "start_time": 6,
                 "argv": ["ccc-agent-runner"], "exe": "/usr/bin/python"},
            100: {"pid": 100, "ppid": 1, "start_time": 1,
                  "argv": ["bwrap"], "exe": "/usr/bin/bwrap"},
        }
        with mock.patch.object(control_mod, "_proc_identity",
                               side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  return_value=True):
            # A later malicious descendant named `codex` is not accepted merely
            # because its process name and ancestry look plausible.
            self.assertIsNone(server._eligible_mcp_peer(30))
            server._registered_client_fingerprint = (20, 2)
            eligible = server._eligible_mcp_peer(10)
            self.assertEqual(eligible["fingerprint"], (10, 3, 20, 2))
            self.assertTrue(eligible["destructive_authorized"])
            self.assertIsNone(server._eligible_mcp_peer(30))

    def test_pid1_runner_registers_exact_initial_client_once(self):
        server = ControlServer("/unused", lambda req: {}, "token",
                               expected_clients=("codex",))
        server._launch_pid = 100
        server._launch_start_time = 1
        server._launch_supported = True
        identities = {
            20: {"pid": 20, "ppid": 50, "start_time": 2,
                 "argv": ["codex"], "exe": "/usr/bin/codex"},
            50: {"pid": 50, "ppid": 100, "start_time": 6,
                 "argv": ["ccc-agent-runner"], "exe": "/usr/bin/python"},
            100: {"pid": 100, "ppid": 1, "start_time": 1,
                  "argv": ["bwrap"], "exe": "/usr/bin/bwrap"},
        }
        with mock.patch.object(control_mod, "peer_credentials",
                               return_value=(50, os.geteuid(), os.getegid())), \
                mock.patch.object(control_mod, "_proc_identity",
                                  side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_namespace_pids",
                                  return_value=(50, 1)), \
                mock.patch.object(control_mod, "_registered_child_host_pid",
                                  return_value=20), \
                mock.patch.object(control_mod, "_is_descendant",
                                  return_value=True):
            response = server._register_initial_client(object(), 2)

        self.assertTrue(response["registered"])
        self.assertEqual(server._registered_client_fingerprint, (20, 2))
        self.assertEqual(server._registered_runner_fingerprint, (50, 6))

    def test_only_hardened_registered_client_can_open_workspace_channel(self):
        server = ControlServer("/unused", lambda req: {}, "token",
                               expected_clients=("hermes",),
                               require_launch_boundary=False)
        server._registered_client_fingerprint = (20, 2)
        identities = {
            20: {"pid": 20, "ppid": 50, "start_time": 2,
                 "argv": ["hermes"], "exe": "/usr/bin/hermes"},
            30: {"pid": 30, "ppid": 20, "start_time": 3,
                 "argv": ["python"], "exe": "/usr/bin/python"},
        }
        with mock.patch.object(control_mod, "_proc_identity",
                               side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  return_value=True):
            self.assertEqual(server._eligible_workspace_peer(20), (20, 2))
            self.assertIsNone(server._eligible_workspace_peer(30))
        with mock.patch.object(control_mod, "_proc_identity",
                               side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  return_value=False):
            self.assertIsNone(server._eligible_workspace_peer(20))

    def test_route_lookup_requires_exact_wrapper_descendant_and_vendor(self):
        server = ControlServer("/unused", lambda req: {}, "token",
                               expected_clients=("codex",),
                               require_launch_boundary=False)
        server._registered_client_fingerprint = (20, 2)
        server._registered_client_name = "codex"
        server.route_wrapper_paths = frozenset(("/opt/vendor/bwrap",))
        identities = {
            20: {"pid": 20, "ppid": 1, "start_time": 2,
                 "argv": ["codex", "app-server"], "exe": "/opt/codex"},
            30: {"pid": 30, "ppid": 40, "start_time": 3,
                 "argv": ["/usr/bin/python3", "/opt/vendor/bwrap"],
                 "exe": "/usr/bin/python3"},
            40: {"pid": 40, "ppid": 20, "start_time": 4,
                 "argv": ["codex-linux-sandbox"], "exe": "/opt/codex"},
            50: {"pid": 50, "ppid": 20, "start_time": 5,
                 "argv": ["bash"], "exe": "/usr/bin/bash"},
        }
        with mock.patch.object(control_mod, "peer_credentials",
                               return_value=(30, os.geteuid(), os.getegid())), \
                mock.patch.object(control_mod, "_proc_identity",
                                  side_effect=lambda pid: identities.get(pid)):
            admitted = server._eligible_route_peer(object(), {
                "op": "route-lookup", "provider": "codex"})
            self.assertEqual(admitted["client"], "codex")
            self.assertIsNone(server._eligible_route_peer(object(), {
                "op": "route-lookup", "provider": "claude"}))
        with mock.patch.object(control_mod, "peer_credentials",
                               return_value=(50, os.geteuid(), os.getegid())), \
                mock.patch.object(control_mod, "_proc_identity",
                                  side_effect=lambda pid: identities.get(pid)):
            self.assertIsNone(server._eligible_route_peer(object(), {
                "op": "route-lookup", "provider": "codex"}))

    def test_lost_pinned_transport_revokes_roots_only_while_client_is_live(self):
        calls = []
        server = ControlServer("/unused", lambda req: calls.append(req) or {},
                               "token", transport_revoke_grace=0)
        mcp_conn = object()
        server._mcp_conn = mcp_conn
        server._mcp_fingerprint = (20, 2, 10, 1)
        server._mcp_client_name = "codex"
        server._mcp_destructive_authorized = True
        with mock.patch.object(control_mod, "_proc_identity",
                               return_value={"pid": 10, "start_time": 1}), \
                mock.patch.object(server, "_mcp_connection_valid",
                                  return_value=True):
            server._release_pinned_connection(mcp_conn)

        self.assertIsNone(server._mcp_conn)
        self.assertEqual(calls, [{
            "op": "workspace-authority-revoke",
            "source": "mcp-codex",
            "authority_instance": "mcp-20-2-10-1",
            "reason": "trusted-transport-closed",
        }])

        calls[:] = []
        workspace_conn = object()
        server._workspace_conn = workspace_conn
        server._workspace_fingerprint = (30, 3)
        with mock.patch.object(control_mod, "_proc_identity", return_value=None):
            server._release_pinned_connection(workspace_conn)
        self.assertEqual(calls, [])

    def test_privileged_connections_revalidate_live_launch_boundary(self):
        server = ControlServer("/unused", lambda req: {}, "token")
        server._launch_pid = 100
        server._launch_start_time = 1
        server._launch_supported = True
        conn = object()
        server._mcp_conn = conn
        server._mcp_fingerprint = (30, 3, 20, 2)
        identities = {
            30: {"pid": 30, "ppid": 20, "start_time": 3},
            20: {"pid": 20, "ppid": 50, "start_time": 2},
            100: {"pid": 100, "ppid": 1, "start_time": 1},
        }
        with mock.patch.object(control_mod, "_proc_identity",
                               side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  return_value=True), \
                mock.patch.object(control_mod, "_is_descendant",
                                  return_value=True):
            self.assertTrue(server._mcp_connection_valid(conn))
        identities[100]["start_time"] = 99
        with mock.patch.object(control_mod, "_proc_identity",
                               side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  return_value=True), \
                mock.patch.object(control_mod, "_is_descendant",
                                  return_value=True):
            self.assertFalse(server._mcp_connection_valid(conn))

    def test_trusted_runner_workspace_channel_requires_hidden_runner_and_client(self):
        server = ControlServer("/unused", lambda req: {}, "token",
                               require_launch_boundary=False)
        server._registered_runner_fingerprint = (50, 6)
        server._registered_client_fingerprint = (20, 2)
        identities = {
            50: {"pid": 50, "ppid": 100, "start_time": 6,
                 "argv": ["ccc-agent-runner"], "exe": "/usr/bin/python"},
            20: {"pid": 20, "ppid": 50, "start_time": 2,
                 "argv": ["codex"], "exe": "/usr/bin/codex"},
        }
        with mock.patch.object(control_mod, "peer_credentials",
                               return_value=(50, os.geteuid(), os.getegid())), \
                mock.patch.object(control_mod, "_proc_identity",
                                  side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  side_effect=lambda pid: pid == 50):
            self.assertFalse(server._runner_connection_valid(object()))
        with mock.patch.object(control_mod, "peer_credentials",
                               return_value=(50, os.geteuid(), os.getegid())), \
                mock.patch.object(control_mod, "_proc_identity",
                                  side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  return_value=True):
            self.assertTrue(server._runner_connection_valid(object()))

    def test_process_lineage_without_hidden_proc_fds_is_read_only(self):
        server = ControlServer("/unused", lambda req: {}, "token",
                               expected_clients=("codex",),
                               require_launch_boundary=False)
        identities = {
            10: {"pid": 10, "ppid": 20, "start_time": 3,
                 "argv": ["ccc-agent", "mcp-server"], "exe": "/bin/python"},
            20: {"pid": 20, "ppid": 1, "start_time": 2,
                 "argv": ["codex"], "exe": "/usr/bin/codex"},
        }
        with mock.patch.object(control_mod, "_proc_identity",
                               side_effect=lambda pid: identities.get(pid)), \
                mock.patch.object(control_mod, "_proc_fds_hidden",
                                  return_value=False):
            eligible = server._eligible_mcp_peer(10)

        self.assertEqual(eligible["fingerprint"], (10, 3, 20, 2))
        self.assertFalse(eligible["destructive_authorized"])
        self.assertIn("descriptor", eligible["authorization_reason"])

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
        self.assertIn("ccc_agent/assets/security/ccc_client_hardening.c", names)
        for agent in ("claude", "codex"):
            prefix = "ccc_agent/assets/plugins/%s-ccc-containment/" % agent
            self.assertIn(prefix + ".mcp.json", names)
            self.assertIn(prefix + "skills/ccc-containment/SKILL.md", names)
            self.assertNotIn(prefix + "skills/ccc/SKILL.md", names)


if __name__ == "__main__":
    unittest.main()
