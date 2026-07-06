#!/bin/sh
# Hermes hook adapter for CCC agent sessions.
#
# Wire from a CCC-managed Hermes plugin/profile (post_llm_call or gateway
# agent:end), e.g.:
#
#   hooks:
#     post_llm_call:
#       - exec: /opt/ccc-agent/hooks/hermes-turn-record.sh
#
# Reports turn completion only; commit authority stays with the supervisor.
# Deliberately no turn-check here: post_llm_call cannot block/feed
# instructions back, and an advisory check would silently burn the session's
# repair budget. Long-running Hermes sessions are closed by a human/operator
# via `ccc-agent finish`, which freezes and applies policy.
set -eu

CTL="${CCC_AGENT_CLI:-ccc-agent}"

[ -n "${CCC_AGENT_SESSION:-}" ] || exit 0
command -v "$CTL" >/dev/null 2>&1 || exit 0

"$CTL" turn-record "$CCC_AGENT_SESSION" || true
exit 0
