#!/bin/sh
# ccc-agent SSH shell router.
#
# Intended for use as a user's login shell inside CCC containers.  OpenSSH runs
# the user's shell as `shell -c '<remote command>'` for `ssh host command`; this
# router inspects that command and wraps Claude/Codex remote launch commands in
# `ccc-agent run`, while passing unrelated commands and interactive shells through
# to the real shell unchanged.
set -eu

_truthy() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

_real_shell() {
    if [ -n "${CCC_AGENT_REAL_SHELL:-}" ] && [ -x "${CCC_AGENT_REAL_SHELL}" ]; then
        printf '%s\n' "${CCC_AGENT_REAL_SHELL}"
        return 0
    fi
    if [ -x /bin/bash ]; then
        printf '%s\n' /bin/bash
    else
        printf '%s\n' /bin/sh
    fi
}

_passthrough() {
    _shell="$1"
    shift
    if [ "$#" -eq 0 ]; then
        exec "${_shell}" -l
    fi
    exec "${_shell}" "$@"
}

_path_without_shims() {
    _input_path=${1:-}
    _agent=${2:-}
    _out_path=""
    _old_ifs="$IFS"
    IFS=:
    for _dir in $_input_path; do
        [ -n "$_dir" ] || _dir=.
        _skip=0
        if [ -n "${CCC_AGENT_SHIM_DIR:-}" ] && [ "$_dir" = "${CCC_AGENT_SHIM_DIR}" ]; then
            _skip=1
        fi
        if [ "$_skip" = 0 ] && [ -n "$_agent" ] && [ -n "${CCC_AGENT_SHIM_PATH:-}" ] && [ -e "$_dir/$_agent" ]; then
            if [ "$_dir/$_agent" -ef "${CCC_AGENT_SHIM_PATH}" ] 2>/dev/null; then
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

_detect_agent() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 - "$1" <<'PY'
import os
import shlex
import sys

command = sys.argv[1]

SEPARATORS = {";", "&&", "||", "|"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
WRAPPERS = {"command", "exec", "nohup"}


def is_assignment(token):
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return bool(name) and all(c.isalnum() or c == "_" for c in name)


def normalize_path(token):
    token = token.strip()
    if token.startswith("~/"):
        home = os.environ.get("HOME", "")
        if home:
            token = os.path.join(home, token[2:])
    return token.replace("\\", "/")


def token_agent(token):
    path = normalize_path(token)
    base = os.path.basename(path).lower()
    if base in ("claude", "claude.exe"):
        return "claude"
    if base in ("codex", "codex.exe"):
        return "codex"
    if "/.claude/remote/srv/" in path and base == "server":
        return "claude"
    if "/.claude/remote/src/" in path and base == "server":
        return "claude"
    if "/.claude/remote/ccd-cli/" in path:
        return "claude"
    # Codex remote-control/server internals are less stable than Claude's public
    # claude-ssh layout, so match executable positions under ~/.codex only.  This
    # catches future ~/.codex/remote/* launchers without routing `grep .codex`.
    if "/.codex/" in path:
        return "codex"
    return None


def shell_tokens(command_string):
    lexer = shlex.shlex(command_string, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def detect_command(command_string, depth=0):
    if depth > 3:
        return None
    try:
        tokens = shell_tokens(command_string)
    except ValueError:
        return None
    i = 0
    while i < len(tokens):
        if tokens[i] in SEPARATORS:
            i += 1
            continue
        agent = inspect_at(tokens, i, depth)
        if agent:
            return agent
        # Advance to next shell command segment.
        while i < len(tokens) and tokens[i] not in SEPARATORS:
            i += 1


def inspect_at(tokens, start, depth):
    i = start
    saw_claude_remote_env = False
    while i < len(tokens) and is_assignment(tokens[i]):
        name = tokens[i].split("=", 1)[0]
        if name.startswith("CLAUDE_CODE_REMOTE"):
            saw_claude_remote_env = True
        i += 1
    if i >= len(tokens) or tokens[i] in SEPARATORS:
        return "claude" if saw_claude_remote_env else None

    exe = tokens[i]
    base = os.path.basename(normalize_path(exe)).lower()
    direct = token_agent(exe)
    if direct:
        return direct

    if base == "env":
        i += 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--":
                i += 1
                break
            if tok.startswith("-") and not is_assignment(tok):
                i += 1
                continue
            if is_assignment(tok):
                name = tok.split("=", 1)[0]
                if name.startswith("CLAUDE_CODE_REMOTE"):
                    saw_claude_remote_env = True
                i += 1
                continue
            break
        if i < len(tokens):
            nested = inspect_at(tokens, i, depth)
            return nested or ("claude" if saw_claude_remote_env else None)
        return "claude" if saw_claude_remote_env else None

    if base in WRAPPERS:
        if i + 1 < len(tokens):
            return inspect_at(tokens, i + 1, depth)
        return None

    if base == "timeout":
        i += 1
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 1
        if i < len(tokens):
            i += 1  # duration
        if i < len(tokens):
            return inspect_at(tokens, i, depth)
        return None

    if base in SHELLS:
        j = i + 1
        while j < len(tokens):
            tok = tokens[j]
            if tok == "--":
                j += 1
                continue
            if tok.startswith("-"):
                if "c" in tok[1:] and j + 1 < len(tokens):
                    return detect_command(tokens[j + 1], depth + 1)
                j += 1
                continue
            break
        return None

    return "claude" if saw_claude_remote_env else None


agent = detect_command(command)
if agent:
    print(agent)
    raise SystemExit(0)
raise SystemExit(1)
PY
}

shell="$(_real_shell)"

# This router is intentionally tied to the same operator knob as PATH shims.
if ! _truthy "${CCC_AGENT_ENABLE_SHIMS:-0}" || [ -n "${CCC_AGENT_SESSION:-}" ] || _truthy "${CCC_AGENT_SHIM_BYPASS:-0}"; then
    _passthrough "${shell}" "$@"
fi

if [ "$#" -lt 2 ] || [ "${1:-}" != "-c" ]; then
    _passthrough "${shell}" "$@"
fi

original_command="$2"
agent="$(_detect_agent "${original_command}" || true)"
if [ -z "${agent}" ]; then
    _passthrough "${shell}" "$@"
fi

launcher="${CCC_AGENT_CLI:-ccc-agent}"
if ! { [ -x "${launcher}" ] || command -v "${launcher}" >/dev/null 2>&1; }; then
    echo "ccc-agent-ssh-shell-router: refusing unprotected ${agent} remote command; ${launcher} not found" >&2
    exit 127
fi

echo "ccc-agent-ssh-shell-router: redirect active for ${agent} remote command via ccc-agent run" >&2
export CCC_AGENT_SSH_ORIGINAL_COMMAND="${original_command}"
if [ -z "${CCC_AGENT_SHIM_UNDERLYING_PATH:-}" ]; then
    CCC_AGENT_SHIM_UNDERLYING_PATH="$(_path_without_shims "${PATH:-}" "${agent}")"
    export CCC_AGENT_SHIM_UNDERLYING_PATH
fi
exec "${launcher}" run --agent "${agent}" -- "${shell}" -c "${original_command}"
