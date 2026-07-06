#!/bin/sh
# Generic transparent launch shim for agent CLIs (codex, claude, hermes,
# opencode, ...). Install by symlinking this file into a PATH directory that
# precedes the real binary, named after the tool:
#
#   ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/codex
#
# Behavior:
#   - resolves the real binary as the next match in PATH after this shim,
#     also checking ~/.local/bin for user-level agent installs;
#   - nested agents (CCC_AGENT_SESSION set) run directly: the launcher reuses
#     the existing session, so no new branch bundle is created;
#   - CCC_AGENT_SHIM_BYPASS=1 skips containment entirely (debug only; policy
#     may forbid it on managed deployments).
set -eu

AGENT_NAME="$(basename "$0")"
SHIM_PATH="$(command -v -- "$AGENT_NAME" || true)"
CODEX_DISABLE_INNER_SANDBOX_ARG="--dangerously-bypass-approvals-and-sandbox"

codex_inner_sandbox_state() {
    # Return codes:
    #   0: nested Codex should receive CODEX_DISABLE_INNER_SANDBOX_ARG
    #   1: no change needed (non-Codex, --yolo, explicit no-sandbox mode)
    #   2: explicit nested Codex sandbox requested; refuse before it hangs
    [ "$AGENT_NAME" = "codex" ] || return 1
    expect_sandbox_value=0
    for arg in "$@"; do
        if [ "$expect_sandbox_value" = "1" ]; then
            [ "$arg" = "danger-full-access" ] && return 1
            return 2
        fi
        case "$arg" in
            "$CODEX_DISABLE_INNER_SANDBOX_ARG"|--yolo|--sandbox=danger-full-access|-s=danger-full-access)
                return 1
                ;;
            --sandbox|-s)
                expect_sandbox_value=1
                ;;
            --sandbox=*|-s=*)
                return 2
                ;;
        esac
    done
    [ "$expect_sandbox_value" = "1" ] && return 2
    return 0
}

# Find the real binary: first PATH entry whose $AGENT_NAME is not this shim.
# Also check ~/.local/bin because Codex/Claude are commonly installed there,
# and CCC startup/service environments may not include it in PATH.
REAL_BIN=""
SEARCH_PATH="$PATH"
if [ -n "${HOME:-}" ]; then
    USER_LOCAL_BIN="${HOME%/}/.local/bin"
    case ":$SEARCH_PATH:" in
        *":$USER_LOCAL_BIN:"*) ;;
        *) SEARCH_PATH="$SEARCH_PATH:$USER_LOCAL_BIN" ;;
    esac
fi
OLD_IFS="$IFS"; IFS=:
for dir in $SEARCH_PATH; do
    candidate="$dir/$AGENT_NAME"
    [ -x "$candidate" ] || continue
    if [ "$candidate" != "$SHIM_PATH" ] && [ ! "$candidate" -ef "$SHIM_PATH" ]; then
        REAL_BIN="$candidate"
        break
    fi
done
IFS="$OLD_IFS"

if [ -z "$REAL_BIN" ]; then
    echo "ccc-agent-shim: no real '$AGENT_NAME' binary found in PATH or ~/.local/bin" >&2
    exit 127
fi

if [ "${CCC_AGENT_SHIM_BYPASS:-0}" = "1" ]; then
    echo "ccc-agent-shim: bypass enabled, running $REAL_BIN unprotected" >&2
    exec "$REAL_BIN" "$@"
fi

if [ -n "${CCC_AGENT_SESSION:-}" ]; then
    # already inside a contained session: run directly, stay in the branch
    set +e
    codex_inner_sandbox_state "$@"
    codex_sandbox_state=$?
    set -e
    if [ "$codex_sandbox_state" = "0" ]; then
        echo "ccc-agent-shim: nested codex inside ccc-agent; disabling Codex inner sandbox (outer containment active)" >&2
        exec "$REAL_BIN" "$CODEX_DISABLE_INNER_SANDBOX_ARG" "$@"
    elif [ "$codex_sandbox_state" = "2" ]; then
        echo "ccc-agent-shim: refusing nested Codex sandbox inside ccc-agent; use --yolo/--sandbox danger-full-access or omit --sandbox so the shim can disable Codex's inner sandbox" >&2
        exit 2
    fi
    exec "$REAL_BIN" "$@"
fi

LAUNCH="${CCC_AGENT_CLI:-ccc-agent}"
if ! command -v "$LAUNCH" >/dev/null 2>&1; then
    echo "ccc-agent-shim: launcher not found at $LAUNCH" >&2
    echo "ccc-agent-shim: refusing to run '$AGENT_NAME' unprotected (set CCC_AGENT_SHIM_BYPASS=1 to override)" >&2
    exit 1
fi

echo "ccc-agent-shim: containing '$AGENT_NAME' via $LAUNCH (real: $REAL_BIN)" >&2
exec "$LAUNCH" run --agent "$AGENT_NAME" -- "$REAL_BIN" "$@"
