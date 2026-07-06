"""Transport tests for the per-turn control channel (ccc_agent.control).

These exercise the socket server/client and token framing with a fake handler;
the freeze/classify/checkpoint integration is tested via the runner.
"""

import os
import tempfile
import threading
import unittest

from ccc_agent.control import (ControlClient, ControlError, ControlServer,
                               VERDICT_COMMITTED, VERDICT_NEEDS_APPROVAL)


class TestControlChannel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sock = os.path.join(self._tmp.name, "run", "control.sock")
        self.seen = []
        self.token = "tok-12345"

    def tearDown(self):
        self._tmp.cleanup()

    def _server(self, handler):
        srv = ControlServer(self.sock, handler, self.token)
        srv.start()
        self.addCleanup(srv.stop)
        return srv

    def test_finalize_turn_round_trips_handler_verdict(self):
        def handler(req):
            self.seen.append(req)
            return {"verdict": VERDICT_COMMITTED, "committed": ["a.txt"]}

        self._server(handler)
        client = ControlClient(self.sock, self.token)
        resp = client.finalize_turn()
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertEqual(resp["committed"], ["a.txt"])
        self.assertEqual(self.seen[-1]["op"], "turn-finalize")
        self.assertEqual(self.seen[-1]["version"], 1)

    def test_finalize_turn_can_request_default_keep(self):
        def handler(req):
            self.seen.append(req)
            return {"verdict": VERDICT_COMMITTED, "committed": ["ok"],
                    "kept": ["/storage/user/escape.txt"]}

        self._server(handler)
        resp = ControlClient(self.sock, self.token).finalize_turn(
            default_keep=True)

        self.assertEqual(resp["verdict"], VERDICT_COMMITTED)
        self.assertTrue(self.seen[-1]["default_keep"])

    def test_needs_approval_carries_paths_and_token(self):
        def handler(req):
            return {"verdict": VERDICT_NEEDS_APPROVAL,
                    "out_of_scope": ["/storage/user/x"], "deny": [],
                    "approval_token": "appr-999"}

        self._server(handler)
        resp = ControlClient(self.sock, self.token).finalize_turn()
        self.assertEqual(resp["verdict"], VERDICT_NEEDS_APPROVAL)
        self.assertEqual(resp["out_of_scope"], ["/storage/user/x"])
        self.assertEqual(resp["approval_token"], "appr-999")

    def test_approve_turn_passes_decision(self):
        def handler(req):
            return {"echo_op": req["op"], "echo_decision": req.get("decision"),
                    "echo_appr": req.get("approval_token")}

        self._server(handler)
        resp = ControlClient(self.sock, self.token).approve_turn("appr-1", "yes")
        self.assertEqual(resp["echo_op"], "turn-approve")
        self.assertEqual(resp["echo_decision"], "yes")
        self.assertEqual(resp["echo_appr"], "appr-1")

    def test_approve_turn_passes_granular_path_decisions(self):
        def handler(req):
            return {"echo_op": req["op"],
                    "commit": req.get("commit_paths"),
                    "keep": req.get("keep_paths"),
                    "discard": req.get("discard_paths")}

        self._server(handler)
        resp = ControlClient(self.sock, self.token).approve_turn(
            "appr-1", "select", commit_paths=["/a"], keep_paths=["/b"],
            discard_paths=["/c"])

        self.assertEqual(resp["echo_op"], "turn-approve")
        self.assertEqual(resp["commit"], ["/a"])
        self.assertEqual(resp["keep"], ["/b"])
        self.assertEqual(resp["discard"], ["/c"])

    def test_resolve_turn_passes_later_path_decision(self):
        def handler(req):
            return {"echo_op": req["op"],
                    "echo_decision": req.get("decision"),
                    "echo_paths": req.get("paths")}

        self._server(handler)
        resp = ControlClient(self.sock, self.token).resolve_turn(
            "discard", ["/storage/user/held.txt"])

        self.assertEqual(resp["echo_op"], "turn-resolve")
        self.assertEqual(resp["echo_decision"], "discard")
        self.assertEqual(resp["echo_paths"], ["/storage/user/held.txt"])

    def test_kept_status_and_review_kept_pass_control_ops(self):
        def handler(req):
            return {"echo_op": req["op"], "verdict": "kept-status"}

        self._server(handler)
        client = ControlClient(self.sock, self.token)

        self.assertEqual(client.kept_status()["echo_op"], "turn-kept-status")
        self.assertEqual(client.review_kept()["echo_op"], "turn-review-kept")

    def test_bad_token_rejected(self):
        self._server(lambda req: {"verdict": VERDICT_COMMITTED})
        client = ControlClient(self.sock, "wrong-token")
        with self.assertRaises(ControlError) as ctx:
            client.finalize_turn()
        self.assertIn("unauthorized", str(ctx.exception))

    def test_handler_exception_becomes_error_not_crash(self):
        def handler(req):
            raise RuntimeError("boom")

        self._server(handler)
        with self.assertRaises(ControlError) as ctx:
            ControlClient(self.sock, self.token).finalize_turn()
        self.assertIn("boom", str(ctx.exception))

    def test_concurrent_clients(self):
        def handler(req):
            return {"verdict": VERDICT_COMMITTED, "n": req.get("n")}

        self._server(handler)
        errors = []

        def worker(n):
            try:
                c = ControlClient(self.sock, self.token)
                r = c._request({"op": "turn-finalize", "n": n})
                if r.get("n") != n:
                    errors.append((n, r))
            except Exception as exc:  # noqa
                errors.append((n, exc))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_client_errors_when_no_server(self):
        client = ControlClient(os.path.join(self._tmp.name, "absent.sock"),
                               self.token, timeout=2)
        with self.assertRaises(Exception):
            client.finalize_turn()


if __name__ == "__main__":
    unittest.main()
