"""Operator/hook control surface over branch sessions (ccc-agent).

Hooks are *reporters*: ``finish-turn`` only records lifecycle events, and
``check-before-final`` only reads live status to drive bounded self-repair.
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

from .branchfs import StatusReport
from .policy import (Change, IgnoredChange, PolicyConfig, classify,
                     filter_ignored, split_ignored)
from .runner import finalize_session
from .paths import is_within
from .session import TERMINAL_STATES

# check-before-final outcomes (stable strings for hook adapters and logs)
CHECK_ALLOW = "allow"          # change set clean: finish normally
CHECK_REPAIR = "repair"        # dirty, budget left: agent should revert
CHECK_EXHAUSTED = "exhausted"  # dirty, budget spent: defer to human review

# States whose BranchFS branches should already be closed/discarded. Failed and
# pending-review sessions are deliberately kept for manual recovery/review.
CLEANUP_STATES = ("auto-committed", "committed", "aborted")


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
    return "%s %s (%s, %d bytes)" % (
        change.op, change.path, change.kind, change.bytes)


def _ignored_line(ignored):
    return "%s (ignored by %s)" % (_change_line(ignored.change),
                                    ignored.pattern)


def _ignored_pattern_counts(ignored):
    counts = {}
    for item in ignored:
        counts[item.pattern] = counts.get(item.pattern, 0) + 1
    return counts


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
            for change in changes:
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

    def _stored_review_changes(self, review):
        changes = []
        ignored = []
        saw_status = False
        for name in sorted(os.listdir(review)):
            path = os.path.join(review, name)
            if name.startswith("status.") and name.endswith(".json"):
                saw_status = True
                with open(path) as fh:
                    changes.extend(Change.from_dict(entry)
                                   for entry in json.load(fh))
            elif name.startswith("ignored.") and name.endswith(".json"):
                with open(path) as fh:
                    ignored.extend(IgnoredChange.from_dict(entry)
                                   for entry in json.load(fh))
        return saw_status, changes, ignored

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
            for change in report.changes:
                out.write("%s %s (%s, %d bytes)\n"
                          % (change.op, change.path, change.kind,
                             change.bytes))
        return session

    def diff(self, session_id, path=None, out=None, show_ignored=False):
        """Stored review diff; falls back to live status when absent."""
        out = out or sys.stdout
        session = self._load(session_id)
        if path is not None:
            return self._diff_path(session, path, out,
                                   include_ignored=show_ignored)
        review = self.store.review_dir(session_id)
        if os.path.isdir(review):
            saw_status, changes, ignored = self._stored_review_changes(review)
            if saw_status:
                self._write_change_sections(out, changes, ignored,
                                            show_ignored, session_id)
                return session

        config = PolicyConfig.from_dict(session.policy)
        all_changes = []
        ignored = []
        for name, root in sorted(session.protected_roots.items()):
            out.write("# root %s (branch %s)\n" % (name, root.branch))
            report = self._live_status_report(session, root, action="diff")
            self._write_status_warnings(out, report.warnings)
            changes, root_ignored = split_ignored(report.changes, config,
                                                  self.alias_map)
            all_changes.extend(changes)
            ignored.extend(root_ignored)
        self._write_change_sections(out, all_changes, ignored, show_ignored,
                                    session_id)
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

    def _matching_change(self, session, path, include_ignored=False):
        matches = []
        absolute = path.startswith("/")
        canonical_path = self.alias_map.canonicalize(path) if absolute else None
        for root, change in self._changes(session,
                                          include_ignored=include_ignored):
            rel, _delta, _base = self._store_paths(root, change)
            if absolute:
                if self.alias_map.canonicalize(change.path) == canonical_path:
                    matches.append((root, change))
            elif os.path.normpath(rel) in self._path_candidates(session, root, path):
                matches.append((root, change))
        if not matches:
            raise ControlError("no changed file matching %s" % path)
        if len(matches) > 1:
            raise ControlError("path %s is ambiguous (%d matches)"
                               % (path, len(matches)))
        return matches[0]

    def _read_text_lines(self, path, label):
        if not os.path.isfile(path):
            return []
        with open(path, "rb") as fh:
            data = fh.read()
        if b"\0" in data:
            raise ControlError("cannot diff binary file: %s" % label)
        return data.decode("utf-8", errors="replace").splitlines(True)

    def _diff_path(self, session, path, out, include_ignored=False):
        root, change = self._matching_change(
            session, path, include_ignored=include_ignored)
        if change.kind != "file" and change.op != "D":
            raise ControlError("cannot diff non-file path: %s" % change.path)
        rel, delta, base = self._store_paths(root, change)
        old = self._read_text_lines(base, "base:" + rel)
        new = []
        if change.op != "D":
            if not os.path.isfile(delta):
                raise ControlError("branch delta missing for %s" % change.path)
            new = self._read_text_lines(delta, "branch:" + rel)
        for line in difflib.unified_diff(old, new,
                                         fromfile="a/" + rel,
                                         tofile="b/" + rel):
            out.write(line if line.endswith("\n") else line + "\n")
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
        try:
            for root, change in changes:
                self._apply_change_from_store(root, change)
        except Exception as exc:
            session.add_event("error", "commit failed, branch preserved: %s" % exc)
            self.store.save(session)
            raise ControlError("commit failed, branch preserved: %s" % exc)
        for name, root in sorted(session.protected_roots.items()):
            self._discard_branch(session, name, root, "commit cleanup",
                                 strict=False)
            session.add_event("committed-root", name)
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

    def _apply_change_from_store(self, root, change):
        """Apply one change to base by reading its delta from the store (the
        branch is not mounted post-session)."""
        _rel, delta, base = self._store_paths(root, change)
        if change.op == "D":
            if os.path.islink(base) or os.path.isfile(base):
                os.unlink(base)
            elif os.path.isdir(base):
                shutil.rmtree(base)
        elif change.kind == "dir":
            os.makedirs(base, exist_ok=True)
        elif os.path.exists(delta):
            parent = os.path.dirname(base)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(delta, base)

    def _changes(self, session, include_ignored=False):
        out = []
        config = PolicyConfig.from_dict(session.policy)
        for _name, root in sorted(session.protected_roots.items()):
            changes = self._live_status(session, root, action="review")
            if not include_ignored:
                changes, _ignored = split_ignored(changes, config, self.alias_map)
            for change in changes:
                out.append((root, change))
        return out

    def _emit_patch(self, changes, out):
        """Unified base-vs-view diff the user can prune to a hunk subset, then
        re-apply with `review --apply-patch`."""
        for root, change in changes:
            rel, delta, base = self._store_paths(root, change)
            old = []
            if os.path.isfile(base):
                with open(base, "r", errors="replace") as fh:
                    old = fh.readlines()
            new = []
            if change.op != "D" and os.path.isfile(delta):
                with open(delta, "r", errors="replace") as fh:
                    new = fh.readlines()
            for line in difflib.unified_diff(old, new,
                                             fromfile="a/" + rel,
                                             tofile="b/" + rel):
                out.write(line if line.endswith("\n") else line + "\n")

    def review(self, session_id, accept=False, reject=False, commit_paths=None,
               emit_patch=False, apply_patch=None, out=None,
               show_ignored=False, include_ignored=False):
        """Post-session review of a pending/frozen session's change set.

        Default browses the diff.  ``accept`` commits policy-visible changes by
        default, or all changes when ``include_ignored`` is true; ``reject``
        discards everything, ``commit_paths`` commits a file-level subset (the
        rest are discarded), ``emit_patch`` prints a base-vs-view unified diff,
        and ``apply_patch`` applies a (possibly pruned) patch to base for
        line-level control.
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

        return self.diff(session_id, out=out, show_ignored=show_ignored)

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
        session.transition("running")
        session.add_event("thawed")
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
        self._require_state(session, ("running",), "check-before-final")

        config = PolicyConfig.from_dict(session.policy)
        changes = []
        for _name, root in sorted(session.protected_roots.items()):
            changes.extend(self._live_status(session, root,
                                            action="check-before-final"))
        changes = filter_ignored(changes, config, self.alias_map)
        out_of_scope, deny_matches = classify(changes, config, self.alias_map)

        if not out_of_scope and not deny_matches:
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
            "attempt %d/%d: %d out-of-scope, %d deny match(es)"
            % (session.repair_attempts, config.max_policy_repair_attempts,
               len(out_of_scope), len(deny_matches)))
        self.store.save(session)

        out.write("policy violations; revert these before finishing "
                  "(attempt %d/%d):\n"
                  % (session.repair_attempts,
                     config.max_policy_repair_attempts))
        for path in out_of_scope:
            out.write("  out-of-scope: %s\n" % path)
        for match in deny_matches:
            out.write("  deny-pattern %s: %s\n" % (match.pattern, match.path))
        out.write("undo the listed changes (restore original content, or "
                  "delete files you created), then finish again.\n")
        return CHECK_REPAIR
