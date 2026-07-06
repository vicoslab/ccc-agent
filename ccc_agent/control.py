"""Per-turn control channel between the in-sandbox hook and the supervisor.

The agent (codex/claude) runs inside a bwrap sandbox where the BranchFS store
and daemon socket are hidden, so its Stop hook cannot drive BranchFS directly.
Instead the supervisor (OUTSIDE the sandbox) serves a small unix-socket control
server; a fixed path is bind-mounted into the sandbox so the hook can reach it
as a thin client.

Trust model (deliberately naive/accidental — see docs/architecture.md): the
agent and the trusted hook share a uid and namespaces, so they are mutually
indistinguishable to the supervisor.  The per-session token below is therefore
*best effort* — it stops unrelated processes from poking the socket, not a
determined agent forging a request.  The supervisor never commits out-of-scope
changes without a relayed user approval; an agent can at worst spoof its OWN
approval (accepted) but can never escape the in-scope policy.

Protocol: one JSON object per line, request then response, connection per call.
  request : {"version":1, "token":..., "op":"turn-finalize"|"turn-approve"|"turn-resolve"|"turn-kept-status"|"turn-review-kept", ...}
  response: {"ok":true, "verdict":..., ...} | {"ok":false, "error":...}
"""

import json
import os
import socket
import threading

PROTOCOL_VERSION = 1

# verdicts returned by turn-finalize / turn-approve
VERDICT_COMMITTED = "committed"          # in-scope (or approved): applied to base
VERDICT_NEEDS_APPROVAL = "needs-approval"  # out-of-scope: relay to the user
VERDICT_NOOP = "noop"                    # nothing changed this turn
VERDICT_HELD = "held"                    # approval denied: left uncommitted
VERDICT_KEPT_STATUS = "kept-status"      # read-only remembered kept-path view
VERDICT_NEEDS_KEPT_REVIEW = "needs-kept-review"  # final idle ask is needed


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


class ControlServer(object):
    """Supervisor-side control socket (runs OUTSIDE the sandbox).

    ``handler(request_dict) -> response_dict`` does the privileged work
    (freeze/classify/checkpoint).  The server owns transport, token checking,
    and error framing only.
    """

    def __init__(self, socket_path, handler, token):
        self.socket_path = socket_path
        self.handler = handler
        self.token = token
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

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

    def _handle_conn(self, conn):
        try:
            reader = conn.makefile("r")
            try:
                req = _recv_line(reader)
            except ControlError as exc:
                _send_line(conn, {"ok": False, "error": str(exc)})
                return
            if req.get("token") != self.token:
                _send_line(conn, {"ok": False, "error": "unauthorized"})
                return
            try:
                resp = self.handler(req)
                if not isinstance(resp, dict):
                    resp = {"ok": False, "error": "handler returned non-dict"}
                else:
                    resp.setdefault("ok", True)
            except Exception as exc:  # never let a handler bug kill the agent
                resp = {"ok": False, "error": "%s: %s"
                        % (type(exc).__name__, exc)}
            _send_line(conn, resp)
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
    """In-sandbox hook side: one short-lived connection per request."""

    def __init__(self, socket_path, token, timeout=30):
        self.socket_path = socket_path
        self.token = token
        self.timeout = timeout

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
        """Signal end-of-turn (Stop boundary). Returns the supervisor verdict."""
        req = {"op": "turn-finalize", "default_keep": bool(default_keep)}
        return self._request(req)

    def approve_turn(self, approval_token, decision, paths=None,
                     commit_paths=None, keep_paths=None, discard_paths=None):
        """Relay the user's decision for an out-of-scope turn.

        ``decision`` is "yes" (commit all flagged), "no"/"keep" (leave
        uncommitted, session continues), or "revert" (hold + ask the agent to
        undo).  ``paths`` (optional legacy form) selects a file-level subset to
        commit; the rest are kept.  ``commit_paths``, ``keep_paths``, and
        ``discard_paths`` provide explicit per-file decisions in one response.
        """
        req = {"op": "turn-approve",
               "approval_token": approval_token,
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
        """Resolve already-kept paths later in a live contained session."""
        req = {"op": "turn-resolve", "decision": decision,
               "paths": list(paths)}
        return self._request(req)

    def kept_status(self):
        """Read remembered kept paths that are still live branch changes."""
        return self._request({"op": "turn-kept-status"})

    def review_kept(self):
        """Ask whether remembered kept paths need a final user decision."""
        return self._request({"op": "turn-review-kept"})
