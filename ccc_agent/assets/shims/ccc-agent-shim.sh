#!/bin/sh
# Generic transparent launch shim for agent CLIs (codex, claude, hermes,
# opencode, ...). Install by symlinking this file into a trusted PATH directory
# that precedes the real binary, named after the tool:
#
#   ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/codex
#
# Behavior:
#   - outside an active ccc-agent session, only redirects to
#     `ccc-agent run --agent <name> -- <name> ...` and announces that redirect;
#   - exports a PATH with this shim directory removed so the contained launch can
#     resolve the real binary from the user's active PATH/conda env;
#   - nested agents (CCC_AGENT_SESSION set) run the real command directly from
#     that unshimmed PATH, so no new branch bundle is created;
#   - CCC_AGENT_SHIM_BYPASS=1 skips containment entirely (debug only; policy may
#     forbid it on managed deployments).
set -eu

AGENT_NAME="$(basename "$0")"
SHIM_PATH="$(command -v -- "$AGENT_NAME" 2>/dev/null || true)"
SHIM_DIR=""
case "$SHIM_PATH" in
    */*) SHIM_DIR="${SHIM_PATH%/*}" ;;
esac
CODEX_DISABLE_INNER_SANDBOX_ARG="--dangerously-bypass-approvals-and-sandbox"

ccc_agent_path_without_this_shim() {
    _input_path=${1:-}
    _out_path=""
    _old_ifs="$IFS"
    IFS=:
    for _dir in $_input_path; do
        [ -n "$_dir" ] || _dir=.
        _skip=0
        if [ -n "$SHIM_DIR" ] && [ "$_dir" = "$SHIM_DIR" ]; then
            _skip=1
        fi
        if [ "$_skip" = 0 ] && [ -n "${CCC_AGENT_SHIM_DIR:-}" ] && [ "$_dir" = "$CCC_AGENT_SHIM_DIR" ]; then
            _skip=1
        fi
        _candidate="$_dir/$AGENT_NAME"
        if [ "$_skip" = 0 ] && [ -n "$SHIM_PATH" ] && [ -e "$_candidate" ]; then
            if [ "$_candidate" -ef "$SHIM_PATH" ] 2>/dev/null; then
                _skip=1
            fi
        fi
        [ "$_skip" = 0 ] || continue
        if [ -z "$_out_path" ]; then
            _out_path="$_dir"
        else
            _out_path="$_out_path:$_dir"
        fi
    done
    IFS="$_old_ifs"
    printf '%s\n' "$_out_path"
}

UNSHIMMED_PATH="${CCC_AGENT_SHIM_UNDERLYING_PATH:-}"
if [ -z "$UNSHIMMED_PATH" ]; then
    UNSHIMMED_PATH="$(ccc_agent_path_without_this_shim "${PATH:-}")"
fi

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

exec_underlying_agent() {
    PATH="$UNSHIMMED_PATH"
    export PATH
    exec "$AGENT_NAME" "$@"
}

if [ "${CCC_AGENT_SHIM_BYPASS:-0}" = "1" ]; then
    echo "ccc-agent-shim: bypass enabled, running '$AGENT_NAME' from unshimmed PATH" >&2
    exec_underlying_agent "$@"
fi

if [ -n "${CCC_AGENT_SESSION:-}" ]; then
    # Already inside a contained session: run the underlying agent command from
    # the unshimmed PATH, staying in the existing branch instead of redirecting
    # to another ccc-agent run.
    set +e
    codex_inner_sandbox_state "$@"
    codex_sandbox_state=$?
    set -e
    if [ "$codex_sandbox_state" = "0" ]; then
        echo "ccc-agent-shim: nested codex inside ccc-agent; disabling Codex inner sandbox (outer containment active)" >&2
        PATH="$UNSHIMMED_PATH"
        export PATH
        exec "$AGENT_NAME" "$CODEX_DISABLE_INNER_SANDBOX_ARG" "$@"
    elif [ "$codex_sandbox_state" = "2" ]; then
        echo "ccc-agent-shim: refusing nested Codex sandbox inside ccc-agent; use --yolo/--sandbox danger-full-access or omit --sandbox so the shim can disable Codex's inner sandbox" >&2
        exit 2
    fi
    exec_underlying_agent "$@"
fi

LAUNCH="${CCC_AGENT_CLI:-ccc-agent}"
if ! command -v "$LAUNCH" >/dev/null 2>&1; then
    echo "ccc-agent-shim: launcher not found at $LAUNCH" >&2
    echo "ccc-agent-shim: refusing to run '$AGENT_NAME' unprotected (set CCC_AGENT_SHIM_BYPASS=1 to override)" >&2
    exit 1
fi

export CCC_AGENT_SHIM_UNDERLYING_PATH="$UNSHIMMED_PATH"
echo "ccc-agent-shim: redirect active for '$AGENT_NAME' via $LAUNCH run --agent $AGENT_NAME -- $AGENT_NAME" >&2
exec "$LAUNCH" run --agent "$AGENT_NAME" -- "$AGENT_NAME" "$@"
