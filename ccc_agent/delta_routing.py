"""Durable records for routing vendor work into nested BranchFS deltas.

A vendor conversation/session identifier is useful for correlating callbacks, but
it is not suitable as a capability, filesystem name, or public routing key.
``DeltaRoute`` therefore keeps that identifier only as trusted metadata and uses
an independently random ``route_id`` for nested branch names and lookups.

The records in this module deliberately contain no backend behavior.  They are
small, versioned, JSON-serializable models that can be persisted by a supervisor
without importing a vendor SDK or a non-stdlib serialization package.
"""

import hashlib
import json
import os
import re
import time
import uuid


SCHEMA_VERSION = 2
ROUTE_STATES = (
    "provisioning",
    "active",
    "quiescing",
    "frozen",
    "merged",
    "pending-review",
    "aborted",
    "failed",
)
TERMINAL_ROUTE_STATES = ("merged", "aborted", "failed")

_ROUTE_TRANSITIONS = {
    "provisioning": ("active", "aborted", "failed"),
    "active": ("quiescing", "failed"),
    "quiescing": ("active", "frozen", "failed"),
    "frozen": ("active", "merged", "pending-review", "aborted", "failed"),
    "pending-review": ("active", "merged", "aborted", "failed"),
    "merged": (),
    "aborted": (),
    "failed": (),
}

_OPAQUE_ID_RE = re.compile(r"^route-[0-9a-f]{32}$")
_SESSION_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RouteStateError(ValueError):
    """Raised when a delta route is moved through an illegal state edge."""


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_route_id(_vendor_id_hint=None):
    """Return a path-safe opaque route identifier.

    ``_vendor_id_hint`` is accepted so callers can pass the identifier they are
    routing without being tempted to interpolate it.  It is intentionally
    ignored: opacity comes from independent randomness, not encoding or hashing
    the vendor value.
    """
    return "route-%s" % uuid.uuid4().hex


def _required_string(value, label):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("%s must be a non-empty string without NUL" % label)
    return value


def _validate_route_id(route_id):
    route_id = _required_string(route_id, "route_id")
    if not _OPAQUE_ID_RE.match(route_id):
        raise ValueError("route_id must be an opaque route-<32 lowercase hex> id")
    return route_id


def digest_logical_session(vendor_session_id):
    raw = _required_string(vendor_session_id, "vendor_session_id")
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_session_digest(digest):
    digest = _required_string(digest, "logical_session_digest")
    if not _SESSION_DIGEST_RE.match(digest):
        raise ValueError("logical_session_digest must be sha256:<64 lowercase hex>")
    return digest


def _coverage_record(coverage=None):
    coverage = dict(coverage or {})
    normalized = {
        "routed_bwrap_calls": int(coverage.get("routed_bwrap_calls", 0)),
        "bypassed_or_unattributed_calls": int(
            coverage.get("bypassed_or_unattributed_calls", 0)),
        "last_warning": coverage.get("last_warning"),
        "capability": str(coverage.get("capability", "unknown")),
    }
    if (normalized["routed_bwrap_calls"] < 0 or
            normalized["bypassed_or_unattributed_calls"] < 0):
        raise ValueError("route coverage counters cannot be negative")
    if (normalized["last_warning"] is not None and
            not isinstance(normalized["last_warning"], str)):
        raise ValueError("route coverage last_warning must be a string or null")
    return normalized


def _copy_json_records(records, label):
    records = list(records or ())
    try:
        # Round-tripping both rejects non-JSON values and avoids retaining a
        # caller-owned mutable object in a durable record.
        return json.loads(json.dumps(records))
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be JSON serializable: %s" % (label, exc))


class NestedRoot(object):
    """One protected root represented by a child of an outer BranchFS branch."""

    __slots__ = (
        "name",
        "base",
        "store",
        "parent_branch",
        "branch",
        "mount",
        "visible",
        "home_subdir",
        "hide_paths",
    )

    def __init__(self, name, base, store, parent_branch, branch, mount, visible,
                 home_subdir=None, hide_paths=()):
        self.name = _required_string(name, "nested root name")
        self.base = _required_string(base, "nested root base")
        self.store = _required_string(store, "nested root store")
        self.parent_branch = _required_string(parent_branch, "parent branch")
        self.branch = _validate_route_id(branch)
        if self.branch == self.parent_branch:
            raise ValueError("nested branch must differ from its parent")
        self.mount = _required_string(mount, "nested root mount")
        self.visible = _required_string(visible, "nested root visible path")
        self.home_subdir = home_subdir
        self.hide_paths = [str(path) for path in (hide_paths or ())]

    @classmethod
    def from_parent(cls, parent_root, route_id, mount_dir):
        """Build a child-root record without deriving names from vendor data."""
        route_id = _validate_route_id(route_id)
        return cls(
            name=parent_root.name,
            base=parent_root.base,
            store=parent_root.store,
            parent_branch=parent_root.branch,
            branch=route_id,
            mount=os.path.join(str(mount_dir), parent_root.name),
            visible=parent_root.visible,
            home_subdir=getattr(parent_root, "home_subdir", None),
            hide_paths=getattr(parent_root, "hide_paths", ()) or (),
        )

    def to_dict(self):
        data = {
            "name": self.name,
            "base": self.base,
            "store": self.store,
            "parent_branch": self.parent_branch,
            "branch": self.branch,
            "mount": self.mount,
            "visible": self.visible,
            "hide_paths": list(self.hide_paths),
        }
        if self.home_subdir is not None:
            data["home_subdir"] = self.home_subdir
        return data

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("nested root record must be an object")
        try:
            return cls(
                name=data["name"],
                base=data["base"],
                store=data["store"],
                parent_branch=data["parent_branch"],
                branch=data["branch"],
                mount=data["mount"],
                visible=data["visible"],
                home_subdir=data.get("home_subdir"),
                hide_paths=data.get("hide_paths", ()),
            )
        except KeyError as exc:
            raise ValueError("nested root record is missing %s" % exc.args[0])


class DeltaRoute(object):
    """Versioned mapping from one logical vendor session to nested roots."""

    def __init__(self, route_id, provider, parent_session_id, roots,
                 vendor_session_id=None, logical_session_digest=None,
                 state="provisioning", created_at=None, updated_at=None,
                 events=None, coverage=None):
        self.route_id = _validate_route_id(route_id)
        self.provider = _required_string(provider, "provider")
        if logical_session_digest is None:
            logical_session_digest = digest_logical_session(vendor_session_id)
        self.logical_session_digest = _validate_session_digest(
            logical_session_digest)
        self.parent_session_id = _required_string(
            parent_session_id, "parent_session_id")
        if state not in ROUTE_STATES:
            raise ValueError("unknown route state %r" % (state,))
        if not isinstance(roots, dict):
            raise ValueError("roots must be a mapping")
        normalized_roots = {}
        for name, root in roots.items():
            if not isinstance(root, NestedRoot):
                raise ValueError("root %s is not a NestedRoot" % name)
            if str(name) != root.name:
                raise ValueError("nested root key %s does not match %s" %
                                 (name, root.name))
            if root.branch != self.route_id:
                raise ValueError("nested root %s does not use route branch %s" %
                                 (name, self.route_id))
            normalized_roots[root.name] = root
        self.roots = normalized_roots
        self.state = state
        self.created_at = created_at or utc_now()
        self.updated_at = updated_at or self.created_at
        self.events = _copy_json_records(events, "route events")
        self.coverage = _coverage_record(coverage)

    @classmethod
    def create(cls, provider, vendor_session_id, parent_session_id,
               parent_roots, mount_dir, route_id=None):
        """Create an opaque route and child records for all outer roots."""
        route_id = _validate_route_id(route_id or new_route_id(vendor_session_id))
        if not isinstance(parent_roots, dict):
            raise ValueError("parent_roots must be a mapping")
        route_mount_dir = os.path.join(str(mount_dir), route_id)
        roots = {
            str(name): NestedRoot.from_parent(
                root, route_id=route_id, mount_dir=route_mount_dir)
            for name, root in parent_roots.items()
        }
        return cls(
            route_id=route_id,
            provider=provider,
            vendor_session_id=vendor_session_id,
            parent_session_id=parent_session_id,
            roots=roots,
        )

    def transition(self, new_state, at=None, detail=None):
        if new_state not in ROUTE_STATES:
            raise RouteStateError("unknown route state %r" % (new_state,))
        if new_state not in _ROUTE_TRANSITIONS[self.state]:
            raise RouteStateError("illegal route transition %s -> %s" %
                                  (self.state, new_state))
        timestamp = at or utc_now()
        old_state = self.state
        self.state = new_state
        self.updated_at = timestamp
        event = {"time": timestamp, "from": old_state, "to": new_state}
        if detail is not None:
            event["detail"] = detail
        # Validate/copy detail before retaining it as durable metadata.
        self.events.append(_copy_json_records([event], "route event")[0])

    def record_coverage(self, outcome, warning=None, capability=None):
        """Record sanitized routing coverage without retaining command data."""
        if outcome == "routed":
            self.coverage["routed_bwrap_calls"] += 1
        elif outcome in ("bypassed", "unattributed"):
            self.coverage["bypassed_or_unattributed_calls"] += 1
        else:
            raise ValueError("unknown routing coverage outcome %r" % outcome)
        if warning is not None:
            self.coverage["last_warning"] = _required_string(
                warning, "routing warning")
        if capability is not None:
            self.coverage["capability"] = _required_string(
                capability, "routing capability")
        self.updated_at = utc_now()

    def set_capability(self, capability, warning=None):
        capability = _required_string(capability, "routing capability")
        self.coverage["capability"] = capability
        if warning is not None:
            self.coverage["last_warning"] = _required_string(
                warning, "routing warning")
        self.updated_at = utc_now()

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "route_id": self.route_id,
            "provider": self.provider,
            "logical_session_digest": self.logical_session_digest,
            "parent_session_id": self.parent_session_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "roots": {name: root.to_dict()
                      for name, root in sorted(self.roots.items())},
            "events": _copy_json_records(self.events, "route events"),
            "coverage": _coverage_record(self.coverage),
        }

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("delta route record must be an object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported delta route schema version %r" %
                             data.get("schema_version"))
        roots_data = data.get("roots")
        if not isinstance(roots_data, dict):
            raise ValueError("delta route roots must be an object")
        try:
            return cls(
                route_id=data["route_id"],
                provider=data["provider"],
                logical_session_digest=data["logical_session_digest"],
                parent_session_id=data["parent_session_id"],
                roots={name: NestedRoot.from_dict(root)
                       for name, root in roots_data.items()},
                state=data["state"],
                created_at=data["created_at"],
                updated_at=data["updated_at"],
                events=data.get("events", ()),
                coverage=data.get("coverage"),
            )
        except KeyError as exc:
            raise ValueError("delta route record is missing %s" % exc.args[0])

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, payload):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid delta route JSON: %s" % exc)
        return cls.from_dict(data)


# Descriptive aliases retained for callers that name the durable records rather
# than the routing concept itself.
RouteRecord = DeltaRoute
NestedRootRecord = NestedRoot
