#!/usr/bin/env bash
# Run harness self-tests, then optionally run the real user-facing acceptance
# matrix. Real Codex/Claude/Hermes sessions are never started without an explicit
# manifest and CCC_AGENT_ACCEPTANCE=1.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO"

usage() {
    cat <<'EOF'
usage:
  scripts/run-user-facing-acceptance.sh --self-test-only
  scripts/run-user-facing-acceptance.sh --platform MANIFEST
  scripts/run-user-facing-acceptance.sh --cell AGENT TRANSPORT MANIFEST
  scripts/run-user-facing-acceptance.sh MANIFEST [core|full]

--platform  deterministic real BranchFS/bwrap/run/serve/review/routing-fallback
            deployment checks; no model client or credentials required
--cell      run one real user cell; AGENT is codex|claude|hermes and TRANSPORT
            is local-cli|ssh-cli|remote-server
core        actual direct local `ccc-agent run ... -- REAL_CLIENT` flows
full        core + direct SSH CLI + human-observed Desktop/WebUI flows (default)

The platform mode writes only below MANIFEST.test_root and performs no model calls.
Core/full perform real model calls. Every mode first runs non-destructive harness tests.
EOF
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    usage
    exit 0
fi

python3 -m unittest tests.user_facing_acceptance.test_harness \
  tests.user_facing_acceptance.test_manual_observed_driver \
  tests.user_facing_acceptance.test_platform_acceptance -v

if [[ ${1:-} == "--self-test-only" ]]; then
    exit 0
fi
if [[ ${1:-} == "--platform" ]]; then
    if [[ $# -ne 2 || ! -f $2 ]]; then
        echo "platform acceptance manifest not found: ${2:-}" >&2
        exit 2
    fi
    exec python3 -m tests.user_facing_acceptance.platform "$2"
fi
if [[ ${1:-} == "--cell" ]]; then
    if [[ $# -ne 4 || ! -f $4 ]]; then
        echo "usage: $0 --cell AGENT TRANSPORT MANIFEST" >&2
        exit 2
    fi
    case $2 in codex|claude|hermes) ;; *) echo "unknown agent: $2" >&2; exit 2 ;; esac
    case $3 in local-cli|ssh-cli|remote-server) ;;
        *) echo "unknown transport: $3" >&2; exit 2 ;;
    esac
    export CCC_AGENT_ACCEPTANCE=1
    export CCC_AGENT_ACCEPTANCE_MANIFEST=$4
    export CCC_AGENT_ACCEPTANCE_LEVEL=full
    export CCC_AGENT_ACCEPTANCE_AGENT=$2
    export CCC_AGENT_ACCEPTANCE_TRANSPORT=$3
    exec python3 -m unittest tests.user_facing_acceptance.test_real_user_flows -v
fi
if [[ $# -lt 1 || $# -gt 2 ]]; then
    usage >&2
    exit 2
fi

manifest=$1
level=${2:-full}
if [[ ! -f "$manifest" ]]; then
    echo "acceptance manifest not found: $manifest" >&2
    exit 2
fi
case "$level" in
    core|full) ;;
    *) echo "level must be core or full" >&2; exit 2 ;;
esac

export CCC_AGENT_ACCEPTANCE=1
export CCC_AGENT_ACCEPTANCE_MANIFEST=$manifest
export CCC_AGENT_ACCEPTANCE_LEVEL=$level
python3 -m unittest tests.user_facing_acceptance.test_real_user_flows -v
