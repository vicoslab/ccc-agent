# ccc-agent architecture: trust boundaries and contained roots

This document covers the runtime mechanics. The accepted cross-repo design
lives in the workspace root (`CCC_AGENT_BRANCHFS_PROTECTION_REVIEW_DESIGN.md`);
this is the implementation view.

## Trust split

```text
untrusted: the agent process tree (codex/claude/hermes/... and children)
trusted:   ccc-agent run / ccc-agent / ccc_agent (supervisor)
           branchfs daemon + store
           ccc-fuse-sidecar (privileged FUSE broker)
```

The supervisor launches the agent inside a **rootless bwrap sandbox** (no extra
container privilege); bwrap is part of the untrusted-launch boundary, not a
privileged component.

The agent can *produce branch deltas*. Only the supervisor can freeze,
inspect, commit, or abort them. This holds because:

1. agent-visible paths are BranchFS **agent mounts** (`--agent`): the
   `.branchfs_ctl` control file and `@branch` virtual dirs are not exposed;
2. the BranchFS store (deltas, tombstones, metadata, `daemon.sock`) lives
   outside the agent view — commit requests are only accepted over that
   socket;
3. session state (`<state_dir>/<session-id>/session`), review artifacts,
   mountpoints, and per-turn control sockets are grouped under one
   `<state_dir>/<session-id>/` bundle outside the view and additionally covered
   by the `.ccc-agent` deny pattern;
4. hooks invoke `ccc-agent finish-turn` (records an event) and
   `ccc-agent check-before-final` (reads live status; exit 2 asks the
   agent to revert policy violations, bounded by
   `max_policy_repair_attempts`) — there is no hook path that freezes or
   commits.

## Session lifecycle (process-exit completion, first milestone)

```text
created -> mounting -> running -> finalizing -> frozen
        -> auto-committed | pending-review | committed | aborted | failed
```

- `ccc-agent run` materializes one branch per protected root (branch name =
  session id), mounts agent views, runs the command with
  `CCC_AGENT_SESSION` set, and finalizes on exit.
- If a node/container reboots while a session is `running`, `ccc-agent resume
  <session>` reuses the existing branch bundle, re-mounts the saved roots, runs
  the stored `agent_command` by default (or a custom command after `--`), and
  then follows the same process-exit finalization path. Resume also supports
  `pending-review` follow-up work by thawing the preserved branch before
  mounting. `aborted` sessions are restartable under the same session id, but
  because abort has already discarded the branch delta, resume recreates an
  empty branch from the current base. Custom resume commands do not overwrite the
  stored original exec.
- Freeze happens **after** completion — and, for harnesses with blocking
  Stop hooks, after the bounded self-repair loop (`check-before-final`) has
  allowed the stop — then `branchfs status --json` per root feeds the policy
  engine.
- `pending-review` keeps branches frozen; the branchfs daemon may exit (it
  auto-exits with its last mount) — `ccc-agent commit/abort SESSION [SESSION ...]`
  re-ensures it from session metadata (`branchfs start-daemon --base ... --storage ...`).
- Commit failures never abort: the branch is preserved and the session is
  marked `failed` for manual recovery.
- BranchFS branches use lazy live-base inheritance: a running session keeps its
  own deltas/tombstones, while untouched inherited paths may reflect commits
  from other sessions. Same-path parent changes are handled at commit/review
  time. Clean text 3-way merges are treated like disjoint changes; unclean
  overlaps/binary/type/delete conflicts are recorded but do not make low-level
  commit fail by default. ccc-agent must surface those records to both blocking
  LLM repair hooks and human review artifacts.
- Nested agents: a shim or `ccc-agent run` invoked with `CCC_AGENT_SESSION`
  already set reuses the outer session — one review unit per task, no branch
  explosion.

### Review artifacts and cache validity

`<state_dir>/<session-id>/reviews/` is a durable review snapshot, not a live
status database. `ccc-agent diff` may use cached `status.*.json` only when the
session is quiescent (`pending-review`, terminal states, or a preserved failed
session). For live states (`created`, `mounting`, `running`, `finalizing`), it
must read live BranchFS status and ignore any old review cache left by a previous
freeze/thaw cycle.

Finalization always recomputes status after freezing and rewrites generated
review files from scratch: `status.*.json`, `ignored.*.json`, `warnings.*.json`,
`policy-decision.json`, `summary.md`, and `session.json`. `ccc-agent thaw` clears
those generated artifacts after BranchFS thaw succeeds because the branch is
mutable again; non-generated operator files such as notes or saved patches remain
in the review directory. Cached tombstone/delete entries whose underlying base
path is absent are filtered from review display because they are no-op remnants,
not real deletes.

## Contained root layout (bwrap mode)

`ccc_agent.runner._bwrap_command` builds a bubblewrap invocation that assembles
a rootless user+mount+pid namespace — no container `CAP_SYS_ADMIN`, just
unprivileged user namespaces. The agent runs as the calling uid mapped to 0
inside the namespace; the namespace (and all its mounts) disappears with the
agent process. Layout the agent sees:

```text
/usr /etc /opt                    read-only binds of the container image
/bin /sbin /lib /lib64            recreated as the host's usrmerge symlinks
/proc                             bound from the container (bwrap_proc_mode:
                                  bind|ro; "fresh" needs systempaths=unconfined)
/dev                              existing container device namespace (rw bind by
                                  default, including container-visible /dev/fuse;
                                  bwrap minimal dev with --full-isolation)
/tmp                              fresh tmpfs (session-scoped)
/var                              existing container /var (ro bind by default;
                                  includes conventional /var/run socket paths;
                                  omitted with --full-isolation)
/run                              existing container runtime namespace (rw bind
                                  by default; omitted with --full-isolation or
                                  container_run_access=false)
/storage/user                     BranchFS agent view (rw) — overlays + hides
                                  the real underlay at the same path
/home/<user>                      same view or its home_subdir (rw)
~/.codex ~/.claude ~/.hermes      direct shared rw binds over the home view
                                  (default agent/system state, not BranchFS)
/ccc-agent/plugins/…              CCC agent plugin root (ro) when supported
~/.codex/plugins/ccc-agent       Codex plugin scan path (ro bind on top of
                                  shared ~/.codex when Codex requires it)
/tmp/ccc-agent/control.sock       per-turn control socket (per-turn mode;
                                  under private /tmp so it does not depend on
                                  permissions in the bound container /run)
```

The CCC agent plugin is a **read-only** bind of root-owned package assets,
injected only for a contained run of the matching agent (Claude `--plugin-dir`,
Codex in-sandbox plugin path, Hermes `HERMES_BUNDLED_PLUGINS`). The untrusted
agent can load it but never edit the hook source. Agent tool homes (`~/.codex`,
`~/.claude`, `~/.hermes`) are direct shared rw binds by default, outside BranchFS
commit/review/rollback. Set `protect_agent_state: true` or run with
`--protect-agent-state` to omit those binds and make the user handle any
agent-state merges through BranchFS review. A failed/missing plugin degrades to
process-exit review — it never grants commit authority. See
`docs/agent-integration.md`.

Deliberately absent: real `/storage/*` underlays (the view `--bind` overlays
the visible path), the BranchFS store, `daemon.sock`, the supervisor state dir,
and any other `/storage` mount. `/home/$USER` and `/storage/user` bind the
**same** view (alias rule), never two branches.

The container's existing `/run`, read-only `/var`, and `/dev` are **not**
treated as data that ccc-agent must hide by default. CCC containers already
isolate those namespaces from the host unless the container deployment
intentionally exposes a socket or device. Therefore bwrap mode binds container
`/run`, `/var` (read-only), and `/dev` into the sandbox by default so agents can
use container-provided runtime services such as Docker (including the default
`/var/run/docker.sock` path), ssh-agent, the FUSE sidecar socket, or `/dev/fuse`
when that container has them.
This is an intentional escape-capability tradeoff: a powerful socket/device such
as Docker or the FUSE sidecar may let an agent reach data paths outside the
BranchFS view. That risk is considered deployment-authorized system access, not
a violation of ccc-agent's primary goal (protect and review normal writes to
`/home`/`/storage`). For stricter containment, run `ccc-agent run
--full-isolation` or set `container_run_access: false`; that restores the older
no-ambient-runtime behavior aside from ccc-agent's own control socket and uses
bwrap's isolated minimal `/dev` instead of binding the container device tree.

The agent gets a scrubbed environment (`--clearenv` + an explicit `--setenv`
allowlist). No network or proc isolation is enforced by design; the boundary
is filesystem confinement plus PID-namespace process isolation, not a full
container-escape prevention boundary. Rootless bwrap maps the user id and one
primary group id into the user namespace. It cannot preserve every outer
supplementary group in the common unprivileged path; if default runtime access is
enabled and `/var/run/docker.sock` is accessible outside only via a supplementary
group, `ccc-agent` maps that socket group as the sandbox gid so Docker remains
usable. Set `bwrap_gid` explicitly to override this auto-selection.

In **`none` mode** (debug only) the agent runs with its cwd inside the mounted
view but nothing else isolated — **not a security boundary**: absolute-path
writes go straight to the real underlay and bypass the view. Use it only to
exercise the policy/commit pipeline without bwrap.

## FUSE plumbing

Normal CCC app containers have no `CAP_SYS_ADMIN`; BranchFS mounts go through
`ccc-fuse-sidecar`:

```text
branchfs (unprivileged, in-container)
  -> fusermount3 shim                  (PATH-installed by CCC base image)
  -> /run/ccc-fuse-sidecar/fuse.sock   (host sidecar, SYS_ADMIN)
  -> /dev/fuse open + mount(2), fd passed back SCM_RIGHTS
```

The sidecar stays policy-free. Path translation between the container and
sidecar namespaces is the sidecar's Docker-inspect mode (see that repo's
README); BranchFS mountpoints under the per-session
`<state_dir>/<session-id>/mounts/...` bundle must resolve to the same host paths
in both namespaces or be translated.

## Runtime validation

Everything in `tests/` runs without FUSE. End-to-end validation needs a host
with `/dev/fuse` (via `ccc-fuse-sidecar`) and unprivileged user namespaces
(e.g. `donbot-domen-cuda10`):

```bash
# bwrap confinement, real FUSE, full commit/review cycle:
scripts/test-e2e-bwrap.sh      # in-scope auto-commit, out-of-scope pending,
                               # /usr read-only, underlay + store hidden
# debug pipeline without a sandbox:
scripts/test-e2e-none.sh
```

bwrap's only host requirements are an unprivileged-userns-capable kernel and
the `bwrap` binary (`bwrap_bin` in config). A *fresh* `/proc` (`bwrap_proc_mode:
fresh`) additionally needs the container's `/proc` masks cleared
(`--security-opt systempaths=unconfined`); otherwise use the default bound
`/proc`.
