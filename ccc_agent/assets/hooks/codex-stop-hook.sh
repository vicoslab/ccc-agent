#!/bin/sh
# Codex CLI Stop-hook adapter for CCC agent sessions.
#
# Register through Codex managed requirements/config (trusted path), pointing
# the Stop hook at this script. Semantics match the Claude adapter: run the
# bounded self-repair check (exit 2 + instructions on stderr asks the harness
# to continue so the agent reverts), then report turn completion. Never
# commit from a hook.
set -eu

CTL="${CCC_AGENT_CLI:-ccc-agent}"

[ -n "${CCC_AGENT_SESSION:-}" ] || exit 0
command -v "$CTL" >/dev/null 2>&1 || exit 0

# Per-turn control: signal end-of-turn to the supervisor over the control
# socket (the in-sandbox agent can't reach the store). It commits in-scope
# changes and default-keeps new out-of-scope paths in the BranchFS branch, so
# intermediate autonomous loops do not become approval gates. Final process /
# session review can still ask the user whether kept paths should be committed
# or discarded.
if [ -n "${CCC_AGENT_CONTROL_SOCK:-}" ]; then
    rc=0
    "$CTL" turn-finalize --default-keep || rc=$?
    exit "$rc"
fi

# Fallback (no control socket — dev/none mode): store-based bounded self-repair.
rc=0
"$CTL" turn-check "$CCC_AGENT_SESSION" 1>&2 || rc=$?
[ "$rc" -ne 2 ] || exit 2   # dirty + budget left: agent should repair

"$CTL" turn-record "$CCC_AGENT_SESSION" || true
exit 0
