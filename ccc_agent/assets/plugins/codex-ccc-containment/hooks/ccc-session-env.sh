#!/bin/sh
# Restore the narrow CCC authority/environment handoff for remote Codex hooks.
# This file is sourced by hook scripts; it must not write to stdout.

CCC_SESSION_ENV=${CCC_AGENT_SESSION_ENV_FILE:-/tmp/ccc-agent/session-env.json}
CCC_SESSION_EXPORTS=""
if command -v python3 >/dev/null 2>&1 && [ -r "$CCC_SESSION_ENV" ]; then
    CCC_SESSION_EXPORTS=$(python3 - "$CCC_SESSION_ENV" <<'PY'
import json
import shlex
import sys

allowed = (
    "CCC_AGENT_SESSION", "CCC_AGENT_CONTROL_SOCK",
    "CCC_AGENT_CONTROL_TOKEN", "CCC_AGENT_HOOK_TOKEN",
    "CCC_AGENT_HOOK_SESSION", "CCC_AGENT_CLI",
)
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError, TypeError):
    data = {}
for name in allowed:
    value = data.get(name)
    if isinstance(value, str):
        print("export %s=%s" % (name, shlex.quote(value)))
PY
)
    if [ -n "$CCC_SESSION_EXPORTS" ]; then
        eval "$CCC_SESSION_EXPORTS"
    fi
fi
unset CCC_SESSION_ENV CCC_SESSION_EXPORTS
