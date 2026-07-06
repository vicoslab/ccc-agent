#!/bin/sh
# CCC contained-session Stop-hook adapter (Claude Code / Codex plugin).
#
# Loaded for contained CCC sessions: ccc-agent run injects the enclosing
# plugin read-only and points the agent at it (Claude --plugin-dir, Codex
# installed-plugin cache bind). Codex also keeps a persistent enabled-plugin
# config entry; direct Codex runs are safe because this hook exits immediately
# unless CCC_AGENT_SESSION is set.
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
# commits the turn's in-scope changes and exits 0 (the stop proceeds); on
# out-of-scope changes it exits 2 with the offending paths and an approval
# token on stderr, which the agent feeds back to the user before running
# `ccc-agent turn-approve <token>`.
if [ -n "${CCC_AGENT_CONTROL_SOCK:-}" ]; then
    rc=0
    "$CTL" turn-finalize 1>&2 || rc=$?
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
