"""Dependency-free stdio MCP server for contained CCC agent sessions."""

import argparse
import json
import os
import sys
from urllib.parse import unquote, urlparse

from .control import (ControlError, MCPControlClient,
                      peer_credentials as peer_credentials)
from .runner import ENV_CONTROL_SOCK, ENV_CONTROL_TOKEN

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ccc-agent", "version": "1"}
DESTRUCTIVE = frozenset(("ccc_commit_kept", "ccc_discard_kept",
                         "ccc_abort_session"))


def _tool(name, description, properties=None, required=(), destructive=False):
    item = {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    }
    if destructive:
        item["annotations"] = {"destructiveHint": True,
                               "readOnlyHint": False}
        # Claude uses this extension to avoid silently invoking tools that must
        # perform an MCP elicitation round-trip with the human.
        item["_meta"] = {"anthropic/requiresUserInteraction": True}
    else:
        item["annotations"] = {"readOnlyHint": name != "ccc_keep_kept"}
    return item


PATHS_PROPERTY = {
    "paths": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Currently kept paths to resolve; omit for all kept paths.",
        "uniqueItems": True,
    }
}

TOOLS = (
    _tool("ccc_status", "Return compact counts for this contained session."),
    _tool("ccc_list_kept", "List currently kept non-workspace paths."),
    _tool("ccc_commit_kept",
          "Commit selected currently kept paths to the real filesystem after human confirmation.",
          PATHS_PROPERTY, destructive=True),
    _tool("ccc_discard_kept",
          "Discard selected currently kept BranchFS changes after human confirmation.",
          PATHS_PROPERTY, destructive=True),
    _tool("ccc_keep_kept",
          "Keep selected currently kept paths in BranchFS for later review.",
          PATHS_PROPERTY),
    _tool("ccc_abort_session",
          "After human confirmation, discard every still-live branch change at "
          "process exit; already committed earlier turns are unaffected.",
          destructive=True),
)


class MCPServer(object):
    def __init__(self, reader, writer, control, client="unknown"):
        self.reader = reader
        self.writer = writer
        self.control = control
        self.client = str(client or "unknown")
        self.capabilities = {}
        self.initialized = False
        self._elicitation_id = 0
        self.destructive_authorized = False
        self.authorization_reason = "MCP admission has not completed"
        self._roots_id = 0
        self._pending_roots_id = None
        self._roots_refresh_pending = False

    def _write(self, message):
        self.writer.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.writer.flush()

    def _response(self, req_id, result):
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id, code, message):
        self._write({"jsonrpc": "2.0", "id": req_id,
                     "error": {"code": code, "message": str(message)}})

    def _tool_result(self, value, error=False):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        result = {"content": [{"type": "text", "text": text}],
                  "structuredContent": value}
        if error:
            result["isError"] = True
        return result

    def _has_form_elicitation(self):
        elicitation = self.capabilities.get("elicitation")
        return isinstance(elicitation, dict) and isinstance(
            elicitation.get("form"), dict)

    def _elicit(self, action, paths):
        if not self._has_form_elicitation():
            return False, "client does not advertise form elicitation"
        self._elicitation_id += 1
        nested_id = "ccc-elicitation-%d" % self._elicitation_id
        message = ("Confirm CCC %s of %d kept path(s):\n%s" %
                   (action, len(paths), "\n".join("- " + path for path in paths)))
        self._write({
            "jsonrpc": "2.0",
            "id": nested_id,
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": message,
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "title": "Confirm %s" % action,
                            "description": "Required to perform this filesystem operation.",
                        }
                    },
                    "required": ["confirm"],
                },
            },
        })
        line = self.reader.readline()
        if not line:
            return False, "elicitation connection closed"
        try:
            response = json.loads(line)
        except ValueError:
            return False, "malformed elicitation response"
        if response.get("id") != nested_id or "error" in response:
            return False, "invalid elicitation response"
        result = response.get("result")
        if not isinstance(result, dict) or result.get("action") != "accept":
            return False, "human declined or cancelled"
        content = result.get("content")
        if not isinstance(content, dict) or content.get("confirm") is not True:
            return False, "confirmation was not affirmative"
        return True, None

    def _current_paths(self, arguments):
        status = self.control.kept_status()
        current = list(status.get("kept") or [])
        requested = arguments.get("paths")
        if requested is None:
            return current
        if (not isinstance(requested, list) or
                any(not isinstance(path, str) for path in requested)):
            raise ValueError("paths must be an array of strings")
        unknown = sorted(set(requested) - set(current))
        if unknown:
            raise ValueError("paths are not currently remembered as kept: %s" %
                             ", ".join(unknown))
        return sorted(set(requested))

    def _supports_roots(self):
        return isinstance(self.capabilities.get("roots"), dict)

    def _request_roots(self):
        if not self.destructive_authorized or not self._supports_roots():
            return
        if self._pending_roots_id is not None:
            self._roots_refresh_pending = True
            return
        self._roots_id += 1
        self._pending_roots_id = "ccc-roots-%d" % self._roots_id
        self._write({"jsonrpc": "2.0", "id": self._pending_roots_id,
                     "method": "roots/list", "params": {}})

    def _roots_changed(self):
        if not self.destructive_authorized or not self._supports_roots():
            return
        # The previous set is stale as soon as the trusted client announces a
        # change. Revoke it before waiting for a replacement round trip.
        self.control.confirm_workspace_roots([])
        self._request_roots()

    @staticmethod
    def _root_paths(response):
        result = response.get("result")
        roots = result.get("roots") if isinstance(result, dict) else None
        if not isinstance(roots, list):
            raise ValueError("roots/list result must contain a roots array")
        paths = []
        for root in roots:
            uri = root.get("uri") if isinstance(root, dict) else None
            if not isinstance(uri, str):
                continue
            parsed = urlparse(uri)
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                continue
            path = unquote(parsed.path)
            if path.startswith("/"):
                paths.append(path)
        return sorted(set(paths))

    def _handle_roots_response(self, response):
        if response.get("id") != self._pending_roots_id:
            return False
        self._pending_roots_id = None
        refresh = self._roots_refresh_pending
        self._roots_refresh_pending = False
        try:
            if refresh:
                # A change notification made this response stale while it was
                # in flight; only the follow-up list may restore roots.
                return True
            if "error" not in response:
                try:
                    paths = self._root_paths(response)
                except ValueError:
                    return True
                self.control.confirm_workspace_roots(paths)
            return True
        finally:
            if refresh:
                self._request_roots()

    def _call_tool(self, name, arguments):
        if name == "ccc_status":
            status = self.control.kept_status()
            return self._tool_result({
                "kept_count": int(status.get("count", 0)),
                "committed_count": int(status.get("committed_count", 0)),
            })
        if name == "ccc_list_kept":
            status = self.control.kept_status()
            kept = list(status.get("kept") or [])
            return self._tool_result({"kept": kept, "count": len(kept)})
        if name == "ccc_abort_session":
            if not self.destructive_authorized:
                return self._tool_result({
                    "verdict": "pending-external-approval",
                    "action": "abort-session",
                    "paths": [],
                    "reason": self.authorization_reason,
                })
            accepted, reason = self._elicit(
                "abort remaining session branch",
                ["all still-live BranchFS changes "
                 "(earlier committed turns are unaffected)"])
            if not accepted:
                return self._tool_result({"error": reason,
                                          "verdict": "not-authorized"},
                                         error=True)
            return self._tool_result(self.control.request_abort())
        decisions = {"ccc_commit_kept": "commit",
                     "ccc_discard_kept": "discard",
                     "ccc_keep_kept": "keep"}
        if name not in decisions:
            raise KeyError("unknown tool %s" % name)
        paths = self._current_paths(arguments)
        if not paths:
            return self._tool_result({"verdict": "noop", "paths": []})
        decision = decisions[name]
        if name in DESTRUCTIVE:
            if not self.destructive_authorized:
                return self._tool_result({
                    "verdict": "pending-external-approval",
                    "action": decision,
                    "paths": paths,
                    "reason": self.authorization_reason,
                })
            accepted, reason = self._elicit(decision, paths)
            if not accepted:
                return self._tool_result({"error": reason,
                                          "verdict": "not-authorized"},
                                         error=True)
        return self._tool_result(self.control.resolve_turn(decision, paths))

    def dispatch(self, request):
        method = request.get("method")
        req_id = request.get("id")
        if method == "initialize":
            params = request.get("params") or {}
            self.capabilities = params.get("capabilities") or {}
            # Admission happens before tools are advertised/model work begins.
            admission = self.control.admit(self.client)
            self.destructive_authorized = bool(
                admission.get("destructive_authorized"))
            self.authorization_reason = (admission.get("authorization_reason") or
                                         "trusted client transport is not hardened")
            self.initialized = True
            self._response(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Use ccc_status/list_kept for contained-path state. "
                    "Commit/discard/abort require hardened transport and nested "
                    "human elicitation; otherwise they remain pending external "
                    "review. Keep is non-destructive. At end of turn do not "
                    "interrupt active work; inspect kept paths only when "
                    "finished/idling."
                ),
            })
            return
        if method == "notifications/initialized":
            self._request_roots()
            return
        if method == "notifications/roots/list_changed":
            self._roots_changed()
            return
        if not self.initialized:
            self._error(req_id, -32002, "server is not initialized")
            return
        if method == "ping":
            self._response(req_id, {})
            return
        if method == "tools/list":
            self._response(req_id, {"tools": list(TOOLS)})
            return
        if method == "tools/call":
            params = request.get("params") or {}
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                self._response(req_id, self._tool_result(
                    {"error": "arguments must be an object"}, error=True))
                return
            try:
                result = self._call_tool(params.get("name"), arguments)
            except (ControlError, KeyError, ValueError) as exc:
                result = self._tool_result({"error": str(exc)}, error=True)
            self._response(req_id, result)
            return
        if req_id is not None:
            self._error(req_id, -32601, "method not found")

    def run(self):
        try:
            while True:
                line = self.reader.readline()
                if not line:
                    return 0
                try:
                    request = json.loads(line)
                except ValueError:
                    self._error(None, -32700, "parse error")
                    continue
                try:
                    if (self._pending_roots_id is not None and
                            "method" not in request and
                            self._handle_roots_response(request)):
                        continue
                    self.dispatch(request)
                except ControlError as exc:
                    if request.get("id") is not None:
                        self._error(request.get("id"), -32001, str(exc))
                    return 1
        finally:
            self.control.close()


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(prog="ccc-agent mcp-server")
    parser.add_argument("--client", choices=("claude", "codex"), required=True)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    sock = env.get(ENV_CONTROL_SOCK)
    token = env.get(ENV_CONTROL_TOKEN)
    if not sock or not token:
        sys.stderr.write("ccc-agent mcp-server: not in a contained session\n")
        return 1
    try:
        control = MCPControlClient(sock, token)
    except (ControlError, OSError) as exc:
        sys.stderr.write("ccc-agent mcp-server: control connection failed: %s\n" % exc)
        return 1
    return MCPServer(sys.stdin, sys.stdout, control, client=args.client).run()


if __name__ == "__main__":
    raise SystemExit(main())
