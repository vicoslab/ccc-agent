"""Harness for opt-in, real user-facing ccc-agent acceptance tests.

This module deliberately uses only the Python standard library.  It is safe to
import during the ordinary unit suite; real agents are launched only by
``test_real_user_flows`` when CCC_AGENT_ACCEPTANCE=1 and an explicit manifest
are present.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


AGENTS = ("codex", "claude", "hermes")
TRANSPORTS = ("local-cli", "ssh-cli", "remote-server")


class AcceptanceError(RuntimeError):
    """An acceptance contract, prerequisite, or behavioral assertion failed."""


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise AcceptanceError("cannot read JSON %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict):
        raise AcceptanceError("JSON document must be an object: %s" % path)
    return value


def _atomic_json(path: str, value: Mapping) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _context_with_shell_values(context: Mapping[str, object]) -> Dict[str, str]:
    values = {str(key): str(value) for key, value in context.items()}
    for key, value in list(values.items()):
        values[key + "_shell"] = shlex.quote(value)
    return values


def render_command(template: Sequence[str], context: Mapping[str, object]) -> List[str]:
    """Render a trusted argv template.

    Every normal placeholder is inserted as one argv value.  A companion
    ``{name_shell}`` placeholder is shell-quoted for intentionally remote shell
    snippets such as ``ssh host 'cd {workspace_shell} && codex'``.
    """
    if not isinstance(template, list) or not template:
        raise AcceptanceError("command template must be a non-empty JSON list")
    values = _context_with_shell_values(context)
    rendered = []
    for item in template:
        if not isinstance(item, str):
            raise AcceptanceError("command template entries must be strings")
        try:
            rendered.append(item.format_map(values))
        except KeyError as exc:
            raise AcceptanceError(
                "unknown command placeholder %s in %r" % (exc, item)) from exc
    return rendered


@dataclasses.dataclass(frozen=True)
class AcceptanceManifest:
    path: str
    ccc_agent: str
    ccc_agent_config: str
    test_root: str
    artifacts_dir: str
    agents: Mapping[str, Mapping]
    required_transports: Tuple[str, ...]
    timeout_seconds: float = 900.0
    poll_seconds: float = 0.5

    @classmethod
    def load(cls, path: str, level: str = "full") -> "AcceptanceManifest":
        data = _read_json(path)
        if level not in ("core", "full"):
            raise AcceptanceError("acceptance level must be core or full")
        required = ("local-cli",) if level == "core" else TRANSPORTS
        missing_top = [key for key in (
            "ccc_agent", "ccc_agent_config", "test_root", "agents")
            if not data.get(key)]
        if missing_top:
            raise AcceptanceError("manifest missing: %s" % ", ".join(missing_top))

        test_root = os.path.abspath(os.path.expanduser(str(data["test_root"])))
        # This suite creates and later removes test-owned paths.  Refuse broad or
        # ambiguous roots even when an operator accidentally enables it.
        if (test_root in ("/", "/storage", "/storage/user", os.path.expanduser("~"))
                or "ccc-agent-acceptance" not in os.path.basename(test_root)):
            raise AcceptanceError(
                "test_root must be a dedicated directory whose basename contains "
                "'ccc-agent-acceptance': %s" % test_root)

        agents = data.get("agents")
        if not isinstance(agents, dict):
            raise AcceptanceError("manifest agents must be an object")
        for agent in AGENTS:
            if not isinstance(agents.get(agent), dict):
                raise AcceptanceError("manifest missing agent %s" % agent)
            transports = agents[agent].get("transports")
            client_executable = agents[agent].get("client_executable")
            if (not isinstance(client_executable, str) or
                    not os.path.isabs(client_executable) or
                    os.path.basename(client_executable) != agent):
                raise AcceptanceError(
                    "%s requires an absolute real client executable ending in %s" %
                    (agent, agent))
            if not isinstance(transports, dict):
                raise AcceptanceError("%s transports must be an object" % agent)
            for transport in required:
                entry = transports.get(transport)
                if not isinstance(entry, dict):
                    raise AcceptanceError(
                        "%s missing required transport %s" % (agent, transport))
                if entry.get("driver") not in ("tmux", "external"):
                    raise AcceptanceError(
                        "%s %s driver must be tmux or external" %
                        (agent, transport))
                if not isinstance(entry.get("command"), list) or not entry["command"]:
                    raise AcceptanceError(
                        "%s %s requires a command list" % (agent, transport))
                if not entry.get("expected_agent_kind"):
                    raise AcceptanceError(
                        "%s %s requires expected_agent_kind" %
                        (agent, transport))
                if entry.get("final_review_action", "accept") not in (
                        "accept", "abort"):
                    raise AcceptanceError(
                        "%s %s final_review_action must be accept or abort" %
                        (agent, transport))
                if transport == "local-cli":
                    if entry.get("user_flow") != "direct-cli":
                        raise AcceptanceError(
                            "%s local-cli must declare user_flow=direct-cli" % agent)
                    try:
                        separator = entry["command"].index("--")
                        client = entry["command"][separator + 1]
                    except (ValueError, IndexError):
                        raise AcceptanceError(
                            "%s local-cli must invoke its direct client after --" %
                            agent)
                    if str(client) != client_executable:
                        raise AcceptanceError(
                            "%s local-cli direct client executable must be %s" %
                            (agent, client_executable))
                if transport == "ssh-cli":
                    if entry.get("user_flow") != "direct-cli":
                        raise AcceptanceError(
                            "%s ssh-cli must declare user_flow=direct-cli" % agent)
                    if not any(client_executable in str(item)
                               for item in entry["command"]):
                        raise AcceptanceError(
                            "%s ssh-cli must invoke the direct remote client %s" %
                            (agent, client_executable))
                if transport == "remote-server":
                    if entry["driver"] != "external":
                        raise AcceptanceError(
                            "%s remote-server must use an external observed-client "
                            "driver" % agent)
                    if entry.get("user_flow") != "observed-client":
                        raise AcceptanceError(
                            "%s remote-server must declare user_flow=observed-client" %
                            agent)
                    evidence = entry.get("evidence")
                    if not isinstance(evidence, dict):
                        raise AcceptanceError(
                            "%s remote-server requires evidence" % agent)
                    basis = evidence.get("basis")
                    if basis not in ("direct-observation", "official-public-source"):
                        raise AcceptanceError(
                            "%s remote-server evidence basis must be direct-observation "
                            "or official-public-source" % agent)
                    if basis == "direct-observation":
                        missing = [name for name in (
                            "client_product", "client_version", "observed_at", "artifact")
                            if not evidence.get(name)]
                    else:
                        missing = [] if evidence.get("source_urls") else ["source_urls"]
                    if missing:
                        raise AcceptanceError(
                            "%s remote-server evidence missing: %s" %
                            (agent, ", ".join(missing)))
                    instructions = entry.get("operator_instructions")
                    if not isinstance(instructions, list) or not instructions:
                        raise AcceptanceError(
                            "%s remote-server requires operator_instructions" % agent)

        artifacts = data.get("artifacts_dir")
        if not artifacts:
            artifacts = os.path.join(test_root, "artifacts")
        return cls(
            path=os.path.abspath(path),
            ccc_agent=os.path.expanduser(str(data["ccc_agent"])),
            ccc_agent_config=os.path.expanduser(str(data["ccc_agent_config"])),
            test_root=test_root,
            artifacts_dir=os.path.abspath(os.path.expanduser(str(artifacts))),
            agents=agents,
            required_transports=tuple(required),
            timeout_seconds=float(data.get("timeout_seconds", 900)),
            poll_seconds=float(data.get("poll_seconds", 0.5)),
        )

    def agent(self, name: str) -> Mapping:
        try:
            return self.agents[name]
        except KeyError as exc:
            raise AcceptanceError("unknown acceptance agent: %s" % name) from exc

    def transport(self, agent: str, transport: str) -> Mapping:
        try:
            return self.agent(agent)["transports"][transport]
        except KeyError as exc:
            raise AcceptanceError(
                "%s has no %s transport" % (agent, transport)) from exc


@dataclasses.dataclass(frozen=True)
class Scenario:
    run_id: str
    agent: str
    transport: str
    root: str
    workspace: str
    outside_dir: str
    workspace_new: str
    workspace_nested: str
    workspace_modify: str
    workspace_delete: str
    outside_commit: str
    outside_discard: str
    outside_keep: str
    deny_file: str
    review_marker: str
    status_marker: str
    initial_prompt: str
    decision_prompt: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def build_scenario(test_root: str, agent: str, transport: str,
                   run_id: Optional[str] = None) -> Scenario:
    if agent not in AGENTS:
        raise AcceptanceError("unsupported agent: %s" % agent)
    if transport not in TRANSPORTS:
        raise AcceptanceError("unsupported transport: %s" % transport)
    run_id = run_id or "%s-%s-%s" % (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), agent,
        uuid.uuid4().hex[:8])
    root = os.path.join(os.path.abspath(test_root), run_id, agent, transport)
    workspace = os.path.join(root, "workspace")
    outside = os.path.join(root, "outside-workspace")
    paths = {
        "workspace_new": os.path.join(workspace, "new.txt"),
        "workspace_nested": os.path.join(workspace, "nested", "created.txt"),
        "workspace_modify": os.path.join(workspace, "modify.txt"),
        "workspace_delete": os.path.join(workspace, "delete.txt"),
        "outside_commit": os.path.join(outside, "commit.txt"),
        "outside_discard": os.path.join(outside, "discard.txt"),
        "outside_keep": os.path.join(outside, "keep.txt"),
        # Matches setup's default .env.* deny pattern while still inside the
        # workspace, proving deny policy wins over workspace auto-commit.
        "deny_file": os.path.join(workspace, ".env.ccc-agent-acceptance"),
    }
    token = "%s:%s:%s" % (run_id, agent, transport)
    review_marker = "CCC_REVIEW_DECISION_REQUIRED"
    status_marker = "CCC_STATUS_OPERATION_USED"
    initial = f"""CCC-AGENT USER-FACING ACCEPTANCE TEST {token}

Use real file/terminal tools; do not merely describe or simulate these actions.
Do not call ccc-agent turn-finalize, turn-resolve, turn-approve, or workspace
control commands yourself. The native CCC plugin must handle turn boundaries.

Perform exactly these operations:
1. Verify CCC_AGENT_SESSION is non-empty.
2. Write the exact line {token}:new to {paths['workspace_new']}.
3. Create the parent directory and write {token}:nested to {paths['workspace_nested']}.
4. Replace the existing contents of {paths['workspace_modify']} with {token}:modified.
5. Delete the existing file {paths['workspace_delete']}.
6. Write {token}:commit to {paths['outside_commit']}.
7. Write {token}:discard to {paths['outside_discard']}.
8. Write {token}:keep to {paths['outside_keep']}.
9. Write {token}:denied to {paths['deny_file']}.
10. Stop and wait for the CCC plugin's kept-file review. Do not resolve the
    non-workspace or deny-matched files until I answer. Your response must ask me
    whether to commit, discard, or keep the pending files. Prefix that question
    with one token formed by concatenating `CCC_REVIEW_` and
    `DECISION_REQUIRED`. Do not include that token before the question.
"""
    decision = f"""Use the loaded CCC plugin operation/skill now; do not modify the
files directly. Resolve the remembered pending paths exactly as follows:
- COMMIT {paths['outside_commit']}
- DISCARD {paths['outside_discard']}
- KEEP {paths['outside_keep']}
- DISCARD {paths['deny_file']}
Then use the CCC status operation/skill to report compact committed and kept
counts. Only after that status operation returns, include one token formed by
concatenating `CCC_STATUS_` and `OPERATION_USED`. Stop after reporting the
result.
"""
    return Scenario(run_id=run_id, agent=agent, transport=transport,
                    root=root, workspace=workspace, outside_dir=outside,
                    review_marker=review_marker,
                    status_marker=status_marker,
                    initial_prompt=initial, decision_prompt=decision, **paths)


def visible_to_underlay(path: str, roots: Sequence[Mapping[str, object]]) -> str:
    absolute = os.path.abspath(path)
    matches = []
    for root in roots:
        visible = os.path.abspath(str(root.get("visible", "")))
        base = os.path.abspath(str(root.get("base", "")))
        if absolute == visible or absolute.startswith(visible + os.sep):
            matches.append((len(visible), base, os.path.relpath(absolute, visible)))
    if not matches:
        raise AcceptanceError("path is outside configured protected roots: %s" % path)
    _length, base, relative = max(matches)
    return base if relative == "." else os.path.join(base, relative)


class SessionRegistry:
    """Read persisted session records without importing deployment code."""

    def __init__(self, state_dir: str):
        self.state_dir = os.path.abspath(state_dir)

    def _session_files(self) -> Iterable[Tuple[str, str]]:
        if not os.path.isdir(self.state_dir):
            return []
        found = []
        for child in os.listdir(self.state_dir):
            current = os.path.join(
                self.state_dir, child, "session", "session.json")
            legacy = os.path.join(
                self.state_dir, "sessions", child, "session.json")
            if os.path.isfile(current):
                found.append((child, current))
            elif child != "sessions" and os.path.isfile(legacy):
                found.append((child, legacy))
        legacy_root = os.path.join(self.state_dir, "sessions")
        if os.path.isdir(legacy_root):
            for child in os.listdir(legacy_root):
                path = os.path.join(legacy_root, child, "session.json")
                if os.path.isfile(path) and child not in {item[0] for item in found}:
                    found.append((child, path))
        return found

    def ids(self) -> Set[str]:
        return {session_id for session_id, _path in self._session_files()}

    def load(self, session_id: str) -> dict:
        current = os.path.join(
            self.state_dir, session_id, "session", "session.json")
        legacy = os.path.join(
            self.state_dir, "sessions", session_id, "session.json")
        for path in (current, legacy):
            if os.path.isfile(path):
                return _read_json(path)
        raise AcceptanceError("session record not found: %s" % session_id)

    @staticmethod
    def has_events(session: Mapping, required: Set[str]) -> bool:
        names = {entry.get("event") for entry in session.get("events", ())
                 if isinstance(entry, dict)}
        return required.issubset(names)

    def find_new(self, before: Set[str], expected_agent_kind: str,
                 workspace: Optional[str] = None) -> dict:
        matches = []
        for session_id in self.ids() - set(before):
            session = self.load(session_id)
            if session.get("agent_kind") != expected_agent_kind:
                continue
            if workspace and session.get("workspace") not in (workspace, None):
                continue
            matches.append(session)
        if len(matches) != 1:
            raise AcceptanceError(
                "expected one new %s session for %s; found %s" %
                (expected_agent_kind, workspace, [m.get("session_id") for m in matches]))
        return matches[0]

    def wait_new(self, before: Set[str], expected_agent_kind: str,
                 workspace: Optional[str], timeout: float,
                 poll: float) -> dict:
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                return self.find_new(before, expected_agent_kind, workspace)
            except AcceptanceError as exc:
                last_error = exc
                time.sleep(poll)
        raise AcceptanceError("timed out waiting for session: %s" % last_error)

    def wait(self, session_id: str, predicate, description: str,
             timeout: float, poll: float) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.load(session_id)
            if predicate(last):
                return last
            time.sleep(poll)
        raise AcceptanceError(
            "timed out waiting for %s; last session state=%s events=%s" %
            (description, (last or {}).get("state"),
             [entry.get("event") for entry in (last or {}).get("events", ())]))


class TmuxDriver:
    """Small real-TTY driver shared by Codex, Claude, and Hermes CLIs."""

    def __init__(self, name: str, command: Sequence[str], artifact_dir: str):
        self.name = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:80]
        self.command = list(command)
        self.artifact_dir = artifact_dir

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("tmux"))

    def _run(self, *args: str, check: bool = True,
             input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["tmux", *args], input=input_text, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and proc.returncode != 0:
            raise AcceptanceError(
                "tmux %s failed (%d): %s" %
                (" ".join(args), proc.returncode, proc.stderr.strip()))
        return proc

    def start(self) -> None:
        if not self.available():
            raise AcceptanceError("tmux is required for interactive CLI acceptance")
        self._run("new-session", "-d", "-s", self.name,
                  "-x", "140", "-y", "50", *self.command)
        self._run("set-option", "-t", self.name, "history-limit", "20000")

    def alive(self) -> bool:
        return self._run("has-session", "-t", self.name, check=False).returncode == 0

    def send(self, text: str) -> None:
        buffer_name = self.name + "-input"
        self._run("load-buffer", "-b", buffer_name, "-", input_text=text)
        self._run("paste-buffer", "-b", buffer_name, "-t", self.name, "-d")
        self._run("send-keys", "-t", self.name, "Enter")

    def send_key(self, key: str) -> None:
        self._run("send-keys", "-t", self.name, key)

    def capture(self) -> str:
        proc = self._run("capture-pane", "-p", "-J", "-S", "-", "-t", self.name)
        return proc.stdout

    def write_capture(self, name: str) -> str:
        os.makedirs(self.artifact_dir, exist_ok=True)
        path = os.path.join(self.artifact_dir, name)
        with open(path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(self.capture())
        return path

    def wait_exit(self, timeout: float, poll: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return True
            time.sleep(poll)
        return False

    def kill(self) -> None:
        self._run("kill-session", "-t", self.name, check=False)


class AcceptanceRunner:
    """Execute and verify one complete real-agent user flow."""

    FIRST_TURN_EVENTS = {
        "turn-workspace-add", "turn-default-kept",
        "turn-kept-review-requested",
    }
    DECISION_EVENTS = {
        "turn-resolved-commit", "turn-resolved-discard", "turn-resolved-keep"
    }

    def __init__(self, manifest: AcceptanceManifest):
        self.manifest = manifest
        self.deployment_config = _read_json(manifest.ccc_agent_config)
        state_dir = self.deployment_config.get("state_dir")
        if not state_dir:
            raise AcceptanceError("ccc-agent config has no state_dir")
        self.registry = SessionRegistry(str(state_dir))
        roots = self.deployment_config.get("roots")
        if not isinstance(roots, list) or not roots:
            raise AcceptanceError("ccc-agent config has no protected roots")
        self.roots = roots

    def _context(self, scenario: Scenario, scenario_file: str) -> dict:
        return {
            **scenario.to_dict(),
            "ccc_agent": self.manifest.ccc_agent,
            "ccc_agent_config": self.manifest.ccc_agent_config,
            "scenario_file": scenario_file,
        }

    def preflight(self) -> None:
        problems = []
        requested_agent = os.environ.get("CCC_AGENT_ACCEPTANCE_AGENT")
        requested_transport = os.environ.get("CCC_AGENT_ACCEPTANCE_TRANSPORT")
        preflight_agents = (requested_agent,) if requested_agent else AGENTS
        preflight_transports = ((requested_transport,)
                                if requested_transport
                                else self.manifest.required_transports)
        if os.environ.get("CCC_AGENT_SESSION"):
            problems.append(
                "CCC_AGENT_SESSION is set; run acceptance outside containment")
        if not (os.path.isfile(self.manifest.ccc_agent)
                and os.access(self.manifest.ccc_agent, os.X_OK)):
            problems.append("ccc_agent is not executable: %s" % self.manifest.ccc_agent)
        for key in ("branchfs_bin", "bwrap_bin"):
            binary = self.deployment_config.get(key)
            if not binary or not os.access(str(binary), os.X_OK):
                problems.append("%s is not executable: %s" % (key, binary))
        if self.deployment_config.get("backend", "branchfs") != "branchfs":
            problems.append("backend must be branchfs")
        if self.deployment_config.get("confinement") != "bwrap":
            problems.append("confinement must be bwrap")
        if not os.path.exists("/dev/fuse"):
            problems.append("/dev/fuse is absent")
        if not shutil.which("tmux"):
            problems.append("tmux is unavailable for interactive CLI acceptance")
        if not shutil.which("script"):
            problems.append("util-linux script is unavailable for review TTY acceptance")

        # Loading all three integrations is an explicit acceptance requirement.
        # Current deployments that omit Hermes will fail here rather than being
        # incorrectly certified from process-exit fallback alone.
        plugins = self.deployment_config.get("agent_plugins")
        if not isinstance(plugins, dict):
            plugins = {}
        for agent in preflight_agents:
            spec = plugins.get(agent)
            if not isinstance(spec, dict):
                problems.append("agent_plugins.%s is not configured" % agent)
                continue
            source = spec.get("src")
            if not source or not os.path.isdir(os.path.realpath(str(source))):
                problems.append("agent_plugins.%s.src is unavailable: %s" %
                                (agent, source))

        # Catch manifest placeholders and missing client/driver launchers before
        # the first paid model call rather than midway through the matrix.
        for agent in preflight_agents:
            client_executable = self.manifest.agent(agent).get("client_executable")
            if (any(item in ("local-cli", "ssh-cli")
                    for item in preflight_transports) and
                    (not isinstance(client_executable, str) or
                     not (os.path.isfile(client_executable) and
                          os.access(client_executable, os.X_OK)))):
                problems.append(
                    "%s real client executable is unavailable: %s" %
                    (agent, client_executable))
            for transport in preflight_transports:
                scenario = build_scenario(
                    self.manifest.test_root, agent, transport,
                    run_id="preflight")
                context = self._context(
                    scenario, os.path.join(self.manifest.artifacts_dir,
                                           "preflight-scenario.json"))
                entry = self.manifest.transport(agent, transport)
                if "REPLACE_WITH_" in json.dumps(entry, sort_keys=True):
                    problems.append(
                        "%s/%s entry still contains REPLACE_WITH_ placeholder" %
                        (agent, transport))
                try:
                    command = render_command(
                        entry["command"],
                        context)
                except AcceptanceError as exc:
                    problems.append(
                        "%s/%s command template: %s" %
                        (agent, transport, exc))
                    continue
                if any("REPLACE_WITH_" in item for item in command):
                    problems.append(
                        "%s/%s command still contains REPLACE_WITH_ placeholder" %
                        (agent, transport))
                executable = command[0]
                if os.path.sep in executable:
                    command_exists = (os.path.isfile(executable)
                                      and os.access(executable, os.X_OK))
                else:
                    command_exists = shutil.which(executable) is not None
                if not command_exists:
                    problems.append(
                        "%s/%s command executable is unavailable: %s" %
                        (agent, transport, executable))

        try:
            visible_to_underlay(self.manifest.test_root, self.roots)
        except AcceptanceError as exc:
            problems.append(str(exc))
        if problems:
            raise AcceptanceError("preflight failed:\n- " + "\n- ".join(problems))

        proc = subprocess.run(
            [self.manifest.ccc_agent, "--version"], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise AcceptanceError("ccc-agent --version failed: %s" % proc.stderr)

    def _underlay(self, path: str) -> str:
        return visible_to_underlay(path, self.roots)

    def _prepare(self, scenario: Scenario) -> None:
        underlay_root = self._underlay(scenario.root)
        if os.path.exists(underlay_root):
            raise AcceptanceError("refusing to reuse acceptance path: %s" % underlay_root)
        os.makedirs(self._underlay(scenario.workspace), exist_ok=False)
        os.makedirs(self._underlay(scenario.outside_dir), exist_ok=False)
        with open(self._underlay(scenario.workspace_modify), "w",
                  encoding="utf-8") as handle:
            handle.write("seed:modify\n")
        with open(self._underlay(scenario.workspace_delete), "w",
                  encoding="utf-8") as handle:
            handle.write("seed:delete\n")

    def _artifact_dir(self, scenario: Scenario) -> str:
        path = os.path.join(self.manifest.artifacts_dir, scenario.run_id,
                            scenario.agent, scenario.transport)
        os.makedirs(path, exist_ok=True)
        return path

    def _run_cli(self, op: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        command = [self.manifest.ccc_agent, op, "--config",
                   self.manifest.ccc_agent_config, *args]
        proc = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        if check and proc.returncode != 0:
            raise AcceptanceError(
                "%s failed (%d):\nstdout=%s\nstderr=%s" %
                (shlex.join(command), proc.returncode, proc.stdout, proc.stderr))
        return proc

    @staticmethod
    def _assert_file(path: str, expected_fragment: str) -> None:
        if not os.path.isfile(path):
            raise AcceptanceError("expected committed file is absent: %s" % path)
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        if expected_fragment not in content:
            raise AcceptanceError(
                "file %s lacks expected content %r: %r" %
                (path, expected_fragment, content))

    def _verify_first_turn(self, scenario: Scenario, session: Mapping,
                           transcript: str, artifact_dir: str) -> None:
        session_id = str(session["session_id"])
        events = {entry.get("event") for entry in session.get("events", ())}
        missing_events = self.FIRST_TURN_EVENTS - events
        if missing_events:
            raise AcceptanceError(
                "plugin did not prove first-turn use; missing events: %s" %
                sorted(missing_events))
        lowered = transcript.lower()
        if scenario.review_marker not in transcript:
            raise AcceptanceError(
                "agent response omitted the required visible review marker")
        if not all(word in lowered for word in ("commit", "discard", "keep")):
            raise AcceptanceError(
                "agent did not visibly ask commit/discard/keep after plugin review")

        token = "%s:%s:%s" % (scenario.run_id, scenario.agent,
                               scenario.transport)
        self._assert_file(self._underlay(scenario.workspace_new), token + ":new")
        self._assert_file(self._underlay(scenario.workspace_nested), token + ":nested")
        self._assert_file(self._underlay(scenario.workspace_modify), token + ":modified")
        if os.path.exists(self._underlay(scenario.workspace_delete)):
            raise AcceptanceError("workspace deletion was not auto-committed")
        for path in (scenario.outside_commit, scenario.outside_discard,
                     scenario.outside_keep, scenario.deny_file):
            if os.path.exists(self._underlay(path)):
                raise AcceptanceError(
                    "out-of-scope/denied path committed without approval: %s" % path)

        decisions = session.get("policy", {}).get("turn_path_decisions", {})
        expected_committed = {
            scenario.workspace_new, scenario.workspace_nested,
            scenario.workspace_modify, scenario.workspace_delete,
        }
        for path in expected_committed:
            if decisions.get(path) != "committed":
                raise AcceptanceError("workspace path not marked committed: %s" % path)
        for path in (scenario.outside_commit, scenario.outside_discard,
                     scenario.outside_keep, scenario.deny_file):
            if decisions.get(path) != "kept":
                raise AcceptanceError("unsafe path not remembered as kept: %s" % path)

        outputs = {}
        for op, args in (
                ("list", ()), ("show", (session_id,)),
                ("status", (session_id,)), ("diff", (session_id,))):
            proc = self._run_cli(op, *args)
            outputs[op] = proc.stdout + proc.stderr
            with open(os.path.join(artifact_dir, "%s.txt" % op), "w",
                      encoding="utf-8") as handle:
                handle.write(outputs[op])
        if session_id not in outputs["list"]:
            raise AcceptanceError("ccc-agent list omitted active acceptance session")
        shown = json.loads(outputs["show"])
        if shown.get("session_id") != session_id:
            raise AcceptanceError("ccc-agent show returned the wrong session")
        for path in (scenario.workspace_new, scenario.workspace_nested,
                     scenario.workspace_modify, scenario.workspace_delete,
                     scenario.outside_commit, scenario.outside_discard,
                     scenario.outside_keep, scenario.deny_file):
            if path not in outputs["status"]:
                raise AcceptanceError("ccc-agent status omitted %s" % path)
        for path in (scenario.outside_commit, scenario.outside_discard,
                     scenario.outside_keep, scenario.deny_file):
            if path not in outputs["diff"]:
                raise AcceptanceError("ccc-agent diff omitted pending path %s" % path)

    def _verify_decisions(self, scenario: Scenario, session: Mapping,
                          transcript: str) -> None:
        events = {entry.get("event") for entry in session.get("events", ())}
        missing_events = self.DECISION_EVENTS - events
        if missing_events:
            raise AcceptanceError(
                "agent did not use all CCC resolution actions; missing events: %s" %
                sorted(missing_events))
        if scenario.status_marker not in transcript:
            raise AcceptanceError(
                "agent response omitted the required CCC status-use marker")
        token = "%s:%s:%s" % (scenario.run_id, scenario.agent,
                               scenario.transport)
        self._assert_file(
            self._underlay(scenario.outside_commit), token + ":commit")
        for path in (scenario.outside_discard, scenario.outside_keep,
                     scenario.deny_file):
            if os.path.exists(self._underlay(path)):
                raise AcceptanceError("unexpected committed path after decisions: %s" % path)
        decisions = session.get("policy", {}).get("turn_path_decisions", {})
        if decisions.get(scenario.outside_commit) != "committed":
            raise AcceptanceError("agent-selected commit was not persisted")
        if decisions.get(scenario.outside_keep) != "kept":
            raise AcceptanceError("agent-selected keep was not persisted")
        for path in (scenario.outside_discard, scenario.deny_file):
            if path in decisions:
                raise AcceptanceError("discarded path remains remembered: %s" % path)

    def _review_later_then_finish(self, scenario: Scenario, session_id: str,
                                  artifact_dir: str,
                                  final_action: str) -> None:
        if not shutil.which("script"):
            raise AcceptanceError("util-linux script is required for review TTY test")
        command = [self.manifest.ccc_agent, "review", "--config",
                   self.manifest.ccc_agent_config, session_id]
        proc = subprocess.run(
            ["script", "-qefc", shlex.join(command), "/dev/null"],
            input="l\n", text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120)
        review_output = proc.stdout + proc.stderr
        with open(os.path.join(artifact_dir, "review-later.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(review_output)
        if proc.returncode != 0:
            raise AcceptanceError("interactive review later failed: %s" % review_output)
        for required in ("[c] commit all changes", "[s] selective accept",
                         "[d] discard all changes", "[l] keep for later review"):
            if required not in review_output:
                raise AcceptanceError("review prompt omitted %r" % required)
        if self.registry.load(session_id).get("state") != "pending-review":
            raise AcceptanceError("review later did not preserve pending session")

        if final_action == "accept":
            proc = self._run_cli("review", session_id, "--accept")
            with open(os.path.join(artifact_dir, "review-accept.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write(proc.stdout + proc.stderr)
            token = "%s:%s:%s" % (scenario.run_id, scenario.agent,
                                   scenario.transport)
            self._assert_file(
                self._underlay(scenario.outside_keep), token + ":keep")
            if self.registry.load(session_id).get("state") != "committed":
                raise AcceptanceError(
                    "review --accept did not mark session committed")
            return

        proc = self._run_cli("abort", session_id)
        with open(os.path.join(artifact_dir, "abort.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(proc.stdout + proc.stderr)
        if os.path.exists(self._underlay(scenario.outside_keep)):
            raise AcceptanceError("ccc-agent abort committed the kept path")
        if self.registry.load(session_id).get("state") != "aborted":
            raise AcceptanceError("ccc-agent abort did not mark session aborted")

    def _wait_plugin_turn(self, session_id: str) -> dict:
        return self.registry.wait(
            session_id,
            lambda value: self.registry.has_events(value, self.FIRST_TURN_EVENTS),
            "native plugin workspace/turn commit/default-keep/review",
            self.manifest.timeout_seconds, self.manifest.poll_seconds)

    def _wait_decisions(self, session_id: str) -> dict:
        return self.registry.wait(
            session_id,
            lambda value: self.registry.has_events(value, self.DECISION_EVENTS),
            "agent-driven commit/discard/keep resolution",
            self.manifest.timeout_seconds, self.manifest.poll_seconds)

    def _wait_terminal_review(self, session_id: str) -> dict:
        return self.registry.wait(
            session_id,
            lambda value: value.get("state") == "pending-review",
            "pending-review after agent exit",
            self.manifest.timeout_seconds, self.manifest.poll_seconds)

    def _run_tmux(self, scenario: Scenario, entry: Mapping,
                  scenario_file: str, artifact_dir: str) -> dict:
        context = self._context(scenario, scenario_file)
        command = render_command(entry["command"], context)
        expected_kind = str(entry["expected_agent_kind"]).format_map(
            _context_with_shell_values(context))
        before = self.registry.ids()
        driver = TmuxDriver(
            "ccc-%s-%s-%s" % (scenario.agent, scenario.transport,
                               scenario.run_id[-8:]),
            command, artifact_dir)
        driver.start()
        try:
            time.sleep(float(entry.get("startup_seconds", 5)))
            driver.send(scenario.initial_prompt)
            session = self.registry.wait_new(
                before, expected_kind, scenario.workspace,
                self.manifest.timeout_seconds, self.manifest.poll_seconds)
            session_id = str(session["session_id"])
            session = self._wait_plugin_turn(session_id)
            transcript = driver.capture()
            driver.write_capture("first-turn.txt")
            self._verify_first_turn(
                scenario, session, transcript, artifact_dir)

            driver.send(scenario.decision_prompt)
            session = self._wait_decisions(session_id)
            driver.write_capture("decision-turn.txt")
            self._verify_decisions(scenario, session, driver.capture())

            driver.send(str(entry.get("exit_text", "/exit")))
            if not driver.wait_exit(float(entry.get("exit_timeout_seconds", 90)),
                                    self.manifest.poll_seconds):
                driver.send_key("C-d")
            if not driver.wait_exit(30, self.manifest.poll_seconds):
                raise AcceptanceError("agent CLI did not exit cleanly")
            session = self._wait_terminal_review(session_id)
            self._review_later_then_finish(
                scenario, session_id, artifact_dir,
                str(entry.get("final_review_action", "accept")))
            return self.registry.load(session_id)
        finally:
            if driver.alive():
                driver.write_capture("failure-pane.txt")
                driver.kill()

    @staticmethod
    def _validate_external_result(
            value: Mapping, expected_evidence: Optional[Mapping] = None) -> None:
        required_true = (
            "used_official_client", "server_started_through_ssh_router",
            "protocol_clean", "plugin_loaded", "plugin_used",
            "workspace_registered", "asked_user", "status_used",
        )
        missing = [name for name in required_true if value.get(name) is not True]
        if missing:
            raise AcceptanceError(
                "external driver did not prove: %s" % ", ".join(missing))
        evidence = value.get("evidence")
        if (not isinstance(evidence, dict) or
                evidence.get("basis") != "direct-observation"):
            raise AcceptanceError(
                "external official-client result requires direct observation evidence")
        missing_evidence = [name for name in (
            "client_product", "client_version", "observed_at", "artifact")
            if not evidence.get(name)]
        if missing_evidence:
            raise AcceptanceError(
                "external direct observation evidence missing: %s" %
                ", ".join(missing_evidence))
        if expected_evidence is not None:
            mismatched = [name for name in (
                "basis", "client_product", "client_version", "observed_at", "artifact")
                if evidence.get(name) != expected_evidence.get(name)]
            if mismatched:
                raise AcceptanceError(
                    "external observation does not match manifest evidence: %s" %
                    ", ".join(mismatched))
        inventory = value.get("plugin_inventory")
        if not isinstance(inventory, dict):
            raise AcceptanceError("external driver omitted plugin_inventory")
        if not inventory.get("hooks") or not inventory.get("skills"):
            raise AcceptanceError(
                "external driver must prove both hooks and skills were listed")

    def _wait_external_phase(self, result_file: str, phase: str,
                             proc: subprocess.Popen) -> dict:
        deadline = time.monotonic() + self.manifest.timeout_seconds
        last_error = None
        while time.monotonic() < deadline:
            if os.path.isfile(result_file):
                try:
                    value = _read_json(result_file)
                except AcceptanceError as exc:
                    # The contract requires atomic writes, but tolerate a short
                    # read race so the eventual failure reports the real phase.
                    last_error = exc
                else:
                    if value.get("phase") == phase:
                        return value
                    last_error = AcceptanceError(
                        "expected external driver phase %s, found %s" %
                        (phase, value.get("phase")))
            if proc.poll() is not None:
                break
            time.sleep(self.manifest.poll_seconds)
        raise AcceptanceError(
            "external driver did not reach phase %s before exit/timeout: %s" %
            (phase, last_error or "no result file"))

    def _run_external(self, scenario: Scenario, entry: Mapping,
                      scenario_file: str, artifact_dir: str) -> dict:
        context = self._context(scenario, scenario_file)
        command = render_command(entry["command"], context)
        expected_kind = str(entry["expected_agent_kind"]).format_map(
            _context_with_shell_values(context))
        before = self.registry.ids()
        result_file = os.path.join(artifact_dir, "external-driver-result.json")
        continue_file = os.path.join(artifact_dir, "external-driver-continue")
        stdout_file = os.path.join(artifact_dir, "external-driver.stdout")
        stderr_file = os.path.join(artifact_dir, "external-driver.stderr")
        payload = _read_json(scenario_file)
        payload["driver_result_file"] = result_file
        payload["driver_continue_file"] = continue_file
        payload["evidence"] = entry.get("evidence")
        payload["operator_instructions"] = entry.get("operator_instructions")
        _atomic_json(scenario_file, payload)
        env = dict(os.environ)
        env.update({
            "CCC_AGENT_ACCEPTANCE_SCENARIO": scenario_file,
            "CCC_AGENT_ACCEPTANCE_RESULT": result_file,
            "CCC_AGENT_ACCEPTANCE_CONTINUE": continue_file,
            "CCC_AGENT_ACCEPTANCE_INITIAL_PROMPT": scenario.initial_prompt,
            "CCC_AGENT_ACCEPTANCE_DECISION_PROMPT": scenario.decision_prompt,
        })

        proc = None
        with open(stdout_file, "w", encoding="utf-8") as stdout_handle, \
                open(stderr_file, "w", encoding="utf-8") as stderr_handle:
            proc = subprocess.Popen(
                command, env=env, text=True, stdout=stdout_handle,
                stderr=stderr_handle)
            try:
                first_result = self._wait_external_phase(
                    result_file, "first-turn-ready", proc)
                session = self.registry.wait_new(
                    before, expected_kind, scenario.workspace,
                    self.manifest.timeout_seconds, self.manifest.poll_seconds)
                session_id = str(session["session_id"])
                session = self._wait_plugin_turn(session_id)
                first_transcript = "\n".join(
                    str(first_result.get(key, ""))
                    for key in ("first_response", "transcript"))
                # This runs while the official client/server is paused and open,
                # proving per-turn behavior rather than only finalization.
                self._verify_first_turn(
                    scenario, session, first_transcript, artifact_dir)

                with open(continue_file, "x", encoding="utf-8") as handle:
                    handle.write("continue\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    returncode = proc.wait(timeout=self.manifest.timeout_seconds)
                except subprocess.TimeoutExpired as exc:
                    raise AcceptanceError(
                        "external driver timed out after first-turn release") from exc
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

        with open(os.path.join(artifact_dir, "external-driver.log"), "w",
                  encoding="utf-8") as handle:
            handle.write("$ %s\nstdout: %s\nstderr: %s\n" %
                         (shlex.join(command), stdout_file, stderr_file))
        if returncode != 0:
            try:
                with open(stderr_file, encoding="utf-8") as handle:
                    stderr = handle.read()
            except OSError:
                stderr = ""
            raise AcceptanceError(
                "remote official-client driver failed (%d): %s" %
                (returncode, stderr))

        result = self._wait_external_phase(result_file, "complete", proc)
        self._validate_external_result(result, entry.get("evidence"))
        transcript = "\n".join(str(result.get(key, "")) for key in (
            "first_response", "decision_response", "transcript"))
        session = self._wait_decisions(session_id)
        self._verify_decisions(scenario, session, transcript)
        session = self._wait_terminal_review(session_id)
        self._review_later_then_finish(
            scenario, session_id, artifact_dir,
            str(entry.get("final_review_action", "accept")))
        return self.registry.load(session_id)

    def run(self, agent: str, transport: str) -> dict:
        entry = self.manifest.transport(agent, transport)
        scenario = build_scenario(self.manifest.test_root, agent, transport)
        self._prepare(scenario)
        artifact_dir = self._artifact_dir(scenario)
        scenario_file = os.path.join(artifact_dir, "scenario.json")
        _atomic_json(scenario_file, scenario.to_dict())
        try:
            if entry["driver"] == "tmux":
                session = self._run_tmux(
                    scenario, entry, scenario_file, artifact_dir)
            else:
                session = self._run_external(
                    scenario, entry, scenario_file, artifact_dir)
            _atomic_json(os.path.join(artifact_dir, "final-session.json"), session)
            return session
        except Exception as exc:
            with open(os.path.join(artifact_dir, "FAILURE.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write("%s: %s\n" % (type(exc).__name__, exc))
            raise
