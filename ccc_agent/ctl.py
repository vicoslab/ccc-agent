"""Operator/hook control surface over branch sessions (ccc-agent).

Hooks are *reporters*: ``turn-record`` only records lifecycle events, and
``turn-check`` only reads live status to drive bounded self-repair.
Commit authority stays here, in trusted supervisor code, behind explicit
operator commands (or the runner's policy decision).
"""

import calendar
import difflib
import json
import os
import shutil
import subprocess
import sys
import time

from . import artifacts
from .branchfs import StatusReport
from .policy import (Change, IgnoredChange, PolicyConfig, classify,
                     filter_ignored, net_final_changes,
                     net_final_ignored_changes, split_ignored)
from .runner import finalize_session
from .paths import is_within
from .session import TERMINAL_STATES

# turn-check outcomes (stable strings for hook adapters and logs)
CHECK_ALLOW = "allow"          # change set clean: finish normally
CHECK_REPAIR = "repair"        # dirty, budget left: agent should revert
CHECK_EXHAUSTED = "exhausted"  # dirty, budget spent: defer to human review

# States whose BranchFS branches should already be closed/discarded. Failed and
# pending-review sessions are deliberately kept for manual recovery/review.
CLEANUP_STATES = ("auto-committed", "committed", "aborted")
# While a session is live or being finalized, BranchFS status is the source of
# truth.  Review artifacts from a previous freeze may still exist after `thaw`,
# but they are stale until finalization rewrites them.
LIVE_STATUS_STATES = ("created", "mounting", "running", "finalizing")
MAX_TEXT_MERGE_BYTES = 1024 * 1024
# Avoid dumping arbitrary/binary or very large file contents into review output.
# Operators can still see every changed path; hunks are limited to readable text.
MAX_TEXT_DIFF_BYTES = MAX_TEXT_MERGE_BYTES


def _touch_content_key(relpath):
    return relpath.encode("utf-8", "surrogateescape").hex() + ".base"


def _branch_dir(root):
    return os.path.join(root.store, "branches", root.branch)


def _touches_path(root):
    return os.path.join(_branch_dir(root), "touches.json")


def _touch_content_path(root, key):
    return os.path.join(_branch_dir(root), "touch-content", key)


def _load_touch_records(root):
    try:
        with open(_touches_path(root)) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _path_identity(path):
    try:
        st = os.lstat(path)
    except OSError:
        return {"exists": False, "kind": "missing", "bytes": 0,
                "mtime_ns": None}
    if os.path.isdir(path) and not os.path.islink(path):
        kind = "dir"
        size = 0
    elif os.path.islink(path):
        kind = "symlink"
        size = 0
    elif os.path.isfile(path):
        kind = "file"
        size = st.st_size
    else:
        kind = "other"
        size = 0
    return {"exists": True, "kind": kind, "bytes": size,
            "mtime_ns": getattr(st, "st_mtime_ns", None)}


def _read_bounded_text(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or st.st_size > MAX_TEXT_MERGE_BYTES:
        return None
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\0" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return data


def _changed_range(base, changed):
    prefix = 0
    while prefix < len(base) and prefix < len(changed) and base[prefix] == changed[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < len(base) - prefix and suffix < len(changed) - prefix and
           base[len(base) - 1 - suffix] == changed[len(changed) - 1 - suffix]):
        suffix += 1
    base_end = len(base) - suffix
    changed_end = len(changed) - suffix
    return prefix, base_end, changed[prefix:changed_end]


def _try_three_way_text_merge(base, current, session):
    if current == session:
        return session
    if base == current:
        return session
    if base == session:
        return current
    try:
        base_lines = base.decode("utf-8").splitlines(True)
        current_lines = current.decode("utf-8").splitlines(True)
        session_lines = session.decode("utf-8").splitlines(True)
    except UnicodeDecodeError:
        return None
    cur_start, cur_end, cur_repl = _changed_range(base_lines, current_lines)
    ses_start, ses_end, ses_repl = _changed_range(base_lines, session_lines)
    if not (cur_end <= ses_start or ses_end <= cur_start):
        return None
    if cur_start <= ses_start:
        merged = (base_lines[:cur_start] + cur_repl +
                  base_lines[cur_end:ses_start] + ses_repl +
                  base_lines[ses_end:])
    else:
        merged = (base_lines[:ses_start] + ses_repl +
                  base_lines[ses_end:cur_start] + cur_repl +
                  base_lines[cur_end:])
    return "".join(merged).encode("utf-8")


def _utc_seconds(stamp):
    if not stamp:
        return None
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


class ControlError(Exception):
    pass


def _change_line(change):
    summary = getattr(change, "summary", "")
    suffix = "; " + summary if summary else ""
    return "%s %s (%s, %d bytes%s)" % (
        change.op, change.path, change.kind, change.bytes, suffix)


def _ignored_line(ignored):
    return "%s (ignored by %s)" % (_change_line(ignored.change),
                                    ignored.pattern)


def _ignored_pattern_counts(ignored):
    counts = {}
    for item in ignored:
        counts[item.pattern] = counts.get(item.pattern, 0) + 1
    return counts


def _is_descendant_path(child, parent):
    prefix = parent.rstrip("/") + "/"
    return child != parent and child.startswith(prefix)


def _nested_delete_summary(count):
    if count <= 0:
        return ""
    noun = "deletion" if count == 1 else "deletions"
    return "%d nested %s hidden" % (count, noun)


def _normalized_change_view(changes):
    """Display-only effective view of a raw BranchFS change list.

    The underlying branch can temporarily contain both a tombstone and a delta
    for the same path (delete followed by rewrite).  BranchFS resolves the delta
    first, so the review listing should show the final file change, not both.
    Recursive tree deletes are also collapsed to the ancestor tombstone with a
    child count to keep large `rm -rf` diffs readable.
    """
    normalized = [Change(c.op, c.path, c.kind, c.bytes, c.root,
                         summary=getattr(c, "summary", ""))
                  for c in net_final_changes(changes)]
    visible_delete_paths = {}
    for change in normalized:
        if change.op == "D":
            visible_delete_paths.setdefault(change.root, set()).add(
                os.path.normpath(change.path))

    def outermost_visible_delete_ancestor(change):
        visible = visible_delete_paths.get(change.root, set())
        current = os.path.normpath(change.path).rstrip(os.sep)
        ancestor = None
        parent = os.path.dirname(current)
        while parent and parent != current:
            if parent in visible:
                ancestor = parent
            if parent == os.sep:
                break
            current = parent
            parent = os.path.dirname(current)
        return ancestor

    collapsed = set()
    nested_counts = {(change.root, os.path.normpath(change.path)): 0
                     for change in normalized if change.op == "D"}
    for change in normalized:
        if change.op != "D":
            continue
        ancestor = outermost_visible_delete_ancestor(change)
        if ancestor is not None:
            key = (change.root, os.path.normpath(change.path))
            collapsed.add(key)
            nested_counts[(change.root, ancestor)] += 1

    out = []
    for change in normalized:
        if change.op == "D":
            key = (change.root, os.path.normpath(change.path))
            if key in collapsed:
                continue
            summary = _nested_delete_summary(nested_counts.get(key, 0))
            if summary and not change.summary:
                change.summary = summary
        out.append(change)
    return out


class Controller(object):
    def __init__(self, store, backend, alias_map):
        self.store = store
        self.backend = backend
        self.alias_map = alias_map

    # -- helpers -----------------------------------------------------------
    def _load(self, session_id):
        try:
            return self.store.load(session_id)
        except KeyError:
            raise ControlError("no such session: %s" % session_id)

    def _require_state(self, session, allowed, action):
        if session.state not in allowed:
            raise ControlError(
                "cannot %s session %s in state %s (needs one of: %s)"
                % (action, session.session_id, session.state,
                   ", ".join(allowed)))

    def _live_status_report(self, session, root, action="status"):
        try:
            if hasattr(self.backend, "status_report"):
                return self.backend.status_report(root)
            return StatusReport(changes=self.backend.status(root), warnings=[])
        except Exception as exc:
            hint = ""
            if session.state in ("mounting", "running", "finalizing"):
                hint = (
                    "; session is marked %s. If the node rebooted or the "
                    "agent process is gone, use `ccc-agent resume %s` to "
                    "re-mount and continue, or `ccc-agent finish %s` to "
                    "finalize after verifying no old process is still running"
                    % (session.state, session.session_id, session.session_id))
            raise ControlError(
                "could not read live BranchFS status for session %s root %s "
                "while running %s: %s%s"
                % (session.session_id, root.name, action, exc, hint))

    def _live_status(self, session, root, action="status"):
        return self._live_status_report(session, root, action).changes

    def _write_status_warnings(self, out, warnings):
        for warning in warnings:
            out.write("WARNING %s: %s\n" % (warning.path, warning.message))

    def _write_change_sections(self, out, changes, ignored, show_ignored,
                               session_id):
        """Render a git-status-like split of commit vs ignored changes."""
        if not changes and not ignored:
            return False
        out.write("Changes to be committed:\n")
        if changes:
            for change in _normalized_change_view(changes):
                out.write("  %s\n" % _change_line(change))
        else:
            out.write("  (none)\n")
        if ignored:
            out.write("\nIgnored by policy (not committed):\n")
            if show_ignored:
                for item in ignored:
                    out.write("  %s\n" % _ignored_line(item))
            else:
                total = len(ignored)
                out.write("  %d change(s) hidden; use `ccc-agent diff %s "
                          "--show-ignored` or `ccc-agent review %s "
                          "--show-ignored` to list them.\n"
                          % (total, session_id, session_id))
                for pattern, count in sorted(
                        _ignored_pattern_counts(ignored).items()):
                    out.write("  %s: %d change(s)\n" % (pattern, count))
            out.write("  To accept ignored changes too: `ccc-agent review %s "
                      "--accept --include-ignored`.\n" % session_id)
        return True

    def _can_use_stored_review(self, session):
        return session.state not in LIVE_STATUS_STATES

    def _stored_change_has_underlying_delete_target(self, session, change):
        if change.op != "D":
            return True
        root = session.protected_roots.get(change.root)
        if root is None:
            return True
        visible = self.alias_map.canonicalize(root.visible)
        path = self.alias_map.canonicalize(change.path)
        if not is_within(path, visible):
            return True
        rel = os.path.relpath(path, visible)
        return os.path.lexists(os.path.join(root.base, rel))

    def _stored_review_changes(self, session, review):
        changes = []
        ignored = []
        saw_status = False
        for name in sorted(os.listdir(review)):
            path = os.path.join(review, name)
            if name.startswith("status.") and name.endswith(".json"):
                saw_status = True
                with open(path) as fh:
                    changes.extend(
                        change for change in (
                            Change.from_dict(entry) for entry in json.load(fh))
                        if self._stored_change_has_underlying_delete_target(
                            session, change))
            elif name.startswith("ignored.") and name.endswith(".json"):
                with open(path) as fh:
                    ignored.extend(
                        item for item in (
                            IgnoredChange.from_dict(entry) for entry in json.load(fh))
                        if self._stored_change_has_underlying_delete_target(
                            session, item.change))
        return (saw_status,
                net_final_changes(changes, self.alias_map),
                net_final_ignored_changes(ignored, self.alias_map))

    def _mount_still_active(self, root):
        try:
            return os.path.ismount(root.mount)
        except OSError:
            return False

    def _discard_branch(self, session, name, root, action, strict=True):
        """Unmount a root, then discard its branch delta.

        BranchFS can leave NFS `.nfs*` files and half-aborted branch metadata if
        `abort-branch` is called while the FUSE view is still mounted.  Always
        quiesce the mount first.  For operator `abort`, failures are strict and
        keep the session non-terminal; for post-commit cleanup, the base has
        already been updated, so cleanup failures are recorded as warnings.
        """
        try:
            self.backend.unmount(root)
            session.add_event("unmounted-root", name)
        except Exception as exc:
            detail = "%s could not unmount root %s at %s: %s" % (
                action, name, root.mount, exc)
            if self._mount_still_active(root):
                session.add_event("error", detail)
                self.store.save(session)
                if strict:
                    raise ControlError(detail)
                return False
            session.add_event("unmount-skipped", detail)
        try:
            self.backend.abort(root)
        except Exception as exc:
            detail = "%s could not discard branch %s for root %s: %s" % (
                action, root.branch, name, exc)
            session.add_event("error" if strict else "discard-warning", detail)
            self.store.save(session)
            if strict:
                raise ControlError(detail)
            return False
        return True

    # -- read-only ----------------------------------------------------------
    def list(self, session_prefix=None, out=None):
        if out is None and hasattr(session_prefix, "write"):
            out = session_prefix
            session_prefix = None
        out = out or sys.stdout
        sessions = self.store.list()
        if session_prefix:
            sessions = [session for session in sessions
                        if session.session_id.startswith(session_prefix)]
        out.write("%-42s %-16s %-14s %s\n"
                  % ("SESSION", "STATE", "AGENT", "CREATED"))
        for session in sessions:
            out.write("%-42s %-16s %-14s %s\n"
                      % (session.session_id, session.state,
                         session.agent_kind, session.created_at))
        return sessions

    def cleanup(self, older_than_days=30, dry_run=False, out=None, now=None):
        """Remove old closed session bundles from the session state dir."""
        out = out or sys.stdout
        try:
            older_than_days = int(older_than_days)
        except (TypeError, ValueError):
            raise ControlError("cleanup --older-than must be a non-negative day count")
        if older_than_days < 0:
            raise ControlError("cleanup --older-than must be a non-negative day count")

        cutoff = (time.time() if now is None else now) - older_than_days * 86400
        matched = []
        skipped = []
        verb = "would remove" if dry_run else "removed"
        for session in self.store.list():
            if session.state not in CLEANUP_STATES:
                continue
            stamp = session.finished_at or session.created_at
            seconds = _utc_seconds(stamp)
            if seconds is None or seconds > cutoff:
                continue
            active_mounts = [root.mount for root in session.protected_roots.values()
                             if self._mount_still_active(root)]
            if active_mounts:
                skipped.append(session.session_id)
                out.write("%s: skipped (active mount: %s)\n"
                          % (session.session_id, ", ".join(active_mounts)))
                continue
            if not dry_run:
                try:
                    self.store.remove(session.session_id)
                except (OSError, ValueError) as exc:
                    raise ControlError("could not remove session %s: %s"
                                       % (session.session_id, exc))
            matched.append(session.session_id)
            out.write("%s: %s\n" % (session.session_id, verb))
        out.write("%s %d old session(s)" % (verb, len(matched)))
        if skipped:
            out.write("; skipped %d active session(s)" % len(skipped))
        out.write("\n")
        return matched

    def show(self, session_id, out=None):
        out = out or sys.stdout
        session = self._load(session_id)
        json.dump(session.to_dict(), out, indent=2, sort_keys=True)
        out.write("\n")
        return session

    def status(self, session_id, out=None):
        """Live BranchFS status for each protected root."""
        out = out or sys.stdout
        session = self._load(session_id)
        for name, root in sorted(session.protected_roots.items()):
            out.write("# root %s (branch %s)\n" % (name, root.branch))
            report = self._live_status_report(session, root, action="status")
            self._write_status_warnings(out, report.warnings)
            for change in _normalized_change_view(report.changes):
                out.write("%s\n" % _change_line(change))
        return session

    def diff(self, session_id, path=None, out=None, show_ignored=False,
             show_file_diffs=False):
        """Show changed paths; optionally append unified diffs for text files.

        Cached review artifacts are sufficient for the path summary, but file
        hunks need the preserved branch delta, so ``show_file_diffs`` uses the
        live backend path.
        """
        out = out or sys.stdout
        session = self._load(session_id)
        if path is not None:
            return self._diff_path(session, path, out,
                                   include_ignored=show_ignored)
        review = self.store.review_dir(session_id)
        if (not show_file_diffs and self._can_use_stored_review(session)
                and os.path.isdir(review)):
            saw_status, changes, ignored = self._stored_review_changes(
                session, review)
            if saw_status:
                self._write_change_sections(out, changes, ignored,
                                            show_ignored, session_id)
                return session

        config = PolicyConfig.from_dict(session.policy)
        all_changes = []
        ignored = []
        file_diff_changes = []
        for name, root in sorted(session.protected_roots.items()):
            out.write("# root %s (branch %s)\n" % (name, root.branch))
            report = self._live_status_report(session, root, action="diff")
            self._write_status_warnings(out, report.warnings)
            changes, root_ignored = split_ignored(report.changes, config,
                                                  self.alias_map)
            all_changes.extend(changes)
            ignored.extend(root_ignored)
            file_diff_changes.extend((root, change) for change in changes)
            if show_ignored:
                file_diff_changes.extend((root, item.change)
                                         for item in root_ignored)
        self._write_change_sections(out, all_changes, ignored, show_ignored,
                                    session_id)
        if show_file_diffs:
            self._write_file_diffs(file_diff_changes, out)
        return session

    def _path_candidates(self, session, root, path):
        """Root-relative candidate relpaths for a user-supplied diff path."""
        visible = self.alias_map.canonicalize(root.visible)
        candidates = set()
        if path.startswith("/"):
            canonical = self.alias_map.canonicalize(path)
            if is_within(canonical, visible):
                candidates.add(os.path.relpath(canonical, visible))
            return candidates

        rel = os.path.normpath(path)
        if rel not in ("", ".") and not rel.startswith("../"):
            candidates.add(rel)

        workspace_path = os.path.normpath(os.path.join(session.workspace, path))
        workspace_path = self.alias_map.canonicalize(workspace_path)
        if is_within(workspace_path, visible):
            candidates.add(os.path.relpath(workspace_path, visible))
        return candidates

    def _root_qualified_path(self, session, path):
        """Split an optional ``<root-name>:<path>`` selector.

        Ambiguous diffs can involve distinct protected roots that expose the
        same visible path.  A root-qualified path gives operators a stable way
        to select one without changing ordinary POSIX path handling; it is only
        recognized when the prefix is the name of a protected root.
        """
        prefix, sep, rest = path.partition(":")
        if sep and rest and prefix in session.protected_roots:
            return prefix, rest
        return None, path

    def _existing_file_identity(self, path):
        try:
            stat_result = os.stat(path)
        except OSError:
            return None
        return ("inode", stat_result.st_dev, stat_result.st_ino)

    def _match_file_identity(self, root, change):
        """Best-effort identity for the underlying file a match would diff.

        Prefer real filesystem identity (following symlinks) so alias/symlink
        spellings of the same branch delta collapse to one match.  Fall back to
        the computed store/base paths when the file is missing, which still
        collapses duplicate status entries for the same relpath without merging
        distinct roots or distinct files.
        """
        _rel, delta, base = self._store_paths(root, change)
        paths = []
        if change.op != "D":
            paths.append(delta)
        paths.append(base)
        for candidate in paths:
            identity = self._existing_file_identity(candidate)
            if identity is not None:
                return identity
        return ("paths", os.path.realpath(delta), os.path.realpath(base))

    def _all_matches_same_file(self, matches):
        identities = {self._match_file_identity(root, change)
                      for root, change in matches}
        return len(identities) == 1

    def _match_store_key(self, root, change):
        rel, _delta, _base = self._store_paths(root, change)
        return (root.name, os.path.normpath(rel))

    def _all_matches_same_store_path(self, matches):
        return len({self._match_store_key(root, change)
                    for root, change in matches}) == 1

    def _exact_visible_matches(self, path, matches):
        normalized = os.path.normpath(path)
        return [(root, change) for root, change in matches
                if os.path.normpath(change.path) == normalized]

    def _ambiguous_match_message(self, path, matches):
        lines = ["path %s is ambiguous (%d matches); choose one of:"
                 % (path, len(matches))]
        for root, change in matches:
            rel, _delta, _base = self._store_paths(root, change)
            lines.append("  - %s:%s (root %s, rel %s)"
                         % (root.name, change.path, root.name, rel))
        return "\n".join(lines)

    def _preferred_diff_match(self, matches):
        for match in matches:
            _root, change = match
            if change.op != "D" and change.kind == "file":
                return match
        for match in matches:
            _root, change = match
            if change.op == "D":
                return match
        return matches[0]

    def _matching_change(self, session, path, include_ignored=False):
        matches = []
        root_filter, match_path = self._root_qualified_path(session, path)
        absolute = match_path.startswith("/")
        canonical_path = (self.alias_map.canonicalize(match_path)
                          if absolute else None)
        for root, change in self._changes(
                session, include_ignored=include_ignored):
            if root_filter and root.name != root_filter:
                continue
            rel, _delta, _base = self._store_paths(root, change)
            if absolute:
                if self.alias_map.canonicalize(change.path) == canonical_path:
                    matches.append((root, change))
            elif os.path.normpath(rel) in self._path_candidates(
                    session, root, match_path):
                matches.append((root, change))
        if not matches:
            raise ControlError("no changed file matching %s" % path)
        if len(matches) > 1:
            if (self._all_matches_same_store_path(matches) or
                    self._all_matches_same_file(matches)):
                return self._preferred_diff_match(matches)
            if absolute:
                exact = self._exact_visible_matches(match_path, matches)
                if len(exact) == 1:
                    return exact[0]
                if exact and (self._all_matches_same_store_path(exact) or
                              self._all_matches_same_file(exact)):
                    return self._preferred_diff_match(exact)
            raise ControlError(self._ambiguous_match_message(path, matches))
        return matches[0]

    def _text_lines_for_diff(self, path):
        """Return (lines, skip_reason) for a regular UTF-8 text file diff."""
        if not os.path.exists(path):
            return [], None
        if not os.path.isfile(path):
            return None, "non-file"
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return None, "unreadable (%s)" % exc
        if size > MAX_TEXT_DIFF_BYTES:
            return None, "too large"
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return None, "unreadable (%s)" % exc
        if b"\0" in data:
            return None, "binary/non-text"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, "binary/non-text"
        return text.splitlines(True), None

    def _diff_lines_for_change(self, root, change):
        rel, delta, base = self._store_paths(root, change)
        if change.op == "D":
            if not os.path.isfile(base):
                return None, "non-file"
        elif change.kind != "file":
            return None, "non-file"

        old, skip_reason = self._text_lines_for_diff(base)
        if skip_reason:
            return None, skip_reason
        assert old is not None
        new = []
        if change.op != "D":
            if not os.path.isfile(delta):
                return None, "branch delta missing"
            new, skip_reason = self._text_lines_for_diff(delta)
            if skip_reason:
                return None, skip_reason
            assert new is not None
        return list(difflib.unified_diff(old, new,
                                         fromfile="a/" + rel,
                                         tofile="b/" + rel)), None

    def _write_diff_lines(self, out, lines):
        for line in lines:
            out.write(line if line.endswith("\n") else line + "\n")

    def _write_file_diffs(self, changes, out):
        wrote_header = False
        skipped = []
        for root, change in changes:
            lines, skip_reason = self._diff_lines_for_change(root, change)
            if skip_reason:
                skipped.append((change.path, skip_reason))
                continue
            if not lines:
                continue
            if not wrote_header:
                out.write("\nText file diffs:\n")
                wrote_header = True
            self._write_diff_lines(out, lines)
        if skipped:
            if not wrote_header:
                out.write("\nText file diffs:\n")
                wrote_header = True
            for path, reason in skipped:
                out.write("  skipped %s: %s\n" % (reason, path))
        if wrote_header:
            out.write("\n")

    def _diff_path(self, session, path, out, include_ignored=False):
        root, change = self._matching_change(
            session, path, include_ignored=include_ignored)
        lines, skip_reason = self._diff_lines_for_change(root, change)
        if skip_reason:
            raise ControlError("cannot diff %s: %s" % (skip_reason,
                                                       change.path))
        self._write_diff_lines(out, lines)
        return session

    # -- mutating ------------------------------------------------------------
    def commit(self, session_id, include_ignored=False):
        session = self._load(session_id)
        self._require_state(session, ("pending-review", "frozen"), "commit")
        # Commit only the policy-visible changes by default. Ignored launcher/
        # runtime noise (plugin mountpoints, agent state, caches/history, .nfs
        # files) stays in the branch and is discarded unless an operator
        # explicitly opts in with include_ignored.
        changes = self._changes(session, include_ignored=include_ignored)
        commit_reports = []
        try:
            for root, change in changes:
                report = self._apply_change_from_store(root, change)
                if report:
                    commit_reports.append(report)
        except Exception as exc:
            session.add_event("error", "commit failed, branch preserved: %s" % exc)
            self.store.save(session)
            raise ControlError("commit failed, branch preserved: %s" % exc)
        self._write_commit_conflict_report(session, commit_reports)
        for name, root in sorted(session.protected_roots.items()):
            self._discard_branch(session, name, root, "commit cleanup",
                                 strict=False)
            session.add_event("committed-root", name)
        conflicts = [r for r in commit_reports if r.get("kind") == "conflict"]
        auto_merges = [r for r in commit_reports if r.get("kind") == "auto_merge"]
        if conflicts or auto_merges:
            session.add_event(
                "commit-conflict-report",
                "%d auto-merge(s), %d conflict(s); latest session won conflicts"
                % (len(auto_merges), len(conflicts)))
        session.transition("committed")
        self.store.save(session)
        return session

    def abort(self, session_id):
        session = self._load(session_id)
        if session.state in TERMINAL_STATES:
            raise ControlError("session %s already %s"
                               % (session_id, session.state))
        for name, root in sorted(session.protected_roots.items()):
            self._discard_branch(session, name, root, "abort", strict=True)
            session.add_event("aborted-root", name)
        session.transition("aborted")
        self.store.save(session)
        return session

    # -- selective / line-level review (post-session, branch unmounted) -----
    def _store_paths(self, root, change):
        """(rel, delta-file-in-store, base-file) for a change."""
        visible = self.alias_map.canonicalize(root.visible)
        rel = os.path.relpath(self.alias_map.canonicalize(change.path), visible)
        delta = os.path.join(root.store, "branches", root.branch, "files", rel)
        return rel, delta, os.path.join(root.base, rel)

    def _touch_record_for_rel(self, root, rel):
        touches = _load_touch_records(root)
        return touches.get("/" + rel) or touches.get(rel)

    def _commit_report_for_change(self, root, change, rel, delta, base):
        record = self._touch_record_for_rel(root, rel)
        if not record:
            return None, None
        base_at_first = record.get("base_at_first_touch") or {}
        current = _path_identity(base)
        if base_at_first == current:
            return None, None

        action = "delete" if change.op == "D" else (
            "modify" if base_at_first.get("exists") else "create")
        common = {
            "root": root.name,
            "path": change.path,
            "relpath": "/" + rel,
            "session_action": action,
            "base_at_first_touch": base_at_first,
            "base_at_commit": current,
        }

        if (change.op != "D" and base_at_first.get("kind") == "file" and
                current.get("kind") == "file"):
            key = record.get("base_content_key") or _touch_content_key("/" + rel)
            try:
                with open(_touch_content_path(root, key), "rb") as fh:
                    base_bytes = fh.read()
            except OSError:
                base_bytes = None
            current_bytes = _read_bounded_text(base)
            session_bytes = _read_bounded_text(delta)
            if (base_bytes is not None and current_bytes is not None and
                    session_bytes is not None):
                merged = _try_three_way_text_merge(base_bytes, current_bytes,
                                                   session_bytes)
                if merged is not None:
                    report = dict(common)
                    report.update({"kind": "auto_merge",
                                   "resolution": "clean_text_merge"})
                    return report, merged

        report = dict(common)
        report.update({"kind": "conflict",
                       "resolution": "session_won",
                       "reason": "parent_changed_after_first_touch"})
        return report, None

    def _write_commit_conflict_report(self, session, reports):
        if not reports:
            return
        review = self.store.review_dir(session.session_id)
        os.makedirs(review, exist_ok=True)
        path = os.path.join(review, "commit-conflicts.json")
        data = {
            "auto_merges": [r for r in reports if r.get("kind") == "auto_merge"],
            "conflicts": [r for r in reports if r.get("kind") == "conflict"],
        }
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")

    def _apply_change_from_store(self, root, change):
        """Apply one change to base by reading its delta from the store (the
        branch is not mounted post-session)."""
        rel, delta, base = self._store_paths(root, change)
        report, merged = self._commit_report_for_change(root, change, rel, delta, base)
        if merged is not None:
            parent = os.path.dirname(base)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.islink(base):
                os.unlink(base)
            elif os.path.isdir(base):
                shutil.rmtree(base)
            with open(base, "wb") as fh:
                fh.write(merged)
            return report
        if change.op == "D":
            if os.path.islink(base) or os.path.isfile(base):
                os.unlink(base)
            elif os.path.isdir(base):
                shutil.rmtree(base)
        elif change.kind == "dir":
            os.makedirs(base, exist_ok=True)
        elif os.path.lexists(delta):
            parent = os.path.dirname(base)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # A normalized file/symlink delta may replace a base directory or
            # symlink after its same-path tombstone was hidden from review.  Make
            # the final path match the delta instead of copying through/into the
            # old object.
            if os.path.islink(base):
                os.unlink(base)
            elif os.path.isdir(base):
                shutil.rmtree(base)
            shutil.copy2(delta, base, follow_symlinks=False)
        return report

    def _changes(self, session, include_ignored=False):
        out = []
        config = PolicyConfig.from_dict(session.policy)
        for _name, root in sorted(session.protected_roots.items()):
            changes = self._live_status(session, root, action="review")
            if include_ignored:
                changes = net_final_changes(changes, self.alias_map)
            else:
                changes, _ignored = split_ignored(changes, config, self.alias_map)
            for change in changes:
                out.append((root, change))
        return out

    def _emit_patch(self, changes, out):
        """Unified base-vs-view text diff the user can prune to a hunk subset.

        Binary, non-text, non-file, or oversized paths are intentionally skipped:
        line-level patches only make sense for readable text files.
        """
        for root, change in changes:
            lines, skip_reason = self._diff_lines_for_change(root, change)
            if skip_reason:
                continue
            self._write_diff_lines(out, lines)

    def review(self, session_id, accept=False, reject=False, commit_paths=None,
               emit_patch=False, apply_patch=None, out=None,
               show_ignored=False, include_ignored=False,
               show_file_diffs=False):
        """Post-session review of a pending/frozen session's change set.

        Default lists changed paths only. ``show_file_diffs`` additionally shows
        unified hunks for readable text files. ``accept`` commits policy-visible
        changes by default, or all changes when ``include_ignored`` is true;
        ``reject`` discards everything, ``commit_paths`` commits a file-level
        subset (the rest are discarded), ``emit_patch`` prints a base-vs-view
        unified text diff, and ``apply_patch`` applies a (possibly pruned) patch
        to base for line-level control.
        """
        out = out or sys.stdout
        if accept:
            return self.commit(session_id, include_ignored=include_ignored)
        if reject:
            return self.abort(session_id)
        session = self._load(session_id)
        self._require_state(session, ("pending-review", "frozen"), "review")
        changes = self._changes(session, include_ignored=include_ignored)

        if emit_patch:
            self._emit_patch(changes, out)
            return session

        if apply_patch:
            # patch the base directly (one base per root; the primary root is
            # the common case), then discard the now-stale branch deltas.
            base = sorted(session.protected_roots.values(),
                          key=lambda r: r.name)[0].base
            with open(apply_patch) as fh:
                proc = subprocess.run(["patch", "-p1", "-d", base],
                                      stdin=fh, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True)
            out.write(proc.stdout)
            if proc.returncode != 0:
                raise ControlError("patch failed (rc=%d)" % proc.returncode)
            return self._finish_selective(session, "patch applied")

        if commit_paths:
            chosen = set(commit_paths)
            applied = []
            for root, change in changes:
                if change.path in chosen:
                    self._apply_change_from_store(root, change)
                    applied.append(change.path)
            out.write("committed %d path(s); discarding the rest\n"
                      % len(applied))
            return self._finish_selective(session, "file-level commit")

        return self.diff(session_id, out=out, show_ignored=show_ignored,
                         show_file_diffs=show_file_diffs)

    def _finish_selective(self, session, detail):
        """After a selective/patch apply to base, discard the branch deltas and
        mark the session committed."""
        for name, root in sorted(session.protected_roots.items()):
            self._discard_branch(session, name, root, "selective cleanup",
                                 strict=False)
            session.add_event("selective-commit", "%s: %s" % (name, detail))
        session.transition("committed")
        self.store.save(session)
        return session

    def thaw(self, session_id):
        """Intentionally reopen a pending-review session for more work."""
        session = self._load(session_id)
        self._require_state(session, ("pending-review",), "thaw")
        for root in session.protected_roots.values():
            self.backend.thaw(root)
        artifacts.clear_review_cache(self.store.review_dir(session.session_id))
        session.transition("running")
        session.add_event("thawed")
        session.add_event("review-cache-cleared")
        self.store.save(session)
        return session

    def finish(self, session_id):
        """Finalize a long-running/manual session now (freeze+policy)."""
        session = self._load(session_id)
        self._require_state(session, ("running",), "finish")
        session.transition("finalizing")
        self.store.save(session)
        finalize_session(session, self.store, self.backend, self.alias_map)
        return self.store.load(session_id)

    def finish_turn(self, session_id):
        """Hook entrypoint: record turn completion; never commits."""
        session = self._load(session_id)
        session.add_event("turn-finished")
        self.store.save(session)
        return session

    def check_before_final(self, session_id, out=None):
        """Hook entrypoint (blocking Stop hooks): bounded self-repair check.

        Classifies *live* branch status against the session policy — no
        freeze, commit, or abort.  A dirty change set consumes one unit of
        the per-session repair budget and the offending paths are printed
        for the agent to revert; once the budget is spent the check stands
        aside and finalize parks the session for human review.  Policy
        *mode* (manual/read-only-review/...) is applied at finalize, not
        here: this check is only about scope and deny/hide hygiene.
        """
        out = out or sys.stdout
        session = self._load(session_id)
        self._require_state(session, ("running",), "turn-check")

        config = PolicyConfig.from_dict(session.policy)
        changes = []
        potential_conflicts = []
        for _name, root in sorted(session.protected_roots.items()):
            root_changes = self._live_status(session, root,
                                             action="turn-check")
            changes.extend(root_changes)
            for change in root_changes:
                rel, delta, base = self._store_paths(root, change)
                report, _merged = self._commit_report_for_change(root, change,
                                                                  rel, delta,
                                                                  base)
                if report and report.get("kind") == "conflict":
                    potential_conflicts.append(report)
        changes = filter_ignored(changes, config, self.alias_map)
        allowed_paths = set(change.path for change in changes)
        potential_conflicts = [report for report in potential_conflicts
                               if report.get("path") in allowed_paths]
        out_of_scope, deny_matches = classify(changes, config, self.alias_map)

        if not out_of_scope and not deny_matches and not potential_conflicts:
            session.add_event("check-clean",
                              "%d change(s), all in scope" % len(changes))
            self.store.save(session)
            out.write("clean: %d change(s), all within policy\n"
                      % len(changes))
            return CHECK_ALLOW

        if session.repair_attempts >= config.max_policy_repair_attempts:
            session.add_event(
                "repair-budget-exhausted",
                "%d/%d attempts used; deferring to review at finalize"
                % (session.repair_attempts,
                   config.max_policy_repair_attempts))
            self.store.save(session)
            out.write(
                "repair budget exhausted (%d attempt(s)); changes will be "
                "frozen for human review at finalize\n"
                % session.repair_attempts)
            return CHECK_EXHAUSTED

        session.repair_attempts += 1
        session.add_event(
            "repair-requested",
            "attempt %d/%d: %d out-of-scope, %d deny match(es), %d conflict(s)"
            % (session.repair_attempts, config.max_policy_repair_attempts,
               len(out_of_scope), len(deny_matches), len(potential_conflicts)))
        self.store.save(session)

        out.write("policy/conflict issues; fix these before finishing "
                  "(attempt %d/%d):\n"
                  % (session.repair_attempts,
                     config.max_policy_repair_attempts))
        for path in out_of_scope:
            out.write("  out-of-scope: %s\n" % path)
        for match in deny_matches:
            out.write("  deny-pattern %s: %s\n" % (match.pattern, match.path))
        for conflict in potential_conflicts:
            out.write("  potential-conflict: %s (%s; latest session would win unless reconciled)\n"
                      % (conflict.get("path"), conflict.get("session_action")))
        out.write("undo, revert, or reconcile the listed changes, then finish again.\n")
        return CHECK_REPAIR
