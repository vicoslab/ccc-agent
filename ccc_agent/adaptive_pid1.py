"""Vendor-independent process lifecycle runner used as bwrap namespace PID 1.

The runner treats an early successful launcher exit with surviving children as a
*handoff candidate*.  It accepts detached-service mode only when those children
remain stable and have released both invocation output streams.  A direct child
that survives the bootstrap window is permanently foreground; when it exits, any
leftovers are terminated rather than waited on.
"""

import ctypes
import errno
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time


PR_SET_DUMPABLE = 4
PR_SET_CHILD_SUBREAPER = 36


def _control_request(payload):
    path = os.environ.get("CCC_AGENT_CONTROL_SOCK")
    token = os.environ.get("CCC_AGENT_CONTROL_TOKEN")
    if not path or not token:
        raise RuntimeError("missing CCC control registration environment")
    request = dict(payload, token=token)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(5.0)
        conn.connect(path)
        data = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        conn.sendall(data)
        response = b""
        while b"\n" not in response and len(response) <= 65536:
            block = conn.recv(4096)
            if not block:
                break
            response += block
        result = json.loads(response.split(b"\n", 1)[0].decode())
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "control request rejected")
        return result
    finally:
        conn.close()


def _register_initial_client(pid):
    if os.environ.get("CCC_AGENT_MCP_REGISTER_CLIENT") != "1":
        return False
    _control_request({"op": "mcp-register-client", "pid": int(pid)})
    return True


def _harden_runner_transport():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _confirm_runner_workspace(update):
    if hasattr(update, "to_request"):
        request = update.to_request()
    else:
        if isinstance(update, str):
            update = [update]
        if (not isinstance(update, list) or
                any(not isinstance(path, str) or not os.path.isabs(path)
                    for path in update)):
            raise ValueError("workspace roots must be absolute paths")
        # Compatibility for non-session-aware official clients. Codex app-server
        # observation always uses the typed branch above.
        request = {"op": "turn-confirm-workspace-roots", "paths": update}
    return _control_request(request)


def _client_environment():
    preload = os.environ.pop("CCC_AGENT_CLIENT_PRELOAD", None)
    if not preload:
        return None
    env = os.environ.copy()
    env["LD_PRELOAD"] = preload
    env["CCC_AGENT_HARDEN_CLIENT"] = "1"
    return env


def _is_codex_app_server(command):
    return bool(command and os.path.basename(command[0]) == "codex" and
                "app-server" in command[1:])


class _CodexOutputObserver(object):
    def __init__(self, monitor, confirm):
        self.monitor = monitor
        self.confirm = confirm
        self.buffer = bytearray()

    def feed(self, name, data):
        if name != "stdout":
            return
        self.buffer.extend(data)
        while b"\n" in self.buffer:
            line, _, remainder = self.buffer.partition(b"\n")
            self.buffer[:] = remainder
            if not line:
                continue
            try:
                roots = self.monitor.observe_server(json.loads(line))
                if roots is not None:
                    self.confirm(roots)
            except (TypeError, ValueError, RuntimeError) as exc:
                sys.stderr.write(
                    "ccc-agent adaptive runner: Codex workspace observation "
                    "failed: %s\n" % exc)


def _forward_codex_input(child_stdin, monitor):
    try:
        for line in sys.stdin.buffer:
            try:
                monitor.observe_client(json.loads(line))
            except (TypeError, ValueError):
                pass
            child_stdin.write(line)
            child_stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            child_stdin.close()
        except OSError:
            pass


def _float_env(name, default):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(0.001, value)


def _set_signal(sig, handler):
    try:
        signal.signal(sig, handler)
    except (AttributeError, OSError, RuntimeError, ValueError):
        pass


def _restore_child_signals():
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP):
        _set_signal(sig, signal.SIG_DFL)


def _enable_subreaper_for_tests_and_non_pid1_runs():
    if os.getpid() == 1:
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except Exception:
        pass


def _returncode(status):
    if status is None:
        return None
    return 128 - status if status < 0 else status


class LifecycleChannel(object):
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)

    def send(self, event, **fields):
        payload = dict(fields)
        payload["event"] = event
        data = (json.dumps(payload, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.sock.sendall(data)
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _set_nonblocking(fileobj):
    os.set_blocking(fileobj.fileno(), False)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except BrokenPipeError:
            return
        view = view[written:]


def _read_streams(selector, eof, timeout, observer=None):
    for key, _mask in selector.select(timeout):
        name, output_fd = key.data
        try:
            data = os.read(key.fd, 65536)
        except BlockingIOError:
            continue
        if data:
            if observer is not None:
                observer.feed(name, data)
            _write_all(output_fd, data)
            continue
        eof[name] = True
        try:
            selector.unregister(key.fileobj)
        except Exception:
            pass
        try:
            key.fileobj.close()
        except OSError:
            pass


def _reap_adopted_children():
    """Reap exited adopted children and report whether any child remains."""
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False
        except InterruptedError:
            continue
        if pid == 0:
            return True


def _direct_child_pids():
    """List children adopted by this process (used for bounded test cleanup)."""
    parent = os.getpid()
    result = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return result
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as fh:
                fields = fh.read().split()
            if len(fields) > 3 and int(fields[3]) == parent:
                result.append(int(name))
        except (OSError, ValueError):
            continue
    return result


def _terminate_descendants(grace=0.1):
    if os.getpid() == 1:
        try:
            os.kill(-1, signal.SIGTERM)
        except OSError:
            pass
        return

    # Unit tests execute the runner as a child subreaper rather than Linux PID 1.
    # Signal only adopted direct children; never use kill(-1) outside a private
    # PID namespace.
    for pid in _direct_child_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _reap_adopted_children():
            return
        time.sleep(0.005)
    for pid in _direct_child_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    while _reap_adopted_children():
        time.sleep(0.005)


def _drain_ready(selector, eof, observer=None):
    # Drain bytes already available without waiting on descendants that retain a
    # stream. This preserves normal command output before namespace teardown.
    while selector.get_map():
        before = len(selector.get_map())
        _read_streams(selector, eof, 0, observer=observer)
        if len(selector.get_map()) == before:
            break


def run(command, channel, bootstrap, stability, detach):
    stop_signal = [None]

    def request_stop(sig, _frame):
        stop_signal[0] = sig

    _set_signal(signal.SIGINT, signal.SIG_IGN)
    _set_signal(signal.SIGQUIT, signal.SIG_IGN)
    _set_signal(signal.SIGTERM, request_stop)
    _set_signal(signal.SIGHUP, request_stop)
    _enable_subreaper_for_tests_and_non_pid1_runs()

    is_codex_app_server = _is_codex_app_server(command)
    client_env = _client_environment()
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if is_codex_app_server else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_restore_child_signals,
            env=client_env,
        )
    except OSError as exc:
        channel.send("failed", status=(127 if exc.errno == errno.ENOENT else 126),
                     detail=str(exc))
        return 127 if exc.errno == errno.ENOENT else 126

    trusted_runner = False
    try:
        if _register_initial_client(child.pid):
            _harden_runner_transport()
            trusted_runner = True
    except Exception as exc:
        sys.stderr.write(
            "ccc-agent adaptive runner: initial client/workspace registration "
            "unavailable: %s\n" % exc)

    observer = None
    if is_codex_app_server:
        from codex_workspace import CodexWorkspaceMonitor
        launch_cwd = (os.getcwd() if
                      os.environ.get("CCC_AGENT_CONFIRM_LAUNCH_WORKSPACE") == "1"
                      else None)
        monitor = CodexWorkspaceMonitor(launch_cwd)
        confirm = (_confirm_runner_workspace if trusted_runner else
                   (lambda _paths: None))
        observer = _CodexOutputObserver(monitor, confirm)
        input_thread = threading.Thread(
            target=_forward_codex_input, args=(child.stdin, monitor), daemon=True)
        input_thread.start()

    selector = selectors.DefaultSelector()
    eof = {"stdout": False, "stderr": False}
    _set_nonblocking(child.stdout)
    _set_nonblocking(child.stderr)
    selector.register(child.stdout, selectors.EVENT_READ,
                      ("stdout", sys.stdout.fileno()))
    selector.register(child.stderr, selectors.EVENT_READ,
                      ("stderr", sys.stderr.fileno()))

    started = time.monotonic()
    foreground_locked = False
    candidate_started = None
    candidate_live_since = None
    child_status = None
    handoff = False
    channel.send("started", pid=child.pid)

    while True:
        now = time.monotonic()
        _read_streams(selector, eof, 0.01, observer=observer)

        if stop_signal[0] is not None:
            channel.send("stopping", signal=stop_signal[0])
            _terminate_descendants()
            return 128 + stop_signal[0]

        if child_status is None:
            polled = child.poll()
            if polled is not None:
                child_status = _returncode(polled)
            elif not foreground_locked and now - started >= bootstrap:
                foreground_locked = True
                channel.send("foreground-locked")

        if child_status is None:
            continue

        if foreground_locked:
            _drain_ready(selector, eof, observer=observer)
            _terminate_descendants()
            channel.send("foreground-exited", status=child_status)
            return child_status

        if child_status != 0:
            _drain_ready(selector, eof, observer=observer)
            _terminate_descendants()
            channel.send("failed", status=child_status)
            return child_status

        descendants_alive = _reap_adopted_children()
        if not descendants_alive:
            # No helper can retain the streams now, so drain to EOF without a
            # long timeout before reporting ordinary one-shot completion.
            drain_deadline = time.monotonic() + 0.2
            while selector.get_map() and time.monotonic() < drain_deadline:
                _read_streams(selector, eof, 0.01, observer=observer)
            channel.send("service-exited" if handoff else "one-shot", status=0)
            return 0

        if candidate_started is None:
            candidate_started = now
            candidate_live_since = now
            channel.send("handoff-candidate")

        if (not handoff and now - candidate_live_since >= stability and
                eof["stdout"] and eof["stderr"]):
            handoff = True
            channel.send("handoff")

        if handoff:
            continue

        if now - candidate_started >= detach:
            _terminate_descendants()
            channel.send("handoff-rejected", reason="stdio-attached")
            return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        return 127
    socket_path = os.environ.get("CCC_AGENT_LIFECYCLE_SOCKET")
    if not socket_path:
        sys.stderr.write("ccc-agent adaptive runner: lifecycle socket is unset\n")
        return 125
    channel = LifecycleChannel(socket_path)
    try:
        return run(
            argv,
            channel,
            _float_env("CCC_AGENT_BOOTSTRAP_SECONDS", 2.0),
            _float_env("CCC_AGENT_STABILITY_SECONDS", 0.2),
            _float_env("CCC_AGENT_DETACH_SECONDS", 2.0),
        )
    finally:
        channel.close()


if __name__ == "__main__":
    raise SystemExit(main())
