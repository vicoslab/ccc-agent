"""Change classification and commit decision for agent branch sessions.

The supervisor freezes a session's branches, collects BranchFS status, and
asks this module what to do.  The agent never runs this code path with commit
authority; the decision is enforced by the trusted supervisor.
"""

import fnmatch
import posixpath

from .paths import is_within

# Decision constants (stable strings: they are written into review artifacts).
AUTO_COMMIT = "auto-commit"
PENDING_REVIEW = "pending-review"
ABORT = "abort"
NO_CHANGES = "no-changes"

MODES = (
    "manual",          # always require human review before commit
    "workspace-auto",  # auto-commit iff confined to allowed scopes, no deny hit
    "read-only-review",  # freeze and report, never auto-commit
    "training-run",    # like workspace-auto; scopes = declared artifact dirs
    "throwaway",       # abort at completion unless human preserves explicitly
)

# Paths that force review even when inside the allowed workspace.  Component
# patterns (no '/') match any single path segment; slashed patterns match the
# segment sequence anywhere in the path plus everything below it.
DEFAULT_DENY_PATTERNS = (
    ".ssh",
    ".gnupg",
    ".env",
    ".env.*",
    "*.pem",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".aws",
    ".kube/config",
    ".docker/config.json",
    "credentials.json",
    ".git/config",
    ".git/hooks",
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".condarc",
    ".ccc-agent",  # session/policy/review state must never be agent-writable
)

# Always-ignored sandbox/filesystem/runtime noise: these paths are not useful
# agent deliverables and should never be auto- or manually committed by the
# supervisor.  Keep executable startup config (e.g. .bashrc) in the deny set
# instead: history/cache/state is disposable, startup/config policy is reviewable.
DEFAULT_IGNORE_PATTERNS = (
    # NFS silly-rename artifacts left when an open file is deleted/renamed.
    "*/.nfs*",
    # Generic runtime caches/logs.
    ".cache",
    ".npm/_logs",
    # Interactive shell/REPL/client history files updated as normal operation.
    ".bash_history*",
    ".zsh_history*",
    ".zhistory*",
    ".python_history*",
    ".sqlite_history*",
    ".psql_history*",
    ".mysql_history*",
    ".rediscli_history*",
    ".node_repl_history*",
    ".lesshst",
    ".wget-hsts",
    ".config/fish/fish_history",
    ".local/share/fish/fish_history",
    ".ipython/profile_default/history.sqlite",
)


def path_matches(pattern, path):
    """Glob match for deny/hide rules against a canonical absolute path.

    - ``pattern`` without ``/``: matches if any single path component matches
      (``.env``, ``id_rsa*``, ``*.pem``).
    - ``pattern`` with ``/`` and no leading ``/``: matches that component
      sequence anywhere in the path, and everything below it
      (``.git/hooks`` matches ``a/.git/hooks`` and ``a/.git/hooks/pre-commit``).
    - absolute ``pattern``: plain fnmatch against the whole path, plus
      everything below a matched directory.
    """
    if "/" not in pattern:
        parts = path.strip("/").split("/")
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    pat = pattern if pattern.startswith("/") else "*/" + pattern
    return (fnmatch.fnmatchcase(path, pat)
            or fnmatch.fnmatchcase(path, pat + "/*"))


class Change(object):
    """One changed path from BranchFS status, in agent-visible terms."""

    __slots__ = ("op", "path", "kind", "bytes", "root", "summary")

    def __init__(self, op, path, kind="file", bytes=0, root="", summary=""):
        self.op = op        # "A" added | "M" modified | "D" deleted
        self.path = path    # absolute agent-visible path
        self.kind = kind    # file | dir | symlink
        self.bytes = bytes
        self.root = root    # protected-root name, e.g. "storage_user"
        self.summary = summary  # optional display note, e.g. collapsed children

    def to_dict(self):
        data = {"op": self.op, "path": self.path, "kind": self.kind,
                "bytes": self.bytes, "root": self.root}
        if self.summary:
            data["summary"] = self.summary
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(op=data["op"], path=data["path"],
                   kind=data.get("kind", "file"), bytes=data.get("bytes", 0),
                   root=data.get("root", ""),
                   summary=data.get("summary", ""))


class IgnoredChange(object):
    """One change excluded from commit/review by an ignore pattern."""

    __slots__ = ("change", "pattern")

    def __init__(self, change, pattern):
        self.change = change
        self.pattern = pattern

    def to_dict(self):
        data = self.change.to_dict()
        data["ignore_pattern"] = self.pattern
        return data

    @classmethod
    def from_dict(cls, data):
        return cls(Change.from_dict(data), data.get("ignore_pattern", ""))


def _change_key(change, alias_map=None):
    if alias_map is not None:
        path = alias_map.canonicalize(change.path)
    else:
        path = posixpath.normpath(change.path)
    return (change.root, path)


def _prefer_final_change(current, candidate):
    """Choose the effective final filesystem state for one root/path.

    A same-path tombstone plus a non-delete delta is delete-then-rewrite
    bookkeeping: after commit the path exists with the delta contents.  Show and
    commit that final file/dir/symlink change, not the intermediate tombstone.
    """
    if current.op == "D" and candidate.op != "D":
        return candidate
    if current.op != "D" and candidate.op == "D":
        return current
    return current


def net_final_changes(changes, alias_map=None):
    """Collapse raw status entries to one final-state change per root/path."""
    selected = {}
    order = []
    for change in changes:
        key = _change_key(change, alias_map)
        if key not in selected:
            selected[key] = change
            order.append(key)
        else:
            selected[key] = _prefer_final_change(selected[key], change)
    return [selected[key] for key in order]


def net_final_ignored_changes(ignored, alias_map=None):
    """Collapse ignored changes while preserving the ignore pattern selected."""
    selected = {}
    order = []
    for item in ignored:
        key = _change_key(item.change, alias_map)
        if key not in selected:
            selected[key] = item
            order.append(key)
        else:
            preferred = _prefer_final_change(selected[key].change, item.change)
            if preferred is item.change:
                selected[key] = item
    return [selected[key] for key in order]


class DenyMatch(object):

    __slots__ = ("path", "pattern")

    def __init__(self, path, pattern):
        self.path = path
        self.pattern = pattern

    def to_dict(self):
        return {"path": self.path, "pattern": self.pattern}


class PolicyConfig(object):
    def __init__(self, mode, allowed_scopes=(), deny_patterns=None,
                 hide_patterns=(), max_policy_repair_attempts=2,
                 ignore_patterns=()):
        if mode not in MODES:
            raise ValueError("unknown policy mode %r (expected one of %s)"
                             % (mode, ", ".join(MODES)))
        self.mode = mode
        self.allowed_scopes = list(allowed_scopes)
        self.deny_patterns = (list(DEFAULT_DENY_PATTERNS)
                              if deny_patterns is None else list(deny_patterns))
        self.hide_patterns = list(hide_patterns)
        self.max_policy_repair_attempts = max_policy_repair_attempts
        # Infrastructure paths to drop from the change set entirely — neither
        # committed nor flagged.  Used for sandbox plumbing the agent never
        # "authored": e.g. the mountpoints bwrap creates for re-exposed cred
        # dirs (~/.codex, ~/.claude) live under the home view and would
        # otherwise show up as spurious out-of-scope deltas.
        self.ignore_patterns = list(DEFAULT_IGNORE_PATTERNS) + list(ignore_patterns)

    @classmethod
    def from_dict(cls, data):
        return cls(
            mode=data["mode"],
            allowed_scopes=data.get("allowed_scopes", ()),
            deny_patterns=data.get("deny_patterns"),
            hide_patterns=data.get("hide_patterns", ()),
            max_policy_repair_attempts=data.get("max_policy_repair_attempts", 2),
            ignore_patterns=data.get("ignore_patterns", ()),
        )

    def to_dict(self):
        return {
            "mode": self.mode,
            "allowed_scopes": self.allowed_scopes,
            "deny_patterns": self.deny_patterns,
            "hide_patterns": self.hide_patterns,
            "max_policy_repair_attempts": self.max_policy_repair_attempts,
            "ignore_patterns": self.ignore_patterns,
        }


def ignore_pattern_for_path(path, config, alias_map):
    """Return the first ignore pattern matching ``path``, or ``None``.

    A pattern may be a glob (e.g. ``*/.nfs*``) matched against the path, or an
    absolute path matched as an exact path OR a subtree prefix.  These are
    sandbox/cache/runtime plumbing, not policy-visible authored work by default.
    """
    patterns = list(getattr(config, "ignore_patterns", ()) or ())
    if not patterns:
        return None

    canonical = alias_map.canonicalize(path)
    for pattern in patterns:
        if path_matches(pattern, canonical):
            return pattern
        # subtree match only for real absolute paths (not globs)
        if pattern.startswith("/") and not any(c in pattern for c in "*?["):
            if is_within(canonical, pattern):
                return pattern
    return None


def split_ignored(changes, config, alias_map):
    """Return ``(policy_visible, ignored)`` for a change sequence."""
    visible = []
    ignored = []
    for change in net_final_changes(changes, alias_map):
        pattern = ignore_pattern_for_path(change.path, config, alias_map)
        if pattern is None:
            visible.append(change)
        else:
            ignored.append(IgnoredChange(change, pattern))
    return visible, ignored


def filter_ignored(changes, config, alias_map):
    """Drop changes whose canonical path matches an ignore pattern."""
    return split_ignored(changes, config, alias_map)[0]


class PolicyDecision(object):
    def __init__(self, decision, total_changes, out_of_scope, deny_matches,
                 reasons):
        self.decision = decision
        self.total_changes = total_changes
        self.out_of_scope = out_of_scope        # canonical paths
        self.deny_matches = deny_matches        # [DenyMatch]
        self.reasons = reasons                  # human-readable strings

    def to_dict(self):
        return {
            "decision": self.decision,
            "total_changes": self.total_changes,
            "out_of_scope": self.out_of_scope,
            "deny_matches": [m.to_dict() for m in self.deny_matches],
            "reasons": self.reasons,
        }


def classify(changes, config, alias_map):
    """Return (out_of_scope, deny_matches) for the change set, canonicalized."""
    changes = net_final_changes(changes, alias_map)
    scopes = [alias_map.canonicalize(s) for s in config.allowed_scopes]
    deny = list(config.deny_patterns) + list(config.hide_patterns)
    out_of_scope = []
    deny_matches = []
    for change in changes:
        canonical = alias_map.canonicalize(change.path)
        in_scope = any(is_within(canonical, scope) for scope in scopes)
        # A directory that is an *ancestor* of an allowed scope is structural
        # plumbing: writing a nested file makes BranchFS emit deltas for every
        # parent directory up to the branch root.  Those ancestors sit above
        # the workspace scope but are not real out-of-scope writes, so they
        # must not block auto-commit.  A sibling/unrelated directory is not an
        # ancestor and stays flagged.
        if not in_scope and change.kind == "dir":
            in_scope = any(is_within(scope, canonical) for scope in scopes)
        if not in_scope:
            out_of_scope.append(canonical)
        for pattern in deny:
            if path_matches(pattern, canonical):
                deny_matches.append(DenyMatch(change.path, pattern))
                break
    return out_of_scope, deny_matches


def evaluate(changes, config, alias_map):
    """Decide what to do with a frozen session's change set."""
    changes = net_final_changes(changes, alias_map)
    if not changes:
        return PolicyDecision(NO_CHANGES, 0, [], [],
                              ["branch contains no changes"])

    out_of_scope, deny_matches = classify(changes, config, alias_map)
    reasons = []
    if out_of_scope:
        reasons.append("%d change(s) outside allowed scopes" % len(out_of_scope))
    if deny_matches:
        reasons.append("%d change(s) matched deny/hide patterns"
                       % len(deny_matches))

    if config.mode == "throwaway":
        reasons.append("throwaway mode aborts at completion")
        decision = ABORT
    elif config.mode in ("manual", "read-only-review"):
        reasons.append("%s mode always requires human review" % config.mode)
        decision = PENDING_REVIEW
    elif config.mode in ("workspace-auto", "training-run"):
        if not out_of_scope and not deny_matches:
            reasons.append("all changes confined to allowed scopes")
            decision = AUTO_COMMIT
        else:
            decision = PENDING_REVIEW
    else:  # unreachable: constructor validates mode
        raise ValueError("unknown policy mode %r" % config.mode)

    return PolicyDecision(decision, len(changes), out_of_scope, deny_matches,
                          reasons)
