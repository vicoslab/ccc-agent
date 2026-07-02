"""Unified command-line entrypoint: ``ccc-agent OP ...``.

Runtime configuration comes from a JSON file (stdlib-only trusted layer):

    {
      "state_dir": "/storage/user/.ccc-agent",
      "backend": "branchfs",            // or "fake" for dry-run/demo
      "branchfs_bin": "branchfs",
      "branchfs_timeout_seconds": 30,     // fail hung BranchFS CLI calls
      "user": "domen",
      "home_subdir": "",
      "roots": [
        {"name": "storage_user",
         "base": "/__real/storage_user",
         "store": "/__branchfs_store/storage_user",
         "visible": "/storage/user",
         "home_subdir": ""}
      ]
    }

Search order: --config flag, $CCC_AGENT_CONFIG, /etc/ccc-agent/config.json,
/opt/ccc-agent/config/config.json.
"""

import argparse
import getpass
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import termios
import tty
from importlib import resources

from . import __version__
from .branchfs import BranchfsCli, FakeBranchFS
from .control import (ControlClient, VERDICT_COMMITTED, VERDICT_HELD,
                      VERDICT_NEEDS_APPROVAL)
from .control import ControlError as ChannelError
from .ctl import CHECK_REPAIR, Controller, ControlError
from .paths import AliasMap
from .runner import (ENV_CONTROL_SOCK, ENV_CONTROL_TOKEN, ENV_SESSION,
                     ResumeError, RootSpec, RunnerConfig, resume_session,
                     run_session)
from .session import SessionStore

CONFIG_ENV = "CCC_AGENT_CONFIG"
CONFIG_PATHS = ("/etc/ccc-agent/config.json",
                "/opt/ccc-agent/config/config.json")
_KNOWN_SHELL_NAMES = frozenset((
    "sh", "bash", "dash", "zsh", "fish", "ksh", "mksh", "pdksh",
    "tcsh", "csh",
))


def _is_shell_argv0(value):
    name = os.path.basename(str(value or "")).lstrip("-")
    return name in _KNOWN_SHELL_NAMES


def _parent_shell_command():
    """Best-effort command for the shell that invoked ccc-agent.

    `$SHELL` is often the user's login shell, not necessarily the shell they are
    currently typing in (for example a temporary `sh` inside `zsh`).  On Linux,
    the direct parent process is the most faithful signal for an interactive
    `ccc-agent run`, so prefer /proc/<ppid>/cmdline when it looks like a shell.
    """
    proc = "/proc/%s" % os.getppid()
    argv0 = None
    try:
        with open(os.path.join(proc, "cmdline"), "rb") as fh:
            parts = fh.read().split(b"\0")
        if parts and parts[0]:
            argv0 = parts[0].decode("utf-8", "surrogateescape")
    except OSError:
        argv0 = None

    if argv0 and _is_shell_argv0(argv0):
        # Preserve explicit spellings like /bin/sh, but strip login-shell
        # prefixes such as "-bash" because they are argv[0] decorations, not
        # executable names.
        base = os.path.basename(argv0)
        if base.startswith("-"):
            return [base.lstrip("-")]
        return [argv0]

    try:
        exe = os.readlink(os.path.join(proc, "exe"))
    except OSError:
        exe = None
    if exe and _is_shell_argv0(exe):
        return [exe]
    return None


def _current_shell_command(env=None):
    env = os.environ if env is None else env
    parent = _parent_shell_command()
    if parent:
        return parent
    if env.get("SHELL"):
        return [env["SHELL"]]
    return ["/bin/sh"]


def load_config(path=None, env=None):
    env = os.environ if env is None else env
    candidates = []
    if path:
        candidates.append(path)
    if env.get(CONFIG_ENV):
        candidates.append(env[CONFIG_ENV])
    candidates.extend(CONFIG_PATHS)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            with open(candidate) as fh:
                config = json.load(fh)
            config.setdefault("_source", candidate)
            return config
    raise SystemExit(
        "ccc-agent: no config found (tried: %s). Provide --config or set %s."
        % (", ".join(c for c in candidates if c), CONFIG_ENV))


def build_runtime(config):
    state_dir = config.get("state_dir") or os.path.join(
        os.path.expanduser("~"), ".ccc-agent")
    store = SessionStore(state_dir)
    if config.get("backend", "branchfs") == "fake":
        backend = FakeBranchFS()
    else:
        backend = BranchfsCli(
            binary=config.get("branchfs_bin", "branchfs"),
            timeout_seconds=config.get("branchfs_timeout_seconds", 30))
    user = config.get("user") or getpass.getuser()
    alias_map = AliasMap.for_home(user,
                                  home_subdir=config.get("home_subdir", ""))
    roots = [RootSpec(name=r["name"], base=r["base"], store=r["store"],
                      visible=r["visible"],
                      home_subdir=r.get("home_subdir"),
                      mount=r.get("mount"),
                      hide_paths=r.get("hide_paths", ()))
             for r in config.get("roots", ())]
    if not roots:
        raise SystemExit("ccc-agent: config defines no protected roots")
    return store, backend, alias_map, user, roots


def _container_run_access(config, full_isolation=False):
    """Whether bwrap should inherit the existing container /run namespace."""
    return bool(config.get("container_run_access", True)) and not full_isolation


def _write_session_start_banner(session, alias_map, confinement, stream=None):
    """Print the human-facing handoff banner before the agent command starts."""
    stream = sys.stderr if stream is None else stream
    if confinement == "none":
        stream.write(
            "ccc-agent: started new BranchFS session "
            "(confinement=none debug mode; not a security boundary)\n")
    else:
        stream.write(
            "ccc-agent: dropped into new contained BranchFS environment\n")
    stream.write("ccc-agent: session: %s\n" % session.session_id)
    for _name, root in sorted(session.protected_roots.items()):
        stream.write(
            "ccc-agent: serving visible %s from BranchFS view %s\n"
            % (alias_map.canonicalize(root.visible), root.mount))


def _write_session_resume_banner(session, alias_map, confinement, stream=None):
    """Print the human-facing handoff banner before a resumed command starts."""
    stream = sys.stderr if stream is None else stream
    stream.write("ccc-agent: resumed BranchFS session %s\n"
                 % session.session_id)
    if confinement == "none":
        stream.write(
            "ccc-agent: WARNING confinement=none is NOT a security boundary "
            "(debug only); the agent can write outside the view.\n")
    for _name, root in sorted(session.protected_roots.items()):
        stream.write(
            "ccc-agent: serving visible %s from BranchFS view %s\n"
            % (alias_map.canonicalize(root.visible), root.mount))


def _review_total_changes(store, session):
    path = os.path.join(store.review_dir(session.session_id),
                        "policy-decision.json")
    try:
        with open(path) as fh:
            return int(json.load(fh).get("total_changes", 0))
    except (OSError, ValueError, TypeError):
        return None


def _auto_commit_finish_detail(store, session):
    """Return the short parenthesized result for an auto-committed session."""
    if session.state != "auto-committed":
        return ""
    total = _review_total_changes(store, session)
    if total is None:
        return ""
    if total == 0:
        return " (no changes)"
    noun = "update" if total == 1 else "updates"
    return " (%d %s in workspace)" % (total, noun)


def _pending_review_finish_detail(store, session):
    if session.state != "pending-review":
        return ""
    total = _review_total_changes(store, session)
    if total is None:
        return ""
    noun = "change" if total == 1 else "changes"
    verb = "needs" if total == 1 else "need"
    return " (%d %s %s review)" % (total, noun, verb)


def _finish_state_label(store, session):
    return (session.state + _auto_commit_finish_detail(store, session)
            + _pending_review_finish_detail(store, session))


def _terminal_lines():
    return shutil.get_terminal_size((80, 24)).lines


def _display_or_page(text, stream=None):
    stream = sys.stderr if stream is None else stream
    text = text if text.endswith("\n") else text + "\n"
    too_tall = len(text.splitlines()) > max(1, _terminal_lines() - 4)
    if getattr(stream, "isatty", lambda: False)() and too_tall and shutil.which("less"):
        stream.write(
            "ccc-agent: opening change review in less "
            "(use Up/Down to browse, q to close)\n")
        stream.flush()
        subprocess.run(["less", "-R"], input=text, text=True)
    else:
        stream.write(text)


def _pending_review_text(controller, session):
    changed = io.StringIO()
    controller.diff(session.session_id, out=changed)
    patch = io.StringIO()
    try:
        controller.review(session.session_id, emit_patch=True, out=patch)
    except ControlError as exc:
        patch.write("(diff unavailable: %s)\n" % exc)

    lines = [
        "ccc-agent: Pending changes for %s" % session.session_id,
        "",
        "Changed paths:",
        changed.getvalue().rstrip() or "(none)",
        "",
        "Diff:",
        patch.getvalue().rstrip() or "(no textual diff)",
        "",
    ]
    return "\n".join(lines)


def _is_interactive_review():
    return sys.stdin.isatty() and sys.stderr.isatty()


def _ensure_foreground_for_prompt():
    """Reclaim terminal foreground after an interactive child shell exits.

    An interactive shell started by `ccc-agent run` can leave the controlling
    terminal's foreground process group pointing at the child shell's process
    group.  If ccc-agent then calls input() while still in a background process
    group, the kernel sends SIGTTIN and the outer shell reports the job as
    stopped.  Put ccc-agent's process group back in the foreground first.
    """
    try:
        fd = sys.stdin.fileno()
        current = os.tcgetpgrp(fd)
        ours = os.getpgrp()
    except (AttributeError, OSError, ValueError):
        return False
    if current == ours:
        return True
    try:
        old_ttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        try:
            os.tcsetpgrp(fd, ours)
        finally:
            signal.signal(signal.SIGTTOU, old_ttou)
    except (OSError, ValueError):
        return False
    return True


def _read_review_choice():
    """Read one review decision key from a TTY; fall back to line input."""
    if getattr(sys.stdin, "isatty", lambda: False)():
        try:
            fd = sys.stdin.fileno()
            old_attrs = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                return sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except (AttributeError, OSError, ValueError, termios.error):
            pass
    return input()


def _prompt_pending_review_decision(controller, session, stream=None):
    stream = sys.stderr if stream is None else stream
    _ensure_foreground_for_prompt()
    while True:
        stream.write(
            "ccc-agent: Accept changes? "
            "yes/y=commit / no/n=discard / "
            "later/l/Esc=keep for review [later]: ")
        stream.flush()
        try:
            raw_choice = _read_review_choice()
        except EOFError:
            raw_choice = "later"
        if len(raw_choice) == 1 and raw_choice not in ("\n", "\r"):
            stream.write("\n")
        choice = raw_choice.strip().lower()
        if choice in ("y", "yes"):
            updated = controller.commit(session.session_id)
            stream.write("ccc-agent: committed session %s\n"
                         % updated.session_id)
            return updated
        if choice in ("n", "no"):
            updated = controller.abort(session.session_id)
            stream.write("ccc-agent: discarded session %s\n"
                         % updated.session_id)
            return updated
        if choice in ("", "l", "later", "r", "review", "\x1b", "esc"):
            stream.write("ccc-agent: kept for later review: %s\n"
                         % session.session_id)
            return session
        stream.write("ccc-agent: please answer yes/y, no/n, or later/l/Esc.\n")


def _handle_pending_review_finish(store, backend, alias_map, session, stream=None):
    if session.state != "pending-review":
        return session
    stream = sys.stderr if stream is None else stream
    controller = Controller(store=store, backend=backend, alias_map=alias_map)
    try:
        _display_or_page(_pending_review_text(controller, session), stream=stream)
    except ControlError as exc:
        stream.write("ccc-agent: could not show pending changes: %s\n" % exc)
    if _is_interactive_review():
        try:
            return _prompt_pending_review_decision(controller, session,
                                                   stream=stream)
        except ControlError as exc:
            stream.write("ccc-agent: review decision failed: %s\n" % exc)
    return session


def main_run(argv=None, env=None, prog="ccc-agent run"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run a command inside a contained BranchFS agent session.")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--workspace",
                        help="agent workspace (default: current directory)")
    parser.add_argument("--policy", default="workspace-auto",
                        help="policy mode (default: workspace-auto)")
    parser.add_argument("--scope", action="append", default=[],
                        help="additional allowed scope (repeatable)")
    parser.add_argument("--hide", action="append", default=[],
                        help="hide/deny pattern for sensitive paths "
                             "(repeatable)")
    parser.add_argument("--agent", default="command",
                        help="agent kind label, e.g. codex, claude, hermes")
    parser.add_argument("--protect-agent-state", action="store_true",
                        help="keep ~/.codex, ~/.claude, and ~/.hermes inside "
                             "BranchFS review instead of the default shared "
                             "direct runtime bind")
    parser.add_argument("--full-isolation", action="store_true",
                        help="do not bind the existing container /run into "
                             "the bwrap sandbox; restores the stricter "
                             "no-ambient-runtime-sockets behavior")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the full session event log (always shows "
                             "the error detail on failure)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- command to run (default: current shell)")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = _current_shell_command(env=env)

    config = load_config(args.config, env=env)
    store, backend, alias_map, user, roots = build_runtime(config)
    # The workspace is deliberately a *launch-time* value.  Generated system
    # configs used to include a broad home default (e.g. /home/domen, which
    # aliases to /storage/user/<container> on CCC), but a bare `ccc-agent run
    # codex` must protect the directory where the user invoked it.  Keep config
    # roots/policy as deployment defaults; use --workspace for explicit
    # per-invocation overrides.
    workspace = args.workspace or os.getcwd()
    config_policy = config.get("policy", {})
    policy = {
        "mode": config_policy.get("mode", args.policy),
        "allowed_scopes": ([workspace] + list(args.scope)
                           + list(config_policy.get("allowed_scopes", ()))),
        "hide_patterns": (list(args.hide) + list(config.get("hide_patterns", ()))
                          + list(config_policy.get("hide_patterns", ()))),
        "ignore_patterns": list(config_policy.get("ignore_patterns", ())),
        "max_policy_repair_attempts":
            config_policy.get("max_policy_repair_attempts", 2),
    }
    if config_policy.get("deny_patterns") is not None:
        policy["deny_patterns"] = config_policy["deny_patterns"]
    # bwrap is the real containment boundary and the deployment default; "none"
    # is a debug mode only (no isolation -- absolute-path writes bypass the
    # view), so warn loudly if it is selected.
    confinement = config.get("confinement", "bwrap")
    if confinement == "none":
        sys.stderr.write(
            "ccc-agent: WARNING confinement=none is NOT a security boundary "
            "(debug only); the agent can write outside the view. Set "
            "confinement=bwrap for real containment.\n")
    nested_invocation = bool((os.environ if env is None else env).get(ENV_SESSION))
    runner_config = RunnerConfig(
        store=store, backend=backend, alias_map=alias_map, owner=user,
        agent_kind=args.agent, agent_command=command, workspace=workspace,
        policy=policy, roots=roots,
        confinement=confinement,
        bwrap_bin=config.get("bwrap_bin", "bwrap"),
        bwrap_proc_mode=config.get("bwrap_proc_mode", "bind"),
        bwrap_ro_binds=config.get("bwrap_ro_binds", ()),
        bwrap_setenv=config.get("bwrap_setenv"),
        container_run_access=_container_run_access(config, args.full_isolation),
        cred_mounts=config.get("cred_mounts", ()),
        cred_mask=config.get("cred_mask", ()),
        cred_env=config.get("cred_env"),
        bwrap_uid=config.get("bwrap_uid"),
        bwrap_gid=config.get("bwrap_gid"),
        agent_plugins=({} if config.get("agent_hook_mode") == "disabled"
                       else config.get("agent_plugins")),
        agent_state_binds=config.get("agent_state_binds"),
        protect_agent_state=(args.protect_agent_state or
                             bool(config.get("protect_agent_state", False))),
        ensure_agent_state_dirs=bool(config.get("ensure_agent_state_dirs", False)),
        on_session_start=lambda session: _write_session_start_banner(
            session, alias_map, confinement))
    session = run_session(runner_config, env=env)

    sys.stderr.write("ccc-agent: session %s finished: %s\n"
                     % (session.session_id,
                        _finish_state_label(store, session)))

    # Surface WHY it failed: the failure paths in run_session record the reason
    # as an "error" event (mount/launch/finalize/commit detail, incl. branchfs
    # stderr). Always print those on failure; --verbose dumps the full timeline.
    errors = [e for e in session.events if e.get("event") == "error"]
    if session.state == "failed":
        if errors:
            for e in errors:
                sys.stderr.write("ccc-agent: error: %s\n"
                                 % e.get("detail", "(no detail recorded)"))
        else:
            sys.stderr.write("ccc-agent: failed but no error detail was "
                             "recorded; see the event log (-v) below\n")
    if args.verbose:
        sys.stderr.write("ccc-agent: event log:\n")
        for e in session.events:
            line = "  %s  %s" % (e.get("time", ""), e.get("event", ""))
            if e.get("detail") is not None:
                line += ": %s" % e["detail"]
            sys.stderr.write(line + "\n")
    if session.state == "failed":
        sys.stderr.write(
            "ccc-agent: full record: %s\n"
            "ccc-agent: inspect with: ccc-agent show %s\n"
            % (store.session_file(session.session_id), session.session_id))

    if session.state == "pending-review" and not nested_invocation:
        session = _handle_pending_review_finish(store, backend, alias_map,
                                                session)

    if session.state == "pending-review" and not nested_invocation:
        sys.stderr.write(
            "ccc-agent: review with: ccc-agent diff %s\n"
            "ccc-agent: file diff: ccc-agent diff %s <path>\n"
            "ccc-agent: then: ccc-agent commit %s | ccc-agent abort %s\n"
            % (session.session_id, session.session_id, session.session_id,
               session.session_id))
    if session.state == "failed":
        return 1
    if session.exit_status not in (0, None):
        return session.exit_status
    return 0


def _print_failure_details(store, session, verbose=False):
    """Print failure diagnostics shared by run/resume."""
    errors = [e for e in session.events if e.get("event") == "error"]
    if session.state == "failed":
        if errors:
            for e in errors:
                sys.stderr.write("ccc-agent: error: %s\n"
                                 % e.get("detail", "(no detail recorded)"))
        else:
            sys.stderr.write("ccc-agent: failed but no error detail was "
                             "recorded; see the event log (-v) below\n")
    if verbose:
        sys.stderr.write("ccc-agent: event log:\n")
        for e in session.events:
            line = "  %s  %s" % (e.get("time", ""), e.get("event", ""))
            if e.get("detail") is not None:
                line += ": %s" % e["detail"]
            sys.stderr.write(line + "\n")
    if session.state == "failed":
        sys.stderr.write(
            "ccc-agent: full record: %s\n"
            "ccc-agent: inspect with: ccc-agent show %s\n"
            % (store.session_file(session.session_id), session.session_id))


def main_resume(argv=None, env=None, prog="ccc-agent resume"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Resume a running BranchFS session after a crash/reboot.")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--agent", default=None,
                        help="agent kind label for the resumed command")
    parser.add_argument("--force", action="store_true",
                        help="resume even if the old session mount still "
                             "appears active (use only after verifying no old "
                             "agent process is using it)")
    parser.add_argument("--protect-agent-state", action="store_true",
                        help="keep ~/.codex, ~/.claude, and ~/.hermes inside "
                             "BranchFS review instead of the default shared "
                             "direct runtime bind")
    parser.add_argument("--full-isolation", action="store_true",
                        help="do not bind the existing container /run into "
                             "the bwrap sandbox; restores the stricter "
                             "no-ambient-runtime-sockets behavior")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the full session event log")
    parser.add_argument("session_id", metavar="session-id")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- command to run (default: stored command)")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    custom_command = bool(command)

    config = load_config(args.config, env=env)
    store, backend, alias_map, user, roots = build_runtime(config)
    try:
        existing = store.load(args.session_id)
    except KeyError:
        sys.stderr.write("ccc-agent: no such session: %s\n" % args.session_id)
        return 1
    if not command:
        command = list(existing.agent_command)
    agent_kind = args.agent
    if agent_kind is None:
        agent_kind = "command" if custom_command else existing.agent_kind

    confinement = config.get("confinement", "bwrap")
    runner_config = RunnerConfig(
        store=store, backend=backend, alias_map=alias_map, owner=user,
        agent_kind=agent_kind, agent_command=command,
        workspace=existing.workspace, policy=existing.policy, roots=roots,
        confinement=confinement,
        bwrap_bin=config.get("bwrap_bin", "bwrap"),
        bwrap_proc_mode=config.get("bwrap_proc_mode", "bind"),
        bwrap_ro_binds=config.get("bwrap_ro_binds", ()),
        bwrap_setenv=config.get("bwrap_setenv"),
        container_run_access=_container_run_access(config, args.full_isolation),
        cred_mounts=config.get("cred_mounts", ()),
        cred_mask=config.get("cred_mask", ()),
        cred_env=config.get("cred_env"),
        bwrap_uid=config.get("bwrap_uid"),
        bwrap_gid=config.get("bwrap_gid"),
        agent_plugins=({} if config.get("agent_hook_mode") == "disabled"
                       else config.get("agent_plugins")),
        agent_state_binds=config.get("agent_state_binds"),
        protect_agent_state=(args.protect_agent_state or
                             bool(config.get("protect_agent_state", False))),
        ensure_agent_state_dirs=bool(config.get("ensure_agent_state_dirs", False)),
        on_session_start=lambda session: _write_session_resume_banner(
            session, alias_map, confinement))
    try:
        session = resume_session(args.session_id, runner_config, env=env,
                                 force=args.force)
    except ResumeError as exc:
        sys.stderr.write("ccc-agent: %s\n" % exc)
        return 1

    sys.stderr.write("ccc-agent: resumed session %s finished: %s\n"
                     % (session.session_id,
                        _finish_state_label(store, session)))
    _print_failure_details(store, session, verbose=args.verbose)

    if session.state == "pending-review":
        session = _handle_pending_review_finish(store, backend, alias_map,
                                                session)
    if session.state == "pending-review":
        sys.stderr.write(
            "ccc-agent: review with: ccc-agent diff %s\n"
            "ccc-agent: file diff: ccc-agent diff %s <path>\n"
            "ccc-agent: then: ccc-agent commit %s | ccc-agent abort %s\n"
            % (session.session_id, session.session_id, session.session_id,
               session.session_id))
    if session.state == "failed":
        return 1
    if session.exit_status not in (0, None):
        return session.exit_status
    return 0


def _ctl_socket(args, env):
    """Per-turn control ops that run from INSIDE the sandbox.  They reach the
    supervisor over the control socket (CCC_AGENT_CONTROL_SOCK/TOKEN) — the
    BranchFS store and config are deliberately not reachable here, so these
    never touch load_config/build_runtime.  Degrade safe: never block the
    agent's Stop on missing plumbing or a control error."""
    env = os.environ if env is None else env
    sock = env.get(ENV_CONTROL_SOCK)
    token = env.get(ENV_CONTROL_TOKEN)
    if not sock or not token:
        sys.stderr.write(
            "ccc-agent: no control socket; per-turn control unavailable "
            "(not inside a contained session)\n")
        return 0
    client = ControlClient(sock, token)
    try:
        if args.cmd == "finalize-turn":
            resp = client.finalize_turn()
        else:  # approve-turn
            paths = ([p for p in args.paths.split(",") if p]
                     if getattr(args, "paths", None) else None)
            resp = client.approve_turn(args.approval_token, args.decision,
                                       paths=paths)
    except ChannelError as exc:
        sys.stderr.write("ccc-agent: control error: %s\n" % exc)
        return 0
    verdict = resp.get("verdict")
    if verdict == VERDICT_NEEDS_APPROVAL:
        paths = resp.get("out_of_scope", [])
        token2 = resp.get("approval_token")
        sys.stderr.write(
            "ccc-agent: %d change(s) are OUTSIDE the agent workspace and were "
            "NOT committed:\n" % len(paths))
        for path in paths:
            sys.stderr.write("  - %s\n" % path)
        sys.stderr.write(
            "ccc-agent: ask the user how to handle these, then run ONE of:\n"
            "    ccc-agent approve-turn %s            # commit all\n"
            "    ccc-agent approve-turn %s keep       # keep, don't commit\n"
            "    ccc-agent approve-turn %s revert     # discard (you undo)\n"
            "    ccc-agent approve-turn %s --paths a,b # commit only a,b\n"
            % (token2, token2, token2, token2))
        return 2
    if verdict == VERDICT_COMMITTED:
        msg = "committed %d change(s)" % len(resp.get("committed", []))
        if resp.get("held"):
            msg += " (held %d for review)" % len(resp["held"])
        sys.stdout.write(msg + "\n")
    elif verdict == VERDICT_HELD:
        if resp.get("revert"):
            sys.stdout.write("rejected; revert these in your workspace:\n")
            for path in resp["revert"]:
                sys.stdout.write("  - %s\n" % path)
        else:
            sys.stdout.write("changes held for review (not committed)\n")
    else:
        sys.stdout.write("%s\n" % (verdict or "ok"))
    return 0


_SESSION_ID_CTL_OPS = (
    "show", "status", "commit", "abort", "thaw", "finish",
    "finish-turn", "check-before-final",
)
_BATCH_SESSION_ID_CTL_OPS = (
    "commit", "abort", "thaw", "finish", "finish-turn",
)


def _nonnegative_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a non-negative day count")
    if days < 0:
        raise argparse.ArgumentTypeError("must be a non-negative day count")
    return days


def _add_session_id_arg(parser, multiple=False):
    if multiple:
        parser.add_argument("session_ids", nargs="+", metavar="session-id")
    else:
        parser.add_argument("session_id", metavar="session-id")


def _batch_ok_detail(cmd, session):
    if cmd == "finish":
        return " (%s)" % session.state
    return ""


def _run_batch_session_op(controller, cmd, session_ids, stream=None):
    stream = sys.stderr if stream is None else stream
    failed = False
    for session_id in session_ids:
        try:
            if cmd == "commit":
                session = controller.commit(session_id)
            elif cmd == "abort":
                session = controller.abort(session_id)
            elif cmd == "thaw":
                session = controller.thaw(session_id)
            elif cmd == "finish":
                session = controller.finish(session_id)
            elif cmd == "finish-turn":
                session = controller.finish_turn(session_id)
            else:
                raise ControlError("unsupported batch op: %s" % cmd)
        except ControlError as exc:
            failed = True
            stream.write("%s: error: %s\n" % (session_id, exc))
            continue
        stream.write("%s: ok%s\n"
                     % (session.session_id, _batch_ok_detail(cmd, session)))
    return 1 if failed else 0


def main_ctl(argv=None, env=None, prog="ccc-agent"):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Accept both ``ccc-agent --config X list`` (argparse's natural shape) and
    # ``ccc-agent list --config X`` (the shape people tend to type for verbs).
    if "--config" in argv:
        idx = argv.index("--config")
        if idx > 0 and idx + 1 < len(argv):
            pair = argv[idx:idx + 2]
            del argv[idx:idx + 2]
            argv = pair + argv
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Inspect and control BranchFS agent sessions.")
    parser.add_argument("--config", help="path to config.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("list")
    lp.add_argument("session_id", nargs="?", metavar="session-id-prefix",
                    help="optional session id prefix filter")
    cp = sub.add_parser(
        "cleanup",
        help="remove old closed session bundles (default: older than 30 days)")
    cp.add_argument("--older-than", metavar="DAYS", type=_nonnegative_days,
                    default=30,
                    help="remove closed sessions older than DAYS (default: 30)")
    cp.add_argument("--dry-run", action="store_true",
                    help="show what would be removed without deleting")
    for name in _SESSION_ID_CTL_OPS:
        p = sub.add_parser(name)
        _add_session_id_arg(p, multiple=name in _BATCH_SESSION_ID_CTL_OPS)
    dp = sub.add_parser("diff", help="show changed paths, or a unified diff for one file")
    _add_session_id_arg(dp)
    dp.add_argument("path", nargs="?", help="optional changed file to diff")
    rv = sub.add_parser("review", help="post-session change review")
    _add_session_id_arg(rv)
    rv.add_argument("--accept", action="store_true", help="commit everything")
    rv.add_argument("--reject", action="store_true",
                    help="discard everything (revert)")
    rv.add_argument("--commit", dest="commit_paths",
                    help="comma-separated paths to commit file-by-file "
                         "(the rest are discarded)")
    rv.add_argument("--emit-patch", action="store_true",
                    help="print a base-vs-view unified diff for line-level "
                         "review")
    rv.add_argument("--apply-patch", metavar="FILE",
                    help="apply a (possibly pruned) patch to base for "
                         "line-level commit")
    # per-turn socket ops (no session_id; identified by the socket+token)
    sub.add_parser("finalize-turn")
    ap = sub.add_parser("approve-turn")
    ap.add_argument("approval_token")
    ap.add_argument("decision", nargs="?", default="yes",
                    help="yes (commit all, default) | keep (don't commit) | "
                         "revert (discard)")
    ap.add_argument("--paths", help="comma-separated subset to commit "
                                    "file-by-file; the rest are held")
    args = parser.parse_args(argv)

    if args.cmd in ("finalize-turn", "approve-turn"):
        return _ctl_socket(args, env)

    config = load_config(args.config, env=env)
    store, backend, alias_map, _user, _roots = build_runtime(config)
    controller = Controller(store=store, backend=backend, alias_map=alias_map)

    try:
        if args.cmd == "list":
            controller.list(getattr(args, "session_id", None))
        elif args.cmd == "cleanup":
            controller.cleanup(older_than_days=args.older_than,
                               dry_run=args.dry_run)
        elif args.cmd == "show":
            controller.show(args.session_id)
        elif args.cmd == "status":
            controller.status(args.session_id)
        elif args.cmd == "diff":
            controller.diff(args.session_id, path=args.path)
        elif args.cmd == "review":
            commit_paths = ([p for p in args.commit_paths.split(",") if p]
                            if args.commit_paths else None)
            session = controller.review(
                args.session_id, accept=args.accept, reject=args.reject,
                commit_paths=commit_paths, emit_patch=args.emit_patch,
                apply_patch=args.apply_patch)
            if session.state in ("committed", "aborted"):
                sys.stderr.write("session %s now %s\n"
                                 % (session.session_id, session.state))
        elif args.cmd in _BATCH_SESSION_ID_CTL_OPS:
            return _run_batch_session_op(controller, args.cmd, args.session_ids)
        elif args.cmd == "check-before-final":
            # exit 2 = "block the stop, repair": the only code that loops the
            # agent. Allow and exhausted both exit 0 so hooks cannot livelock.
            if controller.check_before_final(args.session_id) == CHECK_REPAIR:
                return 2
    except ControlError as exc:
        sys.stderr.write("ccc-agent: %s\n" % exc)
        return 1
    return 0


def main_softsandbox(argv=None, env=None):
    """Run the legacy soft sandbox as ``ccc-agent softsandbox``.

    The soft sandbox is still a Bash implementation because it is a diagnostic /
    PoC helper rather than the production BranchFS+bwrap path. Keeping it as a
    package asset lets the public command surface stay unified without a second
    installed executable.
    """
    argv = [] if argv is None else list(argv)
    env = os.environ if env is None else env
    script_ref = resources.files("ccc_agent").joinpath(
        "assets", "scripts", "softsandbox.sh")
    with resources.as_file(script_ref) as script:
        return subprocess.call(["bash", str(script)] + argv, env=env)


_CTL_OPS = (set(_SESSION_ID_CTL_OPS) | {
    "list", "cleanup", "diff", "review", "finalize-turn", "approve-turn",
})
_SESSION_ID_COMPLETION_OPS = (
    set(_SESSION_ID_CTL_OPS) | {"diff", "review", "list", "resume"}
)
_MULTI_SESSION_ID_COMPLETION_OPS = set(_BATCH_SESSION_ID_CTL_OPS)
_MAIN_OPS = tuple(sorted(_CTL_OPS | {
    "run", "launch", "resume", "setup", "softsandbox", "completion",
}))
_TOP_LEVEL_OPTIONS = ("--config", "--version", "--help")
_GLOBAL_VALUE_OPTIONS = frozenset(("--config",))
_CLEANUP_VALUE_OPTIONS = frozenset(("--older-than",))
_REVIEW_VALUE_OPTIONS = frozenset(("--commit", "--apply-patch"))
_RESUME_VALUE_OPTIONS = frozenset(("--agent",))
_RUN_OPTIONS = (
    "--agent", "--full-isolation", "--hide", "--policy",
    "--protect-agent-state", "--scope", "--verbose", "--workspace", "-v",
    "--config", "--help",
)
_CLEANUP_OPTIONS = ("--older-than", "--dry-run", "--config", "--help")
_REVIEW_OPTIONS = (
    "--accept", "--reject", "--commit", "--emit-patch", "--apply-patch",
    "--config", "--help",
)
_RESUME_OPTIONS = (
    "--agent", "--force", "--full-isolation", "--protect-agent-state",
    "--verbose", "-v", "--config", "--help",
)


_COMPLETION_SCRIPTS = {
    "bash": ("assets", "completions", "bash", "ccc-agent"),
    "zsh": ("assets", "completions", "zsh", "_ccc-agent"),
    "fish": ("assets", "completions", "fish", "ccc-agent.fish"),
}


def _completion_script(shell):
    try:
        parts = _COMPLETION_SCRIPTS[shell]
    except KeyError:
        raise SystemExit("ccc-agent: unknown completion shell %r" % shell)
    ref = resources.files("ccc_agent")
    for part in parts:
        ref = ref.joinpath(part)
    return ref.read_text()


def _matching(candidates, prefix):
    return [item for item in sorted(candidates) if item.startswith(prefix)]


def _normalize_completion_words(words, cword):
    words = list(words)
    if cword < 0:
        cword = 0
    while len(words) <= cword:
        words.append("")
    if words and os.path.basename(words[0]) == "ccc-agent":
        words = words[1:]
        cword -= 1
        if cword < 0:
            cword = 0
        while len(words) <= cword:
            words.append("")
    return words, cword


def _first_completion_command_index(tokens):
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "":
            return i
        if token == "--config":
            i += 2
            continue
        if token.startswith("--config="):
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        return i
    return None


def _completion_config_path(tokens, cword):
    config_path = None
    for i, token in enumerate(tokens):
        if token == "--config" and i + 1 < len(tokens):
            if i + 1 != cword and tokens[i + 1]:
                config_path = tokens[i + 1]
        elif token.startswith("--config="):
            value = token.split("=", 1)[1]
            if value:
                config_path = value
    return config_path


def _value_options_for_completion(op):
    values = set()
    values.update(_GLOBAL_VALUE_OPTIONS)
    if op == "cleanup":
        values.update(_CLEANUP_VALUE_OPTIONS)
    if op == "review":
        values.update(_REVIEW_VALUE_OPTIONS)
    if op == "resume":
        values.update(_RESUME_VALUE_OPTIONS)
    return values


def _positionals_before_completion_token(tokens, cmd_idx, cword, op):
    value_options = _value_options_for_completion(op)
    positionals = []
    i = cmd_idx + 1
    while i < len(tokens) and i < cword:
        token = tokens[i]
        if token in value_options:
            if i + 1 == cword:
                return positionals, True
            i += 2
            continue
        if any(token.startswith(opt + "=") for opt in value_options):
            i += 1
            continue
        if token.startswith("-"):
            i += 1
            continue
        positionals.append(token)
        i += 1
    return positionals, False


def _options_for_completion(op):
    if op == "cleanup":
        return _CLEANUP_OPTIONS
    if op == "review":
        return _REVIEW_OPTIONS
    if op == "resume":
        return _RESUME_OPTIONS
    if op == "run":
        return _RUN_OPTIONS
    if op in _CTL_OPS or op in ("launch", "setup", "softsandbox",
                                "completion"):
        return ("--config", "--help")
    return _TOP_LEVEL_OPTIONS


def _session_id_completions(prefix, config_path=None, env=None):
    env = os.environ if env is None else env
    try:
        config = load_config(config_path, env=env)
    except (SystemExit, OSError, ValueError):
        return []
    state_dir = config.get("state_dir") or os.path.join(
        os.path.expanduser("~"), ".ccc-agent")
    try:
        sessions = SessionStore(state_dir).list()
    except (OSError, ValueError):
        return []
    return sorted(session.session_id for session in sessions
                  if session.session_id.startswith(prefix))


def _complete_words(words, cword, env=None):
    tokens, cword = _normalize_completion_words(words, cword)
    prefix = tokens[cword] if 0 <= cword < len(tokens) else ""
    cmd_idx = _first_completion_command_index(tokens)

    if cmd_idx is None or cword <= cmd_idx:
        if prefix.startswith("-"):
            return _matching(_TOP_LEVEL_OPTIONS, prefix)
        return _matching(_MAIN_OPS, prefix)

    op = tokens[cmd_idx]
    if prefix.startswith("-"):
        return _matching(_options_for_completion(op), prefix)

    positionals, current_is_option_value = _positionals_before_completion_token(
        tokens, cmd_idx, cword, op)
    if current_is_option_value:
        return []
    if op in _SESSION_ID_COMPLETION_OPS:
        if op in _MULTI_SESSION_ID_COMPLETION_OPS or not positionals:
            matches = _session_id_completions(
                prefix, config_path=_completion_config_path(tokens, cword),
                env=env)
            if op in _MULTI_SESSION_ID_COMPLETION_OPS:
                return [match for match in matches if match not in positionals]
            return matches
    return []


def main_complete(argv=None, env=None):
    """Hidden entrypoint used by shell completion functions."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _COMPLETION_SCRIPTS:
        argv = argv[1:]
    if not argv:
        return 0
    try:
        cword = int(argv[0])
    except ValueError:
        return 0
    for candidate in _complete_words(argv[1:], cword, env=env):
        sys.stdout.write(candidate + "\n")
    return 0


def main_completion(argv=None, prog="ccc-agent completion"):
    """Print shell completion code for ccc-agent."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Print a shell completion script for ccc-agent.")
    parser.add_argument("shell", nargs="?", default="bash",
                        choices=sorted(_COMPLETION_SCRIPTS),
                        help="shell to generate for (default: bash)")
    args = parser.parse_args(argv)
    sys.stdout.write(_completion_script(args.shell))
    return 0


def _print_main_help(stream=None):
    stream = sys.stdout if stream is None else stream
    stream.write(
        "usage: ccc-agent OP [options]\n\n"
        "Unified CCC agent containment CLI.\n\n"
        "Global options:\n"
        "  --version        print the ccc-agent release version\n\n"
        "Primary ops:\n"
        "  run              run a command, or the current shell when omitted, "
        "in a contained BranchFS session\n"
        "  resume           re-mount and continue a running session after a "
        "crash/reboot\n"
        "  setup            write config, plugin entries, and optional shims\n"
        "  completion       print shell completion code (bash, zsh, fish)\n"
        "  softsandbox      diagnostic non-FUSE soft sandbox helper\n\n"
        "Session/control ops:\n"
        "  list, cleanup, show, status, diff, review, commit, abort, thaw, finish\n"
        "  finish-turn, check-before-final, finalize-turn, approve-turn\n\n"
        "Examples:\n"
        "  ccc-agent run --workspace /home/$USER/project -- codex exec 'fix bug'\n"
        "  ccc-agent resume <session>             # rerun the stored command\n"
        "  ccc-agent resume <session> -- bash     # resume with a custom shell\n"
        "  ccc-agent list\n"
        "  ccc-agent cleanup --older-than 30 --dry-run\n"
        "  ccc-agent review <session> --accept\n"
        "  ccc-agent setup --system --enable-shims\n")


def main(argv=None, env=None):
    """Dispatch the unified ``ccc-agent OP`` command surface."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--version":
        sys.stdout.write("ccc-agent v%s\n" % __version__)
        return 0
    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_main_help()
        return 0

    op, rest = argv[0], argv[1:]
    if op == "__complete":
        return main_complete(rest, env=env)
    if op == "completion":
        return main_completion(rest, prog="ccc-agent completion")
    if op in ("run", "launch"):
        return main_run(rest, env=env, prog="ccc-agent %s" % op)
    if op == "resume":
        return main_resume(rest, env=env, prog="ccc-agent resume")
    if op == "setup":
        from . import setup as setup_mod
        return setup_mod.main(rest, prog="ccc-agent setup")
    if op == "softsandbox":
        return main_softsandbox(rest, env=env)

    # Control operations are direct: ``ccc-agent list``, ``ccc-agent diff ID``.
    # Also pass through leading global flags, e.g. ``ccc-agent --config X list``.
    if op in _CTL_OPS or op.startswith("-"):
        return main_ctl(argv, env=env, prog="ccc-agent")

    sys.stderr.write("ccc-agent: unknown op %r\n\n" % op)
    _print_main_help(stream=sys.stderr)
    return 2
