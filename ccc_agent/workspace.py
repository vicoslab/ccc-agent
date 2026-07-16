"""Central hardened admission for trusted dynamic workspace roots.

Workspace paths arrive in the agent-visible namespace, while existence and type
checks run against the trusted BranchFS mount (or its real base before mounting).
The resulting records contain only visible/canonical path metadata and stable
``lstat`` identities; trusted host mount/base paths are never persisted.
"""

import os
import stat

from .paths import is_within, normalize


class WorkspaceAdmissionError(ValueError):
    """Raised when a workspace root is malformed, outside policy, or unstable."""


def _identity(info):
    return {
        "st_dev": int(info.st_dev),
        "st_ino": int(info.st_ino),
        "st_mode": int(info.st_mode),
        "st_uid": int(info.st_uid),
        "st_gid": int(info.st_gid),
    }


class WorkspaceAdmissionPolicy(object):
    """Validate workspace roots against protected roots and operator ceilings.

    ``protected_roots`` may be a mapping of names to ``ProtectedRoot``/``RootSpec``
    objects or any iterable of such objects. ``workspace_admission_roots`` uses
    agent-visible spellings and defaults to the protected visible roots for
    compatibility. Even with that default, roots must be strict descendants
    unless ``allow_protected_root_workspace`` is explicitly enabled.
    """

    def __init__(self, protected_roots, alias_map,
                 workspace_admission_roots=None,
                 allow_protected_root_workspace=False):
        if isinstance(protected_roots, dict):
            items = list(protected_roots.items())
        else:
            items = [(getattr(root, "name", str(index)), root)
                     for index, root in enumerate(protected_roots)]
        self.alias_map = alias_map
        self.allow_protected_root_workspace = bool(
            allow_protected_root_workspace)
        self._roots = []
        for fallback_name, root in items:
            name = str(getattr(root, "name", fallback_name))
            try:
                visible = self.canonical_key(getattr(root, "visible"))
            except (TypeError, ValueError) as exc:
                raise WorkspaceAdmissionError(
                    "protected root %s has invalid visible path: %s" %
                    (name, exc))
            self._roots.append((visible, name, root))
        self._roots.sort(key=lambda item: len(item[0]), reverse=True)
        if not self._roots:
            raise WorkspaceAdmissionError("at least one protected root is required")

        configured = workspace_admission_roots
        if configured is None:
            configured = [item[0] for item in self._roots]
        if isinstance(configured, str):
            configured = [configured]
        self.workspace_admission_roots = []
        for path in configured or ():
            canonical = self.canonical_key(path)
            root = self._protected_root(canonical, permit_equal=True)
            if root is None:
                raise WorkspaceAdmissionError(
                    "workspace admission root %s is not under a protected root" %
                    path)
            if canonical not in self.workspace_admission_roots:
                self.workspace_admission_roots.append(canonical)
        self.workspace_admission_roots.sort(key=len, reverse=True)

    def canonical_key(self, path):
        if not isinstance(path, str):
            raise WorkspaceAdmissionError("workspace path must be a string")
        if not path or "\x00" in path:
            raise WorkspaceAdmissionError("workspace path is empty or contains NUL")
        if "://" in path or path.startswith("file:"):
            raise WorkspaceAdmissionError("workspace path must be a local absolute path")
        try:
            return self.alias_map.canonicalize(normalize(path))
        except (TypeError, ValueError) as exc:
            raise WorkspaceAdmissionError(str(exc))

    def _protected_root(self, canonical, permit_equal=False):
        for visible, name, root in self._roots:
            if is_within(canonical, visible):
                if canonical == visible and not permit_equal:
                    continue
                return visible, name, root
        return None

    @staticmethod
    def _authority_anchor(root):
        mount = getattr(root, "mount", None)
        if (mount and os.path.isdir(mount) and
                not os.path.islink(mount)):
            return os.path.abspath(str(mount))
        base = getattr(root, "base", None)
        if base:
            return os.path.abspath(str(base))
        return os.path.abspath(str(getattr(root, "visible")))

    @staticmethod
    def _component_records(anchor, relative_path, require_existing):
        paths = [(".", anchor)]
        current = anchor
        if relative_path != ".":
            accumulated = []
            for part in relative_path.split(os.sep):
                accumulated.append(part)
                current = os.path.join(current, part)
                paths.append((os.path.join(*accumulated), current))

        records = []
        missing = False
        for relative, physical in paths:
            if missing:
                break
            try:
                info = os.lstat(physical)
            except OSError as exc:
                if require_existing:
                    raise WorkspaceAdmissionError(
                        "workspace path does not exist: %s" % exc)
                missing = True
                break
            if stat.S_ISLNK(info.st_mode):
                raise WorkspaceAdmissionError(
                    "workspace path traverses symlink component %s" % relative)
            if not stat.S_ISDIR(info.st_mode):
                raise WorkspaceAdmissionError(
                    "workspace path component is not a directory: %s" % relative)
            records.append({"relative_path": relative,
                            "identity": _identity(info)})
        return records, (records[-1]["identity"].copy()
                         if not missing and records else None)

    def _admission_root(self, canonical):
        for ceiling in self.workspace_admission_roots:
            if is_within(canonical, ceiling):
                if (canonical == ceiling and
                        not self.allow_protected_root_workspace):
                    continue
                return ceiling
        return None

    def admit(self, path, require_existing=True):
        visible_path = None
        if isinstance(path, str):
            try:
                visible_path = normalize(path)
            except ValueError:
                visible_path = path
        canonical = self.canonical_key(path)
        matched = self._protected_root(
            canonical, permit_equal=self.allow_protected_root_workspace)
        if matched is None:
            equal_root = self._protected_root(canonical, permit_equal=True)
            if equal_root is not None:
                raise WorkspaceAdmissionError(
                    "protected root itself is not an admissible workspace")
            raise WorkspaceAdmissionError(
                "workspace %s is not a strict descendant of a protected root" %
                path)
        protected_visible, root_name, root = matched
        ceiling = self._admission_root(canonical)
        if ceiling is None:
            raise WorkspaceAdmissionError(
                "workspace %s is outside workspace_admission_roots" % path)

        relative = os.path.relpath(canonical, protected_visible)
        anchor = self._authority_anchor(root)
        components, final_identity = self._component_records(
            anchor, relative, bool(require_existing))
        if require_existing and final_identity is None:
            raise WorkspaceAdmissionError("workspace path does not exist")
        if final_identity is not None:
            final_identity.pop("relative_path", None)
        return {
            "visible_path": visible_path,
            "canonical_path": canonical,
            "canonical_key": canonical,
            "protected_root": root_name,
            "relative_path": relative,
            "admission_root": ceiling,
            "identity": final_identity,
            "components": components,
        }

    def revalidate(self, admitted_root):
        if not isinstance(admitted_root, dict):
            raise WorkspaceAdmissionError("admitted workspace record must be an object")
        canonical = admitted_root.get("canonical_path")
        if not isinstance(canonical, str):
            raise WorkspaceAdmissionError("admitted workspace record has no canonical path")
        current = self.admit(canonical, require_existing=True)
        if (current.get("protected_root") != admitted_root.get("protected_root") or
                current.get("relative_path") != admitted_root.get("relative_path") or
                current.get("identity") != admitted_root.get("identity") or
                current.get("components") != admitted_root.get("components")):
            raise WorkspaceAdmissionError(
                "workspace root identity changed after admission")
        # Preserve the spelling originally shown by the trusted client.
        current["visible_path"] = admitted_root.get("visible_path", canonical)
        return current

    def contains_change(self, admitted_root, change_path):
        try:
            root_key = self.canonical_key(admitted_root["canonical_path"])
            change_key = self.canonical_key(change_path)
        except (KeyError, TypeError, WorkspaceAdmissionError):
            return False
        return is_within(change_key, root_key)
