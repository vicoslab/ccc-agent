"""Deterministic deployed-platform acceptance for ccc-agent.

Unlike the optional model matrix, this module uses only shell workloads. It
exercises the real installed ccc-agent, BranchFS/FUSE, bwrap, review lifecycle,
and session-delta capability/fallback behavior without requiring model tokens.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set


class PlatformAcceptanceError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class PlatformManifest:
    path: str
    ccc_agent: str
    ccc_agent_config: str
    test_root: str
    artifacts_dir: str
    codex_command: Optional[str] = None
    timeout_seconds: float = 180.0
    poll_seconds: float = 0.2


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PlatformAcceptanceError("cannot read JSON %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise PlatformAcceptanceError("JSON document must be an object: %s" % path)
    return value


def _atomic_json(path: str, value: Mapping) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _deployment_integration_problems(config: Mapping) -> List[str]:
    problems = []
    hardening = config.get("mcp_client_hardening_library")
    if not hardening or not os.path.isfile(str(hardening)):
        problems.append(
            "mcp_client_hardening_library is missing: %s" % hardening)
    elif not os.access(str(hardening), os.R_OK | os.X_OK):
        problems.append(
            "mcp_client_hardening_library is not readable/executable: %s" %
            hardening)
    return problems


def load_platform_manifest(path: str) -> PlatformManifest:
    data = _read_json(path)
    missing = [key for key in ("ccc_agent", "ccc_agent_config", "test_root")
               if not data.get(key)]
    if missing:
        raise PlatformAcceptanceError("manifest missing: %s" % ", ".join(missing))
    test_root = os.path.abspath(os.path.expanduser(str(data["test_root"])))
    if (test_root in ("/", "/storage", "/storage/user", os.path.expanduser("~"))
            or "ccc-agent-acceptance" not in os.path.basename(test_root)):
        raise PlatformAcceptanceError(
            "test_root must be a dedicated directory whose basename contains "
            "'ccc-agent-acceptance': %s" % test_root)
    artifacts = os.path.abspath(os.path.expanduser(str(
        data.get("artifacts_dir") or os.path.join(test_root, "artifacts"))))
    return PlatformManifest(
        path=os.path.abspath(path),
        ccc_agent=os.path.expanduser(str(data["ccc_agent"])),
        ccc_agent_config=os.path.expanduser(str(data["ccc_agent_config"])),
        test_root=test_root,
        artifacts_dir=artifacts,
        codex_command=(os.path.expanduser(str(data["codex_command"]))
                       if data.get("codex_command") else None),
        timeout_seconds=float(data.get("timeout_seconds", 180)),
        poll_seconds=float(data.get("poll_seconds", 0.2)),
    )


class PlatformAcceptanceRunner:
    CHECKS = (
        "package-assets",
        "codex-plugin-mcp",
        "foreground-review-boundary",
        "review-accept",
        "review-abort",
        "serve-protocol-cleanliness",
        "bound-proc-routing-fallback",
        "session-cleanup",
    )

    def __init__(self, manifest: PlatformManifest):
        self.manifest = manifest
        self.config = _read_json(manifest.ccc_agent_config)
        self.state_dir = os.path.abspath(str(self.config.get("state_dir", "")))
        if not self.state_dir:
            raise PlatformAcceptanceError("ccc-agent config has no state_dir")
        self.run_id = "platform-%s-%s" % (
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:8])
        self.root = os.path.join(manifest.test_root, self.run_id)
        self.artifact_dir = os.path.join(manifest.artifacts_dir, self.run_id)
        self.results: Dict[str, Mapping] = {}

    def preflight(self) -> None:
        problems = _deployment_integration_problems(self.config)
        if os.environ.get("CCC_AGENT_SESSION"):
            problems.append("CCC_AGENT_SESSION is set; run outside containment")
        if not (os.path.isfile(self.manifest.ccc_agent)
                and os.access(self.manifest.ccc_agent, os.X_OK)):
            problems.append("ccc_agent is not executable: %s" % self.manifest.ccc_agent)
        if self.manifest.codex_command and not (
                os.path.isfile(self.manifest.codex_command) and
                os.access(self.manifest.codex_command, os.X_OK)):
            problems.append("codex_command is not executable: %s" %
                            self.manifest.codex_command)
        if self.config.get("backend", "branchfs") != "branchfs":
            problems.append("backend must be branchfs")
        if self.config.get("confinement") != "bwrap":
            problems.append("confinement must be bwrap")
        for key in ("branchfs_bin", "bwrap_bin"):
            value = self.config.get(key)
            if not value or not os.access(str(value), os.X_OK):
                problems.append("%s is not executable: %s" % (key, value))
        if not os.path.exists("/dev/fuse"):
            problems.append("/dev/fuse is absent")
        roots = self.config.get("roots")
        if not isinstance(roots, list) or not roots:
            problems.append("config has no protected roots")
        elif not any(
                self.root == os.path.abspath(str(root.get("visible", ""))) or
                self.root.startswith(os.path.abspath(str(root.get("visible", ""))) + os.sep)
                for root in roots if isinstance(root, dict)):
            problems.append("test_root is outside configured protected roots")
        if problems:
            raise PlatformAcceptanceError("preflight failed:\n- " + "\n- ".join(problems))

    def _session_files(self) -> Iterable[str]:
        if not os.path.isdir(self.state_dir):
            return ()
        paths = []
        for child in os.listdir(self.state_dir):
            current = os.path.join(self.state_dir, child, "session", "session.json")
            if os.path.isfile(current):
                paths.append(current)
        return paths

    def sessions(self) -> List[dict]:
        values = []
        for path in self._session_files():
            try:
                values.append(_read_json(path))
            except PlatformAcceptanceError:
                continue
        return values

    def session_ids(self) -> Set[str]:
        return {str(value.get("session_id")) for value in self.sessions()
                if value.get("session_id")}

    @staticmethod
    def select_new_session(before: Set[str], sessions: Sequence[Mapping],
                           expected_kind: str) -> Mapping:
        matches = [value for value in sessions
                   if value.get("session_id") not in before and
                   value.get("agent_kind") == expected_kind]
        if len(matches) != 1:
            raise PlatformAcceptanceError(
                "expected exactly one new %s session; found %s" %
                (expected_kind, [value.get("session_id") for value in matches]))
        return matches[0]

    def _wait_new(self, before: Set[str], expected_kind: str) -> dict:
        deadline = time.monotonic() + self.manifest.timeout_seconds
        last = None
        while time.monotonic() < deadline:
            try:
                return dict(self.select_new_session(before, self.sessions(), expected_kind))
            except PlatformAcceptanceError as exc:
                last = exc
                time.sleep(self.manifest.poll_seconds)
        raise PlatformAcceptanceError("timed out waiting for session: %s" % last)

    def _load(self, session_id: str) -> dict:
        path = os.path.join(self.state_dir, session_id, "session", "session.json")
        return _read_json(path)

    def _run(self, command: Sequence[str], name: str,
             check: bool = True) -> subprocess.CompletedProcess:
        os.makedirs(self.artifact_dir, exist_ok=True)
        proc = subprocess.run(list(command), text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              timeout=self.manifest.timeout_seconds)
        with open(os.path.join(self.artifact_dir, name + ".txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("$ %s\nrc=%d\nstdout:\n%s\nstderr:\n%s" %
                         (" ".join(command), proc.returncode,
                          proc.stdout, proc.stderr))
        if check and proc.returncode != 0:
            raise PlatformAcceptanceError(
                "%s failed (%d): %s" % (name, proc.returncode, proc.stderr))
        return proc

    def _ccc(self, op: str, *args: str) -> List[str]:
        return [self.manifest.ccc_agent, op, "--config",
                self.manifest.ccc_agent_config, *args]

    def _assert_no_runtime_leak(self, session_id: str) -> None:
        bundle = os.path.join(self.state_dir, session_id)
        mount_root = os.path.join(bundle, "mounts")
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            mountinfo = handle.read()
        if mount_root in mountinfo:
            raise PlatformAcceptanceError("session mount leaked: %s" % session_id)
        control = os.path.join(bundle, "control", "control.sock")
        if os.path.exists(control):
            raise PlatformAcceptanceError("session control socket leaked: %s" % control)

    def _exercise_review(self, action: str) -> dict:
        scenario = os.path.join(self.root, "review-" + action)
        workspace = os.path.join(scenario, "workspace")
        outside = os.path.join(scenario, "outside", "pending.txt")
        committed = os.path.join(workspace, "committed.txt")
        os.makedirs(workspace, exist_ok=False)
        before = self.session_ids()
        shell = "set -eu; mkdir -p %s; printf committed > %s; printf pending > %s" % (
            shlex.quote(os.path.dirname(outside)),
            shlex.quote(committed),
            shlex.quote(outside))
        proc = self._run(
            self._ccc("run", "--workspace", workspace, "--", "bash", "-lc", shell),
            "review-%s-run" % action)
        session = self._wait_new(before, "command")
        session_id = str(session["session_id"])
        if session.get("state") != "pending-review":
            raise PlatformAcceptanceError("expected pending-review, got %s" % session.get("state"))
        if os.path.exists(committed) or os.path.exists(outside):
            raise PlatformAcceptanceError(
                "unreviewed command changes reached the underlay")
        diff = self._run(self._ccc("diff", session_id),
                         "review-%s-diff" % action)
        rendered_diff = diff.stdout + diff.stderr
        for pending_path in (committed, outside):
            if pending_path not in rendered_diff:
                raise PlatformAcceptanceError(
                    "pending path absent from diff: %s" % pending_path)
        if action == "accept":
            self._run(self._ccc("review", session_id, "--accept"),
                      "review-accept")
            if not os.path.isfile(committed) or not os.path.isfile(outside):
                raise PlatformAcceptanceError(
                    "review --accept did not commit all pending paths")
            expected = "committed"
        else:
            self._run(self._ccc("abort", session_id), "review-abort")
            if os.path.exists(committed) or os.path.exists(outside):
                raise PlatformAcceptanceError("abort committed a pending path")
            expected = "aborted"
        final = self._load(session_id)
        if final.get("state") != expected:
            raise PlatformAcceptanceError("expected %s, got %s" %
                                          (expected, final.get("state")))
        self._assert_no_runtime_leak(session_id)
        return {"session_id": session_id, "state": expected,
                "run_returncode": proc.returncode}

    def _exercise_serve_fallback(self) -> dict:
        scenario = os.path.join(self.root, "serve-fallback")
        workspace = os.path.join(scenario, "workspace")
        output = os.path.join(workspace, "serve.txt")
        os.makedirs(workspace, exist_ok=False)
        config = dict(self.config)
        config.update({
            "per_turn": True,
            "session_delta_routing": True,
            "session_delta_routing_vendors": ["codex"],
        })
        config_path = os.path.join(self.artifact_dir, "routing-config.json")
        _atomic_json(config_path, config)
        before = self.session_ids()
        command = [self.manifest.ccc_agent, "serve", "codex",
                   "--config", config_path, "--workspace", workspace,
                   "--lifecycle", "foreground", "--", "bash", "-lc",
                   "printf serve-ok > %s" % shlex.quote(output)]
        proc = self._run(command, "serve-fallback")
        combined = proc.stdout + proc.stderr
        if "ccc-agent:" in combined.lower():
            raise PlatformAcceptanceError("serve leaked ccc-agent human output")
        session = self._wait_new(before, "codex-remote")
        session_id = str(session["session_id"])
        policy = session.get("policy", {})
        proc_mode = str(config.get("bwrap_proc_mode", "bind"))
        if proc_mode != "fresh":
            if policy.get("route_interposer_available") is not False:
                raise PlatformAcceptanceError("bound-proc routing was unexpectedly enabled")
            if policy.get("route_interposer_external_fallback") is not True:
                raise PlatformAcceptanceError("bound-proc external fallback was not selected")
            if session.get("session_delta_routes"):
                raise PlatformAcceptanceError("fallback provisioned an unusable nested route")
        if session.get("state") != "pending-review":
            raise PlatformAcceptanceError(
                "serve shell without turn authority did not stay pending: %s" %
                session.get("state"))
        if not os.path.isfile(output):
            raise PlatformAcceptanceError(
                "outer workspace policy did not commit the serve write")
        decisions = policy.get("turn_path_decisions", {})
        if decisions.get(output) != "committed":
            raise PlatformAcceptanceError(
                "serve workspace write lacks committed turn decision")
        reconciliation = policy.get("route_reconciliation", {})
        categories = {item.get("category") for item in
                      reconciliation.get("items", ()) if isinstance(item, dict)}
        if proc_mode != "fresh" and "shared/unattributed" not in categories:
            raise PlatformAcceptanceError(
                "fallback write was not classified shared/unattributed")
        self._run([self.manifest.ccc_agent, "review", "--config", config_path,
                   session_id, "--accept"], "serve-fallback-accept")
        session = self._load(session_id)
        if session.get("state") != "committed":
            raise PlatformAcceptanceError("accepted serve session is not committed")
        if not os.path.isfile(output):
            raise PlatformAcceptanceError("accepted serve workspace write is absent")
        self._assert_no_runtime_leak(session_id)
        return {
            "session_id": session_id,
            "state": session.get("state"),
            "bwrap_proc_mode": proc_mode,
            "route_interposer_available": policy.get("route_interposer_available"),
            "route_interposer_external_fallback": policy.get(
                "route_interposer_external_fallback"),
        }

    @staticmethod
    def _codex_probe_problem(stdout: str, stderr: str) -> Optional[str]:
        combined = stdout + "\n" + stderr
        if "initial client/workspace registration unavailable" in combined:
            return "Codex client/workspace registration failed"
        rows = [line.split() for line in stdout.splitlines() if line.strip()]
        if not any(parts and parts[0] == "ccc" and "mcp-server" in parts
                   for parts in rows):
            return "contained Codex did not list the ccc MCP server"
        return None

    def _exercise_codex_plugin_mcp(self) -> Mapping:
        if not self.manifest.codex_command:
            return {"skipped": True, "reason": "codex_command is not configured"}
        workspace = os.path.join(self.root, "codex-plugin-mcp", "workspace")
        os.makedirs(workspace, exist_ok=False)
        before = self.session_ids()
        proc = self._run(
            self._ccc("run", "--agent", "codex", "--workspace", workspace,
                      "--", self.manifest.codex_command, "mcp", "list"),
            "codex-plugin-mcp", check=False)
        session = self._wait_new(before, "codex")
        session_id = str(session["session_id"])
        problem = self._codex_probe_problem(proc.stdout, proc.stderr)
        if proc.returncode != 0 and problem is None:
            problem = "contained Codex MCP probe failed with rc=%d" % proc.returncode
        try:
            if problem:
                raise PlatformAcceptanceError(problem)
        finally:
            current = self._load(session_id)
            if current.get("state") in ("running", "pending-review", "frozen"):
                self._run(self._ccc("abort", session_id),
                          "codex-plugin-mcp-abort")
        final = self._load(session_id)
        if final.get("state") != "aborted":
            raise PlatformAcceptanceError(
                "Codex MCP probe session did not abort cleanly")
        self._assert_no_runtime_leak(session_id)
        return {"session_id": session_id, "mcp_server": "ccc",
                "registration": "hardened", "state": "aborted"}

    def _verify_package_assets(self) -> Mapping:
        script = """import json, os, pathlib, ccc_agent
root = pathlib.Path(ccc_agent.__file__).parent
paths = [
    root / 'assets/scripts/ccc-bwrap-route',
    root / 'assets/codex/bwrap',
    root / 'assets/shims/ccc-agent-ssh-shell-router.sh',
    root / 'assets/plugins/codex-ccc-containment/.mcp.json',
    root / 'assets/plugins/codex-ccc-containment/.codex-plugin/plugin.json',
]
executable = [p.is_file() and os.access(p, os.X_OK) for p in paths[:3]]
readable = [p.is_file() and os.access(p, os.R_OK) for p in paths[3:]]
print(json.dumps({'package_root': str(root), 'assets': [str(p) for p in paths],
                  'executable': executable, 'readable_metadata': readable}))
"""
        proc = subprocess.run(
            [sys.executable, "-I", "-c", script], cwd="/", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise PlatformAcceptanceError(
                "cannot inspect installed package: %s" % proc.stderr)
        try:
            result = json.loads(proc.stdout)
        except ValueError as exc:
            raise PlatformAcceptanceError(
                "installed package inspection returned invalid JSON") from exc
        missing_exec = [path for path, executable in
                        zip(result.get("assets", ())[:3],
                            result.get("executable", ()))
                        if not executable]
        missing_metadata = [path for path, readable in
                            zip(result.get("assets", ())[3:],
                                result.get("readable_metadata", ()))
                            if not readable]
        if (missing_exec or missing_metadata or
                len(result.get("assets", ())) != 5):
            raise PlatformAcceptanceError(
                "missing installed package assets: executables=%s metadata=%s" %
                (missing_exec, missing_metadata))
        return result

    def run(self) -> dict:
        self.preflight()
        if os.path.exists(self.root):
            raise PlatformAcceptanceError("refusing to reuse test root: %s" % self.root)
        os.makedirs(self.root)
        os.makedirs(self.artifact_dir, exist_ok=True)
        self.results["package-assets"] = self._verify_package_assets()
        codex_probe = self._exercise_codex_plugin_mcp()
        self.results["codex-plugin-mcp"] = codex_probe
        accepted = self._exercise_review("accept")
        self.results["foreground-review-boundary"] = accepted
        self.results["review-accept"] = accepted
        aborted = self._exercise_review("abort")
        self.results["review-abort"] = aborted
        served = self._exercise_serve_fallback()
        self.results["serve-protocol-cleanliness"] = {
            "session_id": served["session_id"], "clean": True}
        self.results["bound-proc-routing-fallback"] = served
        cleanup_sessions = [accepted["session_id"], aborted["session_id"],
                            served["session_id"]]
        if not codex_probe.get("skipped"):
            cleanup_sessions.append(codex_probe["session_id"])
        self.results["session-cleanup"] = {
            "sessions": cleanup_sessions,
            "mounts_and_sockets_clean": True,
        }
        result = {
            "ok": True,
            "run_id": self.run_id,
            "checks": self.results,
            "test_root": self.root,
            "artifact_dir": self.artifact_dir,
        }
        _atomic_json(os.path.join(self.artifact_dir, "platform-result.json"), result)
        return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic deployed ccc-agent platform acceptance")
    parser.add_argument("manifest", help="platform acceptance manifest JSON")
    args = parser.parse_args(argv)
    result = PlatformAcceptanceRunner(
        load_platform_manifest(args.manifest)).run()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
