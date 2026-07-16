"""Behavior tests for the vendor-independent adaptive PID-1 runner."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from ccc_agent import adaptive_pid1 as adaptive_pid1_mod
from ccc_agent import setup as setup_mod


class ControlRecorder(object):
    def __init__(self, directory):
        self.path = os.path.join(directory, "control.sock")
        self.requests = []
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.path)
        self._listener.listen(8)
        self._listener.settimeout(0.1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    block = conn.recv(4096)
                    if not block:
                        break
                    data += block
                if not data:
                    continue
                self.requests.append(json.loads(data.split(b"\n", 1)[0]))
                conn.sendall(b'{"ok":true}\n')

    def close(self):
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=1.0)


class AdaptiveHarness(object):
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp.name, "lifecycle.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.socket_path)
        self.listener.listen(1)
        self.events = []
        self._thread = None

    def close(self):
        self.listener.close()
        self.tmp.cleanup()

    def run(self, code=None, bootstrap=0.15, stability=0.05, detach=0.15,
            timeout=3.0, command=None, input_data=None, extra_env=None):
        def receive():
            conn, _ = self.listener.accept()
            with conn:
                buf = b""
                while True:
                    block = conn.recv(4096)
                    if not block:
                        break
                    buf += block
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line:
                            self.events.append(json.loads(line.decode("utf-8")))

        self._thread = threading.Thread(target=receive)
        self._thread.daemon = True
        self._thread.start()
        env = dict(os.environ)
        env.update({
            "CCC_AGENT_LIFECYCLE_SOCKET": self.socket_path,
            "CCC_AGENT_BOOTSTRAP_SECONDS": str(bootstrap),
            "CCC_AGENT_STABILITY_SECONDS": str(stability),
            "CCC_AGENT_DETACH_SECONDS": str(detach),
        })
        env.update(extra_env or {})
        child_command = (list(command) if command is not None else
                         [sys.executable, "-c", str(code)])
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, adaptive_pid1_mod.__file__, "--"] + child_command,
            env=env,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        self._thread.join(timeout=1.0)
        return proc, elapsed


class TestAdaptivePid1(unittest.TestCase):
    def setUp(self):
        self.h = AdaptiveHarness()

    def tearDown(self):
        self.h.close()

    def event_names(self):
        return [entry["event"] for entry in self.h.events]

    def test_one_shot_preserves_binary_streams_and_status(self):
        code = (
            "import os; "
            "os.write(1, b'out\\x00\\xff'); "
            "os.write(2, b'err\\x00\\xfe')"
        )

        proc, _ = self.h.run(code)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, b"out\x00\xff")
        self.assertEqual(proc.stderr, b"err\x00\xfe")
        self.assertIn("one-shot", self.event_names())
        self.assertNotIn("handoff", self.event_names())

    def test_nonzero_child_status_is_preserved(self):
        proc, _ = self.h.run("raise SystemExit(23)")

        self.assertEqual(proc.returncode, 23)
        self.assertIn("failed", self.event_names())
        self.assertNotIn("handoff", self.event_names())

    def test_long_foreground_child_locks_foreground_and_kills_leaked_helper(self):
        code = r'''
import subprocess, sys, time
helper = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, start_new_session=True)
print(helper.pid, flush=True)
time.sleep(0.25)
'''
        proc, elapsed = self.h.run(code, bootstrap=0.05, timeout=2.0)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertIn("foreground-locked", self.event_names())
        self.assertNotIn("handoff", self.event_names())
        helper_pid = int(proc.stdout.strip())
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and os.path.exists("/proc/%d" % helper_pid):
            time.sleep(0.01)
        self.assertFalse(os.path.exists("/proc/%d" % helper_pid), helper_pid)

    def test_early_child_with_stdout_retaining_helper_is_rejected(self):
        code = r'''
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
'''
        proc, elapsed = self.h.run(code, bootstrap=0.5, detach=0.08,
                                   timeout=2.0)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertIn("handoff-candidate", self.event_names())
        self.assertIn("handoff-rejected", self.event_names())
        self.assertNotIn("handoff", self.event_names())

    def test_early_child_with_stderr_retaining_helper_is_rejected(self):
        code = r'''
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
'''
        proc, elapsed = self.h.run(code, bootstrap=0.5, detach=0.08,
                                   timeout=2.0)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.5)
        self.assertIn("handoff-candidate", self.event_names())
        self.assertIn("handoff-rejected", self.event_names())
        self.assertNotIn("handoff", self.event_names())

    def test_clean_detached_child_hands_off_and_runner_waits_for_service_exit(self):
        code = r'''
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(0.25)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, start_new_session=True)
'''
        proc, elapsed = self.h.run(code, bootstrap=0.5, stability=0.03,
                                   detach=0.2, timeout=2.0)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(elapsed, 0.20)
        self.assertIn("handoff-candidate", self.event_names())
        self.assertIn("handoff", self.event_names())
        self.assertIn("service-exited", self.event_names())

    def test_double_forked_child_is_adopted_and_hands_off(self):
        code = r'''
import os, sys, time
pid = os.fork()
if pid == 0:
    pid = os.fork()
    if pid == 0:
        os.close(0); os.close(1); os.close(2)
        time.sleep(0.25)
        os._exit(0)
    os._exit(0)
os.waitpid(pid, 0)
'''
        proc, elapsed = self.h.run(code, bootstrap=0.5, stability=0.03,
                                   detach=0.2, timeout=2.0)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(elapsed, 0.20)
        self.assertIn("handoff", self.event_names())
        self.assertIn("service-exited", self.event_names())

    def test_candidate_that_dies_during_stability_is_one_shot(self):
        code = r'''
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", "pass"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)
'''
        proc, _ = self.h.run(code, bootstrap=0.5, stability=0.2,
                             detach=0.3)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("handoff", self.event_names())
        self.assertIn("one-shot", self.event_names())

    def test_registers_and_hardens_exact_adaptive_client_before_agent_work(self):
        control = ControlRecorder(self.h.tmp.name)
        self.addCleanup(control.close)
        library = setup_mod.build_mcp_client_hardening(
            os.path.join(self.h.tmp.name, "hardening.so"))
        code = r'''
import json, os, time
time.sleep(0.2)
try:
    os.listdir("/proc/%d/fd" % os.getppid())
    parent_fds_hidden = False
except PermissionError:
    parent_fds_hidden = True
print(json.dumps({
    "harden": os.environ.get("CCC_AGENT_HARDEN_CLIENT"),
    "preload": os.environ.get("LD_PRELOAD"),
    "parent_fds_hidden": parent_fds_hidden,
}), flush=True)
'''
        proc, _ = self.h.run(
            code, bootstrap=0.5,
            extra_env={
                "CCC_AGENT_CONTROL_SOCK": control.path,
                "CCC_AGENT_CONTROL_TOKEN": "token",
                "CCC_AGENT_MCP_REGISTER_CLIENT": "1",
                "CCC_AGENT_CLIENT_PRELOAD": library,
            })

        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["harden"], "1")
        self.assertEqual(result["preload"], library)
        self.assertTrue(result["parent_fds_hidden"])
        self.assertEqual([item["op"] for item in control.requests],
                         ["mcp-register-client"])

    def test_adaptive_codex_proxy_confirms_only_successful_protocol_roots(self):
        control = ControlRecorder(self.h.tmp.name)
        self.addCleanup(control.close)
        codex = os.path.join(self.h.tmp.name, "codex")
        with open(codex, "w") as fh:
            fh.write("""#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get('method') == 'thread/start':
        response = {'id': request['id'],
                    'result': {'thread': {'id': 'thread-1'}}}
    else:
        response = {'id': request.get('id'),
                    'error': {'code': -1, 'message': 'rejected'}}
    print(json.dumps(response, separators=(',', ':')), flush=True)
""")
        os.chmod(codex, 0o755)
        request = (json.dumps({
            "id": 7, "method": "thread/start",
            "params": {"cwd": "/storage/user/Projects/observed"},
        }, separators=(",", ":")) + "\n").encode()

        proc, _ = self.h.run(
            command=[codex, "app-server"], input_data=request,
            bootstrap=0.5,
            extra_env={
                "CCC_AGENT_CONTROL_SOCK": control.path,
                "CCC_AGENT_CONTROL_TOKEN": "token",
                "CCC_AGENT_MCP_REGISTER_CLIENT": "1",
            })

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["id"], 7)
        self.assertEqual([item["op"] for item in control.requests], [
            "mcp-register-client", "workspace-session-replace",
        ])
        self.assertEqual(control.requests[-1]["paths"],
                         ["/storage/user/Projects/observed"])
        self.assertEqual(control.requests[-1]["logical_session_id"], "thread-1")
        self.assertEqual(control.requests[-1]["generation"], 1)
        self.assertEqual(control.requests[-1]["state"], "active")


if __name__ == "__main__":
    unittest.main()
