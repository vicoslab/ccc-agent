"""Runtime version and Git traceability helpers for ccc-agent."""

import importlib
import pathlib
import re
import subprocess

from . import __version__

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _normalize_git_commit(value):
    value = str(value or "").strip().lower()
    if _FULL_SHA_RE.match(value):
        return value
    return None


def _build_git_commit():
    """Return the commit embedded into an installed wheel, if present."""
    try:
        build = importlib.import_module("ccc_agent._build")
    except ImportError:
        return None
    return _normalize_git_commit(getattr(build, "GIT_COMMIT", None))


def _source_git_commit(package_file=None):
    """Best-effort Git commit for source/editable checkouts.

    A normal ``pip install git+...`` leaves no ``.git`` directory in
    site-packages, so installed wheels rely on ``_build_git_commit`` instead.
    This fallback keeps source-tree and editable installs traceable too.
    """
    package_file = __file__ if package_file is None else package_file
    try:
        start = pathlib.Path(package_file).resolve()
    except OSError:
        return None
    for candidate in start.parents:
        if not (candidate / ".git").exists():
            continue
        try:
            output = subprocess.check_output(
                ["git", "-C", str(candidate), "rev-parse", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True)
        except (OSError, subprocess.CalledProcessError):
            return None
        return _normalize_git_commit(output)
    return None


def git_commit():
    """Return the exact Git commit SHA for this package when known."""
    return _build_git_commit() or _source_git_commit()


def version_string():
    """Return the human-facing ccc-agent version string."""
    commit = git_commit()
    if commit:
        return "v%s (git %s)" % (__version__, commit)
    return "v%s" % __version__
