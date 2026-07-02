"""BranchFS backends for the trusted supervisor.

``BranchfsCli`` drives the real ``branchfs`` binary (daemon-per-store model:
the unix socket lives inside the store directory).  ``FakeBranchFS`` is a
filesystem-level simulation used by non-FUSE tests: the "mounted view" is the
branch delta directory itself, which is behaviorally adequate for exercising
the supervisor's orchestration, policy, and artifact logic.

Both backends speak in terms of :class:`ccc_agent.session.ProtectedRoot`.
"""

import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import time

from .policy import Change


DEFAULT_BRANCHFS_TIMEOUT_SECONDS = 30


class BranchfsError(Exception):
    pass


class StatusWarning(object):
    """One non-fatal BranchFS status warning in agent-visible namespace."""

    __slots__ = ("path", "message", "root")

    def __init__(self, path, message, root=""):
        self.path = path
        self.message = message
        self.root = root

    def to_dict(self):
        return {"path": self.path, "message": self.message, "root": self.root}


class StatusReport(object):
    """Complete BranchFS status payload: changes plus non-fatal warnings."""

    __slots__ = ("changes", "warnings")

    def __init__(self, changes=(), warnings=()):
        self.changes = list(changes)
        self.warnings = list(warnings)


def _branch_dir(root):
    return os.path.join(root.store, "branches", root.branch)


def _daemon_socket(root):
    return os.path.join(root.store, "daemon.sock")


def _unix_socket_ready(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _remove_empty_orphan_branch_dir(root):
    """Remove a branch directory left behind by a partial abort, if empty.

    Real BranchFS can get into this shape when `abort-branch` is attempted
    while a branch is still mounted: the daemon forgets the branch, but NFS
    silly-renamed files or structural directories can keep the store path from
    being removed.  Treat an absent/empty orphan as already aborted, but do not
    discard real files here because that would hide a more serious cleanup
    failure from the operator.
    """
    path = _branch_dir(root)
    if not os.path.exists(path):
        return True
    if not os.path.isdir(path):
        return False
    for _dirpath, _dirnames, filenames in os.walk(path):
        if filenames:
            return False
    shutil.rmtree(path)
    return True


def _format_timeout_seconds(value):
    return "%gs" % float(value)


def _coerce_output(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _run_subprocess(argv, timeout=DEFAULT_BRANCHFS_TIMEOUT_SECONDS):
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        label = " ".join(str(part) for part in argv[:2])
        command = " ".join(shlex.quote(str(part)) for part in argv)
        detail = (_coerce_output(exc.stderr) or _coerce_output(exc.stdout)).strip()
        if detail:
            detail = ": " + detail
        raise BranchfsError(
            "%s timed out after %s; command: %s%s"
            % (label, _format_timeout_seconds(timeout), command, detail)
        )
    return proc.returncode, proc.stdout, proc.stderr


def _changes_from_status(data, root):
    """Map a `branchfs status --json` document to Change objects in the
    agent-visible namespace.

    BranchFS stores ordinary parent directories in the delta tree so nested
    files have somewhere to live.  Older BranchFS status output exposed those
    directories as separate 0-byte changes.  They are structural, not authored
    changes, so the ccc-agent review surface keeps only leaf paths (files,
    symlinks, tombstones, and standalone directory entries with no changed
    descendants).
    """
    entries = list(data.get("diff", ()))
    relpaths = [e.get("path", "").lstrip("/") for e in entries]

    def has_changed_descendant(relpath):
        prefix = relpath.rstrip("/") + "/"
        return any(other != relpath and other.startswith(prefix)
                   for other in relpaths)

    changes = []
    for entry in entries:
        relpath = entry.get("path", "").lstrip("/")
        kind = entry.get("kind", "file")
        if kind == "dir" and has_changed_descendant(relpath):
            continue
        visible = root.visible.rstrip("/") + "/" + relpath
        if entry.get("op") == "delete":
            op = "D"
        else:
            base_path = os.path.join(root.base, relpath)
            op = "M" if os.path.lexists(base_path) else "A"
        changes.append(Change(op=op, path=visible, kind=kind,
                              bytes=entry.get("bytes", 0), root=root.name))
    return changes


def _visible_warning_path(root, relpath):
    relpath = str(relpath or "").lstrip("/")
    if not relpath:
        return root.visible.rstrip("/")
    return root.visible.rstrip("/") + "/" + relpath


def _warnings_from_status(data, root):
    warnings = []
    for warning in data.get("warnings", ()) or ():
        if not isinstance(warning, dict):
            continue
        warnings.append(StatusWarning(
            path=_visible_warning_path(root, warning.get("path", "")),
            message=str(warning.get("message", "")),
            root=root.name,
        ))
    return warnings


def _status_warning(root, relpath, message):
    return StatusWarning(path=_visible_warning_path(root, relpath),
                         message=str(message), root=root.name)


def _branch_files_dir(root):
    return os.path.join(_branch_dir(root), "files")


def _branch_tombstones_path(root):
    return os.path.join(_branch_dir(root), "tombstones")


def _join_rel(parent, child):
    return child if not parent else parent.rstrip("/") + "/" + child


def _entry_kind_from_mode(mode):
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "file"


def _warn_if_unreadable_regular_file(path, relpath, root, warnings, mode):
    if not stat.S_ISREG(mode):
        return
    try:
        with open(path, "rb"):
            pass
    except PermissionError as exc:
        warnings.append(_status_warning(
            root, relpath,
            "unreadable delta file: %s; commit may fail" % exc))
    except OSError:
        # Status should stay read-only and best-effort here. Other file races are
        # already represented by the changed path; do not turn them into fatal
        # operator-facing errors.
        pass


def _collect_store_delta(files_dir, relpath, root, diff, warnings):
    directory = files_dir if not relpath else os.path.join(files_dir, relpath)
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return
    except PermissionError as exc:
        warnings.append(_status_warning(
            root, relpath,
            "unreadable delta directory: %s; status may be incomplete; "
            "commit may fail" % exc))
        return
    except OSError as exc:
        warnings.append(_status_warning(
            root, relpath,
            "could not scan delta directory: %s; status may be incomplete" % exc))
        return

    for entry in sorted(entries, key=lambda item: item.name):
        child_rel = _join_rel(relpath, entry.name)
        child_path = os.path.join(files_dir, child_rel)
        try:
            metadata = entry.stat(follow_symlinks=False)
        except PermissionError as exc:
            warnings.append(_status_warning(
                root, child_rel,
                "unreadable delta entry metadata: %s; status may be "
                "incomplete; commit may fail" % exc))
            continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.append(_status_warning(
                root, child_rel,
                "could not stat delta entry: %s; status may be incomplete" % exc))
            continue

        kind = _entry_kind_from_mode(metadata.st_mode)
        diff.append({"op": "delta", "path": child_rel, "kind": kind,
                     "bytes": metadata.st_size if kind != "dir" else 0})
        if kind == "dir":
            _collect_store_delta(files_dir, child_rel, root, diff, warnings)
        else:
            _warn_if_unreadable_regular_file(
                child_path, child_rel, root, warnings, metadata.st_mode)


def _collect_store_tombstones(root, diff, warnings):
    path = _branch_tombstones_path(root)
    try:
        with open(path) as fh:
            relpaths = [line.strip() for line in fh if line.strip()]
    except FileNotFoundError:
        return
    except PermissionError as exc:
        warnings.append(_status_warning(
            root, "", "unreadable tombstone list: %s; deletions may be missing"
            % exc))
        return
    except OSError as exc:
        warnings.append(_status_warning(
            root, "", "could not read tombstone list: %s; deletions may be "
            "missing" % exc))
        return

    for relpath in sorted(set(relpaths)):
        diff.append({"op": "delete", "path": relpath.lstrip("/"),
                     "kind": "tombstone", "bytes": 0})


def _fallback_status_report_from_store(root, reason):
    branch_dir = _branch_dir(root)
    if not os.path.isdir(branch_dir):
        raise reason
    diff = []
    warnings = [_status_warning(
        root, "",
        "branchfs status failed; using direct store fallback: %s" % reason)]
    _collect_store_delta(_branch_files_dir(root), "", root, diff, warnings)
    _collect_store_tombstones(root, diff, warnings)
    return StatusReport(changes=_changes_from_status({"diff": diff}, root),
                        warnings=warnings)


class BranchfsCli(object):
    """Drives the branchfs CLI; one daemon per protected root (per store)."""

    def __init__(self, binary="branchfs", run=None,
                 timeout_seconds=DEFAULT_BRANCHFS_TIMEOUT_SECONDS,
                 daemon_ready=None, sleep=None):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._daemon_ready = daemon_ready or _unix_socket_ready
        self._sleep = sleep or time.sleep
        if run is None:
            self._run = lambda argv: _run_subprocess(
                argv, timeout=self.timeout_seconds)
        else:
            self._run = run

    def _invoke(self, *argv):
        code, out, err = self._run([self.binary] + [str(a) for a in argv])
        if code != 0:
            raise BranchfsError("%s %s failed (%d): %s"
                                % (self.binary, argv[0], code,
                                   err.strip() or out.strip()))
        return out

    def _wait_for_daemon_socket(self, root, deadline):
        socket_path = _daemon_socket(root)
        while time.monotonic() < deadline:
            if self._daemon_ready(socket_path):
                return True
            self._sleep(0.1)
        return self._daemon_ready(socket_path)

    def start_daemon(self, root):
        # Idempotent: the daemon auto-exits when its last mount goes away,
        # so every daemon-dependent operation re-ensures it first (e.g.
        # `ccc-agent commit` long after the agent session unmounted).
        started_at = time.monotonic()
        try:
            self._invoke("start-daemon", "--base", root.base,
                         "--storage", root.store)
        except BranchfsError as exc:
            # BranchFS itself has a short internal readiness wait.  On a cold
            # node restart with an existing/large store, the child daemon may be
            # alive but still scanning/loading state when the parent CLI returns
            # "Daemon failed to start".  Do not spawn more daemons immediately;
            # poll the store socket until ccc-agent's own timeout budget expires.
            if "Daemon failed to start" not in str(exc):
                raise
            deadline = started_at + float(self.timeout_seconds)
            if self._wait_for_daemon_socket(root, deadline):
                return
            raise BranchfsError(
                "%s; daemon socket %s was not reachable within ccc-agent's "
                "%s timeout. The BranchFS child may have exited before "
                "binding its socket, or startup may still be slower than the "
                "configured branchfs_timeout_seconds."
                % (exc, _daemon_socket(root),
                   _format_timeout_seconds(self.timeout_seconds)))

    def create_branch(self, root, parent="main"):
        self.start_daemon(root)
        argv = ["create", root.branch, "--parent", parent,
                "--storage", root.store]
        for hidden in getattr(root, "hide_paths", None) or ():
            argv.extend(["--hide", hidden])
        self._invoke(*argv)

    def mount(self, root, agent=True, allow_other=False):
        self.start_daemon(root)
        argv = ["mount", "--storage", root.store, "--branch", root.branch]
        if agent:
            argv.append("--agent")
        if allow_other:
            # Only needed when the daemon and the agent run as different uids
            # (FUSE otherwise denies any uid but the mounting one).  The current
            # bwrap/none models are same-uid, so this stays off; kept for any
            # future privilege-separated mount.
            argv.append("--allow-other")
        argv.append(root.mount)
        self._invoke(*argv)

    def unmount(self, root):
        self._invoke("unmount", root.mount, "--storage", root.store)

    def freeze(self, root):
        self.start_daemon(root)
        self._invoke("freeze", root.branch, "--storage", root.store)

    def thaw(self, root):
        self.start_daemon(root)
        self._invoke("thaw", root.branch, "--storage", root.store)

    def status_report(self, root):
        try:
            self.start_daemon(root)
            out = self._invoke("status", root.branch, "--storage", root.store,
                               "--json")
        except (BranchfsError, OSError) as exc:
            return _fallback_status_report_from_store(root, exc)
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise BranchfsError("unparseable status output: %s" % exc)
        return StatusReport(changes=_changes_from_status(data, root),
                            warnings=_warnings_from_status(data, root))

    def status(self, root):
        return self.status_report(root).changes

    def commit(self, root):
        self.start_daemon(root)
        self._invoke("commit-branch", root.branch, "--storage", root.store)

    def abort(self, root):
        self.start_daemon(root)
        try:
            self._invoke("abort-branch", root.branch, "--storage", root.store)
        except BranchfsError as exc:
            # Idempotent recovery for the observed partial-abort state: the
            # branch registry is already gone, and only an empty store skeleton
            # remains after the stale mount has been unmounted.
            if "branch not found" in str(exc) and _remove_empty_orphan_branch_dir(root):
                return
            raise


class FakeBranchFS(object):
    """Non-FUSE stand-in: the mount *is* the delta directory.

    Reads do not fall through to base (unlike real BranchFS), which is fine
    for supervisor tests: they only assert on orchestration, status, policy,
    commit, and abort behavior.
    """

    def __init__(self):
        self._state = {}      # (store, branch) -> "open" | "frozen"
        self._deletes = {}    # (store, branch) -> set(relpath)
        self._mounted = {}    # mount -> (store, branch)

    # -- helpers -----------------------------------------------------------
    def _key(self, root):
        return (root.store, root.branch)

    def _files_dir(self, root):
        return os.path.join(root.store, "branches", root.branch, "files")

    def branch_state(self, root):
        return self._state.get(self._key(root), "open")

    def record_delete(self, root, relpath):
        """Simulate the agent deleting an inherited path (tombstone)."""
        self._deletes.setdefault(self._key(root), set()).add(relpath)

    # -- backend API --------------------------------------------------------
    def start_daemon(self, root):
        os.makedirs(root.store, exist_ok=True)

    def create_branch(self, root, parent="main"):
        os.makedirs(self._files_dir(root), exist_ok=True)
        self._state[self._key(root)] = "open"
        self._deletes.setdefault(self._key(root), set())

    def mount(self, root, agent=True, allow_other=False):
        files = self._files_dir(root)
        os.makedirs(files, exist_ok=True)
        parent = os.path.dirname(root.mount)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.islink(root.mount) and not os.path.exists(root.mount):
            os.symlink(files, root.mount)
        self._mounted[root.mount] = self._key(root)

    def unmount(self, root):
        self._mounted.pop(root.mount, None)
        if os.path.islink(root.mount):
            os.unlink(root.mount)

    def freeze(self, root):
        self._state[self._key(root)] = "frozen"

    def thaw(self, root):
        self._state[self._key(root)] = "open"

    def status_report(self, root):
        files = self._files_dir(root)
        diff = []
        if os.path.isdir(files):
            for dirpath, _dirnames, filenames in os.walk(files):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, files)
                    diff.append({"op": "delta", "path": rel, "kind": "file",
                                 "bytes": os.path.getsize(full)})
        for rel in sorted(self._deletes.get(self._key(root), ())):
            diff.append({"op": "delete", "path": rel, "kind": "tombstone",
                         "bytes": 0})
        return StatusReport(changes=_changes_from_status({"diff": diff}, root),
                            warnings=[])

    def status(self, root):
        return self.status_report(root).changes

    def _apply_to_base(self, root):
        files = self._files_dir(root)
        if os.path.isdir(files):
            for dirpath, _dirnames, filenames in os.walk(files):
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, files)
                    dest = os.path.join(root.base, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(full, dest)
        for rel in self._deletes.get(self._key(root), ()):
            target = os.path.join(root.base, rel)
            if os.path.isfile(target) or os.path.islink(target):
                os.unlink(target)
            elif os.path.isdir(target):
                shutil.rmtree(target)

    def commit(self, root):
        self._apply_to_base(root)
        self._cleanup(root)

    def abort(self, root):
        self._cleanup(root)

    def _cleanup(self, root):
        files = self._files_dir(root)
        if os.path.isdir(files):
            shutil.rmtree(files)
        os.makedirs(files, exist_ok=True)
        self._deletes[self._key(root)] = set()
