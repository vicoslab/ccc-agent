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
import json
import os
import shutil
import subprocess

from . import artifacts
from .branchfs import StatusReport
from .control import ControlServer
from .paths import is_within, normalize
from .policy import (ABORT, AUTO_COMMIT, NO_CHANGES, PENDING_REVIEW,
                     PolicyConfig, evaluate, split_ignored)
from .session import ProtectedRoot
from .turn import TurnController

ENV_SESSION = "CCC_AGENT_SESSION"
ENV_STATE_DIR = "CCC_AGENT_STATE_DIR"
ENV_CONTROL_SOCK = "CCC_AGENT_CONTROL_SOCK"
ENV_CONTROL_TOKEN = "CCC_AGENT_CONTROL_TOKEN"

# Where the per-turn control socket is bind-mounted INSIDE the bwrap sandbox.
# The host-side socket lives under the state dir, outside the sandbox.  Keep the
# in-sandbox path under private /tmp rather than /run: default container /run is
# now bound into the sandbox and may be root-owned, so bwrap cannot create
# /run/ccc-agent as an unprivileged user.
SANDBOX_CONTROL_SOCK = "/tmp/ccc-agent/control.sock"


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


class RunnerConfig(object):
    def __init__(self, store, backend, alias_map, owner, agent_kind,
                 agent_command, workspace, policy, roots,
                 completion="process-exit", confinement="none",
                 bwrap_bin="bwrap", bwrap_proc_mode="bind",
                 bwrap_ro_binds=(), bwrap_setenv=None, per_turn=None,
                 container_run_access=True,
                 cred_mounts=(), cred_mask=(), cred_env=None,
                 bwrap_uid=None, bwrap_gid=None, agent_plugins=None,
                 agent_state_binds=None, protect_agent_state=False,
                 ensure_agent_state_dirs=False, on_session_start=None):
        self.store = store              # SessionStore
        self.backend = backend          # BranchfsCli or FakeBranchFS
        self.alias_map = alias_map
        self.owner = owner
        self.agent_kind = agent_kind
        self.agent_command = list(agent_command)
        self.workspace = workspace
        self.policy = dict(policy)
        if "allowed_scopes" not in self.policy:
            self.policy["allowed_scopes"] = [workspace]
        PolicyConfig.from_dict(self.policy)  # validate early
        self.roots = list(roots)
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
        # $HOME/storage the view hides, become reachable again).  setenv passes
        # config/API-key env into the otherwise --clearenv'd sandbox.
        self.bwrap_ro_binds = list(bwrap_ro_binds)
        self.bwrap_setenv = dict(bwrap_setenv or {})
        # By default the sandbox inherits selected runtime namespaces from the
        # existing CCC container: /run for deployment-provided sockets and /dev
        # for container-visible devices such as /dev/fuse. These are still the
        # outer container's namespaced resources, not raw host views. Use
        # --full-isolation / container_run_access=false to omit this ambient
        # container runtime view and fall back to bwrap's isolated /dev.
        self.container_run_access = bool(container_run_access)
        # bwrap needs no extra container privilege and no uid/gid: it mints
        # namespace-scoped CAP_SYS_ADMIN from an unprivileged user namespace
        # and runs the agent as the same host uid (mapped to 0 inside).
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


def _agent_cwd(session, alias_map):
    """Map the visible workspace path into the mounted branch view."""
    workspace = alias_map.canonicalize(session.workspace)
    for root in session.protected_roots.values():
        visible = alias_map.canonicalize(root.visible)
        if is_within(workspace, visible):
            rel = os.path.relpath(workspace, visible)
            return (root.mount if rel == "." else
                    os.path.join(root.mount, rel))
    raise ValueError("workspace %s is not under any protected root"
                     % session.workspace)


def _primary_root(session, alias_map):
    """The protected root whose visible path contains the workspace."""
    workspace = alias_map.canonicalize(session.workspace)
    for root in session.protected_roots.values():
        if is_within(workspace, alias_map.canonicalize(root.visible)):
            return root
    raise ValueError("workspace %s is not under any protected root"
                     % session.workspace)


# System paths exposed read-only inside the bwrap sandbox.  The agent sees the
# OS read-only, its BranchFS view read-write, and (by default) the CCC
# container's existing /run runtime namespace.  It still does not see the real
# underlay, BranchFS store, or supervisor state. On merged-/usr systems
# /bin,/sbin,/lib,/lib64 are symlinks into /usr and must be recreated as
# symlinks, not bound as dirs.
BWRAP_RO_DIRS = ("/usr", "/etc", "/opt")
BWRAP_USRMERGE_DIRS = ("/bin", "/sbin", "/lib", "/lib64", "/lib32", "/libx32")
AGENT_STATE_DIRS = (
    ".codex", ".claude", ".hermes",
    ".local/share/claude", ".local/state/claude",
    ".cache/claude-cli-nodejs",
)
AGENT_STATE_FILES = (".claude.json",)
AGENT_STATE_PATHS = AGENT_STATE_DIRS + AGENT_STATE_FILES
CLAUDE_RUNTIME_STATE_PATHS = (
    ".claude", ".claude.json", ".local/share/claude",
    ".local/state/claude", ".cache/claude-cli-nodejs",
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


def _inferred_agent_plugin_names(config):
    """Agent plugin candidates inferred from the executable path only."""
    names = set()
    if config.agent_command:
        names.add(_agent_token(config.agent_command[0]))
    return names


def _matched_agent_plugin(config):
    """Return the validated plugin spec for the contained agent, or None.

    Returns None when no plugin matches the agent, when the trusted plugin
    source does not exist on the host (graceful degradation -> process-exit
    review still runs), or when the agent command uses ``--bare`` (which
    disables plugins/hooks, so per-turn injection would be a silent no-op).
    Direct, uncontained codex/claude/hermes runs never reach here -- the
    launcher only injects for a command it identified as that agent.

    Explicit agent selection (``--agent codex``) wins over the executable
    basename.  If no configured plugin matches that explicit kind,
    fall back to basename inference so existing descriptive labels still work.
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

    explicit_kind = _agent_token(config.agent_kind)
    if explicit_kind and explicit_kind != "command":
        explicit_agent = _plugin_key_for_token(config, explicit_kind)
        if explicit_agent:
            return validated(explicit_agent)

    names = _inferred_agent_plugin_names(config)
    for agent in sorted(config.agent_plugins):
        if agent.lower() in names:
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

    e.g. ``claude -p x`` + ``--plugin-dir P`` -> ``claude --plugin-dir P -p x``.
    User-supplied arguments are preserved; Claude accepts a repeated
    ``--plugin-dir`` so a user-provided one coexists with the CCC one.
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


def _is_claude_agent(config):
    """Best-effort detection of a contained Claude Code invocation."""
    if _agent_token(config.agent_kind) == "claude":
        return True
    return "claude" in _inferred_agent_plugin_names(config)


def _add_claude_runtime_state_ignores(session, config, ignore):
    """Drop narrowly-known Claude Code runtime files from BranchFS review.

    Optional shared binds are skipped when the source is absent.  If Claude then
    creates the path inside the BranchFS home view during this run, it is still
    runtime/config noise rather than a user deliverable.  Add only exact Claude
    runtime paths (not broad ``.local`` or ``.cache`` parents) and only in the
    default shared-state mode; ``--protect-agent-state`` intentionally leaves
    agent state reviewable.
    """
    if config.protect_agent_state or not _is_claude_agent(config):
        return
    home = "/home/%s" % config.owner
    for relpath in CLAUDE_RUNTIME_STATE_PATHS:
        canonical = _canonical_protected_path(os.path.join(home, relpath),
                                             session, config)
        if canonical and canonical not in ignore:
            ignore.append(canonical)


def _add_session_infra_ignores(session, config):
    """Ignore ccc-agent-owned bind/plugin/mask targets inside branch views."""
    ignore = session.policy.setdefault("ignore_patterns", [])
    _add_claude_runtime_state_ignores(session, config, ignore)

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
        names = (os.path.basename(os.path.normpath(src)),
                 os.path.basename(os.path.normpath(dest)))
        if any(name in AGENT_STATE_FILES for name in names):
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


def _bwrap_command(session, config, control=None):
    """Build a bubblewrap command that confines the agent rootlessly.

    This needs no container CAP_SYS_ADMIN and no privileged helper: bwrap
    creates an unprivileged user+mount+pid namespace,
    recursively binds the OS read-only, overlays the BranchFS view read-write
    at its visible path (hiding the real underlay), and execs the agent as the
    same host uid mapped to 0.  No network/proc isolation is enforced (per
    design); /proc is bound from the container by default.
    """
    alias_map = config.alias_map
    primary = _primary_root(session, alias_map)
    # Use the workspace path the user launched from as the in-sandbox cwd.
    # Policy/root selection still canonicalizes aliases, but the process should
    # start in `/home/domen/...` when invoked there rather than surprising the
    # user with the equivalent `/storage/user/<container>/...` spelling.
    workdir = normalize(session.workspace)
    home = "/home/%s" % config.owner

    # Map to the REAL uid inside the namespace (not 0): the view files are owned
    # by this uid, and some agents (claude) refuse to run as root.  Overridable
    # via bwrap_uid/bwrap_gid for agents that genuinely want in-sandbox root.
    uid = str(config.bwrap_uid if config.bwrap_uid is not None else os.getuid())
    gid = str(config.bwrap_gid if config.bwrap_gid is not None else os.getgid())
    argv = [config.bwrap_bin,
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--uid", uid, "--gid", gid,
            "--die-with-parent", "--clearenv"]

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
    # --full-isolation / container_run_access=false to omit these ambient views
    # and restore the older no-ambient-/run plus isolated bwrap-/dev behavior.
    if config.container_run_access and os.path.isdir("/dev"):
        argv += ["--bind", "/dev", "/dev"]
    else:
        argv += ["--dev", "/dev"]
    argv += ["--tmpfs", "/tmp"]
    if config.container_run_access and os.path.isdir("/run"):
        argv += ["--bind", "/run", "/run"]

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

    # Per-turn control socket: bind the host socket to a fixed in-sandbox path
    # so the Stop hook can signal the supervisor.  `control` is (host_sock,
    # token) or None.
    if control is not None:
        host_sock, token = control
        argv += ["--bind", host_sock, SANDBOX_CONTROL_SOCK]

    argv += ["--setenv", ENV_SESSION, session.session_id,
             "--setenv", "HOME", home,
             "--setenv", "USER", config.owner,
             "--setenv", "LOGNAME", config.owner,
             "--setenv", "PATH",
             "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "--setenv", "TERM", os.environ.get("TERM", "xterm")]
    if control is not None:
        argv += ["--setenv", ENV_CONTROL_SOCK, SANDBOX_CONTROL_SOCK,
                 "--setenv", ENV_CONTROL_TOKEN, control[1]]
    # Credentials via env (read from the host auth files; never bound in).
    for var, spec in sorted(config.cred_env.items()):
        value = _extract_cred(spec)
        if value:
            argv += ["--setenv", var, value]
    # Plugin activation env (e.g. HERMES_BUNDLED_PLUGINS); operator bwrap_setenv
    # below can still override.
    if plugin_spec is not None:
        for key, value in sorted(plugin_spec.get("setenv", {}).items()):
            argv += ["--setenv", key, str(value)]
    for key, value in sorted(config.bwrap_setenv.items()):
        argv += ["--setenv", key, str(value)]
    argv += ["--chdir", workdir, "--"]
    argv += _agent_command_with_plugin(config.agent_command, plugin_spec)
    return argv


def _extract_cred(spec):
    """Resolve a credential for env passing.  ``spec`` is either a literal
    string, or {"env": NAME} (pass through from the supervisor env), or
    {"file": path, "json_key": "a.b.c"} (read a dotted key from a JSON auth
    file on the host).  Returns the value, or None if unavailable."""
    if isinstance(spec, str):
        return spec
    if not isinstance(spec, dict):
        return None
    if spec.get("env"):
        return os.environ.get(spec["env"])
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


def apply_change_from_store(root, change, alias_map):
    """Apply one reviewed change to the base by reading its delta from the
    BranchFS store (used at session end, when the branch is unmounted).  This
    is *selective*: only the changes we pass get applied, so ignored noise and
    out-of-scope deltas left in the branch are never written to base — unlike
    branchfs ``commit-branch`` which would apply the whole branch."""
    visible = alias_map.canonicalize(root.visible)
    rel = os.path.relpath(alias_map.canonicalize(change.path), visible)
    delta = os.path.join(root.store, "branches", root.branch, "files", rel)
    base = os.path.join(root.base, rel)
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
    flat_changes = [c for changes in changes_by_root.values()
                    for c in changes]
    flat_warnings = [w for warnings in warnings_by_root.values()
                     for w in warnings]
    decision = evaluate(flat_changes, policy_config, alias_map)
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
                                    ignored_by_root=ignored_by_root)
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
        try:
            for name, root in sorted(session.protected_roots.items()):
                for change in changes_by_root.get(name, ()):
                    apply_change_from_store(root, change, alias_map)
                backend.abort(root)  # discard the branch + any unreviewed noise
                session.add_event("committed-root", name)
        except Exception as exc:  # failure must never lose the branch
            _fail(store, session,
                  "commit failed, branch preserved for manual recovery: %s"
                  % exc)
            return decision
        session.transition("auto-committed")
    else:  # PENDING_REVIEW: branches stay frozen for human review
        session.add_event("pending", "; ".join(decision.reasons))
        session.transition("pending-review")

    store.save(session)
    return decision


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


def _active_mounts(session):
    active = []
    for root in session.protected_roots.values():
        try:
            if os.path.ismount(root.mount):
                active.append(root.mount)
        except OSError:
            pass
    return active


def _run_agent_and_finalize(session, config, env, before_finalize=None,
                            enter_running=False):
    """Launch config.agent_command against an already-mounted session."""
    control_server = None
    try:
        cwd = _agent_cwd(session, config.alias_map)
        os.makedirs(cwd, exist_ok=True)
        run_env = dict(env)
        run_env[ENV_SESSION] = session.session_id
        run_env[ENV_STATE_DIR] = config.store.state_dir

        # Per-turn control channel: start the supervisor-side server (outside
        # the sandbox) BEFORE launching the agent, so the socket exists for the
        # bwrap bind and the Stop hook can signal it.  The host socket path +
        # token go into the agent env (bwrap remaps the path to the in-sandbox
        # mount); finalize at process exit still runs as the session-end pass.
        control = None
        if config.per_turn:
            token = binascii.hexlify(os.urandom(16)).decode("ascii")
            host_sock = config.store.control_socket(session.session_id)
            turn_ctl = TurnController(session, config.store, config.backend,
                                      config.alias_map)
            control_server = ControlServer(host_sock, turn_ctl.handle, token)
            control_server.start()
            session.add_event("control-server", host_sock)
            run_env[ENV_CONTROL_SOCK] = host_sock
            run_env[ENV_CONTROL_TOKEN] = token
            control = (host_sock, token)

        if enter_running:
            session.transition("running")
        config.store.save(session)
        if config.on_session_start is not None:
            config.on_session_start(session)
        if config.confinement == "bwrap":
            # bwrap assembles the namespace itself and --chdir's into the
            # workspace inside the sandbox, so no host-side cwd is set here.
            argv = _bwrap_command(session, config, control=control)
            session.add_event("bwrap-launch", argv[0])
            session.add_event("container-run-access",
                              "enabled" if config.container_run_access
                              else "disabled")
            proc = subprocess.run(argv, env=run_env)
        else:
            proc = subprocess.run(config.agent_command, cwd=cwd, env=run_env)
        session.exit_status = proc.returncode
        session.add_event("agent-exit", str(proc.returncode))
    except Exception as exc:
        _fail(config.store, session, "agent launch failed: %s" % exc)
        if control_server is not None:
            control_server.stop()
        _unmount_all(session, config.backend)
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
        _unmount_all(session, config.backend)

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

    return _run_agent_and_finalize(session, config, env,
                                   before_finalize=before_finalize,
                                   enter_running=True)


def resume_session(session_id, config, env=None, before_finalize=None,
                   force=False, allow_failed=False):
    """Re-mount and continue an existing session branch.

    By default this is for crash/reboot recovery of sessions still marked
    ``running``.  ``allow_failed`` is an explicit operator opt-in for retrying a
    session whose branch was preserved after a failed mount/finalize/commit.

    Resume deliberately does *not* create a new branch and it preserves the
    original stored `agent_command`; `config.agent_command` is the command for
    this invocation only (defaulted by the CLI to the stored command).
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
    if session.state != "running" and not (allow_failed and was_failed):
        if was_failed:
            raise ResumeError(
                "cannot resume session %s in state failed without "
                "--allow-failed (inspect the failure first, then retry with "
                "--allow-failed if the preserved branch should be reopened)"
                % session.session_id)
        raise ResumeError(
            "cannot resume session %s in state %s (resume expects a running "
            "session left behind by a crash/reboot)"
            % (session.session_id, session.state))

    active = _active_mounts(session)
    if active and not force:
        raise ResumeError(
            "refusing to resume session %s because its mount(s) still appear "
            "active: %s; use --force only after verifying no old agent process "
            "is still using the session"
            % (session.session_id, ", ".join(active)))

    session.add_event("resume-command", _command_detail(config.agent_command))
    if was_failed:
        session.add_event("resume-from-failed")
    if config.agent_kind != session.agent_kind:
        session.add_event("resume-agent", config.agent_kind)
    config.store.save(session)

    try:
        _ensure_shared_agent_state_dirs(config)
        for root in session.protected_roots.values():
            config.backend.start_daemon(root)
            if was_failed:
                config.backend.thaw(root)
            _cleanup_stale_mount(root, config.backend)
            config.backend.mount(root, agent=True)
        if was_failed:
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
