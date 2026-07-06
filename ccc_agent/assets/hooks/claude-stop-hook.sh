#!/bin/sh
# Claude Code Stop-hook adapter for CCC agent sessions.
#
# Register from a TRUSTED (read-only to the agent) settings path, e.g.
# managed settings or launcher-injected --settings:
#
#   {
#     "hooks": {
#       "Stop": [{
#         "hooks": [{
#           "type": "command",
#           "command": "/opt/ccc-agent/hooks/claude-stop-hook.sh"
#         }]
#       }]
#     }
#   }
#
# Hooks REPORT lifecycle events and may BLOCK the stop for bounded
# self-repair; they never freeze, commit, or abort.
set -eu

CTL="${CCC_AGENT_CLI:-ccc-agent}"

if [ -z "${CCC_AGENT_SESSION:-}" ]; then
    # not a contained session (e.g. human-run claude outside ccc-agent run)
    exit 0
fi

if ! command -v "$CTL" >/dev/null 2>&1; then
    echo "ccc claude hook: ccc-agent not found at $CTL" >&2
    exit 0   # never block the agent's stop on hook plumbing problems
fi

# Per-turn control: inside a contained session the agent cannot reach the
# BranchFS store, so signal end-of-turn to the supervisor over the control
# socket. It commits the turn's in-scope changes and default-keeps new
# out-of-scope paths in the BranchFS branch, so intermediate autonomous loops do
# not become approval gates. Final process/session review can still ask the user
# whether kept paths should be committed or discarded.
if [ -n "${CCC_AGENT_CONTROL_SOCK:-}" ]; then
    rc=0
    "$CTL" turn-finalize --default-keep || rc=$?
    exit "$rc"
fi

# Fallback (no control socket — dev/none mode): store-based bounded self-repair.
# Exit 2 ("dirty, repair budget left") blocks the stop so the agent reverts the
# flagged paths; any other outcome must NOT block (finalize parks dirty sessions
# as pending-review for a human).
rc=0
"$CTL" turn-check "$CCC_AGENT_SESSION" 1>&2 || rc=$?
if [ "$rc" -eq 2 ]; then
    exit 2
fi

"$CTL" turn-record "$CCC_AGENT_SESSION" || true
exit 0
