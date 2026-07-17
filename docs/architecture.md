# Architecture

`ccc-agent` is a trusted supervisor for reviewable filesystem sessions. It does
not implement BranchFS itself and it does not own privileged FUSE mounting. Its
job is to create a branch session before the agent runs, launch the command in a
protected view, and decide what to do with the branch after real status is known.

## Components and trust split

```text
trusted outside sandbox:
  ccc-agent CLI / supervisor
  session store and review artifacts
  BranchFS store and daemon socket
  real underlay paths
  optional local FUSE sidecar

untrusted inside sandbox:
  agent process tree (codex/claude/hermes/opencode/shell/...)
  writable BranchFS branch views
  agent runtime commands and child processes
```

The agent can produce branch deltas. Only the trusted supervisor can freeze,
inspect, selectively apply, or discard them.

| Component | Responsibility | Must not do |
|---|---|---|
| `ccc-agent run` | Create session, create/mount branches, assemble sandbox, run command, finalize. | Rely on agent self-report for commit decisions. |
| `ccc-agent review/diff/commit/abort/...` | Operator control over persisted sessions. | Bypass policy accidentally. |
| BranchFS | Lazy branch views, deltas, tombstones, freeze/thaw/status primitives. | Decide human policy or agent lifecycle. |
| FUSE sidecar | Local privileged mount plumbing when needed. | Classify paths or commit data. |
| Agent plugins/hooks | Signal lifecycle turn/workspace boundaries and configure the client-launched MCP server. | Directly commit real data. |
| CCC stdio MCP server | Expose compact kept state; relay nested human elicitation over the official client connection. | Admit itself by token claims or bypass supervisor process admission. |

## Session lifecycle

```text
created -> mounting -> running -> finalizing -> frozen
        -> auto-committed | pending-review | committed | aborted | failed
```

Main flow:

1. `ccc-agent run` creates a session record and one BranchFS branch per protected
   root. The branch name is the session id.
2. BranchFS agent mounts are created. Agent-visible mounts do not expose BranchFS
   commit controls.
3. The command runs with `CCC_AGENT_SESSION` and, in bwrap mode, a per-turn
   control socket mapped to `/tmp/ccc-agent/control.sock`.
4. Native plugins may call `turn-finalize` at turn boundaries.
5. Process exit always performs session-end finalization.
6. Finalization freezes the branch, reads BranchFS status, splits ignored noise,
   evaluates policy, writes review artifacts, and applies the decision.
7. Commit is selective: `ccc-agent` applies only reviewed policy-visible changes
   and then discards the rest of the branch.

Commit failures never abort automatically. If applying changes fails, the branch
is preserved and the session becomes `failed` or `pending-review` for recovery.

### Agent-facing MCP admission

For direct Claude/Codex bwrap launches, the trusted namespace PID-1 runner
registers the exact initial client PID/start-time immediately after spawning it
and before waiting for agent work. Registration is accepted only from that
launch's PID 1 and resolves its direct child through host `/proc` plus `NSpid`.
The client then starts the bundled stdio MCP server. The supervisor uses Unix
`SO_PEERCRED` and admits only an MCP process whose direct parent is that exact
registered client; a later descendant merely naming itself `claude`/`codex`
cannot gain destructive authority. The first eligible persistent connection is
pinned to MCP PID/start-time and client PID/start-time. For recognized Codex
server-wrapper topologies, Codex may strip the CCC environment before spawning
plugin MCP processes; the MCP server recovers only the session ID, control socket,
and control token from the launcher-created read-only handoff. If no exact initial
client was registered, the supervisor may pin a direct MCP child of the live Codex
app-server inside the launch boundary for status/read-only use, but marks the
transport non-destructive and grants no workspace authority. Process identity
alone is not commit authority: setup
also installs a root-owned client preload
that makes the trusted client/MCP processes non-dumpable and their transports
close-on-exec. The supervisor probes that `/proc/<pid>/fd` is inaccessible for
both processes before enabling destructive MCP operations. Without this verified
transport boundary, status remains available but commit/discard returns pending
external approval. Ordinary `turn-resolve`/`turn-approve` connections are
rejected; lifecycle hook calls are unchanged. This is Linux process-bound
authorization, not cryptographic process attestation. See the complete
[trusted MCP commit protocol](trusted-mcp-commit-protocol.md) for the trust
chain, exact checks, spoofing analysis, failure behavior, assumptions, and
implementation map; [Agent integration](agent-integration.md#mcp-process-and-transport-admission)
covers the user-facing integration.

### Authenticated live workspace scopes

Workspace scope is commit authority: a newly allowed root can make later changes
auto-committable even without an explicit MCP commit call. Shell lifecycle hooks
therefore do not authenticate additions. A hook path already beneath the static
operator ceiling may refine that scope; any broader/new hook path is recorded as
a proposal and leaves `allowed_scopes` unchanged. Hook removal can clean its own
proposal or narrow a hook-owned sub-scope, but cannot remove static or
authenticated roots.

Dynamic roots come from complete per-logical-session replacement records reported
by authenticated client channels:

- Claude uses MCP `roots/list` on the pinned hardened MCP connection;
- direct Codex app-server launches are observed by the trusted foreground or
  adaptive PID-1 JSONL proxy and applied only after matching successful server
  responses; and
- Hermes uses a persistent channel from the exact hardened initial process,
  accepting framework workspace fields or the authoritative leading WebUI tag
  only when `platform` is `api_server` (current Hermes) or `webui`.

Trusted PID 1 hardens itself after exact-client registration. Every replacement
revalidates the relevant runner/client/MCP connection and descriptor isolation,
then validates all paths under protected BranchFS roots. Missing or spoofable
signals narrow authority and leave changes for review. See the complete
[trusted workspace-scope protocol](trusted-workspace-scope-protocol.md).

### Optional per-logical-session delta routing

When explicitly enabled for a supported vendor, ccc-agent creates opaque nested
BranchFS branches over the live outer session branch. On a fresh-proc runtime
that permits Codex's nested user namespace, a package-owned read-only Codex
bwrap adapter can route one documented thread ID into its pre-provisioned child.
The adapter is not an authority endpoint: it cannot admit workspaces, create
routes, or commit data. Bound-proc runtimes instead use the trusted
external-sandbox adapter, retain the authoritative outer bwrap, and classify
writes as shared/unattributed.

At thread end, children freeze and merge only into the outer branch. Final outer
status is reconciled against route snapshots, the outer baseline, merge outcomes,
and post-merge fingerprints. Only exact attributed paths beneath that route's
admitted roots may auto-apply; conflicts, shared influence, stale fingerprints,
or unresolved route states force pending review. The outer branch remains the
complete authoritative boundary. See
[Per-logical-session delta routing](session-delta-routing.md).

## Sandbox layout in `bwrap` mode

`confinement: "bwrap"` is the real execution boundary. It uses rootless
bubblewrap user/mount/PID namespaces; no container `CAP_SYS_ADMIN` is required
for bwrap itself.

Typical paths visible inside the sandbox:

```text
/usr /etc /opt                    read-only OS/image binds
/bin /sbin /lib /lib64            usrmerge symlinks or read-only binds
/proc                             bound or read-only, depending on bwrap_proc_mode
/dev                              container device tree by default, or minimal dev with --full-isolation
/tmp                              private tmpfs
/var                              read-only container /var by default
/run                              container /run by default
/storage                          BranchFS branch view, read-write
/home/<user>                      same branch or branch subdir, read-write
~/.codex ~/.claude ~/.hermes      shared direct agent state by default
/ccc-agent/plugins/...            read-only bundled plugin assets when injected
/tmp/ccc-agent/control.sock       per-turn control socket inside sandbox
```

Deliberately absent:

- real underlay paths behind protected roots;
- BranchFS stores and daemon sockets;
- generated session/control/review state except the limited control socket;
- commit-capable BranchFS control files in the agent mount.

`container_run_access` defaults to true so the sandbox can use runtime services
that the outer environment intentionally exposes, such as Docker sockets,
ssh-agent sockets, the FUSE sidecar socket, or `/dev/fuse`. This is a pragmatic
compatibility tradeoff, not a full hostile-container escape boundary. Use
`ccc-agent run --full-isolation` or `container_run_access: false` to omit ambient
`/run`, `/var`, and `/dev` access.

`confinement: "none"` is only a debug mode. It runs the command with its current
directory inside the branch mount but does not hide absolute paths. It is not a
security boundary.

## Alias model

When two user-visible paths refer to the same backing data, they must come from
one BranchFS branch. The common case is home storage:

```text
real root:      /storage
branch view:    /storage
home alias:     /home/<user> -> /storage/user/<container-or-home-subdir>
```

Creating separate BranchFS roots for `/home/<user>` and `/storage/user/...` would
produce incoherent aliases for the same files. `home_subdir` and the alias map
exist to avoid that.

## Review artifacts and cache validity

Generated review artifacts live under:

```text
<state_dir>/<session-id>/reviews/
  session.json
  summary.md
  status.<root>.json
  ignored.<root>.json
  warnings.<root>.json
  policy-decision.json
  route-reconciliation.json        # when session-delta routing is enabled
  routes/<route-id>/{route,status,merge}.json
```

Rules:

- Live states (`created`, `mounting`, `running`, `finalizing`) use live BranchFS
  status. Cached review files from an earlier freeze are ignored.
- Finalization freezes the branch and rewrites generated review artifacts from
  fresh status.
- `ccc-agent thaw` reopens a pending branch and removes generated review files
  because the branch is mutable again.
- Operator-authored files in the review directory, such as notes or patches, are
  preserved across thaw/freeze cycles.

## BranchFS and FUSE plumbing

BranchFS is responsible for filesystem behavior:

- O(1) branch creation;
- lazy inheritance from the base/parent;
- deltas for changed files;
- tombstones for inherited deletes;
- freeze/thaw/status/revert/commit/abort primitives;
- agent mounts that hide commit-capable control paths.

When the app container cannot mount FUSE directly, a local sidecar performs the
privileged mount operation:

```text
branchfs client process
  -> fusermount3 shim / sidecar client
  -> local ccc-fuse-sidecar socket
  -> /dev/fuse + mount(2)
  -> FUSE fd returned to BranchFS
```

The sidecar is intentionally policy-free. It may log the `CCC_AGENT_SESSION` for
audit, but it does not decide allowed paths or commits.

## Agent state handling

Known agent runtime state (`~/.codex`, `~/.claude`, `~/.hermes`, selected Claude
runtime paths) is direct-bound read-write over the BranchFS home view by default.
That state is owned by the agent tools and persists immediately. It is not part
of project review/commit/rollback unless the user opts into
`--protect-agent-state`.

Trusted plugin assets are different: they are package/root-owned files mounted
read-only for a matching contained run. The untrusted agent can load them but not
edit them.

## Completion model

Process exit is the authoritative completion signal because it works for every
command. Native hooks improve interactive UX by adding turn-boundary commits and
kept-path prompts, but hook failure cannot grant commit authority. If a plugin
fails to load or an agent version changes hook behavior, session-end finalization
still freezes the branch and applies policy based on BranchFS status.

## Limits

`ccc-agent` protects configured filesystem roots and commit authority. It does
not provide:

- full VM isolation;
- network isolation;
- proof against every container escape if powerful sockets/devices are exposed;
- strict same-file multi-writer semantics for concurrent sessions;
- distributed live-FUSE replication between nodes.

The global/durable operation is the reviewed commit to the real underlay, not the
live FUSE mount.
