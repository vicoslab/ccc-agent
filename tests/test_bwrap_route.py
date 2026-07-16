"""Tests for the non-authoritative Codex/Claude bwrap route adapter."""

import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

from ccc_agent import bwrap_route


class TestBwrapRouteArgv(unittest.TestCase):
    def route(self):
        return {
            "ok": True,
            "route_id": "route-0123456789abcdef0123456789abcdef",
            "bindings": [
                {
                    "source": "/run/ccc-agent/routes/route-0123456789abcdef0123456789abcdef/storage",
                    "destination": "/storage",
                },
                {
                    "source": "/run/ccc-agent/routes/route-0123456789abcdef0123456789abcdef/storage/user/domen-cuda10",
                    "destination": "/home/domen",
                },
            ],
        }

    def test_route_binds_are_last_vendor_mounts_before_command_separator(self):
        original = [
            "--ro-bind", "/", "/", "--bind", "/outer", "/storage",
            "--unshare-user", "--", "/usr/bin/bash", "-c", "echo -- value",
        ]

        routed, applied = bwrap_route.apply_route(original, self.route())

        self.assertTrue(applied)
        separator = routed.index("--")
        self.assertEqual(routed[separator - 6:separator], [
            "--bind",
            "/run/ccc-agent/routes/route-0123456789abcdef0123456789abcdef/storage",
            "/storage",
            "--bind",
            "/run/ccc-agent/routes/route-0123456789abcdef0123456789abcdef/storage/user/domen-cuda10",
            "/home/domen",
        ])
        self.assertEqual(routed[separator + 1:], original[original.index("--") + 1:])

    def test_rejects_sources_outside_opaque_route_directory(self):
        route = self.route()
        route["bindings"][0]["source"] = "/storage/user/real-underlay"
        with self.assertRaises(bwrap_route.RouteProtocolError):
            bwrap_route.apply_route(["--", "/bin/true"], route)

    def test_rejects_destination_traversal_and_mismatched_route_id(self):
        route = self.route()
        route["bindings"][0]["destination"] = "/storage/../etc"
        with self.assertRaises(bwrap_route.RouteProtocolError):
            bwrap_route.apply_route(["--", "/bin/true"], route)
        route = self.route()
        route["route_id"] = "route-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        with self.assertRaises(bwrap_route.RouteProtocolError):
            bwrap_route.apply_route(["--", "/bin/true"], route)

    def test_probe_and_malformed_bwrap_argv_delegate_unchanged(self):
        for argv in (["--help"], ["--version"], ["--ro-bind", "/", "/"]):
            routed, applied = bwrap_route.apply_route(list(argv), self.route())
            self.assertFalse(applied)
            self.assertEqual(routed, list(argv))

    def test_bound_proc_rewrite_preserves_mount_user_and_network_sandbox(self):
        argv = [
            "--unshare-user", "--unshare-pid", "--unshare-net",
            "--ro-bind", "/", "/", "--proc", "/proc",
            "--chdir", "/storage/user/Projects/a", "--", "/bin/true",
        ]
        rewritten, changed = bwrap_route.adapt_bound_proc(argv)
        self.assertTrue(changed)
        self.assertNotIn("--unshare-pid", rewritten)
        self.assertNotIn("--proc", rewritten)
        self.assertIn("--unshare-user", rewritten)
        self.assertIn("--unshare-net", rewritten)
        self.assertIn("--ro-bind", rewritten)
        self.assertEqual(rewritten[-2:], ["--", "/bin/true"])

    def test_bound_proc_unknown_aggregate_delegates_unchanged(self):
        argv = ["--unshare-all", "--", "/bin/true"]
        self.assertEqual(bwrap_route.adapt_bound_proc(argv), (argv, False))


class TestRouteSocketClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp.name, "route.sock")

    def tearDown(self):
        self.tmp.cleanup()

    def serve_once(self, response, requests):
        ready = threading.Event()

        def server():
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(self.socket_path)
                listener.listen(1)
                ready.set()
                conn, _ = listener.accept()
                with conn:
                    payload = b""
                    while b"\n" not in payload:
                        payload += conn.recv(4096)
                    requests.append(json.loads(payload.split(b"\n", 1)[0]))
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            finally:
                listener.close()

        thread = threading.Thread(target=server)
        thread.start()
        ready.wait(2)
        return thread

    def test_lookup_sends_only_vendor_and_logical_hint(self):
        requests = []
        response = {"ok": True, "route_id": "route-" + "0" * 32,
                    "bindings": []}
        thread = self.serve_once(response, requests)

        result = bwrap_route.lookup_route(
            self.socket_path, "codex", "thread-secret", timeout=1)
        thread.join(2)

        self.assertEqual(result, response)
        self.assertEqual(requests, [{
            "op": "route-lookup",
            "provider": "codex",
            "logical_session_hint": "thread-secret",
        }])

    def test_lookup_failure_is_unrouted_not_an_exception(self):
        self.assertIsNone(bwrap_route.lookup_route(
            self.socket_path, "codex", "thread", timeout=0.01))


class TestWrapperMain(unittest.TestCase):
    def test_bound_proc_mode_rewrites_only_pid_and_proc_options(self):
        with mock.patch.object(bwrap_route.os, "execv", side_effect=SystemExit) as execv:
            with self.assertRaises(SystemExit):
                bwrap_route.main([
                    "--unshare-user", "--unshare-pid", "--proc", "/proc",
                    "--", "/bin/true",
                ], environ={"CCC_AGENT_BWRAP_BOUND_PROC": "1"},
                    real_bwrap="/run/ccc-agent/real-bwrap")
        called = execv.call_args.args[1]
        self.assertNotIn("--unshare-pid", called)
        self.assertNotIn("--proc", called)
        self.assertIn("--unshare-user", called)

    def test_missing_hint_execs_real_bwrap_unchanged(self):
        with mock.patch.object(bwrap_route.os, "execv", side_effect=SystemExit) as execv:
            with self.assertRaises(SystemExit):
                bwrap_route.main(["--help"], environ={
                    "CCC_AGENT_ROUTE_VENDOR": "codex",
                }, real_bwrap="/run/ccc-agent/real-bwrap")
        execv.assert_called_once_with(
            "/run/ccc-agent/real-bwrap",
            ["/run/ccc-agent/real-bwrap", "--help"],
        )

    def test_codex_hint_routes_then_execs_real_bwrap(self):
        route = TestBwrapRouteArgv().route()
        with mock.patch.object(bwrap_route, "lookup_route", return_value=route), \
                mock.patch.object(bwrap_route, "record_route_result"), \
                mock.patch.object(bwrap_route.os, "execv", side_effect=SystemExit) as execv:
            with self.assertRaises(SystemExit):
                bwrap_route.main(
                    ["--", "/bin/true"],
                    environ={
                        "CCC_AGENT_ROUTE_VENDOR": "codex",
                        "CODEX_THREAD_ID": "thread-1",
                    },
                    real_bwrap="/run/ccc-agent/real-bwrap",
                    socket_path="/tmp/route.sock",
                )
        called = execv.call_args.args[1]
        self.assertEqual(called[0], "/run/ccc-agent/real-bwrap")
        self.assertIn("/run/ccc-agent/routes/route-0123456789abcdef0123456789abcdef/storage", called)


if __name__ == "__main__":
    unittest.main()
