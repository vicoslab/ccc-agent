#!/bin/sh
# CCC contained-session Stop-hook adapter for Codex.
#
# Loaded for contained CCC Codex sessions: ccc-agent run injects this plugin
# read-only into Codex's installed-plugin cache path. Direct Codex runs are safe
# because this hook exits immediately unless CCC_AGENT_SESSION is set.
#
# Hooks are best-effort turn-boundary SIGNALS. Process-exit finalization remains
# the authoritative fallback. The control-socket path commits in-scope changes,
# default-keeps new out-of-scope paths in the branch, then blocks Stop once with
# a kept-file review prompt so Codex asks the user whether to commit, discard, or
# keep those paths.
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

block_once_for_kept_review() {
    review_rc=0
    REVIEW=$("$CTL" turn-review-kept 2>&1) || review_rc=$?
    if [ "$review_rc" -eq 2 ] && [ -n "$REVIEW" ]; then
        marker_dir="${TMPDIR:-/tmp}"
        marker="$marker_dir/ccc-agent-codex-review-kept.${CCC_AGENT_SESSION}"
        tmp="$marker.$$"
        printf '%s\n' "$REVIEW" > "$tmp" || {
            printf '%s\n' "$REVIEW" >&2
            exit 2
        }
        if [ -f "$marker" ] && cmp -s "$marker" "$tmp"; then
            rm -f "$tmp"
            exit 0
        fi
        mv "$tmp" "$marker" 2>/dev/null || true
        printf '%s\n' "$REVIEW" >&2
        exit 2
    fi
    exit 0
}

# Per-turn control: inside the sandbox the agent cannot reach the BranchFS
# store, so signal end-of-turn to the supervisor over the control socket. It
# commits the turn's in-scope changes and default-keeps new out-of-scope paths
# in the BranchFS branch, so intermediate autonomous loops do not become
# approval gates. If the agent is stopping/idling with kept paths, ask for the
# user's commit/discard/keep decision.
if [ -n "${CCC_AGENT_CONTROL_SOCK:-}" ]; then
    rc=0
    "$CTL" turn-finalize --default-keep 1>&2 || rc=$?
    if [ "$rc" -ne 0 ]; then
        exit "$rc"
    fi
    block_once_for_kept_review
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
