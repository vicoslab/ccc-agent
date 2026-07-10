#!/bin/sh
# CCC contained-session Stop-hook adapter (Claude Code / Codex plugin).
#
# Loaded for contained CCC sessions through agent-native plugin config: Claude
# uses a pre-seeded plugin cache (CLAUDE_CODE_PLUGIN_SEED_DIR), and Codex uses a
# read-only installed-plugin cache bind. Direct/non-CCC runs are safe because hooks
# exit immediately when CCC_AGENT_SESSION is absent.
#
# Hooks are best-effort turn-boundary SIGNALS. They never freeze, commit, or
# abort -- commit authority lives in the trusted supervisor outside the
# sandbox, and process-exit finalization is the authoritative fallback. Every
# failure path here degrades safely (exit 0, never block) so a broken/old hook
# contract can only cost per-turn convenience, never containment.
set -eu

CTL="${CCC_AGENT_CLI:-ccc-agent}"

# Not a contained session (e.g. a human-run agent outside ccc-agent run): the
# plugin should be inert.
if [ -z "${CCC_AGENT_SESSION:-}" ]; then
    exit 0
fi

if ! command -v "$CTL" >/dev/null 2>&1; then
    echo "ccc stop-hook: ccc-agent not found ($CTL); per-turn control" \
         "unavailable, session-end review still active" >&2
    exit 0   # never block the agent's stop on hook plumbing problems
fi

# Per-turn control: inside the sandbox the agent cannot reach the BranchFS
# store, so signal end-of-turn to the supervisor over the control socket. It
# commits the turn's in-scope changes and default-keeps new out-of-scope paths
# in the BranchFS branch, so intermediate autonomous loops do not become
# approval gates. Final process/session review can still ask the user whether
# kept paths should be committed or discarded.
if [ -n "${CCC_AGENT_CONTROL_SOCK:-}" ]; then
    rc=0
    "$CTL" turn-finalize --default-keep 1>&2 || rc=$?
    exit "$rc"
fi

# Fallback (no control socket -- dev/none mode): store-based bounded
# self-repair. Exit 2 ("dirty, repair budget left") blocks the stop so the
# agent reverts the flagged paths; any other outcome must NOT block (finalize
# parks dirty sessions as pending-review for a human).
rc=0
"$CTL" turn-check "$CCC_AGENT_SESSION" 1>&2 || rc=$?
if [ "$rc" -eq 2 ]; then
    exit 2
fi

"$CTL" turn-record "$CCC_AGENT_SESSION" || true
exit 0
