# Trusted workspace-scope protocol

This document specifies how `ccc-agent` decides which live paths are workspace
scopes and may therefore be auto-committed by turn/session policy.

Workspace-scope authorization is separate from the
[trusted MCP commit protocol](trusted-mcp-commit-protocol.md):

- the **commit protocol** authorizes an explicit commit/discard/abort request;
- the **workspace-scope protocol** authorizes a policy change that can make future
  changes under a root eligible for automatic commit.

A workspace update does not call `chdir`, create or remount BranchFS, expose the
real underlay, or commit data immediately. It changes only the trusted
supervisor's persisted path-classification policy. Because later finalization may
copy changes under an allowed workspace to the real underlay, broadening that
policy is itself a privileged operation.

## Threat model

The contained agent and all ordinary descendants are untrusted. In particular,
a malicious same-UID process may be able to:

- read inherited environment values, including the lifecycle hook token;
- execute the bundled hook scripts itself;
- forge hook JSON/stdin and choose an arbitrary `--agent-session` value;
- call `ccc-agent turn-add-workspace` or `turn-remove-workspace` directly;
- rename itself `claude`, `codex`, or `hermes`;
- connect to the session Unix control socket with a copied token;
- create later child processes and attempt PID-reuse or `/proc/<pid>/fd`
  descriptor theft; and
- place workspace-looking text in an ordinary chat message.

Consequently, none of these facts grants workspace authority by itself:

- possession of the control or hook token;
- use of a read-only bundled hook;
- an expected hook command line or process name;
- an arbitrary shell hook event;
- an arbitrary path supplied by the model; or
- a string that merely looks like `[Workspace::v1: ...]`.

The outer `ccc-agent` supervisor, the protected BranchFS configuration, the
trusted namespace PID-1 runner, the system-installed hardening library, and the
official client protocol endpoint are inside the trust boundary. Root, a sandbox
escape, a compromised official client, or replacement of root-owned trusted
assets is outside this protocol's guarantees.

## Security invariants

1. A shell hook can never broaden auto-commit authority beyond the operator
   ceiling.
2. A hook path outside that ceiling is persisted only as a proposal; it is not
   added to `allowed_scopes`.
3. A hook may remove an unconfirmed proposal attributed to its supplied
   hook-session ID or narrow a hook-owned sub-scope, but cannot remove a
   static/operator or authenticated-client root.
4. Authenticated dynamic roots are accepted only from the exact registered
   initial client, its pinned MCP connection, or its trusted namespace PID 1.
5. PID/start-time, parentage, connection identity, and descriptor protection are
   revalidated for every privileged workspace replacement.
6. Authenticated root updates are complete replacement sets, not incremental
   untrusted add/remove commands.
7. Every root must be an absolute path under a configured protected BranchFS
   root after CCC alias canonicalization.
8. Empty authenticated replacement sets are valid and remove the source's
   dynamic roots.
9. Workspace changes never expose a real NFS underlay alias inside the sandbox.
10. Any missing, malformed, stale, ambiguous, or unhardened trust signal fails
    closed; the affected changes remain out of scope and reviewable.

## Persisted state model

The supervisor keeps four distinct categories in session policy:

| State | Meaning | Authority |
|---|---|---|
| `workspace_scope_ceiling` | Static/operator-authorized roots present before untrusted hook updates. | Trusted outer configuration/launch. |
| `mcp_workspace_roots` | Current complete dynamic root set from an authenticated client source. | Process/connection-pinned client or trusted PID 1. |
| `hook_workspace_proposals` | Out-of-ceiling hook hints with per-inner-session owners. | Untrusted; never auto-commit authority. |
| `hook_workspace_refs` | Hook-owned sub-scopes that are already inside the ceiling. | May narrow/refine existing authority, never broaden it. |

`workspace_scopes` and `allowed_scopes` are the effective policy derived from
static scopes plus authenticated roots and safe hook-owned sub-scopes. Legacy
state is upgraded conservatively: old hook-owned and authenticated dynamic roots
are excluded when the operator ceiling is first reconstructed.

All comparisons use normalized absolute paths and CCC's alias map, so equivalent
`/home/<user>` and `/storage/user/...` spellings do not create independent
sources of authority. User-facing spelling is retained where practical.

## Common process registration and transport hardening

Workspace authentication reuses the process-bound trust chain from the commit
protocol.

1. The outer supervisor starts bwrap and pins its host PID plus
   `/proc/<pid>/stat` start time.
2. The trusted namespace PID-1 runner starts exactly one initial direct client.
3. PID 1 sends `mcp-register-client` with the child's namespace PID.
4. The supervisor authenticates PID 1 with Unix `SO_PEERCRED`, verifies it is the
   expected namespace PID 1 beneath the pinned bwrap launch, resolves one unique
   direct host child using `/proc` and `NSpid`, checks the expected client name,
   and pins the child PID/start-time.
5. The verified preload makes the initial client non-dumpable and protects
   inherited descriptors. After registration, PID 1 also sets itself
   non-dumpable before its input/control path is used as workspace authority.
6. Before every privileged update the supervisor checks that the original bwrap
   PID/start-time boundary is still live and ancestral, then checks the pinned
   connection, process start times and parentage, and that the relevant
   runner/client/MCP descriptor directories remain inaccessible through
   `/proc/<pid>/fd`.

The session token is still required, but it is only message framing and defense
in depth. Kernel credentials, exact process identities, connection pinning, and
observed descriptor isolation provide the authority.

## Trusted workspace sources

### 1. Static operator scope and explicit launch workspace

Paths selected by trusted outer configuration (`workspace`, static
`allowed_scopes`, or equivalent operator options) form the initial ceiling.

For an ordinary direct launch with an explicit workspace, bwrap sets
`CCC_AGENT_CONFIRM_LAUNCH_WORKSPACE=1`. After exact-client registration and PID-1
transport hardening, PID 1 may confirm the launch cwd. An incidental server or
bootstrap cwd is not confirmed merely because the process started there.

### 2. Claude Code: MCP Roots

The bundled Claude plugin starts the CCC stdio MCP server as a direct child of
the exact registered Claude client. MCP admission pins:

```text
(MCP PID, MCP start time, client PID, client start time, Unix connection)
```

Only when the client and MCP process are descriptor-hardened does the server:

1. observe that the client advertises MCP Roots;
2. send `roots/list` after `notifications/initialized`;
3. on `notifications/roots/list_changed`, immediately replace dynamic roots with
   an empty set before requesting the new list;
4. if that change arrives while a list request is pending, mark the pending
   response stale, discard it when received, and issue a second list request;
5. accept only absolute local `file:` URIs (`file:///...` or localhost);
6. ignore remote/non-file/relative entries; and
7. send the complete resulting path set to `turn-confirm-workspace-roots` over
   the already pinned control connection.

The supervisor revalidates the MCP connection before applying the replacement.
No Form Mode prompt is used for routine root synchronization. Form Mode remains
reserved for explicit destructive commit/discard/abort decisions.

### 3. Codex app-server: trusted transparent JSONL observation

Codex does not rely on its shell hooks for workspace authority. For a direct
`codex app-server` launch, the trusted PID-1 runner transparently proxies the
JSONL protocol:

```text
external Codex client/UI
  -> trusted PID-1 stdin observer
  -> official codex app-server
  -> trusted PID-1 stdout observer
  -> external client/UI
```

The observer stages candidate roots by JSON-RPC request ID for:

- `thread/start`;
- `thread/resume`;
- `thread/fork`;
- `turn/start`; and
- thread archive/delete cleanup.

A request does not grant authority. Roots are applied only after the matching
server response succeeds. Failed, malformed, unmatched, or incomplete responses
grant nothing. Each tracked request receives a monotonic sequence number;
per-thread successful-response generations prevent an older out-of-order response
from overwriting a newer root or resurrecting a thread after archive/delete.
Resume may obtain its stored cwd/roots from the successful response. Fork
responses are tracked under the new thread ID rather than replacing the parent
thread. Roots are stored per thread and the supervisor receives their union, so
one concurrent thread cannot remove another thread's workspace. Archive/delete
removes only the named thread and can produce an empty replacement set.

The proxy preserves the original JSONL bytes on stdout/stdin. It does not modify
model requests or server responses. The launch cwd is included only when the
outer operator explicitly authorized it.

This integration exists in both CCC PID-1 lifecycle implementations:

- the foreground embedded runner; and
- `adaptive_pid1.py`, used by remote/server lifecycles.

Both register the exact client, apply the client preload, harden PID 1 after
registration, and use the same successful-response-only monitor.

### 4. Hermes: pinned in-process plugin channel

Hermes lifecycle code runs inside the exact official Hermes process, not in a
separate shell hook. The bundled plugin opens one persistent
`WorkspaceControlClient` connection. The supervisor admits it only when:

- `SO_PEERCRED` identifies the exact registered initial Hermes PID/start-time;
- the process descriptor table is hidden;
- the connection is the first and only pinned Hermes workspace connection; and
- each subsequent request still matches that identity and protection state.

The plugin prefers framework-owned `workspace`, `workspace_dir`, `cwd`, or
`current_working_directory` hook fields. For WebUI/API runtimes that guarantee
the server-side prefix, the exact leading
`[Workspace::v1: /absolute/path]` value may be used when the hook's documented
`platform` field is `api_server` (current Hermes) or `webui` (compatible
runtimes). The same text on Telegram or another messaging platform is ordinary
user-controlled content and is ignored. Missing workspace metadata does not fall
back to process `getcwd()`.

This trust is conditional on the WebUI/API adapter, not the model or user prose,
being the component that prepends the tag. Deployments that pass arbitrary API
message text through without enforcing that prefix do not satisfy this trust
assumption and must use a framework-owned workspace hook field instead.

Hermes keeps a locked map of active inner session IDs to roots. Every turn/session
switch sends the complete union, so concurrent WebUI conversations cannot remove
one another's scope. Session end removes only that session and sends the remaining
union (possibly empty). The shell-hook call remains a non-authoritative
proposal/cleanup hint.

Only trusted immutable plugins should execute inside the hardened official
Hermes process. Process pinning cannot distinguish trusted code from malicious
code already injected into that same process.

## Untrusted hook behavior

Claude, Codex, and Hermes integrations retain lifecycle hooks for compatibility,
turn finalization, proposal correlation, and cleanup:

```text
turn-add-workspace --agent-session ID PATH
turn-remove-workspace --agent-session ID PATH
```

The hook token prevents accidental or unrelated calls, but it is not treated as
authentication against the same-UID agent.

`turn-add-workspace` behaves as follows:

1. validate and canonicalize the path under a protected root;
2. release that hook session's previous hook-owned path/proposal;
3. if the path is already inside the operator ceiling, activate only that
   sub-scope without adding new authority;
4. if the path is already static or authenticated, record no new ownership; or
5. otherwise return `workspace-proposed`, persist the owner/path proposal, and
   leave `allowed_scopes` unchanged.

`turn-remove-workspace` may remove a proposal attributed to the supplied inner
session ID or its hook-owned sub-scope after the last recorded owner exits. It
cannot remove static scope or a root still present in `mcp_workspace_roots`. A
malicious forged removal can at most narrow hook-owned refinements or delete
untrusted proposal bookkeeping, causing denial-of-service/review work; it cannot
create broader auto-commit authority.

## Supervisor replacement algorithm

For an authenticated `turn-confirm-workspace-roots(paths)` request, the
`TurnController`:

1. validates every input as an absolute protected path;
2. canonicalizes aliases and removes duplicate spellings;
3. loads the previous authenticated root set;
4. removes old authenticated roots omitted from the new set unless they remain
   independently static/ceiling or hook-owned;
5. adds every new confirmed root to effective workspace and allowed scopes;
6. removes matching untrusted proposals;
7. persists the new complete authenticated set and an audit event; and
8. returns the effective workspace/allowed/proposal state.

The control server accepts this operation only from one of:

- the pinned hardened MCP connection;
- the pinned hardened in-process Hermes workspace connection; or
- the exact registered, post-registration-hardened PID-1 runner while both that
  runner and its registered client still hide their descriptor tables.

A raw socket client with the token, an unhardened client, a second connection, a
changed PID/start-time, changed parentage, or a descriptor-readable process is
rejected before the controller runs.

## Lifecycle cleanup

Before a contained process starts or resumes, `reset_agent_workspaces()` removes
stale hook ownership, unconfirmed proposals, and old authenticated client roots.
Static/operator scopes remain. Fresh client lifecycle signals then repopulate the
current roots.

At runtime:

- MCP Roots and Hermes send complete replacement sets;
- Codex tracks per-thread roots and sends their union;
- hook end events clean only their own proposals/refinements; and
- process-exit BranchFS finalization remains authoritative even if all dynamic
  signaling fails.

If a pinned MCP/Hermes workspace transport closes, the control server waits a
short teardown grace period. If the exact official client is still alive, it
internally replaces authenticated roots with an empty set. If the official client
has already exited, PID-1 teardown prevents further agent work and the last valid
roots remain only for immediate finalization. Thus a lost trusted signal cannot
leave stale policy active while the client continues running; affected live
changes fall back to normal review.

## Attack analysis

| Attempt | Result |
|---|---|
| Copy hook/control token and call `turn-add-workspace /storage/user` | Stored as an untrusted proposal; effective allowed scopes do not broaden. |
| Forge another inner session ID | May create/replace only that proposal ownership; no authority is granted. |
| Forge hook removal for a static or authenticated root | Root remains because the hook does not own that authority source. |
| Rename a descendant `codex`/`claude`/`hermes` | Rejected; exact initial PID/start-time was already registered once. |
| Open another control connection with the token | Rejected for workspace confirmation because it is not a pinned trusted peer/connection. |
| Reopen client/runner/MCP descriptors through `/proc` | Non-dumpability should deny access; if the supervisor observes readable descriptors, the update fails closed. |
| Send a Codex thread request that the server rejects | Candidate is discarded; no root changes. |
| Inject a fake Codex response from a model child | Client and PID-1 descriptor protection are revalidated; loss of protection denies the supervisor update. |
| Put `[Workspace::v1: ...]` in Telegram/user prose | Ignored because only WebUI/API platform labels may use a runtime-enforced leading tag. |
| Supply a remote MCP URI or path outside protected roots | Ignored or rejected before policy update. |
| Kill hooks, MCP, or plugin plumbing | Causes no new authority; affected paths remain reviewable at finalization. |

## Failure behavior

The safe degradation is always narrower authority:

- hook-only new path -> proposal;
- missing system hardening -> no authenticated dynamic update;
- failed PID/client registration -> no runner/MCP/Hermes authority;
- pinned transport loss while its client remains live -> authenticated roots are
  revoked to an empty replacement set;
- malformed MCP Roots or Codex JSON -> no update;
- failed Codex response -> no update;
- invalid/out-of-protected-root path -> request error;
- client/runner/MCP identity or descriptor change -> connection rejected; and
- plugin/control exception -> process-exit review remains authoritative.

No failure path converts an untrusted proposal into an allowed scope.

## Limitations

- This is Linux process-bound authorization, not cryptographic attestation.
- A compromised official client or trusted in-process Hermes plugin is inside the
  trust domain and can report false roots.
- Root, `CAP_SYS_PTRACE`, a sandbox escape, or replacement of trusted system
  assets can defeat process/descriptor checks.
- Exposing powerful ambient sockets/devices may enable attacks outside this
  protocol; use full isolation where required.
- Workspace confirmation binds path scope, not a snapshot of future bytes.
  Content authorization still occurs through turn/session policy and explicit
  kept-path review.
- Hooks can still cause denial-of-service by creating proposals or narrowing
  hook-owned refinements. They cannot broaden commit authority.

## Implementation map

| Concern | Implementation |
|---|---|
| Persisted ceiling/proposals/confirmed roots and effective policy | `ccc_agent/turn.py` |
| `SO_PEERCRED`, exact-client registration, pinned MCP/Hermes/runner checks | `ccc_agent/control.py` |
| Claude MCP Roots request/response handling | `ccc_agent/mcp.py` |
| Foreground PID-1 registration/hardening and Codex proxy | `ccc_agent/runner.py` |
| Adaptive/server PID-1 registration/hardening and Codex proxy | `ccc_agent/adaptive_pid1.py` |
| Successful-response/per-thread Codex root monitor | `ccc_agent/codex_workspace.py` |
| Hermes in-process workspace source validation | `ccc_agent/assets/plugins/hermes-ccc-containment/__init__.py` |
| Shell proposal adapters | `ccc_agent/assets/plugins/*/hooks/` and `ccc_agent/cli.py` |
| Controller and spoofing regressions | `tests/test_turn.py`, `tests/test_control.py`, `tests/test_ctl_socket.py` |
| MCP/process admission and Roots regressions | `tests/test_mcp.py` |
| Codex monitor/proxy regressions | `tests/test_codex_workspace.py`, `tests/test_adaptive_pid1.py`, `tests/test_runner.py` |
| Hermes/plugin regressions | `tests/test_scripts.py` |

## Review checklist

When modifying this protocol, verify all of the following:

- [ ] Hooks cannot add an out-of-ceiling path to `allowed_scopes`.
- [ ] Hooks cannot remove static or authenticated roots.
- [ ] Exact client PID/start-time registration remains one-time and PID-1-bound.
- [ ] Runner, client, MCP, and Hermes workspace connections are revalidated per
      privileged request.
- [ ] Descriptor-readability causes workspace authorization to fail closed.
- [ ] MCP Roots accepts only local absolute file roots and handles list changes.
- [ ] Codex waits for matching successful responses and preserves concurrent
      thread roots, including fork/archive behavior.
- [ ] Both foreground and adaptive PID-1 runners register, harden, and observe
      Codex consistently.
- [ ] Hermes accepts only framework-owned fields or a runtime-enforced WebUI tag
      on `platform=api_server`/`webui`, rejects messaging-platform prose, keeps
      concurrent session roots as a union, and removes only the ending session.
- [ ] Invalid/out-of-protected paths fail before state mutation.
- [ ] Resume/start cleanup removes stale dynamic roots but preserves static scope.
- [ ] Full tests, static checks, wheel asset inspection, and packaged-source tests
      pass before release.
