"""Helpers for recoverable partial commits.

When a reviewed change cannot be written to the real underlay because of
permissions, ccc-agent should keep that path in the BranchFS branch while still
committing every other writable path.  These helpers keep the session metadata
and branch store pruning consistent across runner, operator, and turn commit
paths.
"""

import os
import shutil


COMMIT_PERMISSION_DENIED_KEY = "commit_permission_denied"
COMMIT_PARTIAL_APPLIED_KEY = "commit_partial_applied"


def is_permission_denied(exc):
    """Return True for OS permission failures that should be held, not fatal."""
    return isinstance(exc, PermissionError)


def store_paths(root, change, alias_map):
    """Return ``(rel, delta, base)`` for a BranchFS store-backed change."""
    visible = alias_map.canonicalize(root.visible)
    rel = os.path.relpath(alias_map.canonicalize(change.path), visible)
    delta = os.path.join(root.store, "branches", root.branch, "files", rel)
    return rel, delta, os.path.join(root.base, rel)


def permission_failure_record(root, change, rel, exc):
    return {
        "root": root.name,
        "path": change.path,
        "relpath": "/" + rel.lstrip("/"),
        "op": change.op,
        "kind": change.kind,
        "error": str(exc),
    }


def permission_failures(session):
    failures = session.policy.get(COMMIT_PERMISSION_DENIED_KEY, [])
    return failures if isinstance(failures, list) else []


def has_permission_failures(session):
    return bool(permission_failures(session))


def remember_permission_failures(session, failures, applied_count=0):
    failures = list(failures)
    session.policy[COMMIT_PERMISSION_DENIED_KEY] = failures
    session.policy[COMMIT_PARTIAL_APPLIED_KEY] = int(
        session.policy.get(COMMIT_PARTIAL_APPLIED_KEY, 0) or 0) + int(applied_count)


def clear_permission_failures(session):
    session.policy.pop(COMMIT_PERMISSION_DENIED_KEY, None)
    session.policy.pop(COMMIT_PARTIAL_APPLIED_KEY, None)


def _branch_files_dir(root):
    return os.path.join(root.store, "branches", root.branch, "files")


def _branch_tombstones_path(root):
    return os.path.join(root.store, "branches", root.branch, "tombstones")


def _is_rel_or_descendant(candidate, rel):
    candidate = candidate.strip("/")
    rel = rel.strip("/")
    return candidate == rel or candidate.startswith(rel.rstrip("/") + "/")


def _remove_tombstones(root, rel):
    path = _branch_tombstones_path(root)
    try:
        with open(path) as fh:
            lines = [line.rstrip("\n") for line in fh]
    except FileNotFoundError:
        return
    kept = [line for line in lines
            if line.strip() and not _is_rel_or_descendant(line, rel)]
    if kept:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            for line in kept:
                fh.write(line + "\n")
        os.replace(tmp, path)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _prune_empty_dirs(path, stop):
    path = os.path.abspath(path)
    stop = os.path.abspath(stop)
    while path.startswith(stop + os.sep) and path != stop:
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


def prune_store_change(root, rel):
    """Remove one successfully-applied change from the BranchFS store.

    This is used only after the change has already reached the real underlay.
    Removing the delta/tombstone leaves any permission-denied siblings in the
    branch for later review/resume.
    """
    files_dir = _branch_files_dir(root)
    delta = os.path.join(files_dir, rel)
    if os.path.lexists(delta):
        if os.path.isdir(delta) and not os.path.islink(delta):
            shutil.rmtree(delta)
        else:
            os.unlink(delta)
        _prune_empty_dirs(os.path.dirname(delta), files_dir)
    _remove_tombstones(root, rel)


def prune_backend_change(backend, root, rel):
    """Prune a successful change, allowing test backends to update memory state."""
    prune = getattr(backend, "prune_change", None)
    if prune is not None:
        prune(root, rel)
    else:
        prune_store_change(root, rel)
