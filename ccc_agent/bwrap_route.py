"""Best-effort, non-authoritative bwrap route adapter.

The wrapper using this module runs inside the authoritative outer CCC sandbox.
It can only ask a read-only route socket for an already-provisioned BranchFS
view.  Failure always delegates to the trusted real bwrap unchanged; routing is
provenance convenience and never a containment or commit boundary.
"""

import json
import os
import re
import socket
import sys


DEFAULT_ROUTE_SOCKET = "/tmp/ccc-agent/control.sock"
DEFAULT_REAL_BWRAP = "/run/ccc-agent/real-bwrap"
ROUTE_SOURCE_ROOT = "/run/ccc-agent/routes"
MAX_MESSAGE_BYTES = 65536
MAX_BINDINGS = 16

_ROUTE_ID_RE = re.compile(r"^route-[0-9a-f]{32}$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ROUTE_SOURCE_RE = re.compile(
    r"^(?:/run/ccc-agent/routes|/run/user/[0-9]+/ccc-agent-routes|"
    r"/dev/shm/ccc-agent-agent-[A-Za-z0-9-]+/routes)"
    r"/(route-[0-9a-f]{32})(?:/|$)")
_REAL_BWRAP_RE = re.compile(
    r"^(?:/run/ccc-agent/real-bwrap|"
    r"/run/user/[0-9]+/ccc-agent-real-bwrap|"
    r"/dev/shm/ccc-agent-agent-[A-Za-z0-9-]+/ccc-agent-real-bwrap)$")


class RouteProtocolError(ValueError):
    """Raised for malformed route metadata or unsafe mount rewrites."""


def _safe_absolute(path, label):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise RouteProtocolError("%s must be a non-empty absolute path" % label)
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise RouteProtocolError("%s must be normalized and absolute" % label)
    return path


def _send_request(socket_path, payload, timeout):
    if not isinstance(socket_path, str) or not os.path.isabs(socket_path):
        raise RouteProtocolError("route socket path must be absolute")
    data = (json.dumps(payload, separators=(",", ":"), sort_keys=True) +
            "\n").encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise RouteProtocolError("route request is too large")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(float(timeout))
        conn.connect(socket_path)
        conn.sendall(data)
        response = bytearray()
        while b"\n" not in response:
            block = conn.recv(4096)
            if not block:
                break
            response.extend(block)
            if len(response) > MAX_MESSAGE_BYTES:
                raise RouteProtocolError("route response is too large")
        if b"\n" not in response:
            raise RouteProtocolError("route response is incomplete")
        parsed = json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RouteProtocolError("route response must be an object")
        return parsed
    finally:
        conn.close()


def lookup_route(socket_path, provider, logical_session_hint, timeout=0.2):
    """Return an existing route response, or ``None`` on any lookup failure."""
    try:
        if (not isinstance(provider, str) or
                not _PROVIDER_RE.match(provider)):
            return None
        if (not isinstance(logical_session_hint, str) or
                not logical_session_hint or "\x00" in logical_session_hint or
                len(logical_session_hint.encode("utf-8")) > 4096):
            return None
        response = _send_request(socket_path, {
            "op": "route-lookup",
            "provider": provider,
            "logical_session_hint": logical_session_hint,
        }, timeout)
        if response.get("ok") is not True:
            return None
        return response
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def record_route_result(socket_path, route_id, outcome, reason=None,
                        timeout=0.1):
    """Best-effort sanitized coverage event; never blocks real bwrap execution."""
    try:
        if not _ROUTE_ID_RE.match(str(route_id or "")):
            return False
        if outcome not in ("routed", "bypassed", "unattributed"):
            return False
        payload = {"op": "route-record-result", "route_id": route_id,
                   "outcome": outcome}
        if reason is not None:
            reason = str(reason).replace("\x00", "")[:128]
            payload["reason"] = reason
        response = _send_request(socket_path, payload, timeout)
        return response.get("ok") is True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _route_bind_args(route):
    if not isinstance(route, dict) or route.get("ok") is not True:
        raise RouteProtocolError("route response is not successful")
    route_id = route.get("route_id")
    if not isinstance(route_id, str) or not _ROUTE_ID_RE.match(route_id):
        raise RouteProtocolError("route response has invalid route_id")
    bindings = route.get("bindings")
    if not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS:
        raise RouteProtocolError("route bindings must be a bounded array")
    result = []
    destinations = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise RouteProtocolError("route binding must be an object")
        source = _safe_absolute(binding.get("source"), "route source")
        destination = _safe_absolute(
            binding.get("destination"), "route destination")
        match = _ROUTE_SOURCE_RE.match(source)
        if match is None or match.group(1) != route_id:
            raise RouteProtocolError("route source is outside its opaque route")
        if destination == "/" or destination in destinations:
            raise RouteProtocolError("route destination is unsafe or duplicated")
        destinations.add(destination)
        result.extend(["--bind", source, destination])
    return result


def apply_route(argv, route):
    """Append route binds after vendor mounts and before the command separator."""
    argv = list(argv)
    if argv in (["--help"], ["--version"], ["-h"], ["-V"]):
        return argv, False
    try:
        separator = argv.index("--")
    except ValueError:
        return argv, False
    bind_args = _route_bind_args(route)
    if not bind_args:
        return argv, False
    return argv[:separator] + bind_args + argv[separator:], True


def _logical_hint(provider, environ):
    if provider == "codex":
        return environ.get("CODEX_THREAD_ID")
    if provider == "claude":
        return environ.get("CLAUDE_SESSION_ID")
    return None


def main(argv=None, environ=None, real_bwrap=None,
         socket_path=DEFAULT_ROUTE_SOCKET):
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    if real_bwrap is None:
        real_bwrap = environ.get(
            "CCC_AGENT_REAL_BWRAP", DEFAULT_REAL_BWRAP)
    real_bwrap = _safe_absolute(real_bwrap, "real bwrap")
    if not _REAL_BWRAP_RE.match(real_bwrap):
        raise RouteProtocolError("real bwrap path is outside the trusted runtime")

    routed_argv = argv
    provider = str(environ.get("CCC_AGENT_ROUTE_VENDOR", "")).lower()
    hint = _logical_hint(provider, environ)
    route = None
    if hint and argv not in (["--help"], ["--version"], ["-h"], ["-V"]):
        route = lookup_route(socket_path, provider, hint)
    if route is not None:
        try:
            routed_argv, applied = apply_route(argv, route)
            record_route_result(
                socket_path, route.get("route_id"),
                "routed" if applied else "bypassed",
                None if applied else "unrouteable-bwrap-argv")
        except RouteProtocolError as exc:
            routed_argv = argv
            record_route_result(socket_path, route.get("route_id"),
                                "bypassed", str(exc))

    os.execv(real_bwrap, [real_bwrap] + routed_argv)
    return 126


if __name__ == "__main__":
    raise SystemExit(main())
