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
    # to another ccc-agent run. Do not alter agent-specific sandbox flags here;
    # users may opt into Codex --yolo/--sandbox modes themselves.
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
