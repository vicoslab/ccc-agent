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
import select
import shlex
import signal
import subprocess
import sys
import termios
import time
import tty
from importlib import resources

from .version import version_string
from .branchfs import BranchfsCli, FakeBranchFS
from .commit_failures import has_permission_failures, permission_failures
from .control import (ControlClient, VERDICT_COMMITTED, VERDICT_DISCARDED,
                      VERDICT_HELD, VERDICT_KEPT_STATUS,
                      VERDICT_NEEDS_APPROVAL, VERDICT_NEEDS_KEPT_REVIEW)
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
    """Whether bwrap should inherit container runtime /run, /var, and /dev."""
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


def _stream_stdout(stream):
    """Return stream as a subprocess stdout target when it has a real fd."""
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return stream


def _display_or_page(text, stream=None):
    stream = sys.stderr if stream is None else stream
    text = text if text.endswith("\n") else text + "\n"
    too_tall = len(text.splitlines()) > max(1, _terminal_lines() - 4)
    if getattr(stream, "isatty", lambda: False)() and too_tall and shutil.which("less"):
        # An interactive child shell can leave the terminal foreground process
        # group pointing at the child.  If we spawn less while ccc-agent is still
        # in the background pgrp, less is stopped by job control (SIGTTIN/TTOU)
        # and ccc-agent waits forever.  Reclaim the terminal before paging; if
        # that fails, print directly rather than orphaning a stopped pager.
        if not _ensure_foreground_for_prompt():
            stream.write(text)
            return
        stream.write(
            "ccc-agent: opening change review in less "
            "(use Up/Down to browse, q to close)\n")
        stream.flush()
        kwargs = {"input": text, "text": True}
        stdout = _stream_stdout(stream)
        if stdout is not None:
            # Keep review output on the same terminal stream we tested for TTY.
            # Otherwise a redirected stdout can make less appear to show nothing
            # while stderr was the interactive stream.
            kwargs["stdout"] = stdout
        subprocess.run(["less", "-R"], **kwargs)
    else:
        stream.write(text)


def _pending_review_text(controller, session, show_ignored=False,
                         show_file_diffs=False):
    changed = io.StringIO()
    controller.diff(session.session_id, out=changed,
                    show_ignored=show_ignored,
                    show_file_diffs=show_file_diffs)

    lines = [
        "ccc-agent: Pending changes for %s" % session.session_id,
        "",
        "Changed paths:",
        changed.getvalue().rstrip() or "(none)",
        "",
        "Use `ccc-agent diff %s --show-file-diffs` to include text file hunks."
        % session.session_id,
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


class _ReviewTreeNode(object):
    def __init__(self, name, path, parent=None):
        self.name = name
        self.path = path
        self.parent = parent
        self.children = {}
        self.change = None
        self._changed_paths_cache = None

    def changed_paths(self):
        if self._changed_paths_cache is None:
            paths = []
            if self.change is not None:
                paths.append(self.change.path)
            for child in self.sorted_children():
                paths.extend(child.changed_paths())
            self._changed_paths_cache = tuple(paths)
        return self._changed_paths_cache

    def sorted_children(self):
        return [self.children[name] for name in sorted(self.children)]


def _review_tree_common_root(changes):
    paths = [c.path for c in changes]
    if not paths:
        return "/"
    if len(paths) == 1:
        return os.path.dirname(paths[0]) or "/"
    try:
        return os.path.commonpath(paths) or "/"
    except ValueError:
        return "/"


def _build_review_tree(changes):
    common = _review_tree_common_root(changes)
    root = _ReviewTreeNode(common or "/", common or "/")
    for change in sorted(changes, key=lambda c: c.path):
        try:
            rel = os.path.relpath(change.path, common)
        except ValueError:
            rel = change.path.lstrip(os.sep)
        if rel in ("", "."):
            root.change = change
            continue
        current = root
        prefix = common.rstrip(os.sep)
        for part in rel.split(os.sep):
            prefix = (prefix + os.sep + part) if prefix else os.sep + part
            current = current.children.setdefault(
                part, _ReviewTreeNode(part, prefix, parent=current))
        current.change = change
    return root


def _node_selection_mark(node, selected):
    paths = node.changed_paths()
    if not paths:
        return "[ ]"
    count = sum(1 for path in paths if path in selected)
    if count == len(paths):
        return "[x]"
    if count:
        return "[~]"
    return "[ ]"


def _toggle_node_selection(node, selected):
    paths = node.changed_paths()
    if not paths:
        return
    if all(path in selected for path in paths):
        for path in paths:
            selected.discard(path)
    else:
        for path in paths:
            selected.add(path)


def _review_tree_lines(root, cwd, cursor, selected, message=None):
    lines = [
        "ccc-agent: selective accept",
        "Space selects a file or whole folder subtree; "
        "Enter opens a folder; Backspace goes up; "
        "c commits selected; q/Esc cancels.",
        "current: %s" % cwd.path,
        "selected: %d path(s)" % len(selected),
    ]
    if message:
        lines.append(str(message))
    entries = cwd.sorted_children()
    if not entries:
        lines.append("  (no changed paths here)")
    for idx, node in enumerate(entries):
        marker = ">" if idx == cursor else " "
        suffix = "/" if node.children else ""
        lines.append("%s %s %s%s"
                     % (marker, _node_selection_mark(node, selected),
                        node.name, suffix))
    return lines


def _render_review_tree(root, cwd, cursor, selected, stream, clear_screen=True,
                        message=None):
    if clear_screen:
        stream.write("\x1b[2J\x1b[H")
    for line in _review_tree_lines(root, cwd, cursor, selected,
                                   message=message):
        stream.write(line + "\n")
    stream.flush()


def _decode_tree_escape_sequence(seq):
    """Normalize terminal escape bytes after the leading ESC byte."""
    if not seq:
        return "ESC"
    # Cursor keys may arrive as CSI (ESC [ A) or application cursor mode
    # (ESC O A).  Modified arrows can include parameters such as ESC [ 1 ; 5 A,
    # so look for the final direction byte instead of assuming fixed length.
    if seq[:1] in (b"[", b"O"):
        for byte in seq[1:]:
            key = {
                ord("A"): "KEY_UP",
                ord("B"): "KEY_DOWN",
                ord("C"): "KEY_RIGHT",
                ord("D"): "KEY_LEFT",
            }.get(byte)
            if key is not None:
                return key
    return "ESC"


def _read_tree_key():
    """Read one navigation key and normalize common terminal keys.

    This deliberately uses byte-level ``os.read``.  The previous implementation
    mixed ``sys.stdin.read(1)`` with ``select`` on the underlying file
    descriptor; Python's text buffering could consume the rest of an escape
    sequence before ``select`` saw it, causing arrow keys to be mistaken for a
    bare Escape and cancelling the selector.
    """
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            seq = b""
            try:
                ready, _w, _x = select.select([fd], [], [], 0.2)
            except (OSError, ValueError):
                ready = []
            if ready:
                try:
                    seq = os.read(fd, 1)
                    if seq[:1] in (b"[", b"O"):
                        for _ in range(15):
                            if _decode_tree_escape_sequence(seq) != "ESC":
                                break
                            ready, _w, _x = select.select([fd], [], [], 0.02)
                            if not ready:
                                break
                            seq += os.read(fd, 1)
                except OSError:
                    seq = b""
            return _decode_tree_escape_sequence(seq)
        if ch in (b"\r", b"\n"):
            return "KEY_ENTER"
        if ch in (b"\x7f", b"\b"):
            return "KEY_BACKSPACE"
        try:
            return ch.decode()
        except UnicodeDecodeError:
            return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


_CURSES_UNAVAILABLE = object()


def _configure_curses_default_colors(curses_module, stdscr):
    """Use the terminal's default foreground/background colors in curses.

    Without ``use_default_colors`` curses often paints blank cells with its
    compiled-in black background, which makes the selector look like a solid
    black panel in terminals that use a different default theme.
    """
    normal_attr = 0
    try:
        curses_module.start_color()
        curses_module.use_default_colors()
        curses_module.init_pair(1, -1, -1)
        normal_attr = curses_module.color_pair(1)
        try:
            stdscr.bkgdset(" ", normal_attr)
        except curses_module.error:
            pass
    except Exception:
        normal_attr = 0
    return normal_attr, normal_attr | curses_module.A_REVERSE


def _select_review_paths_curses(changes):
    """Curses-backed selector for real terminals.

    Curses owns keypad/arrow decoding, avoiding fragile manual handling of
    terminal escape sequences in the normal interactive path.  The small
    key-reader loop below remains for unit tests and as a fallback if curses is
    unavailable in a minimal environment.
    """
    try:
        import curses
    except ImportError:
        return _CURSES_UNAVAILABLE

    root = _build_review_tree(changes)
    selected = set()
    state = {"cwd": root, "cursor": 0, "message": None}

    def run(stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        normal_attr, cursor_attr = _configure_curses_default_colors(
            curses, stdscr)
        while True:
            cwd = state["cwd"]
            entries = cwd.sorted_children()
            if entries:
                state["cursor"] = max(0, min(state["cursor"],
                                               len(entries) - 1))
            else:
                state["cursor"] = 0

            stdscr.erase()
            height, width = stdscr.getmaxyx()
            lines = _review_tree_lines(root, cwd, state["cursor"], selected,
                                       message=state["message"])
            state["message"] = None
            for y, line in enumerate(lines[:max(0, height - 1)]):
                try:
                    attr = cursor_attr if line.startswith(">") else normal_attr
                    stdscr.addnstr(y, 0, line, max(1, width - 1), attr)
                except curses.error:
                    pass
            stdscr.refresh()

            key = stdscr.getch()
            key_name = {
                curses.KEY_UP: "KEY_UP",
                curses.KEY_DOWN: "KEY_DOWN",
                curses.KEY_RIGHT: "KEY_RIGHT",
                curses.KEY_LEFT: "KEY_LEFT",
                curses.KEY_BACKSPACE: "KEY_BACKSPACE",
                curses.KEY_ENTER: "KEY_ENTER",
            }.get(key)
            if key == 27:
                seq = []
                stdscr.nodelay(True)
                try:
                    for _ in range(16):
                        nxt = stdscr.getch()
                        if nxt == -1:
                            break
                        if 0 <= nxt <= 255:
                            seq.append(nxt)
                        decoded = _decode_tree_escape_sequence(bytes(seq))
                        if decoded != "ESC":
                            key_name = decoded
                            break
                    else:
                        key_name = _decode_tree_escape_sequence(bytes(seq))
                    if key_name is None:
                        key_name = _decode_tree_escape_sequence(bytes(seq))
                finally:
                    stdscr.nodelay(False)
            cwd = state["cwd"]
            entries = cwd.sorted_children()
            cursor = state["cursor"]
            if key_name == "KEY_UP" or key == ord("k"):
                if entries:
                    state["cursor"] = (cursor - 1) % len(entries)
            elif key_name == "KEY_DOWN" or key == ord("j"):
                if entries:
                    state["cursor"] = (cursor + 1) % len(entries)
            elif key_name == "KEY_ENTER" or key in (10, 13):
                if entries and entries[cursor].children:
                    state["cwd"] = entries[cursor]
                    state["cursor"] = 0
            elif key_name in ("KEY_BACKSPACE", "KEY_LEFT") or key in (8, 127):
                if cwd.parent is not None:
                    state["cwd"] = cwd.parent
                    state["cursor"] = 0
            elif key == ord(" "):
                if entries:
                    _toggle_node_selection(entries[cursor], selected)
            elif key in (ord("c"), ord("C")):
                if selected:
                    return sorted(selected)
                state["message"] = (
                    "No paths selected; select at least one path or press q "
                    "to cancel.")
            elif key in (ord("q"), ord("Q")) or key_name == "ESC":
                return None

    try:
        return curses.wrapper(run)
    except Exception:
        return _CURSES_UNAVAILABLE


def _select_review_paths_interactive(changes, key_reader=None, stream=None,
                                     clear_screen=True):
    """Interactive tree selector for file/folder-level selective accept.

    Returns a list of selected change paths, or ``None`` when the user cancels.
    Real TTY use goes through curses so terminal arrow decoding is delegated to
    the platform terminal library.  The injected key-reader path is kept for
    tests and as a minimal fallback if curses is unavailable.
    """
    stream = sys.stderr if stream is None else stream
    if key_reader is None and clear_screen:
        try:
            stdin_is_tty = sys.stdin.isatty()
        except Exception:
            stdin_is_tty = False
        if stdin_is_tty:
            curses_result = _select_review_paths_curses(changes)
            if curses_result is not _CURSES_UNAVAILABLE:
                return curses_result
    key_reader = key_reader or _read_tree_key
    root = _build_review_tree(changes)
    cwd = root
    cursor = 0
    selected = set()
    message = None
    while True:
        entries = cwd.sorted_children()
        if entries:
            cursor = max(0, min(cursor, len(entries) - 1))
        else:
            cursor = 0
        _render_review_tree(root, cwd, cursor, selected, stream,
                            clear_screen=clear_screen, message=message)
        message = None
        key = key_reader()
        if key in ("KEY_UP", "k"):
            if entries:
                cursor = (cursor - 1) % len(entries)
        elif key in ("KEY_DOWN", "j"):
            if entries:
                cursor = (cursor + 1) % len(entries)
        elif key in ("KEY_ENTER", "\r", "\n"):
            if entries and entries[cursor].children:
                cwd = entries[cursor]
                cursor = 0
        elif key in ("KEY_BACKSPACE", "KEY_LEFT", "\x7f", "\b"):
            if cwd.parent is not None:
                cwd = cwd.parent
                cursor = 0
        elif key == " ":
            if entries:
                _toggle_node_selection(entries[cursor], selected)
        elif key in ("c", "C"):
            if selected:
                return sorted(selected)
            message = "No paths selected; select at least one path or press q to cancel."
        elif key in ("q", "Q", "ESC", "\x1b"):
            return None


def _selector_changes_for_review(controller, session, include_ignored=False):
    """Fast path for selector contents.

    Prefer generated review artifacts for frozen/pending-review sessions so
    opening the selective-accept browser does not run a second expensive
    BranchFS status scan immediately after the review summary was displayed.
    The eventual commit still calls ``Controller.review(... commit_paths=...)``
    and rechecks authoritative branch/store state before mutating storage.
    """
    try:
        review = controller.store.review_dir(session.session_id)
        if (controller._can_use_stored_review(session) and
                os.path.isdir(review)):
            saw_status, changes, ignored = controller._stored_review_changes(
                session, review)
            if saw_status:
                if include_ignored:
                    return list(changes) + [item.change for item in ignored]
                return list(changes)
    except Exception:
        pass
    return [change for _root, change in controller._changes(
        session, include_ignored=include_ignored)]


def _selective_accept_review(controller, session, stream=None,
                             include_ignored=False, selector_changes=None):
    stream = sys.stderr if stream is None else stream
    if selector_changes is None:
        changes = [change for _root, change in controller._changes(
            session, include_ignored=include_ignored)]
    elif callable(selector_changes):
        changes = list(selector_changes())
    else:
        changes = list(selector_changes)
    if not changes:
        stream.write("ccc-agent: no changes available for selective accept\n")
        return session
    selected = _select_review_paths_interactive(changes, stream=stream)
    if selected is None:
        stream.write("ccc-agent: selective accept cancelled\n")
        return None
    updated = controller.review(session.session_id, commit_paths=selected,
                                out=stream, include_ignored=include_ignored)
    stream.write("ccc-agent: selective accept committed %d path(s) in session %s\n"
                 % (len(selected), updated.session_id))
    return updated


def _prompt_permission_denied_remainder(controller, session, stream=None):
    """Prompt after a partial commit left only permission-denied paths."""
    stream = sys.stderr if stream is None else stream
    _ensure_foreground_for_prompt()
    failures = permission_failures(session)
    stream.write(
        "ccc-agent: %d path(s) could not be written to real storage due to "
        "permission denied and remain only in BranchFS:\n" % len(failures))
    for item in failures:
        stream.write("  - %s\n" % item.get("path", "(unknown path)"))
    while True:
        stream.write(
            "ccc-agent: Discard those remaining branch-only files and finish? "
            "discard/d=yes / manual/m/later=keep pending-review [manual]: ")
        stream.flush()
        try:
            raw_choice = _read_review_choice()
        except EOFError:
            raw_choice = "manual"
        if len(raw_choice) == 1 and raw_choice not in ("\n", "\r"):
            stream.write("\n")
        choice = raw_choice.strip().lower()
        if choice in ("d", "discard", "y", "yes"):
            updated = controller.abort(session.session_id)
            stream.write(
                "ccc-agent: discarded permission-denied branch remainder; "
                "session %s finished as %s\n" %
                (updated.session_id, updated.state))
            return updated
        if choice in ("", "m", "manual", "l", "later", "keep", "\x1b", "esc"):
            stream.write(
                "ccc-agent: kept permission-denied paths in BranchFS for "
                "manual handling: %s\n" % session.session_id)
            return session
        stream.write("ccc-agent: please answer discard/d or manual/m/later.\n")


def _prompt_pending_review_decision(controller, session, stream=None,
                                    include_ignored=False,
                                    selector_changes=None):
    stream = sys.stderr if stream is None else stream
    if has_permission_failures(session):
        return _prompt_permission_denied_remainder(controller, session,
                                                   stream=stream)
    _ensure_foreground_for_prompt()
    while True:
        stream.write(
            "ccc-agent: Accept changes?\n"
            "  [c] commit all changes       (aliases: y, yes, commit)\n"
            "  [s] selective accept        (aliases: select, selective)\n"
            "  [d] discard all changes     (aliases: n, no, discard)\n"
            "  [l] keep for later review   (aliases: Enter, Esc, later)\n"
            "choice [l]: ")
        stream.flush()
        try:
            raw_choice = _read_review_choice()
        except EOFError:
            raw_choice = "later"
        if len(raw_choice) == 1 and raw_choice not in ("\n", "\r"):
            stream.write("\n")
        choice = raw_choice.strip().lower()
        if choice in ("c", "commit", "y", "yes"):
            updated = controller.commit(session.session_id,
                                        include_ignored=include_ignored)
            stream.write("ccc-agent: committed session %s\n"
                         % updated.session_id)
            return updated
        if choice in ("s", "select", "selective"):
            updated = _selective_accept_review(
                controller, session, stream=stream,
                include_ignored=include_ignored,
                selector_changes=selector_changes)
            if updated is not None:
                return updated
            continue
        if choice in ("d", "discard", "n", "no"):
            updated = controller.abort(session.session_id)
            stream.write("ccc-agent: discarded session %s\n"
                         % updated.session_id)
            return updated
        if choice in ("", "l", "later", "r", "review", "\x1b", "esc"):
            stream.write("ccc-agent: kept for later review: %s\n"
                         % session.session_id)
            return session
        stream.write(
            "ccc-agent: please choose c=commit, s=select, "
            "d/n=discard, or l/Enter/Esc=later.\n")


def _review_pending_session(controller, session, display_stream=None,
                            prompt_stream=None, show_ignored=False,
                            show_file_diffs=False, include_ignored=False,
                            prompt=True):
    display_stream = sys.stdout if display_stream is None else display_stream
    prompt_stream = sys.stderr if prompt_stream is None else prompt_stream
    if session.state not in ("pending-review", "frozen"):
        return session
    try:
        _display_or_page(_pending_review_text(
            controller, session, show_ignored=show_ignored,
            show_file_diffs=show_file_diffs), stream=display_stream)
    except ControlError as exc:
        prompt_stream.write("ccc-agent: could not show pending changes: %s\n" % exc)
    if prompt and _is_interactive_review():
        try:
            return _prompt_pending_review_decision(
                controller, session, stream=prompt_stream,
                include_ignored=include_ignored,
                selector_changes=lambda: _selector_changes_for_review(
                    controller, session, include_ignored=include_ignored))
        except ControlError as exc:
            prompt_stream.write("ccc-agent: review decision failed: %s\n" % exc)
    return session


def _handle_pending_review_finish(store, backend, alias_map, session, stream=None):
    stream = sys.stderr if stream is None else stream
    controller = Controller(store=store, backend=backend, alias_map=alias_map)
    return _review_pending_session(controller, session, display_stream=stream,
                                   prompt_stream=stream)


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
                        help="keep Codex/Hermes state and Claude Code runtime "
                             "paths inside BranchFS review instead of the "
                             "default shared direct runtime bind")
    parser.add_argument("--full-isolation", action="store_true",
                        help="do not bind the existing container /run, /var, "
                             "or /dev into the bwrap sandbox; restores the "
                             "stricter no-ambient-runtime-sockets behavior")
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
            "ccc-agent: review with: ccc-agent review %s\n"
            "ccc-agent: diff only: ccc-agent diff %s\n"
            "ccc-agent: text diffs: ccc-agent diff %s --show-file-diffs\n"
            "ccc-agent: single file diff: ccc-agent diff %s <path>\n"
            "ccc-agent: scripted: ccc-agent commit %s | ccc-agent abort %s\n"
            % (session.session_id, session.session_id, session.session_id,
               session.session_id, session.session_id, session.session_id))
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


def _split_resume_cmd_option(value):
    try:
        command = shlex.split(value)
    except ValueError as exc:
        raise ValueError("argument --cmd: %s" % exc)
    if not command:
        raise ValueError("argument --cmd: expected a non-empty command")
    return command


def _extract_resume_cmd_option(argv):
    """Pull ``--cmd CMD`` out before argparse's REMAINDER positional.

    ``resume`` already accepts ``SESSION -- argv...`` for exact argv-style
    overrides.  ``--cmd`` is a convenience shell-style string that must work
    after the session id too, so argparse cannot parse it directly once the
    REMAINDER positional starts.  Do not inspect tokens after a literal ``--``;
    those belong to the resumed command, not to ``ccc-agent resume``.
    """
    argv = list(argv)
    try:
        separator = argv.index("--")
    except ValueError:
        scan = argv
        tail = []
    else:
        scan = argv[:separator]
        tail = argv[separator:]

    remaining = []
    command = None
    i = 0
    while i < len(scan):
        token = scan[i]
        if token == "--cmd":
            if command is not None:
                raise ValueError("argument --cmd: may only be specified once")
            i += 1
            if i >= len(scan):
                raise ValueError("argument --cmd: expected one command string")
            command = _split_resume_cmd_option(scan[i])
        elif token.startswith("--cmd="):
            if command is not None:
                raise ValueError("argument --cmd: may only be specified once")
            command = _split_resume_cmd_option(token.split("=", 1)[1])
        else:
            remaining.append(token)
        i += 1
    return remaining + tail, command


def main_resume(argv=None, env=None, prog="ccc-agent resume"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Resume a BranchFS session after a crash/reboot, add more "
                    "work to a pending review, restart an aborted session, or "
                    "retry a failed session with --allow-failed.")
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--agent", default=None,
                        help="agent kind label for the resumed command")
    parser.add_argument("--cmd", metavar="CMD",
                        help="shell-style command string to run instead of "
                             "the stored command, e.g. --cmd 'bash' or "
                             "--cmd 'codex exec ...'; for exact argv use "
                             "SESSION -- argv ...")
    parser.add_argument("--force", action="store_true",
                        help="resume even if the old session mount still "
                             "appears active (use only after verifying no old "
                             "agent process is using it)")
    parser.add_argument("--allow-failed", action="store_true",
                        help="allow retrying a session currently in failed "
                             "state; use only after inspecting the failure and "
                             "confirming the preserved branch should be "
                             "reopened")
    parser.add_argument("--protect-agent-state", action="store_true",
                        help="keep Codex/Hermes state and Claude Code runtime "
                             "paths inside BranchFS review instead of the "
                             "default shared direct runtime bind")
    parser.add_argument("--full-isolation", action="store_true",
                        help="do not bind the existing container /run, /var, "
                             "or /dev into the bwrap sandbox; restores the "
                             "stricter no-ambient-runtime-sockets behavior")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the full session event log")
    parser.add_argument("session_id", metavar="session-id")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- command to run (default: stored command)")
    try:
        parse_argv, cmd_option = _extract_resume_cmd_option(
            list(sys.argv[1:] if argv is None else argv))
    except ValueError as exc:
        parser.error(str(exc))
    args = parser.parse_args(parse_argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if cmd_option is not None:
        if command:
            parser.error("argument --cmd: cannot be combined with a command "
                         "after --")
        command = cmd_option
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
                                 force=args.force,
                                 allow_failed=args.allow_failed)
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
            "ccc-agent: review with: ccc-agent review %s\n"
            "ccc-agent: diff only: ccc-agent diff %s\n"
            "ccc-agent: text diffs: ccc-agent diff %s --show-file-diffs\n"
            "ccc-agent: single file diff: ccc-agent diff %s <path>\n"
            "ccc-agent: scripted: ccc-agent commit %s | ccc-agent abort %s\n"
            % (session.session_id, session.session_id, session.session_id,
               session.session_id, session.session_id, session.session_id))
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
        if args.cmd == "turn-finalize":
            resp = client.finalize_turn(
                default_keep=getattr(args, "default_keep", False))
        elif args.cmd == "turn-kept-status":
            resp = client.kept_status()
        elif args.cmd == "turn-review-kept":
            resp = client.review_kept()
        elif args.cmd == "turn-approve":
            paths = _split_csv_paths(getattr(args, "paths", None))
            commit_paths = _split_csv_paths(getattr(args, "commit_paths", None))
            keep_paths = _split_csv_paths(getattr(args, "keep_paths", None))
            discard_paths = _split_csv_paths(getattr(args, "discard_paths", None))
            resp = client.approve_turn(args.approval_token, args.decision,
                                       paths=paths,
                                       commit_paths=commit_paths,
                                       keep_paths=keep_paths,
                                       discard_paths=discard_paths)
        else:  # turn-resolve
            if getattr(args, "all_kept", False):
                status = client.kept_status()
                paths = list(status.get("kept") or [])
            else:
                paths = _split_csv_paths(args.paths) or []
            resp = client.resolve_turn(args.decision, paths)
    except ChannelError as exc:
        sys.stderr.write("ccc-agent: control error: %s\n" % exc)
        return 0
    verdict = resp.get("verdict")
    if (args.cmd == "turn-finalize" and
            getattr(args, "default_keep", False) and
            verdict != VERDICT_NEEDS_APPROVAL):
        _write_default_keep_summary(resp, sys.stdout)
        return 0
    if verdict == VERDICT_KEPT_STATUS:
        _write_kept_status(resp, sys.stdout,
                           details=getattr(args, "details", False))
        return 0
    if verdict == VERDICT_NEEDS_KEPT_REVIEW:
        _write_kept_review_prompt(resp, sys.stderr,
                                  details=getattr(args, "details", False))
        return 2
    if verdict == VERDICT_NEEDS_APPROVAL:
        paths = resp.get("out_of_scope", [])
        token2 = resp.get("approval_token")
        sys.stderr.write(
            "ccc-agent: %d change(s) are outside the agent workspace or "
            "out of policy and were NOT committed:\n" % len(paths))
        for path in paths:
            sys.stderr.write("  - %s\n" % path)
        if resp.get("permission_denied"):
            sys.stderr.write(
                "ccc-agent: additionally, these in-scope path(s) could not "
                "be written due to permission denied and were kept in "
                "BranchFS only:\n")
            for path in resp["permission_denied"]:
                sys.stderr.write("  - %s\n" % path)
        sys.stderr.write(
            "ccc-agent: ask the user how to handle these, then run ONE of:\n"
            "    ccc-agent turn-approve %s            # commit all\n"
            "    ccc-agent turn-approve %s keep       # keep in branch, don't commit\n"
            "    ccc-agent turn-approve %s discard    # discard all from BranchFS\n"
            "    ccc-agent turn-approve %s --paths a,b # commit a,b; keep the rest\n"
            "    ccc-agent turn-approve %s --commit a --keep b --discard c\n"
            "ccc-agent: if the user is unavailable and work should continue, run:\n"
            "    ccc-agent turn-approve %s keep\n"
            "ccc-agent: kept paths can later be resolved with:\n"
            "    ccc-agent turn-resolve commit --paths a,b\n"
            "    ccc-agent turn-resolve discard --paths c\n"
            % (token2, token2, token2, token2, token2, token2))
        default_keep_after = getattr(args, "default_keep_after", None)
        if default_keep_after is None:
            return 2
        if default_keep_after > 0:
            sys.stderr.write(
                "ccc-agent: no decision within %.1f second(s) will default "
                "to keep-in-branch/non-commit\n" % default_keep_after)
            time.sleep(default_keep_after)
        try:
            resp = client.approve_turn(token2, "keep")
        except ChannelError as exc:
            sys.stderr.write(
                "ccc-agent: pending turn was not auto-kept, possibly already "
                "resolved: %s\n" % exc)
            return 0
        verdict = resp.get("verdict")
    if verdict == VERDICT_COMMITTED:
        msg = "committed %d change(s)" % len(resp.get("committed", []))
        if resp.get("kept"):
            msg += " (kept %d in branch)" % len(resp["kept"])
        elif resp.get("held"):
            msg += " (held %d for review)" % len(resp["held"])
        sys.stdout.write(msg + "\n")
        if resp.get("permission_denied"):
            sys.stdout.write("could not write due to permission denied:\n")
            for path in resp["permission_denied"]:
                sys.stdout.write("  - %s\n" % path)
        if resp.get("kept"):
            _write_kept_paths(resp["kept"], details=getattr(args, "details", False))
        if resp.get("discarded"):
            _write_discarded_paths(resp["discarded"], resp.get("stale"),
                                   details=getattr(args, "details", False))
    elif verdict == VERDICT_HELD:
        if resp.get("permission_denied"):
            sys.stdout.write("could not write due to permission denied:\n")
            for path in resp["permission_denied"]:
                sys.stdout.write("  - %s\n" % path)
        if resp.get("kept"):
            sys.stdout.write("kept %d path(s) in branch (not committed)\n"
                             % len(resp["kept"]))
            _write_kept_paths(resp["kept"], details=getattr(args, "details", False))
        if resp.get("discarded"):
            _write_discarded_paths(resp["discarded"], resp.get("stale"),
                                   details=getattr(args, "details", False))
        if not resp.get("kept") and not resp.get("discarded"):
            sys.stdout.write("changes held for review (not committed)\n")
    elif verdict == VERDICT_DISCARDED:
        _write_discarded_paths(resp.get("discarded") or [], resp.get("stale"),
                               details=getattr(args, "details", False))
    else:
        sys.stdout.write("%s\n" % (verdict or "ok"))
    return 0


def _write_default_keep_summary(resp, stream):
    committed = len(resp.get("committed") or [])
    kept_paths = resp.get("kept")
    if kept_paths is None:
        kept_paths = resp.get("held")
    kept = len(kept_paths or [])
    stream.write("committed (%d), kept local (%d)\n" % (committed, kept))


def _write_discarded_paths(paths, stale=None, stream=None, details=False):
    stream = sys.stdout if stream is None else stream
    paths = list(paths or [])
    stale = list(stale or [])
    if paths:
        stream.write("discarded %d path(s) from BranchFS\n" % len(paths))
        if details:
            for path in paths:
                stream.write("  - %s\n" % path)
    if stale:
        stream.write("already absent/stale path(s): %d\n" % len(stale))
        if details:
            for path in stale:
                stream.write("  - %s\n" % path)
    if not paths and not stale:
        stream.write("no matching live BranchFS changes to discard\n")


def _write_kept_status(resp, stream, details=False):
    committed = list(resp.get("committed") or [])
    paths = list(resp.get("kept") or [])
    stale = list(resp.get("stale") or [])
    committed_stale = list(resp.get("committed_stale") or [])
    stream.write("ccc-agent: committed=%d kept=%d stale=%d\n" %
                 (len(committed), len(paths), len(stale) + len(committed_stale)))
    if committed and details:
        stream.write("committed this live BranchFS session:\n")
        for path in committed:
            stream.write("  - %s\n" % path)
    if not paths:
        stream.write("ccc-agent: no kept non-workspace paths are currently live\n")
    else:
        stream.write("ccc-agent: resolve all with: ccc-agent turn-resolve <commit|discard|keep> --all-kept\n")
        if not details:
            stream.write("ccc-agent: list paths only if needed: ccc-agent turn-kept-status --details\n")
        else:
            stream.write("kept non-workspace paths (not committed to real storage):\n")
            for path in paths:
                stream.write("  - %s\n" % path)
            joined = ",".join(paths)
            stream.write(
                "resolve selected with: ccc-agent turn-resolve commit --paths %s\n"
                "or: ccc-agent turn-resolve discard --paths %s\n"
                % (joined, joined))
    if committed_stale and details:
        stream.write("remembered committed paths no longer present as live changes:\n")
        for path in committed_stale:
            stream.write("  - %s\n" % path)
    if stale and details:
        stream.write("remembered kept paths no longer present as live changes:\n")
        for path in stale:
            stream.write("  - %s\n" % path)


def _write_kept_review_prompt(resp, stream, details=False):
    paths = list(resp.get("kept") or [])
    if not paths:
        stream.write("ccc-agent: no kept non-workspace paths need review\n")
        return
    stream.write(
        "ccc-agent: %d kept non-workspace path(s) pending.\n" % len(paths))
    stream.write(
        "ccc-agent: ask user: commit, discard, or keep pending; then run "
        "ccc-agent turn-resolve <commit|discard|keep> --all-kept\n")
    if not details:
        stream.write("ccc-agent: list paths only if needed: ccc-agent turn-kept-status --details\n")
    else:
        stream.write("kept non-workspace paths (not committed to real storage):\n")
        for path in paths:
            stream.write("  - %s\n" % path)
        joined = ",".join(paths)
        stream.write(
            "selected-path commands:\n"
            "    ccc-agent turn-resolve commit --paths %s\n"
            "    ccc-agent turn-resolve discard --paths %s\n"
            "    ccc-agent turn-resolve keep --paths %s\n"
            % (joined, joined, joined))


def _write_kept_paths(paths, stream=None, details=False):
    stream = sys.stdout if stream is None else stream
    paths = list(paths or [])
    stream.write("kept %d path(s) pending (not committed)\n" % len(paths))
    if details and paths:
        stream.write("kept in branch only (not committed to real storage):\n")
        for path in paths:
            stream.write("  - %s\n" % path)
        joined = ",".join(paths)
        stream.write(
            "resolve later with: ccc-agent turn-resolve commit --paths %s\n"
            "or: ccc-agent turn-resolve discard --paths %s\n"
            % (joined, joined))


def _split_csv_paths(value):
    if not value:
        return None
    return [p for p in str(value).split(",") if p]


_SESSION_ID_CTL_OPS = (
    "show", "status", "commit", "abort", "thaw", "finish",
    "turn-record", "turn-check",
)
_BATCH_SESSION_ID_CTL_OPS = (
    "commit", "abort", "thaw", "finish", "turn-record",
)
_CTL_COMMAND_HELP = {
    "list": "list session records; accepts an optional session-id prefix",
    "cleanup": "remove old terminal session bundles after an age check",
    "show": "dump the full persisted session record as JSON",
    "status": "read live BranchFS status for each protected root",
    "diff": "show changed paths, or a unified diff for one changed file",
    "review": ("browse pending/frozen changes and choose "
               "commit/select/discard/later"),
    "commit": "commit pending/frozen session deltas to the real underlay",
    "abort": "discard session branch deltas and mark the session aborted",
    "thaw": "reopen a pending-review branch for more work",
    "finish": "finalize a running/manual session now (freeze + policy review)",
    "turn-record": "record a turn-boundary event for a session-id hook adapter",
    "turn-check": ("check live changes for hook-driven repair before "
                           "finalizing"),
    "turn-finalize": ("inside-session plugin op: finalize the current turn "
                      "via socket"),
    "turn-approve": ("inside-session plugin op: answer a pending turn "
                     "approval token"),
    "turn-resolve": ("inside-session plugin op: resolve remembered "
                     "kept/discarded turn paths"),
    "turn-kept-status": ("inside-session plugin op: show remembered kept "
                         "non-workspace paths"),
    "turn-review-kept": ("inside-session plugin op: ask about remembered "
                         "kept non-workspace paths"),
}


def _nonnegative_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a non-negative day count")
    if days < 0:
        raise argparse.ArgumentTypeError("must be a non-negative day count")
    return days


def _nonnegative_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a non-negative second count")
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be a non-negative second count")
    return seconds


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
            elif cmd == "turn-record":
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


def _add_ctl_parser(subparsers, name, aliases=()):
    kwargs = {
        "help": _CTL_COMMAND_HELP[name],
        "description": _CTL_COMMAND_HELP[name],
    }
    if aliases:
        kwargs["aliases"] = aliases
    return subparsers.add_parser(name, **kwargs)


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
    lp = _add_ctl_parser(sub, "list", aliases=("ls",))
    lp.add_argument("session_id", nargs="?", metavar="session-id-prefix",
                    help="optional session id prefix filter")
    cp = _add_ctl_parser(sub, "cleanup")
    cp.add_argument("-o", "--older-than", metavar="DAYS",
                    type=_nonnegative_days, default=30,
                    help="remove sessions older than DAYS (default: 30)")
    cp.add_argument("-a", "--all-type", "--all-types", dest="all_types",
                    action="store_true",
                    help="include every session state, not only closed "
                         "terminal sessions")
    cp.add_argument("--dry-run", action="store_true",
                    help="show what would be removed without deleting")
    for name in _SESSION_ID_CTL_OPS:
        p = _add_ctl_parser(sub, name)
        _add_session_id_arg(p, multiple=name in _BATCH_SESSION_ID_CTL_OPS)
    dp = _add_ctl_parser(sub, "diff")
    dp.add_argument("--show-ignored", action="store_true",
                    help="also list policy-ignored/cache/runtime changes")
    dp.add_argument("--show-file-diffs", action="store_true",
                    help="also show unified diffs for changed text files "
                         "(binary/non-text files are skipped)")
    _add_session_id_arg(dp)
    dp.add_argument("path", nargs="?", help="optional changed file to diff")
    rv = _add_ctl_parser(sub, "review")
    rv.description = ("Browse a pending/frozen session's changes. With no "
                      "action flags, an interactive TTY prompts for a decision "
                      "after showing the summary.")
    rv.epilog = ("Prompt choices: yes/y commits, select/s opens a tree selector "
                 "for file/folder-level commit, no/n discards, and later/l/Esc "
                 "keeps the session for review.")
    _add_session_id_arg(rv)
    rv.add_argument("--accept", action="store_true", help="commit everything")
    rv.add_argument("--include-ignored", action="store_true",
                    help="with --accept/--commit/--emit-patch, include "
                         "policy-ignored cache/runtime changes too")
    rv.add_argument("--show-ignored", action="store_true",
                    help="show full policy-ignored/cache/runtime change list")
    rv.add_argument("--show-file-diffs", action="store_true",
                    help="also show unified diffs for changed text files "
                         "(binary/non-text files are skipped)")
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
    fp = _add_ctl_parser(sub, "turn-finalize")
    fp.add_argument("--default-keep", action="store_true",
                    help="do not block for new out-of-scope paths; keep them "
                         "in the branch and continue")
    fp.add_argument("--default-keep-after", metavar="SECONDS",
                    type=_nonnegative_seconds,
                    help="first emit the approval prompt; if no external "
                         "approval arrives within SECONDS, keep the flagged "
                         "paths in the branch and continue")
    ap = _add_ctl_parser(sub, "turn-approve")
    ap.add_argument("approval_token")
    ap.add_argument("decision", nargs="?", default="yes",
                    help="yes (commit all, default) | keep (don't commit) | "
                         "revert (discard)")
    ap.add_argument("--paths", help="comma-separated subset to commit "
                                    "file-by-file; the rest are kept")
    ap.add_argument("--commit", dest="commit_paths",
                    help="comma-separated paths to commit")
    ap.add_argument("--keep", dest="keep_paths",
                    help="comma-separated paths to keep in the branch only")
    ap.add_argument("--discard", dest="discard_paths",
                    help="comma-separated paths to discard/revert")
    rp = _add_ctl_parser(sub, "turn-resolve")
    rp.add_argument("decision", help="commit | keep | discard")
    rp.add_argument("--paths",
                    help="comma-separated remembered paths to resolve")
    rp.add_argument("--all-kept", action="store_true",
                    help="resolve all currently kept non-workspace paths")
    rp.add_argument("--details", action="store_true",
                    help="include resolved path lists in output")
    kp = _add_ctl_parser(sub, "turn-kept-status")
    kp.add_argument("--details", action="store_true",
                    help="list exact paths instead of the compact count summary")
    rvk = _add_ctl_parser(sub, "turn-review-kept")
    rvk.add_argument("--details", action="store_true",
                     help="list exact paths instead of the compact user prompt")
    args = parser.parse_args(argv)

    if args.cmd == "turn-resolve" and not args.paths and not args.all_kept:
        parser.error("turn-resolve requires --paths or --all-kept")

    if args.cmd in ("turn-finalize", "turn-approve", "turn-resolve",
                    "turn-kept-status", "turn-review-kept"):
        return _ctl_socket(args, env)

    config = load_config(args.config, env=env)
    store, backend, alias_map, _user, _roots = build_runtime(config)
    controller = Controller(store=store, backend=backend, alias_map=alias_map)

    try:
        if args.cmd in ("list", "ls"):
            controller.list(getattr(args, "session_id", None))
        elif args.cmd == "cleanup":
            controller.cleanup(older_than_days=args.older_than,
                               dry_run=args.dry_run,
                               all_types=args.all_types)
        elif args.cmd == "show":
            controller.show(args.session_id)
        elif args.cmd == "status":
            controller.status(args.session_id)
        elif args.cmd == "diff":
            controller.diff(args.session_id, path=args.path,
                            show_ignored=args.show_ignored,
                            show_file_diffs=args.show_file_diffs)
        elif args.cmd == "review":
            commit_paths = ([p for p in args.commit_paths.split(",") if p]
                            if args.commit_paths else None)
            has_review_action = bool(args.accept or args.reject or commit_paths
                                     or args.emit_patch or args.apply_patch)
            if has_review_action:
                session = controller.review(
                    args.session_id, accept=args.accept, reject=args.reject,
                    commit_paths=commit_paths, emit_patch=args.emit_patch,
                    apply_patch=args.apply_patch, show_ignored=args.show_ignored,
                    include_ignored=args.include_ignored,
                    show_file_diffs=args.show_file_diffs)
            else:
                session = controller._load(args.session_id)
                controller._require_state(session, ("pending-review", "frozen"),
                                          "review")
                session = _review_pending_session(
                    controller, session, display_stream=sys.stdout,
                    prompt_stream=sys.stderr, show_ignored=args.show_ignored,
                    show_file_diffs=args.show_file_diffs,
                    include_ignored=args.include_ignored)
            if session.state in ("committed", "aborted"):
                sys.stderr.write("session %s now %s\n"
                                 % (session.session_id, session.state))
        elif args.cmd in _BATCH_SESSION_ID_CTL_OPS:
            return _run_batch_session_op(controller, args.cmd, args.session_ids)
        elif args.cmd == "turn-check":
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
    "list", "ls", "cleanup", "diff", "review", "turn-finalize",
    "turn-approve", "turn-resolve", "turn-kept-status",
    "turn-review-kept",
})
_SESSION_ID_COMPLETION_OPS = (
    set(_SESSION_ID_CTL_OPS) | {"diff", "review", "list", "ls", "resume"}
)
_MULTI_SESSION_ID_COMPLETION_OPS = set(_BATCH_SESSION_ID_CTL_OPS)
_MAIN_OPS = tuple(sorted(_CTL_OPS | {
    "run", "resume", "setup", "softsandbox", "completion",
}))
_TOP_LEVEL_OPTIONS = ("--config", "--version", "--help")
_GLOBAL_VALUE_OPTIONS = frozenset(("--config",))
_CLEANUP_VALUE_OPTIONS = frozenset(("--older-than", "-o"))
_REVIEW_VALUE_OPTIONS = frozenset(("--commit", "--apply-patch"))
_RESUME_VALUE_OPTIONS = frozenset(("--agent", "--cmd"))
_RUN_OPTIONS = (
    "--agent", "--full-isolation", "--hide", "--policy",
    "--protect-agent-state", "--scope", "--verbose", "--workspace", "-v",
    "--config", "--help",
)
_CLEANUP_OPTIONS = ("--older-than", "-o", "--all-type", "--all-types", "-a",
                    "--dry-run", "--config", "--help")
_DIFF_OPTIONS = ("--show-ignored", "--show-file-diffs", "--config", "--help")
_REVIEW_OPTIONS = (
    "--accept", "--reject", "--commit", "--emit-patch", "--apply-patch",
    "--show-ignored", "--show-file-diffs", "--include-ignored", "--config",
    "--help",
)
_RESUME_OPTIONS = (
    "--agent", "--cmd", "--allow-failed", "--force", "--full-isolation",
    "--protect-agent-state", "--verbose", "-v", "--config", "--help",
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
    if op == "diff":
        return _DIFF_OPTIONS
    if op == "review":
        return _REVIEW_OPTIONS
    if op == "resume":
        return _RESUME_OPTIONS
    if op == "run":
        return _RUN_OPTIONS
    if op == "turn-finalize":
        return ("--default-keep", "--default-keep-after", "--config", "--help")
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
        "  --version        print the ccc-agent release version and Git commit when known\n\n"
        "Primary user ops:\n"
        "  run              start a contained BranchFS session; when no command "
        "is given, open the invoking shell (legacy alias: launch)\n"
        "  resume           reopen an existing session and run the stored "
        "command, --cmd CMD, or a custom argv after --\n\n"
        "Session/control ops (run outside a contained session):\n"
        "  list, ls         list session records; accepts an optional session-id "
        "prefix\n"
        "  review           browse pending/frozen changes and choose "
        "commit/select/reject/later\n"
        "  diff             show changed paths, or a unified diff for one "
        "changed file\n"
        "  show             dump the full persisted session record as JSON\n"
        "  status           read live BranchFS status for each protected root\n"
        "  finish           finalize a running/manual session now "
        "(freeze + policy review)\n"
        "  commit           commit pending/frozen session deltas to the real "
        "underlay; repeat IDs to batch\n"
        "  abort            discard session branch deltas and mark aborted; "
        "repeat IDs to batch\n"
        "  thaw             reopen a pending-review branch for more work\n"
        "  cleanup          remove old terminal session bundles after an age "
        "check; use --all-type to include failed/non-terminal sessions\n\n"
        "Plugin/hook ops (normally invoked by agent plugins/hooks):\n"
        "  turn-finalize   inside session: finalize the current turn via the "
        "control socket\n"
        "  turn-approve    inside session: answer a pending per-turn approval "
        "token\n"
        "  turn-resolve    inside session: commit/keep/discard a previously "
        "remembered path\n"
        "  turn-check      hook adapter: ask a running session to repair "
        "policy/conflict issues before finalizing\n"
        "  turn-record     hook adapter: record a turn-boundary event for a "
        "session id\n\n"
        "Auxiliary setup/debug ops:\n"
        "  setup            write config, plugin entries, and optional shims\n"
        "  completion       print shell completion code (bash, zsh, fish)\n"
        "  softsandbox      run the legacy diagnostic non-FUSE soft sandbox\n\n"
        "Examples:\n"
        "  ccc-agent run --workspace /home/$USER/project -- codex exec 'fix bug'\n"
        "  ccc-agent resume <session>             # rerun the stored command\n"
        "  ccc-agent resume <session> --cmd bash  # resume with a custom shell\n"
        "  ccc-agent resume <session> -- bash     # exact custom argv\n"
        "  ccc-agent list                         # or: ccc-agent ls\n"
        "  ccc-agent review <session> --accept\n"
        "  ccc-agent diff <session> <path>\n"
        "  ccc-agent cleanup --older-than 30 --dry-run\n"
        "  ccc-agent cleanup -a -o 20           # include failed/non-terminal sessions\n"
        "  ccc-agent setup --system --enable-shims\n")


def main(argv=None, env=None):
    """Dispatch the unified ``ccc-agent OP`` command surface."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--version":
        sys.stdout.write("ccc-agent %s\n" % version_string())
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
