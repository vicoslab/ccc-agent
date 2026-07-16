#!/bin/sh
# CCC contained-session workspace lifecycle adapter for Codex.
#
# Codex documents SessionStart plus SubagentStart/SubagentStop lifecycle hooks.
# There is no root SessionEnd event today, so the root SessionStart workspace is
# kept until the outer ccc-agent run exits/resumes and the trusted supervisor
# clears hook-owned dynamic scopes. Subagent scopes are removed on SubagentStop.
#
# Keep stdout empty: Codex command-hook stdout may be parsed as hook output.
set -eu

# Codex app-server may rebuild the environment before running hooks. Restore
# only the launcher-owned, allowlisted CCC values from the mounted handoff.
CCC_HOOK_DIR=$(dirname "$0")
if [ -r "$CCC_HOOK_DIR/ccc-session-env.sh" ]; then
    . "$CCC_HOOK_DIR/ccc-session-env.sh"
fi
unset CCC_HOOK_DIR

# Not a contained ccc-agent run, or no supervisor control channel: inert.
if [ -z "${CCC_AGENT_SESSION:-}" ] || [ -z "${CCC_AGENT_CONTROL_SOCK:-}" ]; then
    exit 0
fi

# Workspace commands are hook-token gated. If the launcher did not expose the
# hook token to this trusted hook environment, degrade safely.
if [ -z "${CCC_AGENT_HOOK_TOKEN:-}" ]; then
    exit 0
fi

CTL="${CCC_AGENT_CLI:-ccc-agent}"
if ! command -v "$CTL" >/dev/null 2>&1; then
    exit 0
fi

PAYLOAD=$(cat || true)
CCC_AGENT_HOOK_PAYLOAD=$PAYLOAD python3 - "$CTL" >/dev/null 2>&1 <<'PY' || true
import json
import os
import subprocess
import sys

ctl = sys.argv[1]
try:
    data = json.loads(os.environ.get("CCC_AGENT_HOOK_PAYLOAD", "") or "{}")
except Exception:
    data = {}

event = str(data.get("hook_event_name") or "")
if event in ("SessionStart", "SubagentStart"):
    cmd_name = "turn-add-workspace"
elif event == "SubagentStop":
    cmd_name = "turn-remove-workspace"
else:
    raise SystemExit(0)

parent_session = str(
    data.get("session_id")
    or os.environ.get("CCC_AGENT_HOOK_SESSION")
    or os.environ.get("CCC_AGENT_SESSION")
    or ""
)
agent_id = data.get("agent_id")
if event.startswith("Subagent") and agent_id:
    agent_session = "%s/%s" % (parent_session, str(agent_id))
else:
    agent_session = parent_session

workspace = str(data.get("cwd") or os.environ.get("PWD") or os.getcwd())
if not agent_session or not workspace:
    raise SystemExit(0)

subprocess.run(
    [ctl, cmd_name, "--agent-session", agent_session, workspace],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
PY

exit 0
