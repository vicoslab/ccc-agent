"""Authenticated per-turn control channel between hooks/MCP and supervisor.

Lifecycle hooks retain their existing short-lived requests.  Mutating agent
requests (turn-approve/turn-resolve) are accepted only on the one persistent MCP
connection admitted with Linux SO_PEERCRED and process ancestry checks.
"""

import json
import os
import socket
import struct
import threading

PROTOCOL_VERSION = 1

VERDICT_COMMITTED = "committed"
VERDICT_NEEDS_APPROVAL = "needs-approval"
VERDICT_NOOP = "noop"
VERDICT_HELD = "held"
VERDICT_DISCARDED = "discarded"
VERDICT_KEPT_STATUS = "kept-status"
VERDICT_NEEDS_KEPT_REVIEW = "needs-kept-review"
VERDICT_WORKSPACE_UPDATED = "workspace-updated"

WORKSPACE_HOOK_OPS = frozenset(("turn-add-workspace", "turn-remove-workspace"))
MCP_ONLY_OPS = frozenset(("turn-approve", "turn-resolve"))


class ControlError(Exception):
    pass


def _send_line(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _recv_line(fileobj):
    line = fileobj.readline()
    if not line:
        raise ControlError("control connection closed before response")
    try:
        return json.loads(line)
    except ValueError as exc:
        raise ControlError("malformed control message: %s" % exc)


def peer_credentials(conn):
    """Return Linux SO_PEERCRED as ``(pid, uid, gid)`` or fail closed."""
    if not hasattr(socket, "SO_PEERCRED"):
        raise ControlError("SO_PEERCRED is unavailable; refusing MCP admission")
    size = struct.calcsize("3i")
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    if len(raw) != size:
        raise ControlError("invalid SO_PEERCRED result")
    return struct.unpack("3i", raw)


def _proc_identity(pid):
    """Return process PID/PPID/start-time/cmdline identity from procfs."""
    try:
        pid = int(pid)
        with open("/proc/%d/stat" % pid, "rb") as fh:
            stat_line = fh.read().decode("ascii")
        tail = stat_line[stat_line.rfind(")") + 2:].split()
        ppid = int(tail[1])
        start_time = int(tail[19])
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            argv = [part.decode("utf-8", "surrogateescape")
                    for part in fh.read().split(b"\0") if part]
        exe = os.readlink("/proc/%d/exe" % pid)
    except (OSError, ValueError, IndexError):
        return None
    return {"pid": pid, "ppid": ppid, "start_time": start_time,
            "argv": argv, "exe": exe}


def _process_matches(identity, expected):
    if identity is None:
        return False
    names = {os.path.basename(identity.get("exe") or "").lower()}
    argv = identity.get("argv") or []
    if argv:
        names.add(os.path.basename(argv[0]).lower())
        # Node/python launchers can name their script/module just after argv[0].
        names.update(os.path.basename(arg).lower() for arg in argv[1:3])
    return bool(names & set(expected))


def _is_descendant(pid, ancestor_pid, limit=64):
    current = int(pid)
    ancestor_pid = int(ancestor_pid)
    for _ in range(limit):
        if current == ancestor_pid:
            return True
        identity = _proc_identity(current)
        if identity is None or identity["ppid"] <= 0 or identity["ppid"] == current:
            return False
        current = identity["ppid"]
    return False


class ControlServer(object):
    """Supervisor-side Unix control socket.

    ``expected_clients`` is a set of official agent executable/cmdline basenames.
    Production runners also call :meth:`set_launch_process`; an MCP server is
    eligible only when its direct parent matches one of those clients and that
    client is beneath the launched bwrap process.  The first eligible connection
    is pinned to MCP PID/start-time plus client PID/start-time.
    """

    def __init__(self, socket_path, handler, token, hook_token=None,
                 expected_clients=(), require_launch_boundary=True,
                 enforce_mcp_admission=True):
        self.socket_path = socket_path
        self.handler = handler
        self.token = token
        self.hook_token = hook_token
        self.expected_clients = tuple(os.path.basename(str(name)).lower()
                                      for name in expected_clients if name)
        self.require_launch_boundary = bool(require_launch_boundary)
        self.enforce_mcp_admission = bool(enforce_mcp_admission)
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._launch_pid = None
        self._launch_start_time = None
        self._launch_supported = False
        self._launch_ready = threading.Condition()
        self._mcp_fingerprint = None
        self._mcp_conn = None
        self._admission_lock = threading.Lock()

    def start(self):
        parent = os.path.dirname(self.socket_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def set_launch_process(self, pid, supported=True):
        identity = _proc_identity(pid)
        if identity is None:
            raise ControlError("cannot identify launched agent process %s" % pid)
        with self._launch_ready:
            self._launch_pid = int(pid)
            self._launch_start_time = identity["start_time"]
            self._launch_supported = bool(supported)
            self._launch_ready.notify_all()

    def _wait_for_launch(self):
        if not self.require_launch_boundary:
            return True
        with self._launch_ready:
            if self._launch_pid is None:
                # Popen returns to the supervisor just after the child can begin;
                # tolerate that small race without accepting an unbound identity.
                self._launch_ready.wait(timeout=2.0)
            return self._launch_pid is not None

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,),
                             daemon=True).start()

    def _eligible_mcp_peer(self, peer_pid):
        peer = _proc_identity(peer_pid)
        if peer is None or not self.expected_clients:
            return None
        parent = _proc_identity(peer["ppid"])
        # This direct-parent check is what rejects `bash -c ccc-agent mcp-server`.
        if _process_matches(parent, self.expected_clients):
            client = parent
        elif (not self.require_launch_boundary and
              _process_matches(peer, self.expected_clients)):
            # Explicit unit-test mode permits an in-process MCP client.
            client = peer
        else:
            return None
        if self.require_launch_boundary:
            if not self._wait_for_launch() or not self._launch_supported:
                return None
            launch = _proc_identity(self._launch_pid)
            if (launch is None or launch["start_time"] != self._launch_start_time or
                    not _is_descendant(client["pid"], self._launch_pid)):
                return None
        return (peer["pid"], peer["start_time"],
                client["pid"], client["start_time"])

    def _admit_mcp(self, conn):
        try:
            pid, uid, _gid = peer_credentials(conn)
        except ControlError as exc:
            return {"ok": False, "error": str(exc)}
        if uid != os.geteuid():
            return {"ok": False, "error": "MCP peer uid is not the supervisor uid"}
        fingerprint = self._eligible_mcp_peer(pid)
        if fingerprint is None:
            return {"ok": False,
                    "error": "MCP peer is not the eligible official client child"}
        with self._admission_lock:
            if self._mcp_fingerprint is not None:
                return {"ok": False,
                        "error": "an MCP process/connection is already pinned"}
            self._mcp_fingerprint = fingerprint
            self._mcp_conn = conn
        return {"ok": True, "admitted": True}

    def _mcp_connection_valid(self, conn):
        with self._admission_lock:
            fingerprint = self._mcp_fingerprint
            pinned = self._mcp_conn is conn
        if not pinned or fingerprint is None:
            return False
        peer_pid, peer_start, client_pid, client_start = fingerprint
        peer = _proc_identity(peer_pid)
        client = _proc_identity(client_pid)
        return bool(peer and client and peer["start_time"] == peer_start and
                    client["start_time"] == client_start and
                    (peer_pid == client_pid or peer["ppid"] == client_pid))

    def _handle_conn(self, conn):
        try:
            reader = conn.makefile("r")
            while True:
                try:
                    req = _recv_line(reader)
                except ControlError as exc:
                    if "closed before response" not in str(exc):
                        _send_line(conn, {"ok": False, "error": str(exc)})
                    return
                if req.get("token") != self.token:
                    _send_line(conn, {"ok": False, "error": "unauthorized"})
                    return
                if req.get("op") == "mcp-admit":
                    resp = self._admit_mcp(conn)
                    _send_line(conn, resp)
                    if not resp.get("ok"):
                        return
                    continue
                if (req.get("op") in MCP_ONLY_OPS and
                        self.enforce_mcp_admission and
                        not self._mcp_connection_valid(conn)):
                    _send_line(conn, {"ok": False,
                                      "error": "operation requires the pinned MCP connection"})
                    return
                if self._mcp_conn is conn and not self._mcp_connection_valid(conn):
                    _send_line(conn, {"ok": False,
                                      "error": "pinned MCP process identity changed"})
                    return
                if req.get("op") in WORKSPACE_HOOK_OPS:
                    if not self.hook_token or req.get("hook_token") != self.hook_token:
                        _send_line(conn, {"ok": False,
                                          "error": "hook-only workspace operation unauthorized"})
                        return
                try:
                    resp = self.handler(req)
                    if not isinstance(resp, dict):
                        resp = {"ok": False, "error": "handler returned non-dict"}
                    else:
                        resp.setdefault("ok", True)
                except Exception as exc:
                    resp = {"ok": False, "error": "%s: %s"
                            % (type(exc).__name__, exc)}
                _send_line(conn, resp)
                if self._mcp_conn is not conn:
                    return
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class ControlClient(object):
    """Short-lived hook-side client: one connection per request."""

    def __init__(self, socket_path, token, timeout=30, hook_token=None):
        self.socket_path = socket_path
        self.token = token
        self.timeout = timeout
        self.hook_token = hook_token

    def _request(self, payload):
        req = dict(payload, token=self.token, version=PROTOCOL_VERSION)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
            _send_line(sock, req)
            resp = _recv_line(sock.makefile("r"))
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if not resp.get("ok"):
            raise ControlError(resp.get("error", "unknown control error"))
        return resp

    def finalize_turn(self, default_keep=False):
        return self._request({"op": "turn-finalize",
                              "default_keep": bool(default_keep)})

    def approve_turn(self, approval_token, decision, paths=None,
                     commit_paths=None, keep_paths=None, discard_paths=None):
        req = {"op": "turn-approve", "approval_token": approval_token,
               "decision": decision}
        if paths:
            req["paths"] = list(paths)
        if commit_paths:
            req["commit_paths"] = list(commit_paths)
        if keep_paths:
            req["keep_paths"] = list(keep_paths)
        if discard_paths:
            req["discard_paths"] = list(discard_paths)
        return self._request(req)

    def resolve_turn(self, decision, paths):
        return self._request({"op": "turn-resolve", "decision": decision,
                              "paths": list(paths)})

    def kept_status(self):
        return self._request({"op": "turn-kept-status"})

    def review_kept(self):
        return self._request({"op": "turn-review-kept"})

    def _hook_workspace_request(self, op, path, hook_session):
        if not self.hook_token:
            raise ControlError("hook-only workspace operation requires hook token")
        return self._request({"op": op, "path": path,
                              "hook_session": hook_session,
                              "hook_token": self.hook_token})

    def add_workspace(self, path, hook_session):
        return self._hook_workspace_request("turn-add-workspace", path,
                                            hook_session)

    def remove_workspace(self, path, hook_session):
        return self._hook_workspace_request("turn-remove-workspace", path,
                                            hook_session)


class MCPControlClient(ControlClient):
    """Persistent connection used only by the admitted stdio MCP process."""

    def __init__(self, socket_path, token, timeout=30):
        super().__init__(socket_path, token, timeout=timeout)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(socket_path)
        self._reader = self._sock.makefile("r")
        self._lock = threading.Lock()

    def _request(self, payload):
        req = dict(payload, token=self.token, version=PROTOCOL_VERSION)
        with self._lock:
            _send_line(self._sock, req)
            resp = _recv_line(self._reader)
        if not resp.get("ok"):
            raise ControlError(resp.get("error", "unknown control error"))
        return resp

    def admit(self, client):
        return self._request({"op": "mcp-admit", "client": str(client)})

    def close(self):
        try:
            self._reader.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
