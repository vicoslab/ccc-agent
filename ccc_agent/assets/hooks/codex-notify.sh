#!/bin/sh
# Codex `notify` adapter for CCC agent sessions.
#
# Codex has no Claude-style Stop hook; instead it calls a `notify` program with
# a single JSON event argument on certain events (notably agent-turn-complete).
# Register it in ~/.codex/config.toml (or via -c):
#
#   notify = ["/opt/ccc-agent/hooks/codex-notify.sh"]
#
# On turn completion this signals end-of-turn to the supervisor over the control
# socket, which commits the turn's in-scope changes.
#
# IMPORTANT LIMITATION: codex ignores the notify program's exit code and does
# NOT wait on it, so — unlike the Claude Stop hook — it CANNOT block the turn
# for out-of-scope approval.  For codex, out-of-scope changes are therefore
# deferred to session-end review (pending-review) rather than prompted mid-turn.
# Use `codex exec` (one turn per process) or Claude Code if you need the
# blocking per-turn approval flow.
set -eu

CTL="${CCC_AGENT_CLI:-ccc-agent}"

[ -n "${CCC_AGENT_SESSION:-}" ] || exit 0
[ -n "${CCC_AGENT_CONTROL_SOCK:-}" ] || exit 0
command -v "$CTL" >/dev/null 2>&1 || exit 0

event="${1:-}"
case "$event" in
    *'agent-turn-complete'*|*'turn-complete'*|*'turn_complete'*)
        # report-only: commit in-scope changes; out-of-scope defers to review
        "$CTL" turn-finalize 1>&2 || true
        ;;
esac
exit 0
