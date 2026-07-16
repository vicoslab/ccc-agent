"""Session orchestration for ccc-agent run.

The trusted launcher flow (first milestone: process-exit completion)::

    create session -> create+mount branch bundle -> run agent ->
    freeze -> status -> policy -> auto-commit | pending-review | abort ->
    review artifacts -> unmount

Confinement is a property of *how* the agent is launched, not of the lifecycle:
``bwrap`` mode wraps the command in a rootless user+mount+pid namespace (the
real boundary); ``none`` mode just runs it with its cwd inside the view (debug
only -- not a boundary).  Either way the runner owns the lifecycle and the
commit decision, and the agent process never does.
"""

import binascii
import hashlib
import hmac
import json
import os
import re
import selectors
import shutil
import socket
import stat
import subprocess

from . import artifacts
from .branchfs import StatusReport, _mountinfo_entry
from .commit_failures import (clear_permission_failures,
                              is_permission_denied,
                              permission_failure_record,
                              prune_backend_change,
                              remember_permission_failures,
                              store_paths)
from .control import ControlServer
from .paths import is_within, normalize
from .policy import (ABORT, AUTO_COMMIT, NO_CHANGES, PENDING_REVIEW,
                     PolicyConfig, PolicyDecision, evaluate, split_ignored)
from .previous_commits import split_previously_committed_changes
from .route_manager import DeltaRouteManager
from .session import (TERMINAL_STATES, ProtectedRoot, Session, is_remote_bridge,
                      remote_bridge_agent_kind)
from .turn import TurnController
from .workspace import WorkspaceAdmissionPolicy

ENV_SESSION = "CCC_AGENT_SESSION"
ENV_STATE_DIR = "CCC_AGENT_STATE_DIR"
ENV_CONTROL_SOCK = "CCC_AGENT_CONTROL_SOCK"
ENV_CONTROL_TOKEN = "CCC_AGENT_CONTROL_TOKEN"
ENV_HOOK_TOKEN = "CCC_AGENT_HOOK_TOKEN"
ENV_HOOK_SESSION = "CCC_AGENT_HOOK_SESSION"
ENV_SHIM_UNDERLYING_PATH = "CCC_AGENT_SHIM_UNDERLYING_PATH"
ENV_LIFECYCLE_SOCKET = "CCC_AGENT_LIFECYCLE_SOCKET"
ENV_BOOTSTRAP_SECONDS = "CCC_AGENT_BOOTSTRAP_SECONDS"
ENV_STABILITY_SECONDS = "CCC_AGENT_STABILITY_SECONDS"
ENV_DETACH_SECONDS = "CCC_AGENT_DETACH_SECONDS"
ENV_HARDEN_CLIENT = "CCC_AGENT_HARDEN_CLIENT"
ENV_MCP_REGISTER_CLIENT = "CCC_AGENT_MCP_REGISTER_CLIENT"
ENV_CONFIRM_LAUNCH_WORKSPACE = "CCC_AGENT_CONFIRM_LAUNCH_WORKSPACE"
ENV_CLIENT_PRELOAD = "CCC_AGENT_CLIENT_PRELOAD"
ENV_ROUTE_VENDOR = "CCC_AGENT_ROUTE_VENDOR"
ENV_REAL_BWRAP = "CCC_AGENT_REAL_BWRAP"
ENV_BWRAP_BOUND_PROC = "CCC_AGENT_BWRAP_BOUND_PROC"

# Values from an enclosing/stale ccc-agent session must never become authority in
# a new session.  Remove them before assigning this launch's fresh identity and
# control credentials.  This is intentionally narrow: container/runtime values
# and external API credentials are inherited unless the operator explicitly
# lists them in bwrap_unsetenv.
TRANSIENT_INTERNAL_ENV = (
    ENV_SESSION, ENV_STATE_DIR, ENV_CONTROL_SOCK, ENV_CONTROL_TOKEN,
    ENV_HOOK_TOKEN, ENV_HOOK_SESSION, ENV_LIFECYCLE_SOCKET,
    ENV_BOOTSTRAP_SECONDS, ENV_STABILITY_SECONDS, ENV_DETACH_SECONDS,
    ENV_HARDEN_CLIENT, ENV_MCP_REGISTER_CLIENT,
    ENV_CONFIRM_LAUNCH_WORKSPACE, ENV_CLIENT_PRELOAD,
    ENV_ROUTE_VENDOR, ENV_REAL_BWRAP, ENV_BWRAP_BOUND_PROC,
)
BWRAP_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CODEX_BWRAP_ADAPTER = os.path.join(os.path.dirname(__file__), "assets",
                                   "codex", "bwrap")

# Where the per-turn control socket is bind-mounted INSIDE the bwrap sandbox.
# The host-side socket lives under the state dir, outside the sandbox.  Keep the
# in-sandbox path under private /tmp rather than /run: default container /run is
# now bound into the sandbox and may be root-owned, so bwrap cannot create
# /run/ccc-agent as an unprivileged user.
SANDBOX_CONTROL_SOCK = "/tmp/ccc-agent/control.sock"
SANDBOX_LIFECYCLE_SOCK = "/tmp/ccc-agent/lifecycle.sock"
SANDBOX_ADAPTIVE_RUNNER = "/tmp/ccc-agent/adaptive_pid1.py"
SANDBOX_CODEX_WORKSPACE = "/tmp/ccc-agent/codex_workspace.py"
SANDBOX_SESSION_ENV = "/tmp/ccc-agent/session-env.json"
SANDBOX_HARDENING_LIBRARY = "/opt/ccc-agent/libccc-client-hardening.so"
SANDBOX_CODEX_BWRAP_MARKER = "/tmp/ccc-agent/codex-external-sandbox"

# Run the sandbox command under a tiny PID-1 lifecycle wrapper.  Without this,
# bubblewrap's default PID-1 reaper keeps the namespace alive until every helper
# process exits.  Interactive agents such as Claude Code can leave short-lived or
# stuck helper processes behind after the foreground UI exits; then ccc-agent
# remains blocked in subprocess.run(bwrap ...) and never reaches finalization.
# With --as-pid-1 below, this wrapper is namespace init; when the foreground
# agent child exits, the wrapper exits with the same status and the kernel tears
# down any remaining processes in that PID namespace.
BWRAP_AGENT_RUNNER_ARG0 = "ccc-agent-runner"
BWRAP_AGENT_RUNNER = r"""
import ctypes
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading

command = sys.argv[2:]
if not command:
    sys.exit(127)


def set_signal(sig, handler):
    try:
        signal.signal(sig, handler)
    except (AttributeError, OSError, RuntimeError, ValueError):
        pass


# This wrapper is PID 1.  Keep terminal Ctrl-C / Ctrl-\\ for the foreground
# agent child; the wrapper only owns namespace teardown after that child exits.
set_signal(signal.SIGINT, signal.SIG_IGN)
set_signal(signal.SIGQUIT, signal.SIG_IGN)


def restore_child_signals():
    set_signal(signal.SIGINT, signal.SIG_DFL)
    set_signal(signal.SIGQUIT, signal.SIG_DFL)
    set_signal(signal.SIGTERM, signal.SIG_DFL)
    set_signal(signal.SIGHUP, signal.SIG_DFL)


def register_initial_client(pid):
    if os.environ.get("CCC_AGENT_MCP_REGISTER_CLIENT") != "1":
        return False
    path = os.environ.get("CCC_AGENT_CONTROL_SOCK")
    token = os.environ.get("CCC_AGENT_CONTROL_TOKEN")
    if not path or not token:
        raise RuntimeError("missing CCC control registration environment")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(5.0)
        conn.connect(path)
        request = {"op": "mcp-register-client", "token": token,
                   "pid": int(pid)}
        conn.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        data = b""
        while b"\n" not in data and len(data) <= 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        response = json.loads(data.split(b"\n", 1)[0].decode())
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "registration rejected")
    finally:
        conn.close()
    return True


def harden_runner_transport():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def confirm_runner_workspace(paths):
    control_path = os.environ.get("CCC_AGENT_CONTROL_SOCK")
    token = os.environ.get("CCC_AGENT_CONTROL_TOKEN")
    if isinstance(paths, str):
        paths = [paths]
    if (not control_path or not token or not isinstance(paths, list) or
            any(not isinstance(path, str) or not os.path.isabs(path)
                for path in paths)):
        raise RuntimeError("workspace roots/control environment are invalid")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(5.0)
        conn.connect(control_path)
        request = {"op": "turn-confirm-workspace-roots", "token": token,
                   "paths": paths}
        conn.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
        data = b""
        while b"\n" not in data and len(data) <= 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        response = json.loads(data.split(b"\n", 1)[0].decode())
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "workspace rejected")
    finally:
        conn.close()


client_env = None
client_preload = os.environ.pop("CCC_AGENT_CLIENT_PRELOAD", None)
if client_preload:
    client_env = os.environ.copy()
    client_env["LD_PRELOAD"] = client_preload
    client_env["CCC_AGENT_HARDEN_CLIENT"] = "1"


is_codex_app_server = (os.path.basename(command[0]) == "codex" and
                       "app-server" in command[1:])
try:
    child = subprocess.Popen(
        command, preexec_fn=restore_child_signals, env=client_env,
        stdin=subprocess.PIPE if is_codex_app_server else None,
        stdout=subprocess.PIPE if is_codex_app_server else None)
except OSError as exc:
    print("ccc-agent-runner: failed to exec %s: %s" % (command[0], exc),
          file=sys.stderr)
    sys.exit(127 if exc.errno == errno.ENOENT else 126)

trusted_runner = False
try:
    if register_initial_client(child.pid):
        harden_runner_transport()
        trusted_runner = True
        if os.environ.get("CCC_AGENT_CONFIRM_LAUNCH_WORKSPACE") == "1":
            confirm_runner_workspace(os.getcwd())
except Exception as exc:
    print("ccc-agent-runner: initial client/workspace registration unavailable: %s" % exc,
          file=sys.stderr)


def forward_signal(sig, _frame):
    try:
        child.send_signal(sig)
    except Exception:
        pass


set_signal(signal.SIGTERM, forward_signal)
set_signal(signal.SIGHUP, forward_signal)

if is_codex_app_server:
    from ccc_agent.codex_workspace import CodexWorkspaceMonitor
    launch_cwd = (os.getcwd() if
                  os.environ.get("CCC_AGENT_CONFIRM_LAUNCH_WORKSPACE") == "1"
                  else None)
    monitor = CodexWorkspaceMonitor(launch_cwd)

    def forward_app_server_input():
        try:
            for line in sys.stdin.buffer:
                try:
                    monitor.observe_client(json.loads(line))
                except (TypeError, ValueError):
                    pass
                child.stdin.write(line)
                child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass

    input_thread = threading.Thread(target=forward_app_server_input,
                                    daemon=True)
    input_thread.start()
    try:
        for line in child.stdout:
            try:
                roots = monitor.observe_server(json.loads(line))
                if trusted_runner and roots is not None:
                    confirm_runner_workspace(roots)
            except (TypeError, ValueError, RuntimeError) as exc:
                print("ccc-agent-runner: Codex workspace observation failed: %s" % exc,
                      file=sys.stderr)
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    finally:
        try:
            child.stdout.close()
        except OSError:
            pass

returncode = child.wait()
if returncode < 0:
    returncode = 128 - returncode
sys.exit(returncode)
""".strip()


class ResumeError(Exception):
    """Raised when an existing session cannot be resumed safely."""


class RootSpec(object):
    """Template for one protected root; branch/mount are filled per session."""

    __slots__ = ("name", "base", "store", "visible", "home_subdir", "mount",
                 "hide_paths")

    def __init__(self, name, base, store, visible, home_subdir=None,
                 mount=None, hide_paths=()):
        self.name = name
        self.base = base
        self.store = store
        self.visible = visible
        self.home_subdir = home_subdir
        self.mount = mount  # default: <state>/<session>/mounts/<name>
        self.hide_paths = list(hide_paths)

    def materialize(self, session_id, state_dir, mount_dir=None):
        root_mount_dir = mount_dir or os.path.join(state_dir, session_id,
                                                   "mounts")
        mount = self.mount or os.path.join(root_mount_dir, self.name)
        return ProtectedRoot(name=self.name, base=self.base, store=self.store,
                             branch=session_id, mount=mount,
                             visible=self.visible,
                             home_subdir=self.home_subdir,
                             hide_paths=self.hide_paths)


# "bwrap" is the real containment boundary (rootless user+mount+pid namespace).
# "none" runs the agent with only its cwd inside the view and is NOT a security
# boundary -- absolute-path writes bypass the view entirely; keep it for
# debugging the policy/commit pipeline without bwrap, never for confinement.
CONFINEMENT_MODES = ("none", "bwrap")
BWRAP_PROC_MODES = ("bind", "ro", "fresh")
LIFECYCLE_MODES = ("foreground", "adaptive")


class RunnerConfig(object):
    def __init__(self, store, backend, alias_map, owner, agent_kind,
                 agent_command, workspace, policy, roots, launch_cwd=None,
                 completion="process-exit", confinement="none",
                 bwrap_bin="bwrap", bwrap_proc_mode="bind",
                 bwrap_ro_binds=(), bwrap_setenv=None, bwrap_unsetenv=(),
                 per_turn=None,
                 container_run_access=True,
                 cred_mounts=(), cred_mask=(), cred_env=None,
                 bwrap_uid=None, bwrap_gid=None, agent_plugins=None,
                 agent_state_binds=None, protect_agent_state=False,
                 ensure_agent_state_dirs=False, on_session_start=None,
                 server_mode=False, lifecycle="foreground",
                 adaptive_bootstrap_seconds=10.0,
                 adaptive_stability_seconds=0.2,
                 adaptive_detach_seconds=2.0,
                 mcp_client_hardening_library=None,
                 workspace_admission_roots=None,
                 allow_protected_root_workspace=False,
                 session_delta_routing=False,
                 session_delta_routing_vendors=("codex",),
                 require_existing_workspace=True):
        self.store = store              # SessionStore
        self.backend = backend          # BranchfsCli or FakeBranchFS
        self.alias_map = alias_map
        self.owner = owner
        self.agent_kind = agent_kind
        self.agent_command = list(agent_command)
        self.roots = list(roots)
        self._operator_workspace_policy = WorkspaceAdmissionPolicy(
            self.roots, alias_map,
            workspace_admission_roots=[root.visible for root in self.roots],
            allow_protected_root_workspace=True)
        if not isinstance(allow_protected_root_workspace, bool):
            raise ValueError(
                "allow_protected_root_workspace must be true or false")
        self.workspace_admission_policy = WorkspaceAdmissionPolicy(
            self.roots, alias_map,
            workspace_admission_roots=workspace_admission_roots,
            allow_protected_root_workspace=allow_protected_root_workspace)
        self.workspace_admission_roots = list(
            self.workspace_admission_policy.workspace_admission_roots)
        self.allow_protected_root_workspace = allow_protected_root_workspace
        if not isinstance(session_delta_routing, bool):
            raise ValueError("session_delta_routing must be true or false")
        self.session_delta_routing = session_delta_routing
        if isinstance(session_delta_routing_vendors, str):
            raise ValueError("session_delta_routing_vendors must be an array")
        vendors = []
        for vendor in session_delta_routing_vendors or ():
            vendor = str(vendor).strip().lower()
            if vendor not in ("codex", "claude", "hermes"):
                raise ValueError("unsupported session delta routing vendor %r" %
                                 vendor)
            if vendor not in vendors:
                vendors.append(vendor)
        self.session_delta_routing_vendors = tuple(vendors)
        self._launch_workspace_admission = None
        if workspace:
            self._launch_workspace_admission = (
                self._operator_workspace_policy.admit(
                    workspace,
                    require_existing=bool(require_existing_workspace)))
            workspace = self._launch_workspace_admission["visible_path"]
        self.workspace = workspace
        # A server may need to start in the SSH launch directory before an
        # inner agent SessionStart hook identifies its real workspace. Keep
        # that process cwd separate from commit-policy workspace metadata.
        self.launch_cwd = launch_cwd if launch_cwd is not None else workspace
        if not self.launch_cwd:
            raise ValueError("launch_cwd is required when workspace is unset")
        self.policy = dict(policy)
        self.policy["workspace_admission_roots"] = list(
            self.workspace_admission_roots)
        self.policy["allow_protected_root_workspace"] = (
            self.allow_protected_root_workspace)
        self.policy["session_delta_routing"] = self.session_delta_routing
        self.policy["session_delta_routing_vendors"] = list(
            self.session_delta_routing_vendors)
        route_uid = bwrap_uid if bwrap_uid is not None else os.getuid()
        self.policy["sandbox_route_root"] = (
            "/run/user/%d/ccc-agent-routes" % int(route_uid))
        if self._launch_workspace_admission is not None:
            self.policy["launch_workspace_admission"] = dict(
                self._launch_workspace_admission)
        if "allowed_scopes" not in self.policy:
            self.policy["allowed_scopes"] = ([workspace] if workspace else [])
        PolicyConfig.from_dict(self.policy)  # validate early
        self.completion = completion
        if confinement not in CONFINEMENT_MODES:
            raise ValueError("unknown confinement %r (expected one of %s)"
                             % (confinement, ", ".join(CONFINEMENT_MODES)))
        self.confinement = confinement
        self.bwrap_bin = bwrap_bin
        if bwrap_proc_mode not in BWRAP_PROC_MODES:
            raise ValueError("unknown bwrap_proc_mode %r (expected one of %s)"
                             % (bwrap_proc_mode, ", ".join(BWRAP_PROC_MODES)))
        self.bwrap_proc_mode = bwrap_proc_mode
        # Extra read-only paths to re-expose inside the sandbox AFTER the view
        # binds (so the agent's own runtime + creds, which live under the real
        # $HOME/storage the view hides, become reachable again). The bwrap child
        # inherits the complete invocation environment by default. Operators can
        # explicitly remove names, then apply trusted value overrides.
        self.bwrap_ro_binds = list(bwrap_ro_binds)
        self.bwrap_setenv = dict(bwrap_setenv or {})
        self.bwrap_unsetenv = []
        for name in bwrap_unsetenv or ():
            name = str(name)
            if not name or "=" in name or "\x00" in name:
                raise ValueError("invalid bwrap_unsetenv name %r" % name)
            self.bwrap_unsetenv.append(name)
        # By default the sandbox inherits selected runtime namespaces from the
        # existing CCC container: /run and a read-only /var for
        # deployment-provided sockets (including conventional /var/run paths),
        # plus a device-capable /dev bind for container-visible devices such as
        # /dev/fuse. These are still the outer container's namespaced resources,
        # not raw host views. Use --full-isolation / container_run_access=false
        # to omit this ambient container runtime view and fall back to bwrap's
        # isolated /dev.
        self.container_run_access = bool(container_run_access)
        # bwrap needs no extra container privilege and no uid/gid: it mints
        # namespace-scoped CAP_SYS_ADMIN from an unprivileged user namespace
        # while the sandbox process still runs as the real uid by default.
        # Per-turn (Stop-boundary) commit via the control socket; defaults on
        # for bwrap (the interactive case) and off otherwise.  `none` can opt in
        # for debugging (the hook reaches the host socket directly).
        self.per_turn = (confinement == "bwrap") if per_turn is None else per_turn
        # Credential overrides: agent state dirs (e.g. ~/.codex, ~/.claude,
        # ~/.hermes) are normally direct shared rw binds, outside BranchFS.
        # cred_mounts is only for narrow special-case read-only overlays;
        # cred_mask hides individual secret files (overmounted with /dev/null);
        # cred_env lets the supervisor read a host auth file and pass a value via
        # env so that particular file never enters the sandbox.
        self.cred_mounts = list(cred_mounts)
        self.cred_mask = list(cred_mask)
        self.cred_env = dict(cred_env or {})
        self.bwrap_uid = bwrap_uid
        self.bwrap_gid = bwrap_gid
        # Native per-agent plugin injection (replaces the old config-file
        # overlay). Each value is a spec: {src, sandbox_path, argv?, setenv?,
        # ensure_dirs?}. Only the spec matching the contained agent is injected.
        self.agent_plugins = dict(agent_plugins or {})
        # By default the agent tools' own runtime/config state is shared system
        # state, not BranchFS-protected project data.  Paths are bound rw over
        # the branch view so Codex/Claude/Hermes own their config/session/cache
        # concurrency (including Claude's ~/.claude.json and .local/.cache
        # runtime dirs).  --protect-agent-state/config protect_agent_state omits
        # these binds for users who intentionally want agent state in review.
        self.agent_state_binds = (list(agent_state_binds)
                                  if agent_state_binds is not None
                                  else _default_agent_state_binds(owner))
        self.protect_agent_state = bool(protect_agent_state)
        self.ensure_agent_state_dirs = bool(ensure_agent_state_dirs)
        self.on_session_start = on_session_start
        self.server_mode = bool(server_mode)
        self.mcp_client_hardening_library = (
            os.path.abspath(str(mcp_client_hardening_library))
            if mcp_client_hardening_library else None)
        if lifecycle not in LIFECYCLE_MODES:
            raise ValueError("unknown lifecycle %r (expected one of %s)"
                             % (lifecycle, ", ".join(LIFECYCLE_MODES)))
        if lifecycle == "adaptive" and confinement != "bwrap":
            raise ValueError("adaptive lifecycle requires bwrap confinement")
        self.lifecycle = lifecycle
        self.adaptive_bootstrap_seconds = self._positive_seconds(
            "adaptive_bootstrap_seconds", adaptive_bootstrap_seconds)
        self.adaptive_stability_seconds = self._positive_seconds(
            "adaptive_stability_seconds", adaptive_stability_seconds)
        self.adaptive_detach_seconds = self._positive_seconds(
            "adaptive_detach_seconds", adaptive_detach_seconds)

    @staticmethod
    def _positive_seconds(name, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("%s must be a positive number" % name)
        if value <= 0:
            raise ValueError("%s must be a positive number" % name)
        return value


def _agent_cwd(session, alias_map, launch_cwd=None):
    """Map the process launch cwd into the mounted branch view."""
    launch_cwd = launch_cwd or session.workspace
    workspace = alias_map.canonicalize(launch_cwd)
    for root in session.protected_roots.values():
        visible = alias_map.canonicalize(root.visible)
        if is_within(workspace, visible):
            rel = os.path.relpath(workspace, visible)
            return (root.mount if rel == "." else
                    os.path.join(root.mount, rel))
    raise ValueError("launch cwd %s is not under any protected root"
                     % launch_cwd)


def _primary_root(session, alias_map, launch_cwd=None):
    """The protected root whose visible path contains the process launch cwd."""
    launch_cwd = launch_cwd or session.workspace
    workspace = alias_map.canonicalize(launch_cwd)
    for root in session.protected_roots.values():
        if is_within(workspace, alias_map.canonicalize(root.visible)):
            return root
    raise ValueError("launch cwd %s is not under any protected root"
                     % launch_cwd)


# System paths exposed read-only inside the bwrap sandbox.  The agent sees the
# OS read-only, its BranchFS view read-write, and (by default) the CCC
# container's existing /run runtime namespace.  It still does not see the real
# underlay, BranchFS store, or supervisor state. On merged-/usr systems
# /bin,/sbin,/lib,/lib64 are symlinks into /usr and must be recreated as
# symlinks, not bound as dirs.
BWRAP_RO_DIRS = ("/usr", "/etc", "/opt")
BWRAP_USRMERGE_DIRS = ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")
CONTAINER_RUNTIME_GROUP_SOCKET_PATHS = ("/var/run/docker.sock", "/run/docker.sock")
AGENT_STATE_DIRS = (
    ".codex", ".claude", ".hermes",
    ".local/share/claude", ".local/state/claude",
    ".cache/claude-cli-nodejs",
)
AGENT_STATE_FILES = (".claude.json", ".local/bin/codex", ".local/bin/claude")
AGENT_STATE_PATHS = AGENT_STATE_DIRS + AGENT_STATE_FILES
CODEX_RUNTIME_STATE_PATHS = (
    ".local/bin/codex",
)
CLAUDE_RUNTIME_STATE_PATHS = (
    ".claude", ".claude.json", ".local/bin/claude",
    ".local/share/claude", ".local/state/claude",
    ".cache/claude-cli-nodejs",
)


def _default_agent_state_binds(owner):
    home = "/home/%s" % owner
    return [os.path.join(home, name) for name in AGENT_STATE_PATHS]


def _bind_parts(entry):
    entry = str(entry)
    return entry.split(":", 1) if ":" in entry else (entry, entry)


def _optional_ro_bind(entry):
    """Return a safe optional read-only bind triple, or None to skip it.

    Entries are ``src`` or ``src:dest``.  Optional agent-runtime/config binds
    often name paths under ``/home/<user>`` which CCC may implement as symlinks
    into ``/storage/user``.  If bwrap is asked to mount on the symlink itself
    *after* the BranchFS view is already bound over /home or /storage, the
    destination symlink is resolved inside /newroot and can point at a path that
    does not exist there.  Resolve symlinked optional binds on the trusted host
    first and bind the real target path read-only instead.

    Missing optional paths are skipped here instead of passed through as
    ``--ro-bind-try``.  In particular, a broken symlink has no valid target and
    should not be added to the sandbox command at all.
    """
    src, dest = entry.split(":", 1) if ":" in entry else (entry, entry)
    explicit_dest = ":" in entry

    resolved_src = os.path.realpath(src)
    if not os.path.exists(resolved_src):
        return None

    if explicit_dest:
        resolved_dest = os.path.realpath(dest)
        # If the requested destination itself traverses a symlink, bind onto
        # that target instead.  If the target is absent, the symlink is broken
        # for our purposes and the optional bind should be skipped.
        if os.path.normpath(resolved_dest) != os.path.normpath(dest):
            if not os.path.exists(resolved_dest):
                return None
            dest = resolved_dest
    else:
        dest = resolved_src

    return (resolved_src, dest)


def _optional_rw_agent_state_bind(entry):
    """Return a safe optional read-write agent-state bind, or None.

    Shared agent state is mounted after the BranchFS home/storage view.  Most
    entries are directories, but Claude Code also keeps a top-level
    ``~/.claude.json`` file.  Bind existing directories *and* files; skip missing
    optional paths so bwrap is never handed a source that would make launch fail.

    Like read-only optional binds, a destination such as ``~/.claude`` can be a
    symlink to a storage path.  Passing the symlink itself to bwrap lets bwrap
    resolve it inside the new root, after /home and /storage have been overlaid,
    which can fail with ENOENT.  Resolve destination symlinks on the trusted host
    first and bind onto the real target path instead.
    """
    src, dest = _bind_parts(entry)
    if not src or not dest or not src.startswith("/") or not dest.startswith("/"):
        return None
    resolved_src = os.path.realpath(src)
    if not (os.path.isdir(resolved_src) or os.path.isfile(resolved_src)):
        return None
    resolved_dest = os.path.realpath(dest)
    if os.path.normpath(resolved_dest) != os.path.normpath(dest):
        if not os.path.exists(resolved_dest):
            return None
        dest = resolved_dest
    return (resolved_src, dest)


def _agent_token(value):
    """Normalize an agent kind or executable path to a plugin lookup token."""
    if not value:
        return ""
    return os.path.basename(str(value)).lower()


def _plugin_key_for_token(config, token):
    """Return the configured plugin key matching token, preserving key spelling."""
    token = (token or "").lower()
    for agent in config.agent_plugins:
        if agent.lower() == token:
            return agent
    return None


def _plugin_token_for_agent_kind(value):
    """Map durable remote session labels back to their configured agent key."""
    token = _agent_token(value)
    for suffix in ("-remote-bridge", "-remote"):
        if token.endswith(suffix) and len(token) > len(suffix):
            return token[:-len(suffix)]
    return token


def _inferred_agent_plugin_names(config):
    """Agent plugin candidates inferred from the executable path only."""
    names = set()
    if config.agent_command:
        names.add(_agent_token(config.agent_command[0]))
    return names


def _direct_agent_command_matches(config, agent):
    """Return true when argv[0] is the agent CLI that can accept plugin argv.

    SSH routers often label containment sessions as codex/claude because the
    payload eventually starts those tools, but the direct command is a shell or
    server helper such as `/bin/bash -c ...` or `~/.claude/remote/.../server`.
    Those commands cannot accept opt-in launch flags; only decorate direct agent
    CLI invocations when an operator explicitly configured argv activation.
    """
    if not config.agent_command:
        return False
    return _agent_token(config.agent_command[0]) == str(agent or "").lower()


def _plugin_has_argv_activation(spec):
    """Return true only for activation that mutates the command argv.

    Environment such as ``CLAUDE_CODE_PLUGIN_SEED_DIR`` is safe and necessary
    for explicitly identified SSH/server wrappers whose eventual child is the
    requested agent. It must not make those wrappers fail plugin matching.
    """
    return bool(spec and spec.get("argv"))


def _matched_agent_plugin(config):
    """Return the validated plugin spec for the contained agent, or None.

    Specs without argv activation may be selected by explicit ``--agent`` even
    when the direct command is an SSH/server shell wrapper. This permits safe
    plugin binds and environment such as Claude's seed directory to reach the
    eventual child agent. Specs that append argv are selected only when argv[0]
    is the direct agent CLI.

    Returns None when no plugin matches, its trusted source is unavailable, or
    the direct command uses ``--bare``. Missing plugins degrade to authoritative
    process-exit review.
    """
    if "--bare" in config.agent_command:
        return None

    def validated(agent):
        spec = config.agent_plugins.get(agent)
        if not isinstance(spec, dict):
            return None
        src = spec.get("src")
        if not src or not os.path.isdir(os.path.realpath(src)):
            return None  # missing trusted asset: degrade to session-end review
        return spec

    explicit_kind = _plugin_token_for_agent_kind(config.agent_kind)
    if explicit_kind and explicit_kind != "command":
        explicit_agent = _plugin_key_for_token(config, explicit_kind)
        if explicit_agent:
            spec = validated(explicit_agent)
            if spec and (not _plugin_has_argv_activation(spec)
                         or _direct_agent_command_matches(config, explicit_agent)):
                return spec
            return None

    names = _inferred_agent_plugin_names(config)
    for agent in sorted(config.agent_plugins):
        if agent.lower() in names and _direct_agent_command_matches(config, agent):
            return validated(agent)
    return None


def _append_agent_plugin_binds(argv, spec):
    """Mount one matched plugin's trusted source read-only into the sandbox.

    The plugin source is root-owned/package-owned and always mounted read-only,
    so the untrusted agent can load it but never edit it.  ``ensure_dirs`` are
    created first so a mount target nested under an agent state dir (e.g.
    ~/.codex/plugins) exists inside the namespace.
    """
    if spec is None:
        return
    for directory in spec.get("ensure_dirs", ()):
        argv += ["--dir", directory]
    src = spec.get("src")
    sandbox_path = spec.get("sandbox_path")
    if src and sandbox_path:
        argv += ["--ro-bind", os.path.realpath(src), sandbox_path]


def _agent_command_with_plugin(command, spec):
    """Insert the plugin's activation flags right after the agent executable.

    e.g. ``agent -p x`` + ``--flag P`` -> ``agent --flag P -p x``.
    Setup-generated defaults do not use this for Codex or Claude; it remains an
    explicit operator escape hatch for custom plugin integrations.
    """
    command = list(command)
    if spec is None or not command:
        return command
    extra = list(spec.get("argv", ()))
    if not extra:
        return command
    return [command[0]] + extra + command[1:]


def _infra_ignore_paths_for(path, session, config):
    """Canonical policy ignore paths for supervisor-created sandbox plumbing.

    bwrap creates mountpoint directories for ``--dir``/``--ro-bind`` targets. If
    those targets sit under the BranchFS-backed home/storage view, BranchFS can
    report the mountpoints as branch deltas.  They are launcher infrastructure,
    not agent-authored work, so add exact/subtree ignores for them.  For paths
    below $HOME, include each ancestor below the home alias (for example a
    `~/.ccc-runtime/plugin` target adds `~/.ccc-runtime` and descendants)
    because real BranchFS may report parent directories as structural deltas.
    Agent-state homes have a narrower special case below.
    """
    if not path or not str(path).startswith("/"):
        return []
    try:
        canonical = config.alias_map.canonicalize(str(path))
        home = config.alias_map.canonicalize("/home/%s" % config.owner)
    except ValueError:
        return []

    # Only add ignores for paths that are actually inside one of this session's
    # protected roots.  Outside-view paths (/ccc-agent, /opt, /run, ...) cannot
    # become BranchFS deltas and should not clutter policy artifacts.
    protected = []
    for root in session.protected_roots.values():
        visible = config.alias_map.canonicalize(root.visible)
        if is_within(canonical, visible) and canonical != visible:
            protected.append(visible)
    if not protected:
        return []

    # Agent-state dirs are outside BranchFS in the default shared mode, so
    # plugin paths mounted inside them cannot become branch deltas and should
    # not add broad `.codex`/`.claude`/`.hermes` ignores.  In opt-in protected
    # mode, ignore only the infrastructure subpath, not the whole agent home,
    # so user/tool config/state remains reviewable.
    for entry in config.agent_state_binds:
        _src, dest = _bind_parts(entry)
        try:
            dest_canonical = config.alias_map.canonicalize(dest)
        except ValueError:
            continue
        if is_within(canonical, dest_canonical):
            return [canonical] if config.protect_agent_state else []

    if is_within(canonical, home) and canonical != home:
        rel = os.path.relpath(canonical, home)
        current = home
        paths = []
        for part in rel.split(os.sep):
            if not part or part == ".":
                continue
            current = os.path.join(current, part)
            paths.append(current)
        return paths
    return [canonical]


def _canonical_protected_path(path, session, config):
    """Return canonical path if it is inside a protected root, else None."""
    if not path or not str(path).startswith("/"):
        return None
    try:
        canonical = config.alias_map.canonicalize(str(path))
    except ValueError:
        return None
    for root in session.protected_roots.values():
        visible = config.alias_map.canonicalize(root.visible)
        if is_within(canonical, visible) and canonical != visible:
            return canonical
    return None


def _is_agent_kind(config, name):
    """Best-effort detection of a contained invocation for one agent kind."""
    token = name.lower()
    if _plugin_token_for_agent_kind(config.agent_kind) == token:
        return True
    return token in _inferred_agent_plugin_names(config)


def _is_claude_agent(config):
    """Best-effort detection of a contained Claude Code invocation."""
    return _is_agent_kind(config, "claude")


def _is_codex_agent(config):
    """Best-effort detection of a contained Codex invocation."""
    return _is_agent_kind(config, "codex")


def _mcp_admission_config(config):
    """Return expected process names and whether this direct launch is eligible.

    Agent labels routed through bash/SSH/server wrappers are insufficient: the
    official client must be the direct configured command under bwrap. An
    unsupported launch gets an unmatchable name so ordinary mutating control
    calls are still rejected while hooks retain lifecycle access.
    """
    direct = (_agent_token(config.agent_command[0])
              if config.agent_command else "")
    kind = _agent_token(config.agent_kind).split("-", 1)[0]
    recognized = direct if direct in ("claude", "codex", "hermes") else (
        kind if kind in ("claude", "codex", "hermes") else "")
    supported = bool(recognized and direct == recognized and
                     config.confinement == "bwrap")
    return ((recognized or "__unsupported_ccc_mcp_client__",), supported)


def _secure_root_owned_path(path, agent_uid):
    if not path or not os.path.isabs(path):
        return False
    current = os.path.abspath(path)
    try:
        while True:
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                return False
            if info.st_uid == agent_uid or info.st_mode & 0o022:
                return False
            if current == "/":
                break
            current = os.path.dirname(current)
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _secure_hardening_library(path, agent_uid=None):
    """Verify immutable ownership and the system-setup digest manifest."""
    if not path or not os.path.isabs(path):
        return False
    agent_uid = os.getuid() if agent_uid is None else int(agent_uid)
    manifest = path + ".sha256"
    if not (_secure_root_owned_path(path, agent_uid) and
            _secure_root_owned_path(manifest, agent_uid)):
        return False
    try:
        with open(manifest) as fh:
            expected = fh.read().strip().split()[0]
        if len(expected) != 64:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return hmac.compare_digest(expected.lower(), digest.hexdigest())
    except (OSError, IndexError, ValueError):
        return False


def _mcp_client_hardening(config):
    path = config.mcp_client_hardening_library
    agent_uid = (config.bwrap_uid if config.bwrap_uid is not None
                 else os.getuid())
    _expected, supported = _mcp_admission_config(config)
    return path if supported and _secure_hardening_library(path, agent_uid) else None


def _workspace_admission_policy(session, config):
    """Build the one admission policy used by trusted runtime root updates."""
    return WorkspaceAdmissionPolicy(
        session.protected_roots, config.alias_map,
        workspace_admission_roots=getattr(
            config, "workspace_admission_roots", None),
        allow_protected_root_workspace=getattr(
            config, "allow_protected_root_workspace", False))


def _codex_bwrap_adapter_binds(config, process_env):
    """Return contained-only bwrap masks for Codex's nested Linux sandbox.

    With ``bwrap_proc_mode=bind`` (or ``ro``), CCC unshares the outer PID
    namespace but exposes a procfs mounted for the parent container namespace.
    Nested bubblewrap then looks up its namespace PID in the wrong procfs and
    can block forever. A fresh procfs does not have that mismatch and keeps
    Codex's native nested sandbox unchanged.

    Codex may prefer a Conda/runtime-local bwrap over /usr/bin/bwrap, so mask
    every executable candidate in its actual launch PATH. Resolve symlinks to
    bind over the path Codex ultimately opens inside the BranchFS view.
    """
    if not _is_codex_agent(config) or config.bwrap_proc_mode == "fresh":
        return []

    adapter = os.path.realpath(CODEX_BWRAP_ADAPTER)
    if not os.path.isfile(adapter) or not os.access(adapter, os.X_OK):
        raise RuntimeError("Codex bwrap adapter is missing or not executable: %s"
                           % adapter)

    binds = []
    for target in _vendor_bwrap_paths(config, env=process_env):
        if target != adapter:
            binds.append((adapter, target))
    return binds


def _add_runtime_state_ignores(session, config, ignore, relpaths):
    home = "/home/%s" % config.owner
    for relpath in relpaths:
        canonical = _canonical_protected_path(os.path.join(home, relpath),
                                             session, config)
        if canonical and canonical not in ignore:
            ignore.append(canonical)


def _add_agent_runtime_state_ignores(session, config, ignore):
    """Drop narrowly-known agent runtime files from BranchFS review.

    Optional shared binds are skipped when the source is absent. If the agent
    then creates the path inside the BranchFS home view during this run, it is
    still runtime/config noise rather than a user deliverable. Add only exact
    known runtime paths (not broad ``.local`` or ``.cache`` parents) and only in
    the default shared-state mode; ``--protect-agent-state`` intentionally leaves
    agent state reviewable.
    """
    if config.protect_agent_state:
        return
    if _is_codex_agent(config):
        _add_runtime_state_ignores(session, config, ignore,
                                   CODEX_RUNTIME_STATE_PATHS)
    if _is_claude_agent(config):
        _add_runtime_state_ignores(session, config, ignore,
                                   CLAUDE_RUNTIME_STATE_PATHS)


def _add_session_infra_ignores(session, config):
    """Ignore ccc-agent-owned bind/plugin/mask targets inside branch views."""
    ignore = session.policy.setdefault("ignore_patterns", [])
    _add_agent_runtime_state_ignores(session, config, ignore)

    def add(path):
        for canonical in _infra_ignore_paths_for(path, session, config):
            if canonical not in ignore:
                ignore.append(canonical)

    for entry in list(config.bwrap_ro_binds) + list(config.cred_mounts):
        bind = _optional_ro_bind(entry)
        if bind is not None:
            add(bind[1])
    for masked in config.cred_mask:
        if os.path.exists(masked):
            add(masked)

    plugin_spec = _matched_agent_plugin(config)
    if plugin_spec is not None:
        add(plugin_spec.get("sandbox_path"))
        for directory in plugin_spec.get("ensure_dirs", ()):
            add(directory)


def _agent_state_file_path(path):
    """Return True when path names a known single-file agent-state bind."""
    if not path:
        return False
    normalized = os.path.normpath(str(path))
    for relpath in AGENT_STATE_FILES:
        rel = os.path.normpath(relpath)
        if os.path.basename(rel) == rel:
            if os.path.basename(normalized) == rel:
                return True
        elif normalized.endswith(os.sep + rel):
            return True
    return False


def _ensure_shared_agent_state_dirs(config):
    """Create real shared agent-state dirs before BranchFS branch creation.

    This makes the default same-path binds visible as inherited directories in
    the branch view, so bwrap does not create `.codex`/`.claude`/`.hermes`
    mountpoint deltas while preparing the sandbox.
    """
    if config.protect_agent_state or not config.ensure_agent_state_dirs:
        return
    for entry in config.agent_state_binds:
        src, dest = _bind_parts(entry)
        if not src or not str(src).startswith("/"):
            continue
        resolved = os.path.realpath(src)
        if os.path.exists(resolved):
            # Existing files such as ~/.claude.json are shared binds too, but
            # ensure_agent_state_dirs must never turn them into directories.
            continue
        if _agent_state_file_path(src) or _agent_state_file_path(dest):
            continue
        os.makedirs(resolved, mode=0o700, exist_ok=True)


def _agent_state_symlink_target_dir(path):
    """Return the agent-state directory containing a symlink target, if any.

    Users sometimes keep a file such as ``~/.codex/config.toml`` as an absolute
    symlink to another shared location like ``/storage/user/.codex/config.toml``.
    Binding only ``~/.codex`` over the BranchFS home view is not enough: inside
    bwrap, following that symlink reaches the protected ``/storage`` view unless
    the target agent-state directory is also rebound as shared runtime state.

    Keep this intentionally narrow: only known agent-state directory suffixes
    (``.codex``, ``.claude``, ``.hermes``, and Claude's ``.local``/cache dirs)
    are auto-bound.  A symlink from an agent state dir to an arbitrary
    project/data path should remain protected.
    """
    if not path or not str(path).startswith("/"):
        return None
    normalized = os.path.normpath(os.path.realpath(path))
    for relpath in sorted(AGENT_STATE_DIRS, key=len, reverse=True):
        marker = os.sep + relpath.replace("/", os.sep)
        start = normalized.find(marker)
        while start != -1:
            end = start + len(marker)
            if end == len(normalized) or normalized[end] == os.sep:
                candidate = normalized[:end]
                return candidate if os.path.isdir(candidate) else None
            start = normalized.find(marker, start + 1)
    return None


def _shared_agent_state_symlink_target_binds(config):
    """Extra same-path rw binds for top-level symlink targets in agent homes.

    Keep this shallow on purpose.  Agent state directories can contain large
    caches/history trees, and bwrap setup runs on every contained session.  The
    compatibility case this protects is a top-level config symlink such as
    ``~/.codex/config.toml -> /storage/user/.codex/config.toml``; recursively
    walking all runtime/cache contents would make unit tests and launches scale
    with unrelated agent history size.
    """
    if config.protect_agent_state:
        return []
    binds = []
    seen = set()
    for entry in config.agent_state_binds:
        src, _dest = _bind_parts(entry)
        if not src or not str(src).startswith("/"):
            continue
        root = os.path.realpath(src)
        if not os.path.isdir(root):
            continue
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for dir_entry in entries:
            try:
                is_link = dir_entry.is_symlink()
            except OSError:
                continue
            if not is_link:
                continue
            target_dir = _agent_state_symlink_target_dir(dir_entry.path)
            if target_dir and target_dir != root and target_dir not in seen:
                seen.add(target_dir)
                binds.append((target_dir, target_dir))
    return binds


def _append_shared_agent_state_binds(argv, config):
    """Bind Codex/Claude/Hermes state rw over BranchFS-backed views."""
    if config.protect_agent_state:
        return
    for entry in config.agent_state_binds:
        bind = _optional_rw_agent_state_bind(entry)
        if bind is not None:
            argv += ["--bind", bind[0], bind[1]]
    for src, dest in _shared_agent_state_symlink_target_binds(config):
        argv += ["--bind", src, dest]


def _container_runtime_socket_gid():
    """Return a supplementary runtime-socket gid that bwrap should map.

    Rootless bwrap can map only the uid/gid we ask it to use. Supplementary
    groups from the outer container are not preserved, so a Docker socket that is
    accessible outside ccc-agent only through a supplementary group can become
    `nogroup`/inaccessible inside the user namespace.  If the current process can
    access a known runtime socket through one of its supplementary groups, map
    that socket gid as the sandbox's primary gid.  Explicit ``bwrap_gid`` still
    wins for deployments that prefer a fixed group.
    """
    uid = os.getuid()
    primary_gid = os.getgid()
    supplementary = set(os.getgroups())
    for path in CONTAINER_RUNTIME_GROUP_SOCKET_PATHS:
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISSOCK(st.st_mode):
            continue
        if st.st_gid == primary_gid:
            return None
        if st.st_uid == uid and (st.st_mode & stat.S_IWUSR):
            return None
        if st.st_gid not in supplementary:
            continue
        if not (st.st_mode & stat.S_IWGRP):
            continue
        if os.access(path, os.R_OK | os.W_OK):
            return st.st_gid
    return None


def _bwrap_gid(config):
    if config.bwrap_gid is not None:
        return config.bwrap_gid
    if config.container_run_access:
        socket_gid = _container_runtime_socket_gid()
        if socket_gid is not None:
            return socket_gid
    return os.getgid()


_SIMPLE_EXEC_RE = re.compile(
    r"^\s*exec\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))", re.MULTILINE)


def _resolved_vendor_launcher(config, env=None):
    source_env = os.environ if env is None else env
    command = config.agent_command[0] if config.agent_command else None
    if not command:
        return None
    path = command if os.path.isabs(command) else shutil.which(
        command, path=(source_env.get(ENV_SHIM_UNDERLYING_PATH) or
                       source_env.get("PATH") or BWRAP_DEFAULT_PATH))
    if not path:
        return None
    path = os.path.realpath(path)
    try:
        if os.path.getsize(path) <= 16384:
            with open(path, errors="replace") as fh:
                match = _SIMPLE_EXEC_RE.search(fh.read())
            if match:
                target = next(value for value in match.groups() if value)
                if os.path.isabs(target) and os.path.isfile(target):
                    path = os.path.realpath(target)
    except (OSError, StopIteration):
        pass
    return path


def _vendor_bwrap_paths(config, env=None):
    source_env = os.environ if env is None else env
    if (not _is_codex_agent(config) or
            "codex" not in config.session_delta_routing_vendors):
        return []
    candidates = []
    seen_launchers = set()

    def add_candidate(path):
        if path:
            candidates.append(path)

    def add_launcher_chain(path):
        if not path:
            return
        current = os.path.abspath(path)
        visited = set()
        while current not in visited:
            if current in seen_launchers:
                return
            visited.add(current)
            seen_launchers.add(current)
            add_candidate(os.path.join(os.path.dirname(current), "bwrap"))
            if not os.path.islink(current):
                break
            target = os.readlink(current)
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(current), target)
            current = os.path.normpath(target)
        try:
            if os.path.getsize(current) > 16384:
                return
            with open(current, errors="replace") as fh:
                text = fh.read()
        except (OSError, UnicodeError):
            return
        if not text.startswith("#!"):
            return
        match = _SIMPLE_EXEC_RE.search(text)
        if match:
            target = next((value for value in match.groups() if value), None)
            if target and os.path.isabs(target):
                add_launcher_chain(target)

    path = (source_env.get(ENV_SHIM_UNDERLYING_PATH) or
            source_env.get("PATH") or BWRAP_DEFAULT_PATH)
    for directory in path.split(os.pathsep):
        directory = directory or os.getcwd()
        add_candidate(os.path.join(directory, "bwrap"))
        codex = os.path.join(directory, "codex")
        if os.path.exists(codex) and os.access(codex, os.X_OK):
            add_launcher_chain(codex)

    launcher = _resolved_vendor_launcher(config, env=source_env)
    add_launcher_chain(launcher)
    add_launcher_chain(os.path.join(
        "/home", config.owner, ".local", "bin", "codex"))
    add_launcher_chain(source_env.get("CCC_AGENT_REAL_CMD"))

    result = []
    for candidate in candidates:
        candidate = os.path.realpath(candidate)
        if (os.path.isfile(candidate) and os.access(candidate, os.X_OK) and
                candidate not in result):
            result.append(candidate)
    return result


def _prepare_delta_routing(session, config, env=None):
    candidates = _vendor_bwrap_paths(config, env=env)
    wrapper = os.path.join(os.path.dirname(__file__), "assets", "scripts",
                           "ccc-bwrap-route")
    runtime_root = os.path.join(
        "/dev/shm", "ccc-agent-%s" % session.session_id)
    sandbox_routes = os.path.join(runtime_root, "routes")
    session.policy["sandbox_route_root"] = sandbox_routes
    available = bool(
        config.session_delta_routing and config.per_turn and
        config.confinement == "bwrap" and
        config.bwrap_proc_mode == "fresh" and candidates and
        os.path.isfile(wrapper) and os.path.isdir("/dev/shm") and
        os.access("/dev/shm", os.W_OK | os.X_OK))
    session.policy["route_interposer_available"] = available
    session.policy["route_interposer_bwrap_paths"] = candidates if available else []
    if config.session_delta_routing:
        if available:
            detail = "codex bwrap route adapter ready"
        elif config.bwrap_proc_mode != "fresh":
            detail = ("delta routing unavailable with bound parent proc; "
                      "trusted outer-sandbox adapter active and writes remain "
                      "shared/unattributed")
        else:
            detail = ("delta routing unavailable; writes remain "
                      "shared/unattributed")
        session.add_event("session-delta-routing", detail)
    if available:
        os.makedirs(sandbox_routes, mode=0o700, exist_ok=True)
        os.chmod(runtime_root, 0o700)
        os.chmod(sandbox_routes, 0o700)
        route_mounts = os.path.join(
            config.store.bundle_dir(session.session_id), "route-mounts")
        os.makedirs(route_mounts, mode=0o711, exist_ok=True)
        os.chmod(route_mounts, 0o711)
    config.store.save(session)
    return available


def _cleanup_delta_routing_runtime(session):
    expected = os.path.join("/dev/shm", "ccc-agent-%s" % session.session_id)
    root = os.path.dirname(str(session.policy.get("sandbox_route_root") or ""))
    if os.path.normpath(root) != os.path.normpath(expected):
        return
    try:
        if os.path.islink(root):
            os.unlink(root)
        elif os.path.isdir(root):
            shutil.rmtree(root)
    except OSError:
        pass


def _bwrap_command(session, config, control=None, lifecycle_socket=None,
                   session_env_path=None, process_env=None):
    """Build a bubblewrap command that confines the agent rootlessly.

    This needs no container CAP_SYS_ADMIN and no privileged helper: bwrap
    creates an unprivileged user+mount+pid namespace,
    recursively binds the OS read-only, overlays the BranchFS view read-write
    at its visible path (hiding the real underlay), and execs the agent as the
    same uid by default.  No network/proc isolation is enforced (per design);
    /proc is bound from the container by default.
    """
    alias_map = config.alias_map
    primary = _primary_root(session, alias_map, config.launch_cwd)
    # Use the process launch path as the in-sandbox cwd. For remote servers this
    # is deliberately not an allowed workspace until a trusted hook adds one.
    # Policy/root selection still canonicalizes aliases, but the process should
    # start in `/home/domen/...` when invoked there rather than surprising the
    # user with the equivalent `/storage/user/<container>/...` spelling.
    workdir = normalize(config.launch_cwd)
    home = "/home/%s" % config.owner

    # Map to the REAL uid inside the namespace (not 0): the view files are owned
    # by this uid, and some agents (claude) refuse to run as root.  bwrap can
    # only map one gid in the common unprivileged path, so choose an accessible
    # runtime-socket gid when Docker access depends on a supplementary group;
    # explicit bwrap_uid/bwrap_gid still override for deployments that need a
    # fixed identity.
    uid = str(config.bwrap_uid if config.bwrap_uid is not None else os.getuid())
    gid = str(_bwrap_gid(config))
    argv = [config.bwrap_bin,
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--as-pid-1",
            "--uid", uid, "--gid", gid,
            "--die-with-parent"]

    for d in BWRAP_RO_DIRS:
        if os.path.isdir(d):
            argv += ["--ro-bind", d, d]
    for d in BWRAP_USRMERGE_DIRS:
        if os.path.islink(d):
            argv += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            argv += ["--ro-bind", d, d]

    # /proc: fresh mount fails under Docker's locked proc masks unless the
    # deployment unmasks them (systempaths=unconfined); default to binding the
    # container's /proc, which always works and is benign in a single-user box.
    if config.bwrap_proc_mode == "fresh":
        argv += ["--proc", "/proc"]
    elif config.bwrap_proc_mode == "ro":
        argv += ["--ro-bind", "/proc", "/proc"]
    else:
        argv += ["--bind", "/proc", "/proc"]

    # Expose the existing CCC/container runtime namespace by default.  These are
    # not raw host binds unless the outer container already has that access; they
    # intentionally preserve access to container-provided sockets and devices
    # such as Docker, ssh-agent, the FUSE sidecar socket, and /dev/fuse.  Use
    # --dev-bind for /dev: ordinary --bind makes device nodes appear under a
    # nodev mount inside bwrap, so /dev/urandom cannot be opened and Python dies
    # during hash-randomization startup.
    if config.container_run_access and os.path.isdir("/dev"):
        argv += ["--dev-bind", "/dev", "/dev"]
    else:
        argv += ["--dev", "/dev"]
    argv += ["--tmpfs", "/tmp"]
    if config.container_run_access and os.path.isdir("/var"):
        # Expose the container's /var read-only in default runtime-access mode.
        # This makes the conventional /var/run/docker.sock path work when the
        # outer CCC container exposes Docker, without letting the agent write
        # logs/cache/lock files into the real container /var.
        argv += ["--ro-bind", "/var", "/var"]
    if config.container_run_access and os.path.isdir("/run"):
        argv += ["--bind", "/run", "/run"]

    if session.policy.get("route_interposer_available"):
        sandbox_routes = session.policy["sandbox_route_root"]
        route_mounts = os.path.join(
            config.store.bundle_dir(session.session_id), "route-mounts")
        candidates = list(session.policy.get(
            "route_interposer_bwrap_paths") or ())
        real_bwrap = os.path.join(os.path.dirname(sandbox_routes),
                                  "ccc-agent-real-bwrap")
        argv += ["--dir", sandbox_routes,
                 "--bind", route_mounts, sandbox_routes,
                 "--ro-bind", candidates[0], real_bwrap]

    # the BranchFS view, read-write, at its visible path and at $HOME; the
    # --bind overlays (and thus hides) the real underlay at the same path.
    argv += ["--bind", primary.mount, alias_map.canonicalize(primary.visible)]
    if primary.home_subdir:
        argv += ["--bind", os.path.join(primary.mount, primary.home_subdir),
                 home]
    else:
        argv += ["--bind", primary.mount, home]
    for name, root in sorted(session.protected_roots.items()):
        if root is primary:
            continue
        argv += ["--bind", root.mount, alias_map.canonicalize(root.visible)]

    # Agent-owned state is deliberately outside BranchFS by default: bind the
    # real shared Codex/Claude/Hermes runtime paths back over the protected home
    # view (including Claude's top-level JSON file and .local/.cache dirs).
    # Plugins/hooks are mounted read-only after this so trusted CCC assets still
    # override any writable user/plugin state.
    _append_shared_agent_state_binds(argv, config)

    # Re-expose the agent runtime + creds read-only.  Each entry is "src" (bind
    # at the same path) or "src:dest" (bind src at dest).  IMPORTANT: a dest
    # UNDER a view (/storage/user, /home/<user>) makes bwrap mkdir mountpoints
    # INTO the FUSE view — creating spurious dir-deltas and churning inodes
    # (ESTALE) — so runtime that the agent doesn't need at a fixed in-view path
    # should bind to a dest outside the views (e.g. /opt/ccc-agent, /ccc-agent).
    # Optional symlinked same-path binds (e.g. ~/.claude -> /storage/user/...)
    # are resolved on the host and bound at their real target path, because the
    # destination symlink may point somewhere different or missing once /home and
    # /storage have been overlaid with BranchFS views inside bwrap.
    for entry in config.bwrap_ro_binds:
        bind = _optional_ro_bind(entry)
        if bind is not None:
            src, dest = bind
            argv += ["--ro-bind", src, dest]

    # Codex's own Linux sandbox uses nested bubblewrap. When CCC must bind an
    # older procfs into its PID namespace, replace only Codex-visible bwrap
    # candidates with the trusted external-sandbox adapter. The outer CCC
    # bwrap executable has already launched and is unaffected by these binds.
    codex_bwrap_binds = _codex_bwrap_adapter_binds(config, process_env)
    for src, dest in codex_bwrap_binds:
        argv += ["--ro-bind", src, dest]
    if codex_bwrap_binds:
        # Codex strips CCC_AGENT_SESSION from its dedicated filesystem helper.
        # Bind the same trusted adapter inode at a stable marker path so that
        # helper can still prove it is inside the launcher-owned namespace.
        argv += ["--ro-bind", codex_bwrap_binds[0][0],
                 SANDBOX_CODEX_BWRAP_MARKER]

    # Optional read-only credential overlays.  Do not use this for whole
    # ~/.codex/~/.claude/~/.hermes trees in normal deployments; direct
    # agent_state_binds keep them shared writable outside BranchFS. The real
    # credential can still be passed via env below for API-key style auth.
    for src in config.cred_mounts:
        bind = _optional_ro_bind(src)
        if bind is not None:
            argv += ["--ro-bind", bind[0], bind[1]]
    for masked in config.cred_mask:
        if os.path.exists(masked):  # only mask a secret that's actually present
            argv += ["--ro-bind", "/dev/null", masked]

    plugin_spec = _matched_agent_plugin(config)
    _append_agent_plugin_binds(argv, plugin_spec)

    if session.policy.get("route_interposer_available"):
        sandbox_routes = session.policy["sandbox_route_root"]
        real_bwrap = os.path.join(os.path.dirname(sandbox_routes),
                                  "ccc-agent-real-bwrap")
        wrapper = os.path.join(os.path.dirname(__file__), "assets", "scripts",
                               "ccc-bwrap-route")
        for candidate in session.policy["route_interposer_bwrap_paths"]:
            argv += ["--ro-bind", wrapper, candidate]
        argv += ["--setenv", ENV_ROUTE_VENDOR, "codex",
                 "--setenv", ENV_REAL_BWRAP, real_bwrap]
        if config.bwrap_proc_mode != "fresh":
            argv += ["--setenv", ENV_BWRAP_BOUND_PROC, "1"]

    _expected_mcp, direct_mcp_client = _mcp_admission_config(config)
    if direct_mcp_client:
        argv += ["--setenv", ENV_MCP_REGISTER_CLIENT, "1"]
        if config.workspace:
            argv += ["--setenv", ENV_CONFIRM_LAUNCH_WORKSPACE, "1"]

    hardening_library = _mcp_client_hardening(config)
    if hardening_library is not None:
        argv += ["--ro-bind", hardening_library, SANDBOX_HARDENING_LIBRARY,
                 "--setenv", ENV_CLIENT_PRELOAD,
                 SANDBOX_HARDENING_LIBRARY]

    # Per-turn control socket: bind the host socket to a fixed in-sandbox path
    # so hooks can signal the supervisor.  `control` is (host_sock, token,
    # hook_token) or None.
    if control is not None:
        host_sock, token, hook_token = control
        argv += ["--bind", host_sock, SANDBOX_CONTROL_SOCK]
    if lifecycle_socket is not None:
        module_dir = os.path.dirname(__file__)
        adaptive_runner = os.path.join(module_dir, "adaptive_pid1.py")
        codex_workspace = os.path.join(module_dir, "codex_workspace.py")
        argv += ["--bind", lifecycle_socket, SANDBOX_LIFECYCLE_SOCK,
                 "--ro-bind", adaptive_runner, SANDBOX_ADAPTIVE_RUNNER,
                 "--ro-bind", codex_workspace, SANDBOX_CODEX_WORKSPACE]
    if session_env_path is not None:
        argv += ["--ro-bind", session_env_path, SANDBOX_SESSION_ENV]

    if control is not None:
        # The socket path must be remapped into private sandbox /tmp. Tokens and
        # all other values are inherited through the bwrap process environment.
        argv += ["--setenv", ENV_CONTROL_SOCK, SANDBOX_CONTROL_SOCK]
    if lifecycle_socket is not None:
        argv += [
            "--setenv", ENV_LIFECYCLE_SOCKET, SANDBOX_LIFECYCLE_SOCK,
            "--setenv", ENV_BOOTSTRAP_SECONDS,
            str(config.adaptive_bootstrap_seconds),
            "--setenv", ENV_STABILITY_SECONDS,
            str(config.adaptive_stability_seconds),
            "--setenv", ENV_DETACH_SECONDS,
            str(config.adaptive_detach_seconds),
        ]
    # Plugin argv activation is separate from value-bearing environment. Server
    # wrappers may receive safe plugin discovery env, but never direct CLI argv.
    plugin_argv_activation = (plugin_spec is not None and
                              not config.server_mode)
    argv += ["--chdir", workdir, "--"]
    command = _agent_command_with_plugin(
        config.agent_command, plugin_spec if plugin_argv_activation else None)
    if lifecycle_socket is not None:
        argv += ["/usr/bin/python3", SANDBOX_ADAPTIVE_RUNNER, "--"] + command
    else:
        argv += ["/usr/bin/python3", "-c", BWRAP_AGENT_RUNNER,
                 BWRAP_AGENT_RUNNER_ARG0] + command
    return argv


def _fresh_run_env(env, session, state_dir):
    """Copy the caller env while replacing stale ccc-agent authority values."""
    run_env = dict(env)
    for name in TRANSIENT_INTERNAL_ENV:
        run_env.pop(name, None)
    run_env[ENV_SESSION] = session.session_id
    run_env[ENV_STATE_DIR] = state_dir
    return run_env


def _write_session_env_handoff(run_env, session, config):
    """Write the narrow CCC env needed by agent-native child sessions.

    Remote agent servers can deliberately rebuild the environment for an inner
    Claude/Codex session. Mount this file read-only at a stable sandbox path so
    trusted hooks can restore only ccc-agent's session/control values without
    broadening the agent's own environment inheritance policy.
    """
    values = {ENV_SESSION: session.session_id}
    if run_env.get(ENV_CONTROL_SOCK):
        values[ENV_CONTROL_SOCK] = SANDBOX_CONTROL_SOCK
    for name in (ENV_CONTROL_TOKEN, ENV_HOOK_TOKEN, ENV_HOOK_SESSION):
        if run_env.get(name):
            values[name] = str(run_env[name])
    if run_env.get("CCC_AGENT_CLI"):
        values["CCC_AGENT_CLI"] = str(run_env["CCC_AGENT_CLI"])

    control_dir = config.store.control_dir(session.session_id)
    os.makedirs(control_dir, exist_ok=True)
    path = os.path.join(control_dir, "session-env.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(values, fh, sort_keys=True, separators=(",", ":"))
        fh.write("\n")
    os.chmod(path, 0o600)
    return path


def _remove_session_env_handoff(path):
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _bwrap_process_env(run_env, config, session):
    """Build bwrap's inherited env with explicit removals and overrides."""
    sandbox_env = dict(run_env)
    # The host-side state path is supervisor-only. Control paths are remapped by
    # bwrap and fresh token values are assigned after stale values were removed.
    sandbox_env.pop(ENV_STATE_DIR, None)
    for name in config.bwrap_unsetenv:
        sandbox_env.pop(name, None)

    workdir = normalize(config.launch_cwd)
    home = "/home/%s" % config.owner
    sandbox_env.update({
        ENV_SESSION: session.session_id,
        "HOME": home,
        "USER": config.owner,
        "LOGNAME": config.owner,
        "PATH": (run_env.get(ENV_SHIM_UNDERLYING_PATH) or BWRAP_DEFAULT_PATH),
        "SHELL": (run_env.get("SHELL") or os.environ.get("SHELL") or "/bin/sh"),
        "TERM": run_env.get("TERM", os.environ.get("TERM", "xterm")),
        "PWD": workdir,
    })

    # Apply value-bearing settings in the process environment, not bwrap argv:
    # API keys and plugin/operator values can be sensitive and argv is readable
    # through /proc/<pid>/cmdline. Explicit overrides follow removals.
    for var, spec in sorted(config.cred_env.items()):
        value = _extract_cred(spec, env=run_env)
        if value:
            sandbox_env[str(var)] = str(value)
    plugin_spec = _matched_agent_plugin(config)
    if plugin_spec is not None:
        for key, value in sorted(plugin_spec.get("setenv", {}).items()):
            sandbox_env[str(key)] = str(value)
    for key, value in sorted(config.bwrap_setenv.items()):
        sandbox_env[str(key)] = str(value)
    _expected_mcp, direct_mcp_client = _mcp_admission_config(config)
    if direct_mcp_client:
        # Never let invocation-controlled preload state enter the trusted client.
        # The verified library is assigned by bwrap only after its read-only bind
        # exists inside the completed sandbox.
        sandbox_env.pop("LD_PRELOAD", None)
        sandbox_env.pop(ENV_HARDEN_CLIENT, None)
    return sandbox_env


def _extract_cred(spec, env=None):
    """Resolve a credential for env passing.  ``spec`` is either a literal
    string, or {"env": NAME} (pass through from the supervisor env), or
    {"file": path, "json_key": "a.b.c"} (read a dotted key from a JSON auth
    file on the host).  Returns the value, or None if unavailable."""
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return None
    if spec.get("env"):
        source_env = os.environ if env is None else env
        return source_env.get(spec["env"])
    path = spec.get("file")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return None
    for part in str(spec.get("json_key", "")).split("."):
        if not part:
            continue
        if isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return None
    return data if isinstance(data, str) else None


def _fail(store, session, detail):
    session.add_event("error", detail)
    if session.state != "failed":
        session.transition("failed")
    store.save(session)


def collect_status_reports(session, backend):
    reports = {}
    for name, root in sorted(session.protected_roots.items()):
        if hasattr(backend, "status_report"):
            reports[name] = backend.status_report(root)
        else:
            reports[name] = StatusReport(changes=backend.status(root), warnings=[])
    return reports


def collect_status(session, backend):
    return {name: report.changes
            for name, report in collect_status_reports(session, backend).items()}


def _verify_apply_parent(root, base):
    """Reject underlay parent symlinks before a selective apply."""
    root_base = os.path.normpath(root.base)
    parent = os.path.dirname(os.path.normpath(base))
    try:
        rel = os.path.relpath(parent, root_base)
    except ValueError:
        raise ValueError("apply destination is outside protected root")
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError("apply destination is outside protected root")
    current = root_base
    if rel == os.curdir:
        return
    for component in rel.split(os.sep):
        current = os.path.join(current, component)
        if not os.path.lexists(current):
            break
        if os.path.islink(current):
            raise ValueError("apply parent is a symlink: %s" % current)
        if not os.path.isdir(current):
            raise ValueError("apply parent is not a directory: %s" % current)


def apply_change_from_store(root, change, alias_map):
    """Apply one reviewed change to the base by reading its delta from the
    BranchFS store (used at session end, when the branch is unmounted).  This
    is *selective*: only the changes we pass get applied, so ignored noise and
    out-of-scope deltas left in the branch are never written to base — unlike
    branchfs ``commit-branch`` which would apply the whole branch."""
    rel, delta, base = store_paths(root, change, alias_map)
    _verify_apply_parent(root, base)
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


def _pending_decision_for_permission_failures(failures, changes_by_root):
    count = len(failures)
    noun = "change" if count == 1 else "changes"
    total = sum(len(changes) for changes in changes_by_root.values())
    return PolicyDecision(
        PENDING_REVIEW,
        total,
        [],
        [],
        ["%d %s could not be committed because the real underlay returned "
         "permission denied; writable changes were committed and these paths "
         "remain in BranchFS for discard or manual handling" % (count, noun)],
    )


def _rewrite_review_for_permission_failures(session, store, backend, alias_map,
                                           failures):
    policy_config = PolicyConfig.from_dict(session.policy)
    status_reports = collect_status_reports(session, backend)
    changes_by_root = {}
    ignored_by_root = {}
    for name, report in status_reports.items():
        changes, ignored = split_ignored(report.changes, policy_config,
                                         alias_map)
        changes_by_root[name] = changes
        ignored_by_root[name] = ignored
    warnings_by_root = {name: list(report.warnings)
                        for name, report in status_reports.items()
                        if report.warnings}
    decision = _pending_decision_for_permission_failures(failures,
                                                         changes_by_root)
    review = artifacts.write_review(store, session, changes_by_root, decision,
                                    warnings_by_root=warnings_by_root,
                                    ignored_by_root=ignored_by_root)
    session.add_event("review-artifacts", review)


def _apply_preflight_errors(session, changes_by_root, alias_map):
    errors = []
    for name, changes in sorted(changes_by_root.items()):
        root = session.protected_roots[name]
        for change in changes:
            try:
                _rel, _delta, base = store_paths(root, change, alias_map)
                _verify_apply_parent(root, base)
            except (OSError, ValueError) as exc:
                errors.append("%s: %s" % (change.path, exc))
    return errors


def _revalidate_commit_admissions(session, alias_map):
    """Revalidate every persisted admission identity before writing underlay."""
    policy = WorkspaceAdmissionPolicy(
        session.protected_roots, alias_map,
        workspace_admission_roots=session.policy.get(
            "workspace_admission_roots"),
        allow_protected_root_workspace=session.policy.get(
            "allow_protected_root_workspace", False))
    records = []
    launch = session.policy.get("launch_workspace_admission")
    if isinstance(launch, dict):
        records.append(launch)
    for route_id, payload in session.session_delta_routes.items():
        if not isinstance(payload, dict):
            return ["route %s admission metadata is malformed" % route_id]
        admitted = payload.get("admitted_roots") or ()
        if not isinstance(admitted, list):
            return ["route %s admitted roots are malformed" % route_id]
        records.extend(item for item in admitted if isinstance(item, dict))

    errors = []
    seen = set()
    for record in records:
        key = record.get("canonical_key") or record.get("canonical_path")
        if key in seen:
            continue
        seen.add(key)
        try:
            policy.revalidate(record)
        except (OSError, ValueError) as exc:
            errors.append("workspace admission %s changed: %s" % (key, exc))
    return errors


def _route_reconciliation(session, changes_by_root, alias_map):
    """Reconcile route attribution with the complete frozen outer delta."""
    raw = session.policy.get("route_path_attribution")
    records = raw if isinstance(raw, dict) else {}
    by_key = {}
    for path, record in records.items():
        if isinstance(path, str) and isinstance(record, dict):
            by_key[alias_map.canonicalize(path)] = record

    items = []
    authorized_paths = []
    blockers = []
    category_counts = {}
    for name, changes in sorted(changes_by_root.items()):
        root = session.protected_roots[name]
        for change in changes:
            key = alias_map.canonicalize(change.path)
            record = by_key.get(key)
            if record is None:
                category = "shared/unattributed"
                authorized = False
                route_ids = []
            else:
                category = str(record.get("category") or "conflicted")
                authorized = record.get("authorized") is True
                route_ids = list(record.get("route_ids") or ())
                if authorized:
                    try:
                        current = DeltaRouteManager._fingerprint_change(
                            root, change)
                    except (OSError, ValueError) as exc:
                        current = None
                        blockers.append(
                            "%s could not be revalidated: %s" % (key, exc))
                    if current != record.get("fingerprint"):
                        authorized = False
                        category = "conflicted-after-merge"
                        record["authorized"] = False
                        record["category"] = category
                        blockers.append(
                            "%s changed after its route merge" % key)
                if category in ("conflicted", "multiply-influenced",
                                "attributed-out-of-scope"):
                    blockers.append("%s is %s" % (key, category))
            if authorized:
                authorized_paths.append(key)
            category_counts[category] = category_counts.get(category, 0) + 1
            items.append({
                "path": key, "root": name, "op": change.op,
                "category": category, "authorized": authorized,
                "route_ids": route_ids,
            })

    route_states = {}
    coverage = {"routed_bwrap_calls": 0,
                "bypassed_or_unattributed_calls": 0}
    for route_id, payload in sorted(session.session_delta_routes.items()):
        if not isinstance(payload, dict):
            blockers.append("route %s metadata is malformed" % route_id)
            continue
        state = str(payload.get("state") or "unknown")
        route_states[route_id] = state
        route_coverage = payload.get("coverage") or {}
        coverage["routed_bwrap_calls"] += int(
            route_coverage.get("routed_bwrap_calls", 0))
        coverage["bypassed_or_unattributed_calls"] += int(
            route_coverage.get("bypassed_or_unattributed_calls", 0))
        if state in ("provisioning", "active", "quiescing", "frozen",
                     "pending-review"):
            blockers.append("route %s remains %s" % (route_id, state))

    reconciliation = {
        "schema_version": 1,
        "items": items,
        "category_counts": category_counts,
        "authorized_paths": sorted(set(authorized_paths)),
        "blockers": sorted(set(blockers)),
        "route_states": route_states,
        "coverage": coverage,
    }
    session.policy["route_reconciliation"] = reconciliation
    return reconciliation


def finalize_session(session, store, backend, alias_map):
    """freeze -> status -> policy -> artifacts -> apply decision.

    Expects the session in state ``finalizing``.  Returns the decision.
    """
    for root in session.protected_roots.values():
        backend.freeze(root)
    session.add_event("frozen-bundle")
    session.transition("frozen")
    store.save(session)

    policy_config = PolicyConfig.from_dict(session.policy)
    status_reports = collect_status_reports(session, backend)
    changes_by_root = {}
    ignored_by_root = {}
    for name, report in status_reports.items():
        changes, ignored = split_ignored(report.changes, policy_config,
                                         alias_map)
        changes_by_root[name] = changes
        ignored_by_root[name] = ignored
    warnings_by_root = {name: list(report.warnings)
                        for name, report in status_reports.items()
                        if report.warnings}
    previously_committed_by_root = {}
    for name, changes in changes_by_root.items():
        already, _new = split_previously_committed_changes(
            changes, session, session.protected_roots, alias_map)
        if already:
            previously_committed_by_root[name] = already
    flat_changes = [c for changes in changes_by_root.values()
                    for c in changes]
    flat_warnings = [w for warnings in warnings_by_root.values()
                     for w in warnings]
    admission_errors = _revalidate_commit_admissions(session, alias_map)
    admission_errors.extend(
        _apply_preflight_errors(session, changes_by_root, alias_map))
    reconciliation = None
    if (session.policy.get("session_delta_routing") or
            session.session_delta_routes):
        reconciliation = _route_reconciliation(
            session, changes_by_root, alias_map)
        policy_config.allowed_scopes.extend(
            reconciliation["authorized_paths"])
    decision = evaluate(flat_changes, policy_config, alias_map)
    if admission_errors:
        reason = ("safe apply preflight failed: %s" %
                  "; ".join(admission_errors[:8]))
        if reason not in decision.reasons:
            decision.reasons.append(reason)
        if decision.decision in (AUTO_COMMIT, NO_CHANGES):
            decision.decision = PENDING_REVIEW
    if reconciliation and reconciliation["blockers"]:
        reason = ("session-delta reconciliation requires review: %s" %
                  "; ".join(reconciliation["blockers"][:8]))
        if reason not in decision.reasons:
            decision.reasons.append(reason)
        if decision.decision in (AUTO_COMMIT, NO_CHANGES):
            decision.decision = PENDING_REVIEW
    if flat_warnings:
        reason = ("%d BranchFS status warning(s); manual review required "
                  "because status may be incomplete or commit may fail"
                  % len(flat_warnings))
        if reason not in decision.reasons:
            decision.reasons.append(reason)
        if decision.decision in (AUTO_COMMIT, NO_CHANGES):
            decision.decision = PENDING_REVIEW
    review = artifacts.write_review(store, session, changes_by_root, decision,
                                    warnings_by_root=warnings_by_root,
                                    ignored_by_root=ignored_by_root,
                                    previously_committed_by_root=(
                                        previously_committed_by_root))
    session.add_event("review-artifacts", review)

    # Apply the decision against unmounted branches.  The real branchfs binary
    # cannot commit/abort a branch whose store is still busy with a live mount
    # (commit-branch fails with ENOTEMPTY), and a pending-review branch is
    # inspected later through the store, not this mount.  Unmount here so every
    # terminal path operates on a quiescent branch; run_session's finally is a
    # harmless idempotent backstop.
    _unmount_all(session, backend)
    session.add_event("unmounted-bundle")

    if decision.decision == NO_CHANGES:
        for root in session.protected_roots.values():
            backend.abort(root)
        session.add_event("closed", "no changes (no-op); branch discarded")
        session.transition("auto-committed")
    elif decision.decision == ABORT:
        for root in session.protected_roots.values():
            backend.abort(root)
        session.add_event("closed", "throwaway policy; branch aborted")
        session.transition("aborted")
    elif decision.decision == AUTO_COMMIT:
        # Selectively apply only the reviewed in-scope changes (the same set
        # used for the decision), then discard the branch.  This avoids
        # branchfs commit-branch applying the *whole* branch — which would
        # commit ignored config-dir churn and choke (ENOTEMPTY) on stale .nfs
        # deltas the agent left in non-workspace areas.
        permission_denied = []
        applied = []
        clear_permission_failures(session)
        try:
            for name, root in sorted(session.protected_roots.items()):
                for change in changes_by_root.get(name, ()):
                    try:
                        apply_change_from_store(root, change, alias_map)
                    except Exception as exc:
                        if not is_permission_denied(exc):
                            raise
                        rel, _delta, _base = store_paths(root, change, alias_map)
                        record = permission_failure_record(root, change, rel, exc)
                        permission_denied.append(record)
                        session.add_event(
                            "commit-permission-denied",
                            "%s: %s" % (change.path, exc))
                        continue
                    rel, _delta, _base = store_paths(root, change, alias_map)
                    applied.append((root, rel))
        except Exception as exc:  # failure must never lose the branch
            _fail(store, session,
                  "commit failed, branch preserved for manual recovery: %s"
                  % exc)
            return decision
        if permission_denied:
            for root, rel in applied:
                prune_backend_change(backend, root, rel)
            remember_permission_failures(session, permission_denied,
                                        applied_count=len(applied))
            _rewrite_review_for_permission_failures(
                session, store, backend, alias_map, permission_denied)
            session.add_event(
                "pending",
                "%d permission-denied path(s) remain in BranchFS" %
                len(permission_denied))
            session.transition("pending-review")
        else:
            for name, root in sorted(session.protected_roots.items()):
                backend.abort(root)  # discard the branch + any unreviewed noise
                session.add_event("committed-root", name)
            session.transition("auto-committed")
    else:  # PENDING_REVIEW: branches stay frozen for human review
        session.add_event("pending", "; ".join(decision.reasons))
        session.transition("pending-review")

    store.save(session)
    return decision


def _discard_remote_bridge_session(session, store, backend):
    """Abort and forget a completed adaptive remote bridge.

    Bridges are transport helpers, not reviewable agent work sessions. They must
    never commit branch deltas. Keep a failed abort/removal record for recovery,
    but remove the complete bundle immediately after a successful discard.
    """
    _unmount_all(session, backend)
    session.add_event("unmounted-bundle")
    try:
        for root in session.protected_roots.values():
            backend.abort(root)
    except Exception as exc:
        _fail(store, session,
              "remote bridge discard failed; branch preserved: %s" % exc)
        return False

    session.add_event("closed", "remote bridge finished; branch discarded")
    session.transition("aborted")
    store.save(session)
    try:
        store.remove(session.session_id)
    except (OSError, ValueError) as exc:
        # The branch is already discarded, so retain the closed metadata bundle
        # with an explicit cleanup error instead of attempting an illegal
        # aborted -> failed transition.
        session.add_event("error", "remote bridge bundle cleanup failed: %s" % exc)
        store.save(session)
        return False
    return True


def _unmount_all(session, backend):
    for root in session.protected_roots.values():
        try:
            backend.unmount(root)
        except Exception:
            pass  # unmount is cleanup; never mask the session outcome


def _cleanup_stale_mount(root, backend):
    cleanup = getattr(backend, "cleanup_stale_mount", None)
    if cleanup is not None:
        cleanup(root)


def _command_detail(command):
    return " ".join(str(part) for part in command)


def _active_mounts(session, backend=None):
    mountinfo_path = getattr(backend, "_mountinfo_path", "/proc/self/mountinfo")
    active = []
    for root in session.protected_roots.values():
        if _mountinfo_entry(root.mount, mountinfo_path) is not None:
            active.append(root.mount)
    return active


def _adaptive_status(fd, event, **fields):
    payload = dict(fields)
    payload["event"] = event
    data = (json.dumps(payload, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")
    try:
        os.write(fd, data)
        return True
    except OSError:
        return False


def _adaptive_relay_write(fd, data):
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except (BrokenPipeError, OSError):
            return False
        view = view[written:]
    return True


def _adaptive_stop_process(proc):
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
    except OSError:
        pass


def _adaptive_event_name(event):
    return {
        "started": "adaptive-started",
        "foreground-locked": "adaptive-foreground-locked",
        "handoff-candidate": "adaptive-handoff-candidate",
        "handoff-rejected": "adaptive-handoff-rejected",
        "handoff": "adaptive-handoff",
        "service-exited": "adaptive-service-exited",
        "one-shot": "adaptive-one-shot",
        "failed": "adaptive-child-failed",
        "stopping": "adaptive-stopping",
    }.get(event)


def _adaptive_supervise_process(proc, listener, session, config, status_fd,
                                frontend_pid):
    """Relay one bwrap invocation until completion or clean service handoff."""
    selector = selectors.DefaultSelector()
    listener.setblocking(False)
    selector.register(listener, selectors.EVENT_READ, "listener")
    for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    try:
        os.fstat(0)
        selector.register(0, selectors.EVENT_READ, "stdin")
    except OSError:
        pass
    os.set_blocking(proc.stdin.fileno(), False)

    lifecycle_conn = None
    lifecycle_buffer = b""
    stdin_buffer = bytearray()
    stdin_write_registered = False
    stdin_eof = False
    handed_off = False

    def close_stdin():
        nonlocal stdin_write_registered
        if stdin_write_registered:
            try:
                selector.unregister(proc.stdin)
            except Exception:
                pass
            stdin_write_registered = False
        try:
            proc.stdin.close()
        except OSError:
            pass

    while proc.poll() is None:
        if not handed_off and os.getppid() != frontend_pid:
            session.add_event("adaptive-frontend-disconnected")
            config.store.save(session)
            _adaptive_stop_process(proc)
            break

        for key, mask in selector.select(0.05):
            kind = key.data
            if kind == "listener":
                conn, _ = listener.accept()
                conn.setblocking(False)
                lifecycle_conn = conn
                selector.register(conn, selectors.EVENT_READ, "lifecycle")
                selector.unregister(listener)
                listener.close()
                continue

            if kind in ("stdout", "stderr"):
                try:
                    data = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if data:
                    _adaptive_relay_write(1 if kind == "stdout" else 2, data)
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                continue

            if kind == "stdin":
                try:
                    data = os.read(0, 65536)
                except BlockingIOError:
                    continue
                if data:
                    stdin_buffer.extend(data)
                    if not stdin_write_registered:
                        selector.register(proc.stdin, selectors.EVENT_WRITE,
                                          "proc-stdin")
                        stdin_write_registered = True
                else:
                    selector.unregister(0)
                    stdin_eof = True
                    if not stdin_buffer:
                        close_stdin()
                continue

            if kind == "proc-stdin" and mask & selectors.EVENT_WRITE:
                try:
                    written = os.write(proc.stdin.fileno(), stdin_buffer)
                    del stdin_buffer[:written]
                except BlockingIOError:
                    continue
                except (BrokenPipeError, OSError):
                    stdin_buffer[:] = b""
                    stdin_eof = True
                if not stdin_buffer:
                    selector.unregister(proc.stdin)
                    stdin_write_registered = False
                    if stdin_eof:
                        close_stdin()
                continue

            if kind == "lifecycle":
                try:
                    data = lifecycle_conn.recv(4096)
                except BlockingIOError:
                    continue
                if not data:
                    selector.unregister(lifecycle_conn)
                    lifecycle_conn.close()
                    lifecycle_conn = None
                    continue
                lifecycle_buffer += data
                while b"\n" in lifecycle_buffer:
                    line, lifecycle_buffer = lifecycle_buffer.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    event = message.get("event")
                    durable = _adaptive_event_name(event)
                    if durable:
                        if (config.server_mode and
                                event in ("foreground-locked",
                                          "handoff-rejected")):
                            session.agent_kind = remote_bridge_agent_kind(
                                session.agent_kind)
                            session.add_event("adaptive-remote-bridge", event)
                        detail = message.get("reason")
                        session.add_event(durable, detail)
                        config.store.save(session)
                    if event == "handoff" and not handed_off:
                        handed_off = True
                        _adaptive_status(status_fd, "handoff")
                        try:
                            os.close(status_fd)
                        except OSError:
                            pass
                        close_stdin()
                        # The adaptive PID-1 runner proved that all descendants
                        # released their output streams. Stop owning the SSH
                        # channel before the frontend returns.
                        for fd in (0, 1, 2):
                            try:
                                devnull = os.open(os.devnull, os.O_RDWR)
                                os.dup2(devnull, fd)
                                os.close(devnull)
                            except OSError:
                                pass
                        break

        if handed_off:
            # No service descendant owns protocol stdout/stderr after the EOF
            # gates. The outer bwrap process can retain its pipes, so close our
            # read ends and wait without keeping the SSH channel alive.
            for stream in (proc.stdout, proc.stderr):
                try:
                    selector.unregister(stream)
                except Exception:
                    pass
                try:
                    stream.close()
                except OSError:
                    pass
            break

    return handed_off, proc.wait()


def _run_adaptive_supervisor(session, config, env, before_finalize, status_fd,
                             frontend_pid):
    control_server = None
    listener = None
    session_env_path = None
    discard_remote_bridge = False
    lifecycle_path = os.path.join(config.store.control_dir(session.session_id),
                                  "lifecycle.sock")
    try:
        os.makedirs(os.path.dirname(lifecycle_path), exist_ok=True)
        try:
            os.unlink(lifecycle_path)
        except OSError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(lifecycle_path)
        listener.listen(1)

        cwd = _agent_cwd(session, config.alias_map, config.launch_cwd)
        os.makedirs(cwd, exist_ok=True)
        run_env = _fresh_run_env(env, session, config.store.state_dir)
        _prepare_delta_routing(session, config, env=run_env)
        control = None
        turn_ctl = None
        _mcp_supported = False
        if config.per_turn:
            token = binascii.hexlify(os.urandom(16)).decode("ascii")
            hook_token = binascii.hexlify(os.urandom(16)).decode("ascii")
            host_sock = config.store.control_socket(session.session_id)
            turn_ctl = TurnController(
                session, config.store, config.backend, config.alias_map,
                workspace_admission_policy=_workspace_admission_policy(
                    session, config))
            turn_ctl.route_manager.recover()
            turn_ctl.reset_agent_workspaces()
            expected_clients, _mcp_supported = _mcp_admission_config(config)
            control_server = ControlServer(
                host_sock, turn_ctl.handle, token, hook_token=hook_token,
                expected_clients=expected_clients,
                route_wrapper_paths=session.policy.get(
                    "route_interposer_bwrap_paths"))
            control_server.start()
            session.add_event("control-server", host_sock)
            run_env[ENV_CONTROL_SOCK] = host_sock
            run_env[ENV_CONTROL_TOKEN] = token
            run_env[ENV_HOOK_TOKEN] = hook_token
            run_env[ENV_HOOK_SESSION] = session.session_id
            control = (host_sock, token, hook_token)

        session_env_path = _write_session_env_handoff(
            run_env, session, config)
        session.transition("running")
        session.add_event("adaptive-supervisor", str(os.getpid()))
        config.store.save(session)
        if config.on_session_start is not None:
            config.on_session_start(session)

        bwrap_env = _bwrap_process_env(run_env, config, session)
        argv = _bwrap_command(
            session, config, control=control,
            lifecycle_socket=lifecycle_path,
            session_env_path=session_env_path, process_env=bwrap_env)
        session.add_event("bwrap-launch", argv[0])
        config.store.save(session)
        proc = subprocess.Popen(argv, env=bwrap_env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                bufsize=0)
        if control_server is not None:
            control_server.set_launch_process(proc.pid,
                                              supported=_mcp_supported)
        _handed_off, returncode = _adaptive_supervise_process(
            proc, listener, session, config, status_fd, frontend_pid)
        listener = None
        session.exit_status = returncode
        session.add_event("agent-exit", str(returncode))
        if turn_ctl is not None:
            route_outcomes = turn_ctl.route_manager.recover()
            if route_outcomes:
                session.add_event("session-delta-route-recovery",
                                  json.dumps(route_outcomes, sort_keys=True))
                config.store.save(session)

        if is_remote_bridge(session):
            # Bundle removal must wait until the control server and handoff files
            # are closed in ``finally``; otherwise SessionStore.remove races a
            # concurrently changing non-empty control directory.
            discard_remote_bridge = True
        else:
            session.transition("finalizing")
            config.store.save(session)
            if before_finalize is not None:
                before_finalize(session)
            finalize_session(session, config.store, config.backend,
                             config.alias_map)
    except Exception as exc:
        _fail(config.store, session, "adaptive launch failed: %s" % exc)
    finally:
        if control_server is not None:
            control_server.stop()
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        try:
            os.unlink(lifecycle_path)
        except OSError:
            pass
        _unmount_all(session, config.backend)
        _remove_session_env_handoff(session_env_path)
        _cleanup_delta_routing_runtime(session)

    if discard_remote_bridge:
        try:
            _discard_remote_bridge_session(session, config.store, config.backend)
        except Exception as exc:
            # Teardown has already completed, so always deliver a terminal status
            # even if unexpected metadata I/O fails during bundle removal.
            session.add_event(
                "error", "unexpected remote bridge cleanup failure: %s" % exc)
            if session.state not in TERMINAL_STATES:
                try:
                    session.transition("failed")
                except ValueError:
                    pass
            try:
                config.store.save(session)
            except Exception:
                pass

    _adaptive_status(status_fd, "finished", session=session.to_dict())
    try:
        os.close(status_fd)
    except OSError:
        pass


def _run_adaptive_session(session, config, env, before_finalize=None):
    """Fork a trusted supervisor; return on foreground finish or daemon handoff."""
    status_read, status_write = os.pipe()
    frontend_pid = os.getpid()
    try:
        supervisor_pid = os.fork()
    except OSError as exc:
        os.close(status_read)
        os.close(status_write)
        _fail(config.store, session, "adaptive supervisor fork failed: %s" % exc)
        _unmount_all(session, config.backend)
        return session

    if supervisor_pid == 0:
        os.close(status_read)
        try:
            os.setsid()
        except OSError:
            pass
        _run_adaptive_supervisor(session, config, env, before_finalize,
                                 status_write, frontend_pid)
        os._exit(0)

    os.close(status_write)
    with os.fdopen(status_read, "rb", buffering=0) as status_stream:
        line = status_stream.readline()
    try:
        message = json.loads(line.decode("utf-8")) if line else {}
    except (UnicodeDecodeError, ValueError):
        message = {}
    if message.get("event") != "handoff":
        try:
            os.waitpid(supervisor_pid, 0)
        except OSError:
            pass
        if isinstance(message.get("session"), dict):
            return Session.from_dict(message["session"])
    try:
        return config.store.load(session.session_id)
    except KeyError:
        return session


def _run_wait_with_launch_identity(command, control_server=None,
                                   mcp_supported=False, **kwargs):
    """Run a foreground child and publish its PID before waiting.

    Older runner tests intercept ``subprocess.run`` to inspect bwrap argv. Keep
    that test-double seam without using it in production; real executions always
    use Popen so MCP admission can bind to the live launch PID before model work.
    """
    if hasattr(subprocess.run, "mock_calls"):
        return subprocess.run(command, **kwargs)
    proc = subprocess.Popen(command, **kwargs)
    if control_server is not None:
        control_server.set_launch_process(proc.pid, supported=mcp_supported)
    proc.wait()
    return proc


def _run_agent_and_finalize(session, config, env, before_finalize=None,
                            enter_running=False):
    """Launch config.agent_command against an already-mounted session."""
    control_server = None
    session_env_path = None
    try:
        cwd = _agent_cwd(session, config.alias_map, config.launch_cwd)
        os.makedirs(cwd, exist_ok=True)
        run_env = _fresh_run_env(env, session, config.store.state_dir)
        _prepare_delta_routing(session, config, env=run_env)

        # Per-turn control channel: start the supervisor-side server (outside
        # the sandbox) BEFORE launching the agent, so the socket exists for the
        # bwrap bind and the Stop hook can signal it.  The host socket path +
        # token go into the agent env (bwrap remaps the path to the in-sandbox
        # mount); finalize at process exit still runs as the session-end pass.
        control = None
        turn_ctl = None
        _mcp_supported = False
        if config.per_turn:
            token = binascii.hexlify(os.urandom(16)).decode("ascii")
            hook_token = binascii.hexlify(os.urandom(16)).decode("ascii")
            host_sock = config.store.control_socket(session.session_id)
            turn_ctl = TurnController(
                session, config.store, config.backend, config.alias_map,
                workspace_admission_policy=_workspace_admission_policy(
                    session, config))
            turn_ctl.route_manager.recover()
            turn_ctl.reset_agent_workspaces()
            expected_clients, _mcp_supported = _mcp_admission_config(config)
            control_server = ControlServer(
                host_sock, turn_ctl.handle, token, hook_token=hook_token,
                expected_clients=expected_clients,
                route_wrapper_paths=session.policy.get(
                    "route_interposer_bwrap_paths"))
            control_server.start()
            session.add_event("control-server", host_sock)
            run_env[ENV_CONTROL_SOCK] = host_sock
            run_env[ENV_CONTROL_TOKEN] = token
            run_env[ENV_HOOK_TOKEN] = hook_token
            run_env[ENV_HOOK_SESSION] = session.session_id
            control = (host_sock, token, hook_token)

        if config.confinement == "bwrap":
            session_env_path = _write_session_env_handoff(
                run_env, session, config)
        if enter_running:
            session.transition("running")
        config.store.save(session)
        if config.on_session_start is not None:
            config.on_session_start(session)
        if config.confinement == "bwrap":
            # bwrap assembles the namespace itself and --chdir's into the
            # workspace inside the sandbox, so no host-side cwd is set here.
            bwrap_env = _bwrap_process_env(run_env, config, session)
            argv = _bwrap_command(
                session, config, control=control,
                session_env_path=session_env_path, process_env=bwrap_env)
            session.add_event("bwrap-launch", argv[0])
            session.add_event("container-run-access",
                              "enabled" if config.container_run_access
                              else "disabled")
            proc = _run_wait_with_launch_identity(
                argv, env=bwrap_env, control_server=control_server,
                mcp_supported=_mcp_supported)
        else:
            proc = _run_wait_with_launch_identity(
                config.agent_command, cwd=cwd, env=run_env,
                control_server=control_server, mcp_supported=False)
        session.exit_status = proc.returncode
        session.add_event("agent-exit", str(proc.returncode))
        if turn_ctl is not None:
            route_outcomes = turn_ctl.route_manager.recover()
            if route_outcomes:
                session.add_event("session-delta-route-recovery",
                                  json.dumps(route_outcomes, sort_keys=True))
                config.store.save(session)
    except Exception as exc:
        _fail(config.store, session, "agent launch failed: %s" % exc)
        if control_server is not None:
            control_server.stop()
        _remove_session_env_handoff(session_env_path)
        _unmount_all(session, config.backend)
        _cleanup_delta_routing_runtime(session)
        return session

    try:
        session.transition("finalizing")
        config.store.save(session)
        if before_finalize is not None:
            before_finalize(session)
        finalize_session(session, config.store, config.backend,
                         config.alias_map)
    except Exception as exc:
        _fail(config.store, session, "finalize failed: %s" % exc)
    finally:
        if control_server is not None:
            control_server.stop()
        _remove_session_env_handoff(session_env_path)
        _unmount_all(session, config.backend)
        _cleanup_delta_routing_runtime(session)

    return session


def run_session(config, env=None, before_finalize=None):
    """Run one contained agent session to its final state.

    ``env`` defaults to ``os.environ``.  If ``CCC_AGENT_SESSION`` is already
    set, the invocation is nested inside an existing session: reuse it and
    run the command without creating a new branch bundle.
    """
    env = dict(os.environ if env is None else env)

    nested_id = env.get(ENV_SESSION)
    if nested_id:
        try:
            session = config.store.load(nested_id)
        except KeyError:
            session = None
        if session is not None:
            session.add_event("nested-run", _command_detail(config.agent_command))
            config.store.save(session)
            subprocess.call(config.agent_command, env=env)
            return session

    if (config._launch_workspace_admission is not None and
            config._launch_workspace_admission.get("identity") is not None):
        config._operator_workspace_policy.revalidate(
            config._launch_workspace_admission)

    session = config.store.create(
        owner=config.owner,
        agent_kind=config.agent_kind,
        agent_command=config.agent_command,
        workspace=config.workspace,
        policy=config.policy,
        protected_roots={},  # filled below, once the session id exists
        completion=config.completion,
    )
    session.protected_roots = {
        spec.name: spec.materialize(
            session.session_id,
            config.store.state_dir,
            mount_dir=config.store.mount_dir(session.session_id),
        )
        for spec in config.roots
    }
    _add_session_infra_ignores(session, config)
    config.store.save(session)

    try:
        session.transition("mounting")
        config.store.save(session)
        _ensure_shared_agent_state_dirs(config)
        for root in session.protected_roots.values():
            config.backend.start_daemon(root)
            config.backend.create_branch(root)
            config.backend.mount(root, agent=True)
        session.add_event("mounted-bundle")
    except Exception as exc:
        _fail(config.store, session, "mount failed: %s" % exc)
        _unmount_all(session, config.backend)
        return session

    if config.lifecycle == "adaptive" and not os.isatty(0):
        return _run_adaptive_session(session, config, env,
                                     before_finalize=before_finalize)
    if config.lifecycle == "adaptive":
        session.add_event("adaptive-tty-foreground")
        config.store.save(session)
    return _run_agent_and_finalize(session, config, env,
                                   before_finalize=before_finalize,
                                   enter_running=True)


def resume_session(session_id, config, env=None, before_finalize=None,
                   force=False, allow_failed=False):
    """Re-mount and continue an existing session branch.

    Resume handles crash/reboot recovery for ``running`` sessions and operator
    follow-up work for ``pending-review`` sessions.  ``aborted`` sessions are
    restarted by recreating the same branch id, because a successful abort has
    discarded the previous branch delta.  ``allow_failed`` is an explicit
    operator opt-in for retrying a session whose branch was preserved after a
    failed mount/finalize/commit.

    Resume preserves the original stored `agent_command`; `config.agent_command`
    is the command for this invocation only (defaulted by the CLI to the stored
    command).
    """
    env = dict(os.environ if env is None else env)
    if env.get(ENV_SESSION):
        raise ResumeError("cannot resume a session from inside another "
                          "ccc-agent session")
    try:
        session = config.store.load(session_id)
    except KeyError:
        raise ResumeError("no such session: %s" % session_id)
    was_failed = session.state == "failed"
    was_pending_review = session.state == "pending-review"
    was_aborted = session.state == "aborted"
    resumable = session.state in ("running", "pending-review", "aborted")
    if not resumable and not (allow_failed and was_failed):
        if was_failed:
            raise ResumeError(
                "cannot resume session %s in state failed without "
                "--allow-failed (inspect the failure first, then retry with "
                "--allow-failed if the preserved branch should be reopened)"
                % session.session_id)
        raise ResumeError(
            "cannot resume session %s in state %s (resume accepts running, "
            "pending-review, aborted, or failed with --allow-failed)"
            % (session.session_id, session.state))

    session.add_event("resume-command", _command_detail(config.agent_command))
    if was_failed:
        session.add_event("resume-from-failed")
    if was_pending_review:
        session.add_event("resume-from-pending-review")
    if was_aborted:
        session.add_event("resume-from-aborted")
    if config.agent_kind != session.agent_kind:
        session.add_event("resume-agent", config.agent_kind)
    config.store.save(session)

    try:
        _ensure_shared_agent_state_dirs(config)
        for root in session.protected_roots.values():
            _cleanup_stale_mount(root, config.backend)

        active = _active_mounts(session, config.backend)
        if active and not force:
            raise ResumeError(
                "refusing to resume session %s because its mount(s) still "
                "appear active after stale BranchFS cleanup: %s; use --force "
                "only after verifying no old agent process is still using the "
                "session"
                % (session.session_id, ", ".join(active)))

        for root in session.protected_roots.values():
            config.backend.start_daemon(root)
            if was_aborted:
                config.backend.create_branch(root)
            elif was_failed or was_pending_review:
                config.backend.thaw(root)
            config.backend.mount(root, agent=True)
        if was_failed or was_pending_review or was_aborted:
            session.transition("running")
        session.add_event("resumed-bundle")
        config.store.save(session)
    except Exception as exc:
        detail = "resume mount failed: %s" % exc
        session.add_event("error", detail)
        config.store.save(session)
        _unmount_all(session, config.backend)
        raise ResumeError(detail)

    return _run_agent_and_finalize(session, config, env,
                                   before_finalize=before_finalize,
                                   enter_running=False)
