"""ccc-agent per-turn socket subcommands (turn-finalize / turn-approve),
driven against a real ControlServer with a fake handler."""

import contextlib
import io
import os
import tempfile
import unittest

from ccc_agent.cli import main_ctl
from ccc_agent.control import (ControlServer, VERDICT_COMMITTED,
                               VERDICT_NEEDS_APPROVAL)


class TestCtlSocket(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sock = os.path.join(self._tmp.name, "run", "control.sock")
        self.token = "tok-abc"
        self.calls = []

    def tearDown(self):
        self._tmp.cleanup()

    def _serve(self, handler):
        srv = ControlServer(self.sock, handler, self.token)
        srv.start()
        self.addCleanup(srv.stop)

    def _env(self):
        return {"CCC_AGENT_CONTROL_SOCK": self.sock,
                "CCC_AGENT_CONTROL_TOKEN": self.token}

    def _run(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main_ctl(argv, env=env)
        return code, out.getvalue(), err.getvalue()

    def _record(self, resp):
        def handler(req):
            self.calls.append(req)
            return resp
        return handler

    def test_finalize_turn_committed_exits_zero(self):
        self._serve(self._record({"verdict": VERDICT_COMMITTED,
                                  "committed": ["a", "b"]}))
        code, out, _err = self._run(["turn-finalize"], self._env())
        self.assertEqual(code, 0)
        self.assertIn("committed 2", out)
        self.assertEqual(self.calls[-1]["op"], "turn-finalize")

    def test_finalize_turn_needs_approval_exits_two_with_instructions(self):
        self._serve(self._record({"verdict": VERDICT_NEEDS_APPROVAL,
                                  "out_of_scope": ["/storage/user/x"],
                                  "approval_token": "appr-7"}))
        code, _out, err = self._run(["turn-finalize"], self._env())
        self.assertEqual(code, 2)
        self.assertIn("/storage/user/x", err)
        self.assertIn("turn-approve appr-7", err)

    def test_approve_turn_relays_token_and_decision(self):
        self._serve(self._record({"verdict": VERDICT_COMMITTED,
                                  "committed": ["x"]}))
        code, _out, _err = self._run(["turn-approve", "appr-7", "yes"],
                                     self._env())
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[-1]["op"], "turn-approve")
        self.assertEqual(self.calls[-1]["decision"], "yes")
        self.assertEqual(self.calls[-1]["approval_token"], "appr-7")

    def test_approve_turn_defaults_to_yes(self):
        self._serve(self._record({"verdict": VERDICT_COMMITTED}))
        code, _out, _err = self._run(["turn-approve", "appr-9"], self._env())
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[-1]["decision"], "yes")

    def test_no_socket_degrades_to_zero(self):
        # outside a contained session (no control env): never block the stop
        code, _out, err = self._run(["turn-finalize"], {})
        self.assertEqual(code, 0)
        self.assertIn("no control socket", err)

    def test_control_error_degrades_to_zero(self):
        # socket configured but server refused (bad token) -> degrade safe
        self._serve(self._record({"verdict": VERDICT_COMMITTED}))
        env = self._env()
        env["CCC_AGENT_CONTROL_TOKEN"] = "wrong"
        code, _out, err = self._run(["turn-finalize"], env)
        self.assertEqual(code, 0)
        self.assertIn("control error", err)


if __name__ == "__main__":
    unittest.main()
