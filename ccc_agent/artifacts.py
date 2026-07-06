"""Durable review artifacts for completed/frozen sessions.

Everything a human needs to decide commit-vs-abort lands under
``<state>/<session-id>/reviews/`` so sessions can be inspected long after the
agent (and even the node it ran on) is gone.
"""

import json
import os
from collections import Counter

from .commit_failures import COMMIT_PERMISSION_DENIED_KEY


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _is_review_cache_file(name):
    if name in ("session.json", "policy-decision.json", "summary.md",
                "commit-permission-denied.json"):
        return True
    return ((name.startswith("status.") or name.startswith("ignored.") or
             name.startswith("warnings.")) and name.endswith(".json"))


def clear_review_cache(review):
    """Remove generated review/cache artifacts, preserving operator files."""
    if not os.path.isdir(review):
        return
    for name in os.listdir(review):
        if not _is_review_cache_file(name):
            continue
        path = os.path.join(review, name)
        if os.path.isfile(path) or os.path.islink(path):
            os.unlink(path)


def write_review(store, session, changes_by_root, decision,
                 warnings_by_root=None, ignored_by_root=None,
                 previously_committed_by_root=None):
    """Write session, status, ignored, warning, and decision artifacts."""
    warnings_by_root = warnings_by_root or {}
    ignored_by_root = ignored_by_root or {}
    previously_committed_by_root = previously_committed_by_root or {}
    review = store.review_dir(session.session_id)
    os.makedirs(review, exist_ok=True)
    clear_review_cache(review)

    _write_json(os.path.join(review, "session.json"), session.to_dict())
    for root_name, changes in changes_by_root.items():
        _write_json(os.path.join(review, "status.%s.json" % root_name),
                    [c.to_dict() for c in changes])
    for root_name in changes_by_root:
        ignored = ignored_by_root.get(root_name, [])
        _write_json(os.path.join(review, "ignored.%s.json" % root_name),
                    [c.to_dict() for c in ignored])
    for root_name, warnings in warnings_by_root.items():
        _write_json(os.path.join(review, "warnings.%s.json" % root_name),
                    [w.to_dict() for w in warnings])
    failures = session.policy.get(COMMIT_PERMISSION_DENIED_KEY) or []
    if failures:
        _write_json(os.path.join(review, "commit-permission-denied.json"),
                    failures)
    _write_json(os.path.join(review, "policy-decision.json"),
                decision.to_dict())

    with open(os.path.join(review, "summary.md"), "w") as fh:
        fh.write(render_summary(session, changes_by_root, decision,
                                warnings_by_root=warnings_by_root,
                                ignored_by_root=ignored_by_root,
                                previously_committed_by_root=(
                                    previously_committed_by_root)))
    return review


def _ignored_pattern_counts(ignored_by_root):
    counts = Counter()
    for ignored in ignored_by_root.values():
        for item in ignored:
            counts[item.pattern] += 1
    return counts


def _change_key(change):
    return (change.root, change.op, change.path, change.kind, change.bytes,
            getattr(change, "summary", ""))


def _render_change_item(out, change):
    summary = getattr(change, "summary", "")
    suffix = "; %s" % summary if summary else ""
    out("- `%s` `%s` (%s, %d bytes%s)"
        % (change.op, change.path, change.kind, change.bytes, suffix))


def render_summary(session, changes_by_root, decision, warnings_by_root=None,
                   ignored_by_root=None, previously_committed_by_root=None):
    warnings_by_root = warnings_by_root or {}
    ignored_by_root = ignored_by_root or {}
    previously_committed_by_root = previously_committed_by_root or {}
    lines = []
    out = lines.append
    out("# Agent session %s" % session.session_id)
    out("")
    out("| field | value |")
    out("|---|---|")
    out("| agent | %s |" % session.agent_kind)
    out("| command | `%s` |" % " ".join(session.agent_command))
    out("| workspace | %s |" % session.workspace)
    out("| owner | %s |" % session.owner)
    out("| created | %s |" % session.created_at)
    out("| finished | %s |" % (session.finished_at or "-"))
    out("| exit status | %s |" % (session.exit_status
                                  if session.exit_status is not None else "-"))
    out("| completion | %s |" % session.completion)
    out("| policy mode | %s |" % session.policy.get("mode", "-"))
    out("| decision | **%s** |" % decision.decision)
    out("")
    out("## Protected roots")
    out("")
    for name, root in sorted(session.protected_roots.items()):
        changes = changes_by_root.get(name, [])
        ignored = ignored_by_root.get(name, [])
        ignored_detail = (", %d ignored" % len(ignored)) if ignored else ""
        out("- `%s`: branch `%s` over `%s` (%d change(s)%s)"
            % (name, root.branch, root.base, len(changes), ignored_detail))
    out("")
    if decision.reasons:
        out("## Decision reasons")
        out("")
        for reason in decision.reasons:
            out("- %s" % reason)
        out("")
    if decision.out_of_scope:
        out("## Out-of-scope paths")
        out("")
        for path in decision.out_of_scope:
            out("- `%s`" % path)
        out("")
    if decision.deny_matches:
        out("## Deny/hide rule matches")
        out("")
        for match in decision.deny_matches:
            out("- `%s` (rule `%s`)" % (match.path, match.pattern))
        out("")
    failures = session.policy.get(COMMIT_PERMISSION_DENIED_KEY) or []
    if failures:
        out("## Permission denied during commit")
        out("")
        out("Writable changes were applied to the real underlay. The paths below could not be written and remain only in the BranchFS branch.")
        out("")
        for item in failures:
            out("- `%s` `%s`: %s" % (
                item.get("root", ""), item.get("path", ""),
                item.get("error", "permission denied")))
        out("")
        out("Choose `abort`/discard to drop these remaining branch-only files and finish, or keep the session pending and `resume` it to copy/move them elsewhere manually.")
        out("")
    warning_total = sum(len(warnings)
                        for warnings in warnings_by_root.values())
    if warning_total:
        out("## BranchFS status warnings")
        out("")
        out("These warnings mean status may be incomplete or commit may fail; review before committing.")
        out("")
        for root_name, warnings in sorted(warnings_by_root.items()):
            for warning in warnings:
                out("- `%s` `%s`: %s" % (root_name, warning.path,
                                           warning.message))
        out("")
    out("## Changed paths")
    out("")
    previous_total = sum(len(changes)
                         for changes in previously_committed_by_root.values())
    if previous_total:
        previous_keys = set()
        out("already commited previously:")
        for name, changes in sorted(previously_committed_by_root.items()):
            for change in changes:
                previous_keys.add(_change_key(change))
                _render_change_item(out, change)
        out("")
        out("new commits:")
        new_total = 0
        for name, changes in sorted(changes_by_root.items()):
            for change in changes:
                if _change_key(change) in previous_keys:
                    continue
                _render_change_item(out, change)
                new_total += 1
        if not new_total:
            out("(none)")
    else:
        total = 0
        for name, changes in sorted(changes_by_root.items()):
            for change in changes:
                _render_change_item(out, change)
                total += 1
        if not total:
            out("(none)")
    out("")
    ignored_counts = _ignored_pattern_counts(ignored_by_root)
    ignored_total = sum(ignored_counts.values())
    if ignored_total:
        out("## Ignored by policy (not committed)")
        out("")
        out("These changes are excluded from ordinary `diff`, "
            "`review --accept`, and `commit` decisions and will be "
            "discarded unless explicitly included.")
        out("")
        for pattern, count in sorted(ignored_counts.items()):
            noun = "change" if count == 1 else "changes"
            out("- `%s`: %d %s" % (pattern, count, noun))
        out("")
        out("To inspect them, run `ccc-agent diff %s --show-ignored` or "
            "`ccc-agent review %s --show-ignored`."
            % (session.session_id, session.session_id))
        out("To accept them too, run `ccc-agent review %s --accept "
            "--include-ignored`." % session.session_id)
        out("")
    out("## Next steps")
    out("")
    out("```bash")
    out("ccc-agent show %s" % session.session_id)
    out("ccc-agent review %s        # browse and choose accept/select/reject/later" %
        session.session_id)
    out("ccc-agent diff %s          # list changed paths only" % session.session_id)
    out("ccc-agent diff %s <path>   # unified diff for one text file" %
        session.session_id)
    out("ccc-agent commit %s   # scripted full commit; repeat IDs to batch" %
        session.session_id)
    out("ccc-agent abort %s    # scripted discard; repeat IDs to batch" % session.session_id)
    out("```")
    out("")
    return "\n".join(lines)
