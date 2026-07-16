"""Trusted lifecycle manager for best-effort logical-session delta routes.

Nested BranchFS branches are provenance overlays only.  Every method here keeps
the outer session branch authoritative; child merges target the outer branch and
never the real base directly.
"""

import hashlib
import json
import os
import stat
import tempfile
import threading

from .delta_routing import DeltaRoute, digest_logical_session
from .paths import is_within


SANDBOX_ROUTE_ROOT = "/run/ccc-agent/routes"


class DeltaRouteManager(object):
    def __init__(self, session, store, backend):
        self.session = session
        self.store = store
        self.backend = backend
        self._lock = threading.RLock()
        self._routes = {}
        self._load()

    @property
    def enabled(self):
        return self.session.policy.get("session_delta_routing") is True

    @property
    def vendors(self):
        values = self.session.policy.get(
            "session_delta_routing_vendors", ("codex",))
        return tuple(str(value).strip().lower() for value in values)

    def _mount_root(self):
        return os.path.join(self.store.bundle_dir(self.session.session_id),
                            "route-mounts")

    def _sandbox_route_root(self):
        value = self.session.policy.get(
            "sandbox_route_root", SANDBOX_ROUTE_ROOT)
        value = os.path.normpath(str(value))
        if not os.path.isabs(value):
            raise ValueError("sandbox route root must be absolute")
        return value

    def _review_root(self, route_id):
        return os.path.join(self.store.review_dir(self.session.session_id),
                            "routes", route_id)

    def _load(self):
        raw = self.session.session_delta_routes
        if not isinstance(raw, dict):
            raise ValueError("session_delta_routes must be an object")
        routes = {}
        for route_id, payload in raw.items():
            route = DeltaRoute.from_dict(payload)
            if route.route_id != route_id:
                raise ValueError("delta route key does not match route_id")
            if route.parent_session_id != self.session.session_id:
                raise ValueError("delta route belongs to another outer session")
            routes[route_id] = route
        self._routes = routes

    def _save(self, route):
        self._routes[route.route_id] = route
        self.session.session_delta_routes[route.route_id] = route.to_dict()
        self.store.save(self.session)

    def get(self, route_id):
        return self._routes.get(route_id)

    def routes(self):
        return dict(self._routes)

    def _route_for_workspace(self, workspace_session_key):
        matches = [route for route in self._routes.values()
                   if route.workspace_session_key == workspace_session_key and
                   route.state not in ("merged", "aborted", "failed")]
        if len(matches) > 1:
            raise ValueError("multiple live routes exist for one workspace session")
        return matches[0] if matches else None

    def _outer_fingerprints(self):
        baseline = {}
        for name, root in sorted(self.session.protected_roots.items()):
            baseline[name] = {
                change.path: self._fingerprint_change(root, change)
                for change in self.backend.status_report(root).changes
            }
        return baseline

    def provision(self, provider, logical_session_id, workspace_session_key,
                  workspace_generation, admitted_roots):
        """Create or refresh one pre-authorized logical-session child route."""
        provider = str(provider or "").strip().lower()
        if not self.enabled or provider not in self.vendors:
            return None
        with self._lock:
            current = self._route_for_workspace(workspace_session_key)
            if current is not None:
                if current.state != "active":
                    return current
                if workspace_generation < (current.workspace_generation or 0):
                    raise ValueError("stale workspace generation for delta route")
                current.workspace_generation = int(workspace_generation)
                current.admitted_roots = json.loads(json.dumps(admitted_roots))
                self._save(current)
                return current

            route = DeltaRoute.create(
                provider=provider,
                vendor_session_id=logical_session_id,
                parent_session_id=self.session.session_id,
                parent_roots=self.session.protected_roots,
                mount_dir=self._mount_root(),
                workspace_session_key=workspace_session_key,
                workspace_generation=workspace_generation,
                admitted_roots=admitted_roots,
                outer_baseline=self._outer_fingerprints(),
            )
            self._save(route)
            created = []
            try:
                for name, child in sorted(route.roots.items()):
                    outer = self.session.protected_roots[name]
                    self.backend.create_nested_branch(outer, child)
                    created.append((outer, child))
                    self.backend.mount(child, agent=True)
                route.transition("active", detail={"roots": len(route.roots)})
                route.set_capability("ready")
                self._save(route)
                return route
            except Exception as exc:
                cleanup_errors = []
                for outer, child in reversed(created):
                    try:
                        self.backend.unmount(child)
                    except Exception:
                        pass
                    try:
                        self.backend.abort_nested_branch(outer, child)
                    except Exception as cleanup_exc:
                        cleanup_errors.append(str(cleanup_exc))
                target = "failed" if not cleanup_errors else "pending-review"
                try:
                    route.transition(target, detail={
                        "error": str(exc)[:256],
                        "cleanup_errors": cleanup_errors[:8],
                    })
                except Exception:
                    route.state = target
                route.set_capability("unavailable", warning=str(exc)[:128])
                self._save(route)
                return route

    @staticmethod
    def _status_payload(report):
        return {
            "changes": [change.to_dict() for change in report.changes],
            "warnings": [warning.to_dict() for warning in report.warnings],
        }

    @staticmethod
    def _atomic_json(path, payload):
        parent = os.path.dirname(path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _snapshot(self, route):
        payload = {"route_id": route.route_id, "roots": {}}
        for name, child in sorted(route.roots.items()):
            payload["roots"][name] = self._status_payload(
                self.backend.status_report(child))
        self._atomic_json(
            os.path.join(self._review_root(route.route_id), "status.json"),
            payload)
        self._atomic_json(
            os.path.join(self._review_root(route.route_id), "route.json"),
            route.to_dict())
        return payload

    @staticmethod
    def _fingerprint_change(root, change):
        result = {"op": change.op, "kind": change.kind,
                  "bytes": int(change.bytes)}
        if change.op == "D":
            return result
        rel = os.path.relpath(change.path, root.visible)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep):
            raise ValueError("route change is outside its declared root")
        path = os.path.join(root.mount, rel)
        info = os.lstat(path)
        result.update({"mode": stat.S_IFMT(info.st_mode),
                       "size": int(info.st_size)})
        if stat.S_ISLNK(info.st_mode):
            result["symlink_target"] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode) and info.st_size <= 1024 * 1024:
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(65536), b""):
                    digest.update(block)
            result["sha256"] = digest.hexdigest()
        elif stat.S_ISREG(info.st_mode):
            result["mtime_ns"] = int(getattr(
                info, "st_mtime_ns", int(info.st_mtime * 1000000000)))
        return result

    @staticmethod
    def _conflict_paths(conflicts, outer):
        paths = set()
        for item in conflicts:
            if isinstance(item, dict):
                path = item.get("path") or item.get("canonical_path")
            else:
                path = item
            if not isinstance(path, str) or not path:
                continue
            if not os.path.isabs(path):
                path = os.path.join(outer.visible, path)
            paths.add(os.path.normpath(path))
        return paths

    def _record_attribution(self, route, snapshot, outcomes, errors,
                            parent_before_merge):
        records = self.session.policy.setdefault("route_path_attribution", {})
        if not isinstance(records, dict):
            records = {}
            self.session.policy["route_path_attribution"] = records
        error_roots = {item["root"] for item in errors}
        for name, root_snapshot in sorted(snapshot.get("roots", {}).items()):
            outer = self.session.protected_roots[name]
            outcome = outcomes.get(name) or {}
            conflict_paths = self._conflict_paths(
                outcome.get("conflicts") or (), outer)
            current = {change.path: change
                       for change in self.backend.status_report(outer).changes}
            for change_data in root_snapshot.get("changes") or ():
                path = os.path.normpath(change_data["path"])
                admitted_scope = None
                for admitted in route.admitted_roots:
                    canonical = admitted.get("canonical_path")
                    if isinstance(canonical, str) and is_within(path, canonical):
                        admitted_scope = canonical
                        break
                conflict = (name in error_roots or path in conflict_paths or
                            path not in current)
                fingerprint = None
                if not conflict:
                    try:
                        fingerprint = self._fingerprint_change(
                            outer, current[path])
                    except (OSError, ValueError):
                        conflict = True
                existing = records.get(path)
                route_ids = []
                if isinstance(existing, dict):
                    route_ids.extend(existing.get("route_ids") or ())
                if route.route_id not in route_ids:
                    route_ids.append(route.route_id)
                shared_influence = path in parent_before_merge.get(name, {})
                multiply_influenced = (len(route_ids) > 1 or
                                       shared_influence)
                authorized = bool(
                    admitted_scope and not conflict and not multiply_influenced)
                category = ("multiply-influenced" if multiply_influenced else
                            "conflicted" if conflict else
                            "attributed" if admitted_scope else
                            "attributed-out-of-scope")
                records[path] = {
                    "category": category,
                    "authorized": authorized,
                    "route_ids": route_ids,
                    "shared_outer_influence": shared_influence,
                    "root": name,
                    "admitted_scope": admitted_scope,
                    "fingerprint": fingerprint,
                    "updated_at": route.updated_at,
                }

    def end_workspace_session(self, workspace_session_key):
        """Quiesce, snapshot, and merge one child into the live outer branch."""
        with self._lock:
            route = self._route_for_workspace(workspace_session_key)
            if route is None:
                return None
            snapshot = None
            if route.state == "active":
                route.transition("quiescing")
                self._save(route)
            if route.state == "quiescing":
                try:
                    for child in route.roots.values():
                        self.backend.freeze(child)
                    snapshot = self._snapshot(route)
                    for child in route.roots.values():
                        self.backend.unmount(child)
                    route.transition("frozen")
                    self._save(route)
                except Exception as exc:
                    route.transition("pending-review", detail={
                        "phase": "quiesce", "error": str(exc)[:256]})
                    self._save(route)
                    return route
            if route.state != "frozen":
                return route
            if snapshot is None:
                try:
                    with open(os.path.join(
                            self._review_root(route.route_id),
                            "status.json")) as fh:
                        snapshot = json.load(fh)
                except (OSError, ValueError) as exc:
                    route.transition("pending-review", detail={
                        "phase": "recovery",
                        "error": "route snapshot unavailable: %s" % exc})
                    self._save(route)
                    return route

            parent_before_merge = self._outer_fingerprints()
            outcomes = {}
            conflicts = []
            errors = []
            for name, child in sorted(route.roots.items()):
                outer = self.session.protected_roots[name]
                try:
                    outcome = self.backend.commit_nested_branch(outer, child)
                    outcomes[name] = outcome
                    for conflict in outcome.get("conflicts") or ():
                        conflicts.append({"root": name, "conflict": conflict})
                except Exception as exc:
                    errors.append({"root": name, "error": str(exc)[:256]})
            detail = {"outcomes": outcomes, "conflicts": conflicts,
                      "errors": errors}
            self._record_attribution(
                route, snapshot, outcomes, errors, parent_before_merge)
            self._atomic_json(
                os.path.join(self._review_root(route.route_id), "merge.json"),
                detail)
            route.transition(
                "pending-review" if conflicts or errors else "merged",
                detail={"conflicts": len(conflicts), "errors": len(errors)})
            self._save(route)
            return route

    def lookup(self, provider, logical_session_hint):
        provider = str(provider or "").strip().lower()
        try:
            digest = digest_logical_session(logical_session_hint)
        except ValueError:
            return {"ok": False, "reason": "no-active-route"}
        with self._lock:
            matches = [route for route in self._routes.values()
                       if route.provider == provider and
                       route.logical_session_digest == digest and
                       route.state == "active"]
            if len(matches) != 1:
                return {"ok": False, "reason": "no-active-route"}
            route = matches[0]
            bindings = []
            for name, child in sorted(route.roots.items()):
                source = "%s/%s/%s" % (
                    self._sandbox_route_root(), route.route_id, name)
                bindings.append({"source": source,
                                 "destination": child.visible})
                if child.home_subdir:
                    bindings.append({
                        "source": os.path.normpath(os.path.join(
                            source, child.home_subdir)),
                        "destination": "/home/%s" % self.session.owner,
                    })
            return {"ok": True, "route_id": route.route_id,
                    "bindings": bindings}

    def record_result(self, route_id, outcome, reason=None):
        with self._lock:
            route = self._routes.get(route_id)
            if route is None or route.state != "active":
                return {"ok": False, "reason": "no-active-route"}
            route.record_coverage(outcome, warning=reason)
            self._save(route)
            return {"ok": True}

    def recover(self):
        """Conservatively finish routes left live by a supervisor crash."""
        outcomes = {}
        for route in list(self._routes.values()):
            if route.state in ("active", "quiescing", "frozen"):
                recovered = self.end_workspace_session(
                    route.workspace_session_key)
                outcomes[route.route_id] = (
                    recovered.state if recovered is not None else "missing")
            elif route.state == "provisioning":
                route.transition("pending-review", detail={
                    "phase": "recovery", "error": "provisioning was interrupted"})
                self._save(route)
                outcomes[route.route_id] = route.state
        return outcomes
