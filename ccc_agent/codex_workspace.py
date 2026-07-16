"""Trusted Codex app-server workspace observation.

The namespace PID-1 runner uses this only while transparently proxying Codex's
JSONL app-server transport. Requests on that stdin originate from the external
Codex UI/client, before model execution; hook stdin is deliberately not used as
authority.
"""


class CodexWorkspaceMonitor(object):
    def __init__(self, launch_cwd=None):
        self._pending = {}
        self._threads = {}
        self._thread_versions = {}
        self._sequence = 0
        self._launch = [launch_cwd] if self._absolute(launch_cwd) else []

    @staticmethod
    def _absolute(value):
        return isinstance(value, str) and value.startswith("/")

    def _paths(self, value):
        if not isinstance(value, dict):
            return []
        paths = []
        cwd = value.get("cwd")
        if self._absolute(cwd):
            paths.append(cwd)
        roots = value.get("runtimeWorkspaceRoots")
        if isinstance(roots, list):
            paths.extend(path for path in roots if self._absolute(path))
        environments = value.get("environments")
        if isinstance(environments, list):
            for environment in environments:
                paths.extend(self._paths(environment))
        return sorted(set(paths))

    def observe_client(self, message):
        if not isinstance(message, dict) or "id" not in message:
            return
        method = message.get("method")
        params = message.get("params") or {}
        tracked = ("thread/start", "thread/resume", "thread/fork",
                   "turn/start", "thread/archive", "thread/delete")
        if method not in tracked:
            return
        self._sequence += 1
        if method in ("thread/start", "thread/resume", "thread/fork",
                      "turn/start"):
            self._pending[message["id"]] = {
                "method": method,
                "thread_id": params.get("threadId"),
                "paths": self._paths(params),
                "sequence": self._sequence,
            }
        else:
            self._pending[message["id"]] = {
                "method": method,
                "thread_id": params.get("threadId"),
                "paths": [],
                "sequence": self._sequence,
            }

    def observe_server(self, message):
        if not isinstance(message, dict) or "id" not in message:
            return None
        pending = self._pending.pop(message["id"], None)
        if pending is None or "error" in message:
            return None
        method = pending["method"]
        sequence = pending["sequence"]
        if method in ("thread/archive", "thread/delete"):
            thread_id = pending["thread_id"]
            if thread_id is None:
                return None
            thread_id = str(thread_id)
            if sequence < self._thread_versions.get(thread_id, 0):
                return None
            self._thread_versions[thread_id] = sequence
            self._threads.pop(thread_id, None)
            return self.roots()
        result = message.get("result") or {}
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            thread = {}
        result_thread_id = result.get("threadId") if isinstance(result, dict) else None
        response_thread_id = thread.get("id") or result_thread_id
        if method == "thread/fork":
            thread_id = response_thread_id
        else:
            thread_id = pending["thread_id"] or response_thread_id
        paths = pending["paths"] or self._paths(thread)
        if thread_id and paths:
            thread_id = str(thread_id)
            if sequence < self._thread_versions.get(thread_id, 0):
                return None
            self._thread_versions[thread_id] = sequence
            self._threads[thread_id] = paths
            return self.roots()
        return None

    def roots(self):
        paths = list(self._launch)
        for roots in self._threads.values():
            paths.extend(roots)
        return sorted(set(paths))
