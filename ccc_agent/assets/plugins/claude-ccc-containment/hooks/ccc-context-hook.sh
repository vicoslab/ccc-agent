#!/bin/sh
# CCC Claude Code context hook.
#
# Claude plugin skills are model-invoked, not hard-preloaded.  This hook makes
# the contained-session CCC rule explicit by injecting the bundled ccc-containment
# skill at SessionStart, reminding before each user prompt, and asking for a
# kept-file decision when Claude tries to stop with non-workspace changes still
# held in the branch.
set -eu

# Claude's remote server may rebuild the environment before launching ccd-cli.
# Recover only allowlisted ccc-agent values from the launcher-owned JSON file.
# The path override exists for isolated hook tests; JSON values are shell-quoted
# by Python before eval and cannot add commands or arbitrary variable names.
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

# Direct/uncontained Claude runs should not see CCC behavior.
if [ -z "${CCC_AGENT_SESSION:-}" ]; then
    exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
    exit 0
fi

INPUT=$(python3 -c 'import sys; print(sys.stdin.read(), end="")' 2>/dev/null || true)
EVENT=$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    data={}
print(data.get("hook_event_name", ""))' 2>/dev/null || true)
STOP_ACTIVE=$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    data={}
print("1" if data.get("stop_hook_active") else "0")' 2>/dev/null || true)

PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-}}
if [ -z "$PLUGIN_ROOT" ]; then
    PLUGIN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
fi
SKILL_PATH="$PLUGIN_ROOT/skills/ccc-containment/SKILL.md"
CTL="${CCC_AGENT_CLI:-ccc-agent}"

emit_context() {
    hook_event=$1
    python3 -c 'import json, sys
hook_event = sys.argv[1]
text = sys.stdin.read()
if not text.strip():
    sys.exit(0)
print(json.dumps({"hookSpecificOutput": {"hookEventName": hook_event,
                                          "additionalContext": text}},
                 separators=(",", ":")))' "$hook_event"
}

skill_body() {
    python3 - "$SKILL_PATH" <<'PY'
import os
import sys

path = sys.argv[1]
try:
    text = open(path, encoding="utf-8").read()
except OSError:
    text = ""
# Keep the human instructions and drop YAML frontmatter noise.
if text.startswith("---"):
    parts = text.split("---", 2)
    if len(parts) >= 3:
        text = parts[2]
print(text.strip())
PY
}

HOOK_WORKSPACE=$(printf '%s' "$INPUT" | python3 -c 'import json, os, sys
try:
    data=json.load(sys.stdin)
except Exception:
    data={}
for key in ("workspace", "workspace_dir", "cwd", "current_working_directory"):
    value=data.get(key)
    if value:
        print(value)
        break
else:
    print(os.getcwd())' 2>/dev/null || pwd)
HOOK_SESSION=$(printf '%s' "$INPUT" | python3 -c 'import json, os, sys
try:
    data=json.load(sys.stdin)
except Exception:
    data={}
for key in ("session_id", "conversation_id", "thread_id", "transcript_path"):
    value=data.get(key)
    if value:
        print(value)
        break
else:
    print(os.environ.get("CCC_AGENT_HOOK_SESSION") or os.environ.get("CCC_AGENT_SESSION", ""))' 2>/dev/null || true)

workspace_scope() {
    action=$1
    if [ -z "${CCC_AGENT_CONTROL_SOCK:-}" ] || [ -z "${CCC_AGENT_HOOK_TOKEN:-}" ]; then
        return 0
    fi
    if [ -z "$HOOK_SESSION" ] || [ -z "$HOOK_WORKSPACE" ]; then
        return 0
    fi
    if ! command -v "$CTL" >/dev/null 2>&1; then
        return 0
    fi
    if [ "$action" = "add" ]; then
        workspace_cmd=turn-add-workspace
    else
        workspace_cmd=turn-remove-workspace
    fi
    "$CTL" "$workspace_cmd" --agent-session "$HOOK_SESSION" \
        "$HOOK_WORKSPACE" >/dev/null 2>&1 || true
}

case "$EVENT" in
    SessionStart)
        # Claude documents CLAUDE_ENV_FILE as the supported way for a
        # SessionStart hook to persist variables into later Bash tool calls.
        if [ -n "${CLAUDE_ENV_FILE:-}" ] && \
                [ -n "$CCC_SESSION_EXPORTS" ]; then
            printf '%s\n' "$CCC_SESSION_EXPORTS" >> "$CLAUDE_ENV_FILE"
        fi
        workspace_scope add
        BODY=$(skill_body)
        if [ -n "$BODY" ]; then
            printf '%s\n\n%s\n' \
                "CCC contained-session skill ccc-containment is active because CCC_AGENT_SESSION is set. Its rules are part of the current session context." \
                "$BODY" | emit_context SessionStart
        fi
        ;;
    SessionEnd)
        workspace_scope remove
        ;;
    UserPromptSubmit)
        printf '%s\n' \
            "CCC contained-session reminder: workspace/in-policy files are written through by the supervisor, while non-workspace or out-of-policy files stay separate. Do not run turn-resolve or turn-approve. When work is finished and Claude would otherwise idle, use ccc_status and the ccc MCP kept-path tools. Commit/discard perform their own required human elicitation." \
            | emit_context UserPromptSubmit
        ;;
    Stop)
        # The main Stop hook already finalized the turn with --default-keep.
        # If it kept anything, continue the conversation once so Claude asks the
        # user for the required commit/discard/keep decision.  When Claude is
        # already continuing because of a Stop hook, do not create a loop.
        if [ "$STOP_ACTIVE" = "1" ]; then
            exit 0
        fi
        if ! command -v "$CTL" >/dev/null 2>&1; then
            exit 0
        fi
        rc=0
        REVIEW=$("$CTL" turn-review-kept 2>&1) || rc=$?
        if [ "$rc" -eq 2 ] && [ -n "$REVIEW" ]; then
            printf '%s\n\n%s\n' \
                "CCC contained-session review is pending. The supervisor kept non-workspace or out-of-policy files separate and they are not committed. Use ccc_status, ccc_list_kept, and the appropriate ccc MCP resolution tool. Commit/discard perform required nested human elicitation; do not run turn-resolve or turn-approve directly." \
                "$REVIEW" | emit_context Stop
        fi
        ;;
esac

exit 0
