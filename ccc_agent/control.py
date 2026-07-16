"""Authenticated per-turn control channel between hooks/MCP and supervisor.

Lifecycle hooks retain short-lived finalize and workspace proposal/cleanup
requests. Mutating agent decisions and authenticated workspace replacements are
accepted only on process-pinned channels admitted with Linux `SO_PEERCRED`, live
launch ancestry, exact process identities, and descriptor-isolation checks.
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
MCP_ONLY_OPS = frozenset(("turn-approve", "turn-resolve",
                          "turn-request-abort"))
WORKSPACE_CONFIRM_OP = "turn-confirm-workspace-roots"


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
    except (OSError, ValueError, IndexError):
        return None
    try:
        exe = os.readlink("/proc/%d/exe" % pid)
    except OSError:
        # PR_SET_DUMPABLE=0 intentionally hides this symlink from same-uid
        # outsiders. cmdline/stat remain readable and the launch ancestry is
        # independently pinned by the trusted supervisor.
        exe = ""
    return {"pid": pid, "ppid": ppid, "start_time": start_time,
            "argv": argv, "exe": exe}


def _proc_fds_hidden(pid):
    """Whether same-uid outsiders are denied this process's descriptor table."""
    try:
        os.listdir("/proc/%d/fd" % int(pid))
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _proc_namespace_pids(pid):
    try:
        with open("/proc/%d/status" % int(pid)) as fh:
            for line in fh:
                if line.startswith("NSpid:"):
                    return tuple(int(value) for value in line.split()[1:])
    except (OSError, ValueError):
        return ()
    return (int(pid),)


def _registered_child_host_pid(parent_pid, child_namespace_pid):
    """Resolve a runner-reported namespace PID to its direct host child."""
    try:
        path = "/proc/%d/task/%d/children" % (int(parent_pid), int(parent_pid))
        with open(path) as fh:
            children = [int(value) for value in fh.read().split()]
    except (OSError, ValueError):
        return None
    matches = []
    for child_pid in children:
        identity = _proc_identity(child_pid)
        namespace_pids = _proc_namespace_pids(child_pid)
        if (identity and identity["ppid"] == int(parent_pid) and
                int(child_namespace_pid) in namespace_pids):
            matches.append(child_pid)
    return matches[0] if len(matches) == 1 else None


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
    Production runners call :meth:`set_launch_process`, then namespace PID 1
    registers its exact initial direct child. An MCP server is eligible only
    when that registered client is its direct parent. The first eligible
    connection is pinned to MCP PID/start-time plus client PID/start-time.
    """

    def __init__(self, socket_path, handler, token, hook_token=None,
                 expected_clients=(), require_launch_boundary=True,
                 enforce_mcp_admission=True, transport_revoke_grace=0.25):
        self.socket_path = socket_path
        self.handler = handler
        self.token = token
        self.hook_token = hook_token
        self.expected_clients = tuple(os.path.basename(str(name)).lower()
                                      for name in expected_clients if name)
        self.require_launch_boundary = bool(require_launch_boundary)
        self.enforce_mcp_admission = bool(enforce_mcp_admission)
        self.transport_revoke_grace = max(0.0, float(transport_revoke_grace))
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._launch_pid = None
        self._launch_start_time = None
        self._launch_supported = False
        self._launch_ready = threading.Condition()
        self._registered_client_fingerprint = None
        self._registered_runner_fingerprint = None
        self._mcp_fingerprint = None
        self._mcp_conn = None
        self._mcp_destructive_authorized = False
        self._workspace_fingerprint = None
        self._workspace_conn = None
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

    def _register_initial_client(self, conn, child_namespace_pid):
        try:
            runner_pid, uid, _gid = peer_credentials(conn)
            child_namespace_pid = int(child_namespace_pid)
        except (ControlError, TypeError, ValueError) as exc:
            return {"ok": False, "error": "invalid client registration: %s" % exc}
        if uid != os.geteuid():
            return {"ok": False, "error": "runner uid is not the supervisor uid"}
        if not self._wait_for_launch() or not self._launch_supported:
            return {"ok": False, "error": "launch boundary is unavailable"}
        launch = _proc_identity(self._launch_pid)
        runner = _proc_identity(runner_pid)
        runner_nspids = _proc_namespace_pids(runner_pid)
        if (launch is None or launch["start_time"] != self._launch_start_time or
                runner is None or not runner_nspids or runner_nspids[-1] != 1 or
                not _is_descendant(runner_pid, self._launch_pid)):
            return {"ok": False, "error": "registration peer is not trusted PID 1"}
        child_pid = _registered_child_host_pid(runner_pid, child_namespace_pid)
        child = _proc_identity(child_pid) if child_pid is not None else None
        if child is None or not _process_matches(child, self.expected_clients):
            return {"ok": False, "error": "registered child is not the expected client"}
        fingerprint = (child["pid"], child["start_time"])
        with self._launch_ready:
            if (self._registered_client_fingerprint is not None and
                    self._registered_client_fingerprint != fingerprint):
                return {"ok": False, "error": "initial client is already registered"}
            self._registered_client_fingerprint = fingerprint
            self._registered_runner_fingerprint = (
                runner["pid"], runner["start_time"])
            self._launch_ready.notify_all()
        return {"ok": True, "registered": True}

    def _eligible_mcp_peer(self, peer_pid):
        peer = _proc_identity(peer_pid)
        if peer is None or not self.expected_clients:
            return None
        if self.require_launch_boundary:
            if not self._wait_for_launch() or not self._launch_supported:
                return None
            with self._launch_ready:
                if self._registered_client_fingerprint is None:
                    self._launch_ready.wait(timeout=2.0)
                registered = self._registered_client_fingerprint
            if registered is None:
                return None
            client = _proc_identity(registered[0])
            if (client is None or client["start_time"] != registered[1] or
                    peer["ppid"] != client["pid"] or
                    not self._launch_boundary_valid(client["pid"])):
                return None
        else:
            parent = _proc_identity(peer["ppid"])
            if _process_matches(parent, self.expected_clients):
                client = parent
            elif _process_matches(peer, self.expected_clients):
                # Explicit unit-test mode permits an in-process MCP client.
                client = peer
            else:
                return None
        hardened = (_proc_fds_hidden(peer["pid"]) and
                    _proc_fds_hidden(client["pid"]))
        return {
            "fingerprint": (peer["pid"], peer["start_time"],
                            client["pid"], client["start_time"]),
            "destructive_authorized": hardened,
            "authorization_reason": (None if hardened else
                                     "trusted client/MCP descriptor access is not hidden"),
        }

    def _launch_boundary_valid(self, descendant_pid):
        if not self.require_launch_boundary:
            return True
        with self._launch_ready:
            launch_pid = self._launch_pid
            launch_start = self._launch_start_time
            launch_supported = self._launch_supported
        if not launch_supported or launch_pid is None or launch_start is None:
            return False
        launch = _proc_identity(launch_pid)
        return bool(launch and launch["start_time"] == launch_start and
                    _is_descendant(descendant_pid, launch_pid))

    def _runner_connection_valid(self, conn):
        try:
            pid, uid, _gid = peer_credentials(conn)
        except ControlError:
            return False
        with self._launch_ready:
            runner_fingerprint = self._registered_runner_fingerprint
            client_fingerprint = self._registered_client_fingerprint
        runner = _proc_identity(pid)
        client = (_proc_identity(client_fingerprint[0])
                  if client_fingerprint else None)
        return bool(
            uid == os.geteuid() and runner_fingerprint and client_fingerprint and
            runner and client and pid == runner_fingerprint[0] and
            runner["start_time"] == runner_fingerprint[1] and
            client["start_time"] == client_fingerprint[1] and
            client["ppid"] == runner["pid"] and
            self._launch_boundary_valid(runner["pid"]) and
            _proc_fds_hidden(runner["pid"]) and
            _proc_fds_hidden(client["pid"]))

    def _eligible_workspace_peer(self, peer_pid):
        peer = _proc_identity(peer_pid)
        with self._launch_ready:
            registered = self._registered_client_fingerprint
        if peer is None or registered is None:
            return None
        if peer["pid"] != registered[0] or peer["start_time"] != registered[1]:
            return None
        if not self._launch_boundary_valid(peer["pid"]):
            return None
        if not _proc_fds_hidden(peer["pid"]):
            return None
        return (peer["pid"], peer["start_time"])

    def _admit_workspace_client(self, conn, client_name):
        if str(client_name) != "hermes" or "hermes" not in self.expected_clients:
            return {"ok": False,
                    "error": "direct workspace channel is reserved for Hermes"}
        try:
            pid, uid, _gid = peer_credentials(conn)
        except ControlError as exc:
            return {"ok": False, "error": str(exc)}
        if uid != os.geteuid():
            return {"ok": False, "error": "workspace peer uid is not trusted"}
        fingerprint = self._eligible_workspace_peer(pid)
        if fingerprint is None:
            return {"ok": False,
                    "error": "workspace peer is not the hardened initial client"}
        with self._admission_lock:
            if self._workspace_fingerprint is not None:
                return {"ok": False,
                        "error": "a workspace client is already pinned"}
            self._workspace_fingerprint = fingerprint
            self._workspace_conn = conn
        return {"ok": True, "admitted": True}

    def _workspace_connection_valid(self, conn):
        with self._admission_lock:
            fingerprint = self._workspace_fingerprint
            pinned = self._workspace_conn is conn
        if not pinned or fingerprint is None:
            return False
        peer = _proc_identity(fingerprint[0])
        return bool(peer and peer["start_time"] == fingerprint[1] and
                    self._launch_boundary_valid(peer["pid"]) and
                    _proc_fds_hidden(peer["pid"]))

    def _admit_mcp(self, conn):
        try:
            pid, uid, _gid = peer_credentials(conn)
        except ControlError as exc:
            return {"ok": False, "error": str(exc)}
        if uid != os.geteuid():
            return {"ok": False, "error": "MCP peer uid is not the supervisor uid"}
        eligibility = self._eligible_mcp_peer(pid)
        if eligibility is None:
            return {"ok": False,
                    "error": "MCP peer is not the eligible official client child"}
        with self._admission_lock:
            if self._mcp_fingerprint is not None:
                return {"ok": False,
                        "error": "an MCP process/connection is already pinned"}
            self._mcp_fingerprint = eligibility["fingerprint"]
            self._mcp_conn = conn
            self._mcp_destructive_authorized = bool(
                eligibility["destructive_authorized"])
        return {"ok": True, "admitted": True,
                "destructive_authorized": self._mcp_destructive_authorized,
                "authorization_reason": eligibility["authorization_reason"]}

    def _mcp_connection_valid(self, conn):
        with self._admission_lock:
            fingerprint = self._mcp_fingerprint
            pinned = self._mcp_conn is conn
        if not pinned or fingerprint is None:
            return False
        peer_pid, peer_start, client_pid, client_start = fingerprint
        peer = _proc_identity(peer_pid)
        client = _proc_identity(client_pid)
        identity_valid = bool(
            peer and client and peer["start_time"] == peer_start and
            client["start_time"] == client_start and
            (peer_pid == client_pid or peer["ppid"] == client_pid) and
            self._launch_boundary_valid(client_pid))
        if not identity_valid:
            return False
        if self._mcp_destructive_authorized:
            return (_proc_fds_hidden(peer_pid) and
                    _proc_fds_hidden(client_pid))
        return True

    def _release_pinned_connection(self, conn):
        """Revoke dynamic roots if trusted transport dies before its client."""
        client_fingerprint = None
        with self._admission_lock:
            if self._mcp_conn is conn:
                if self._mcp_fingerprint is not None:
                    client_fingerprint = (self._mcp_fingerprint[2],
                                          self._mcp_fingerprint[3])
                self._mcp_conn = None
            if self._workspace_conn is conn:
                client_fingerprint = self._workspace_fingerprint
                self._workspace_conn = None
        if client_fingerprint is None:
            return
        if self.transport_revoke_grace <= 0:
            self._revoke_roots_if_client_live(client_fingerprint)
            return
        threading.Thread(
            target=self._revoke_roots_after_grace,
            args=(client_fingerprint,), daemon=True).start()

    def _revoke_roots_after_grace(self, client_fingerprint):
        if self._stop.wait(self.transport_revoke_grace):
            return
        self._revoke_roots_if_client_live(client_fingerprint)

    def _revoke_roots_if_client_live(self, client_fingerprint):
        client = _proc_identity(client_fingerprint[0])
        if not client or client["start_time"] != client_fingerprint[1]:
            # Normal client/PID-1 teardown has already ended agent authority;
            # preserve the last valid roots for immediate finalization.
            return
        try:
            self.handler({"op": WORKSPACE_CONFIRM_OP, "paths": [],
                          "source": "trusted-transport-closed"})
        except Exception:
            # Connection teardown must not kill the control server. A later
            # trusted replacement/reset still fails closed.
            pass

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
                if req.get("op") == "mcp-register-client":
                    resp = self._register_initial_client(conn, req.get("pid"))
                    _send_line(conn, resp)
                    return
                if req.get("op") == "mcp-admit":
                    resp = self._admit_mcp(conn)
                    _send_line(conn, resp)
                    if not resp.get("ok"):
                        return
                    continue
                if req.get("op") == "workspace-admit":
                    resp = self._admit_workspace_client(conn, req.get("client"))
                    _send_line(conn, resp)
                    if not resp.get("ok"):
                        return
                    continue
                if req.get("op") == WORKSPACE_CONFIRM_OP:
                    trusted_workspace = (
                        (self._mcp_connection_valid(conn) and
                         self._mcp_destructive_authorized) or
                        self._workspace_connection_valid(conn) or
                        self._runner_connection_valid(conn))
                    if not trusted_workspace:
                        _send_line(conn, {"ok": False,
                                          "error": "workspace confirmation requires a pinned hardened client or trusted PID 1"})
                        if self._mcp_conn is conn or self._workspace_conn is conn:
                            continue
                        return
                if (req.get("op") in MCP_ONLY_OPS and
                        self.enforce_mcp_admission and
                        not self._mcp_connection_valid(conn)):
                    _send_line(conn, {"ok": False,
                                      "error": "operation requires the pinned MCP connection"})
                    return
                if (req.get("op") in MCP_ONLY_OPS and
                        self.enforce_mcp_admission and
                        (req.get("op") in ("turn-request-abort",
                                            "turn-confirm-workspace-roots") or
                         req.get("decision") in ("commit", "discard", "revert",
                                                 "yes", "select")) and
                        not self._mcp_destructive_authorized):
                    _send_line(conn, {"ok": False,
                                      "error": "destructive MCP operation requires hardened client transport"})
                    continue
                if self._mcp_conn is conn and not self._mcp_connection_valid(conn):
                    _send_line(conn, {"ok": False,
                                      "error": "pinned MCP process identity changed"})
                    return
                if (self._workspace_conn is conn and
                        not self._workspace_connection_valid(conn)):
                    _send_line(conn, {"ok": False,
                                      "error": "pinned workspace client identity changed"})
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
                if self._mcp_conn is not conn and self._workspace_conn is not conn:
                    return
        except OSError:
            pass
        finally:
            self._release_pinned_connection(conn)
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

    def request_abort(self):
        return self._request({"op": "turn-request-abort"})

    def confirm_workspace_roots(self, paths):
        return self._request({"op": "turn-confirm-workspace-roots",
                              "paths": list(paths)})

    def workspace_proposal_status(self):
        return self._request({"op": "turn-workspace-proposal-status"})

    def close(self):
        try:
            self._reader.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class WorkspaceControlClient(MCPControlClient):
    """Persistent process-pinned channel for an in-process client plugin."""

    def admit(self, client):
        return self._request({"op": "workspace-admit", "client": str(client)})
