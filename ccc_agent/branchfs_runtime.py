"""BranchFS binary discovery and runtime checks for ccc-agent.

The Python supervisor intentionally has no required third-party dependencies.
When users install the optional ``ccc-agent[branchfs]`` extra, pip installs the
``vicoslab-branchfs-bin`` wheel from the vicoslab/branchfs GitHub release.  This
module prefers that packaged binary but falls back to an operator-provided
``branchfs`` on PATH for plain ``ccc-agent`` installs.
"""

import ctypes
import os
import re
import subprocess

EXPECTED_BRANCHFS_VERSION = "0.1.2"
EXPECTED_BRANCHFS_PACKAGE = "vicoslab-branchfs-bin"


def _import_branchfs_bin():
    try:
        import branchfs_bin  # type: ignore
    except ImportError:
        return None
    return branchfs_bin


def packaged_branchfs_bin():
    """Return the packaged vicoslab BranchFS binary path, if installed."""
    module = _import_branchfs_bin()
    if module is None:
        return None
    path_fn = getattr(module, "branchfs_path", None)
    if path_fn is None:
        return None
    path = path_fn()
    if path and os.path.exists(path):
        return path
    return None


def packaged_branchfs_version():
    module = _import_branchfs_bin()
    if module is None:
        return None
    return getattr(module, "__version__", None)


def default_branchfs_bin():
    """Default executable for generated/runtime configs."""
    return packaged_branchfs_bin() or "branchfs"


def is_packaged_branchfs_bin(path):
    packaged = packaged_branchfs_bin()
    if not path or not packaged:
        return False
    try:
        return os.path.samefile(path, packaged)
    except OSError:
        return os.path.abspath(path) == os.path.abspath(packaged)


def libfuse3_status():
    """Return ``(ok, detail)`` for the dynamic libfuse3 runtime dependency."""
    for candidate in ("libfuse3.so.3", "libfuse3.so"):
        try:
            ctypes.CDLL(candidate)
            return True, candidate
        except OSError as exc:
            last_error = str(exc)
    return False, last_error


def probe_branchfs_version(binary, run=None, timeout=5):
    """Best-effort ``branchfs --version`` probe.

    Older BranchFS builds did not expose ``--version``; callers should treat
    ``None`` as unknown and warn without failing setup.
    """
    runner = run or subprocess.run
    try:
        proc = runner([binary, "--version"], stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 or not output:
        return None
    match = re.search(r"\bbranchfs\s+([0-9]+(?:\.[0-9]+)*(?:[-+][A-Za-z0-9_.-]+)?)",
                      output)
    if match:
        return match.group(1)
    return None
