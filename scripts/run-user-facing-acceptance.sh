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
  scripts/run-user-facing-acceptance.sh MANIFEST [core|full]

core  actual local `ccc-agent run` interactive flow for Codex, Claude, Hermes
full  core + direct SSH CLI + official desktop/server protocol flow (default)

This command performs real model calls and writes only below MANIFEST.test_root.
It first runs the non-destructive harness tests. The real suite remains opt-in.
EOF
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    usage
    exit 0
fi

python3 -m unittest tests.user_facing_acceptance.test_harness -v

if [[ ${1:-} == "--self-test-only" ]]; then
    exit 0
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
