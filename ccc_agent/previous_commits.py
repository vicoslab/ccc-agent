"""Helpers for paths already applied to the real underlay during a live session."""

import filecmp
import os


TURN_PATH_DECISIONS = "turn_path_decisions"
DECISION_COMMITTED = "committed"


def committed_path_keys(session, alias_map=None):
    """Return normalized visible-path keys marked as already committed."""
    decisions = session.policy.get(TURN_PATH_DECISIONS, {})
    if not isinstance(decisions, dict):
        return set()
    keys = set()
    for path, decision in decisions.items():
        if decision != DECISION_COMMITTED:
            continue
        normalized = os.path.normpath(path)
        keys.add(normalized)
        if alias_map is not None:
            keys.add(os.path.normpath(alias_map.canonicalize(path)))
    return keys


def change_path_keys(change, alias_map=None):
    keys = {os.path.normpath(change.path)}
    if alias_map is not None:
        keys.add(os.path.normpath(alias_map.canonicalize(change.path)))
    return keys


def store_paths(root, change, alias_map):
    """Return ``(rel, delta, base)`` for a store-backed BranchFS change."""
    visible = alias_map.canonicalize(root.visible)
    rel = os.path.relpath(alias_map.canonicalize(change.path), visible)
    delta = os.path.join(root.store, "branches", root.branch, "files", rel)
    return rel, delta, os.path.join(root.base, rel)


def _same_symlink(left, right):
    try:
        return (os.path.islink(left) and os.path.islink(right) and
                os.readlink(left) == os.readlink(right))
    except OSError:
        return False


def _same_file(left, right):
    try:
        if os.path.getsize(left) != os.path.getsize(right):
            return False
        return filecmp.cmp(left, right, shallow=False)
    except OSError:
        return False


def change_matches_underlay(root, change, alias_map):
    """Return True when the current branch delta is already present in base.

    A committed path can be edited again after it was copied to the underlay.  In
    that case it must move back to the "new commits" section, so a remembered
    path only counts as previously committed while the current branch state still
    matches the real underlay.
    """
    _rel, delta, base = store_paths(root, change, alias_map)
    if change.op == "D":
        return not os.path.lexists(base)
    if change.kind == "dir":
        return os.path.isdir(base) and not os.path.islink(base)
    if not os.path.lexists(delta) or not os.path.lexists(base):
        return False
    if os.path.islink(delta) or os.path.islink(base):
        return _same_symlink(delta, base)
    if os.path.isdir(delta) or os.path.isdir(base):
        return os.path.isdir(delta) and os.path.isdir(base)
    if os.path.isfile(delta) and os.path.isfile(base):
        return _same_file(delta, base)
    return False


def split_previously_committed_changes(changes, session, roots_by_name, alias_map):
    """Split changes into ``(already_committed, new_changes)`` lists."""
    committed = committed_path_keys(session, alias_map)
    already = []
    new = []
    for change in changes:
        root = roots_by_name.get(change.root)
        if (root is not None and
                committed.intersection(change_path_keys(change, alias_map)) and
                change_matches_underlay(root, change, alias_map)):
            already.append(change)
        else:
            new.append(change)
    return already, new
