# Trusted MCP commit protocol

This document specifies how a contained Claude Code or Codex session can ask
`ccc-agent` to resolve kept BranchFS changes without giving the untrusted agent
process tree direct commit authority.

The protocol separates three questions:

1. **Who may ask?** Only the MCP server started by the exact initial supported
   Claude/Codex client launched for this CCC session.
2. **Who may authorize a destructive request?** A human, through MCP Form Mode
   elicitation carried over that same official client connection.
3. **Who may mutate real storage?** Only the trusted `ccc-agent` supervisor
   outside the sandbox, or a separate trusted operator command outside the
   sandbox. The plugin and MCP server never write the real underlay themselves.

The design is fail-closed. If exact-client registration, process admission,
transport hardening, human elicitation, path revalidation, or process identity
checking is unavailable, agent-facing commit/discard/abort is not executed.
Read-only status remains available and destructive requests return
`pending-external-approval` or an error for later operator review.

> **Terminology:** MCP means Model Context Protocol. The CCC control protocol is
> a separate, private Unix-socket protocol between the MCP server/hooks and the
> trusted supervisor.

## Scope

This protocol applies to direct `claude` and `codex` commands under
`confinement: "bwrap"` with system-installed hardening. It protects the
agent-facing per-turn operations:

- `ccc_commit_kept`
- `ccc_discard_kept`
- `ccc_abort_session`

The read-only `ccc_status` and `ccc_list_kept` tools do not require destructive
authority. `ccc_keep_kept` records that a change should remain in BranchFS and
does not write the real underlay.

This protocol does **not** replace session-end policy. In-scope automatic commits
are still made by the trusted supervisor according to configured policy. It also
does not remove the explicit trusted operator commands (`ccc-agent commit`,
`ccc-agent abort`, review commands) that run outside the sandbox.

## Trust boundaries

```text
trusted, outside bwrap
  ccc-agent supervisor
    - owns session state and review artifacts
    - owns the real-underlay write path
    - owns/pins the control-socket connection
    - validates process identities and authorization on every mutation
  BranchFS control/store and real underlay
  system-installed hardening library + digest manifest

trusted launcher inside bwrap
  namespace PID-1 ccc-agent runner
    - creates exactly one initial agent child
    - registers that exact child with the supervisor
    - does not process model output or choose commit paths

conditionally trusted protocol endpoint inside bwrap
  exact initial official Claude/Codex client
  CCC stdio MCP server started directly by that client
    - trusted only after process/connection admission
    - destructive authority enabled only after transport verification

untrusted inside bwrap
  model output and tool requests
  shells, scripts, extensions, and later descendants
  agent-written files in the BranchFS view
```

The real `/home` and `/storage` underlays, BranchFS stores, BranchFS daemon
control sockets, and review state are not exposed to the agent sandbox. The
agent sees writable BranchFS views and a narrowly scoped CCC control socket.

## Security invariants

The implementation maintains these invariants:

1. A token alone is never sufficient for an MCP mutation.
2. A process name such as `codex` or `claude` alone is never sufficient.
3. An arbitrary descendant of the agent cannot register itself as the official
   client after launch.
4. Only one exact initial client PID/start-time and one exact MCP
   PID/start-time/connection are pinned.
5. Destructive requests are accepted only on that pinned persistent
   connection.
6. Process identity and descriptor protection are rechecked before each
   destructive supervisor operation.
7. A model-generated tool call is only a request; it is not human approval.
8. The human confirmation names the exact action and current paths and travels
   as a nested MCP request on the official stdio connection.
9. The supervisor revalidates paths against the live remembered-kept set before
   copying or reverting anything.
10. Only supervisor code outside bwrap copies from the BranchFS view to the real
    underlay.
11. Process exit remains the authoritative session boundary. Hooks can improve
    turn behavior but cannot replace final freeze/status/policy/finalization.
12. Any missing or ambiguous prerequisite disables destructive MCP authority.

## End-to-end protocol

### Phase 0: system setup creates the hardening artifact

`ccc-agent setup --system` compiles the packaged
`ccc_agent/assets/security/ccc_client_hardening.c` for the container's libc and
architecture. Setup:

1. compiles to a temporary file with warnings treated as errors;
2. installs the shared object read/execute-only;
3. computes SHA-256 over the installed library;
4. writes a read-only adjacent `.sha256` manifest; and
5. records the library path as `mcp_client_hardening_library`.

Before using it, the runner verifies:

- the library and manifest are absolute regular files, not symlinks;
- neither file nor any parent directory is owned by the agent UID;
- neither file nor any parent directory is group/world writable; and
- the library SHA-256 matches the manifest using constant-time comparison.

A user-owned setup deliberately cannot authorize destructive MCP operations. A
missing compiler, build failure, unsafe path, missing manifest, or digest
mismatch leaves commit/discard/abort in external-review-only mode.

The digest is an integrity check anchored in the protected installation path; it
is not a signature against a compromised root/operator account.

### Phase 1: supervisor creates the contained session

The trusted supervisor:

1. creates the session and BranchFS branches;
2. creates a fresh Unix control socket outside the sandbox and sets mode `0600`;
3. generates fresh per-session control credentials;
4. mounts only the socket at `/tmp/ccc-agent/control.sock` inside bwrap;
5. records the host bwrap PID and `/proc/<pid>/stat` start time; and
6. derives whether this is a supported direct launch.

Destructive MCP is supported only when the configured direct command is exactly
the recognized `claude` or `codex` client under bwrap. Shell, SSH, server, debug
`confinement: "none"`, and otherwise ambiguous wrappers fail closed.

Inherited CCC session/control variables are removed before fresh values are
assigned, preventing a nested or stale session from reusing old authority.

### Phase 2: trusted PID 1 registers the exact initial client

The bwrap namespace starts a small CCC lifecycle runner as namespace PID 1. The
runner, not model-controlled code:

1. removes `CCC_AGENT_CLIENT_PRELOAD` from its own environment;
2. places the verified library in `LD_PRELOAD` only for the initial
   Claude/Codex child;
3. sets `CCC_AGENT_HARDEN_CLIENT=1` only for that child;
4. spawns the direct configured client exactly once; and
5. immediately sends `mcp-register-client` with the child's namespace PID.

The preload is intentionally **not** loaded into PID 1. PID 1 must remain
observable long enough for the supervisor to authenticate registration.

The supervisor does not trust the reported PID directly. It verifies:

- Unix `SO_PEERCRED` reports the same UID as the supervisor;
- the registration peer is under the exact recorded bwrap launch;
- `/proc/<runner>/status` reports that peer as PID 1 in the inner namespace;
- the reported namespace PID resolves to one unique direct host child through
  `/proc/<runner>/task/<runner>/children` and the child's `NSpid` values;
- that direct child matches the expected official client; and
- the launch PID/start-time still matches, preventing PID-reuse confusion.

The supervisor then pins `(client host PID, client start time)`. Re-registering a
different process is rejected. Consequently, a later descendant cannot rename
itself `codex` or `claude` and become eligible.

The token on this registration message is defense in depth and message framing;
`SO_PEERCRED`, the recorded launch identity, namespace-PID check, direct-child
resolution, expected-client match, and one-time pin are the authority checks.

### Phase 3: the official client starts and admits the MCP server

The bundled read-only plugin config makes the official client start:

```text
ccc-agent mcp-server --client claude
```

or:

```text
ccc-agent mcp-server --client codex
```

The MCP server opens one persistent connection to the CCC control socket and
sends `mcp-admit` during MCP initialization, before advertising tools/model work.
The supervisor obtains the connection peer from Linux `SO_PEERCRED` and verifies:

- peer UID equals the supervisor UID;
- peer PID/start-time is readable and stable;
- its direct parent is the exact previously registered client PID/start-time;
- no other MCP process/connection has already been pinned; and
- the supported launch boundary is still live.

It then pins:

```text
(MCP PID, MCP start time, client PID, client start time, connection object)
```

MCP-only control operations are accepted only on this exact persistent
connection. A new socket with the same token is not equivalent. A fake MCP
server launched by Bash or another descendant is not the direct child of the
registered client and is rejected.

### Phase 4: harden the client-to-MCP transport

The initial client inherits the verified preload and passes it to the MCP child.
The library constructor calls:

```text
prctl(PR_SET_DUMPABLE, 0)
```

If that call fails, the process exits with status 125. Non-dumpability prevents
same-UID descendants from reopening the client or MCP descriptor table through
`/proc/<pid>/fd` on the supported Linux setup.

The library also interposes descriptor-creating operations and adds close-on-exec:

- `pipe` / `pipe2`
- `socketpair`
- `dup` / `dup3`

This prevents unrelated later `exec` descendants from silently inheriting the
client/MCP transport. Launchers can still intentionally map selected MCP stdio
to file descriptors 0/1/2.

Admission does not trust `LD_PRELOAD` or `CCC_AGENT_HARDEN_CLIENT` as claims. The
supervisor actively probes that both the registered client and MCP peer deny
access to `/proc/<pid>/fd`. Without that observed property, MCP status can still
work, but destructive authorization is false.

The supervisor repeats the PID/start-time, parent relationship, pinned
connection, and `/proc/<pid>/fd` checks on every MCP-only mutation. If protection
or identity changes after admission, the request fails closed.

### Phase 5: a model requests a CCC tool

The model can request `ccc_commit_kept`, `ccc_discard_kept`, or
`ccc_abort_session`, but this request is not authorization.

For path operations, the MCP server first asks the trusted supervisor for the
current remembered-kept paths. If the request contains `paths`, every requested
path must still appear in that live set. Unknown, stale, malformed, or newly
invented paths are rejected. Omitting `paths` means the current kept set, not an
agent-supplied historical list.

Plugin policy adds another client-side guard:

- Claude destructive tools carry required-user-interaction metadata.
- Codex configuration sets the destructive CCC tools to per-tool `prompt` and
  does not install a blanket allow rule.

These client policies are defense in depth. The authoritative human gate is the
nested server-side elicitation described next.

### Phase 6: nested human Form Mode elicitation

For an authorized destructive request, the MCP server sends an
`elicitation/create` request back over the same official stdio MCP connection.
The form states the action and exact current paths and requires a boolean
`confirm` field.

The MCP server accepts confirmation only when all checks pass:

- the client advertised form-mode elicitation capability;
- the response uses the exact nested request ID;
- there is no MCP error;
- `result.action` is exactly `accept`;
- `result.content` is an object; and
- `result.content.confirm` is exactly boolean `true`.

Connection close, unsupported elicitation, malformed JSON, mismatched IDs,
cancel, decline, missing content, truthy non-booleans, or any other response is
treated as refusal. No control mutation is sent.

Because transport descriptors are protected, an untrusted sibling/descendant
cannot obtain the stdio endpoint through ordinary inheritance or
`/proc/<pid>/fd` and forge this nested response. The design assumes the official
Claude/Codex client correctly presents Form Mode to the human and does not treat
model text as a human form response.

### Phase 7: trusted signal reaches the supervisor

Only after affirmative elicitation does the MCP server send a private control
operation on its already pinned persistent socket:

```text
ccc_commit_kept   -> turn-resolve(decision=commit, paths=current paths)
ccc_discard_kept  -> turn-resolve(decision=discard, paths=current paths)
ccc_abort_session -> turn-request-abort
```

The control token authenticates the session, but the supervisor additionally
requires all of the following before dispatching a destructive operation:

1. operation is on the pinned connection object;
2. MCP PID/start-time still matches;
3. registered client PID/start-time still matches;
4. MCP peer is still the direct child of that client;
5. both descriptor tables remain hidden; and
6. this connection was granted destructive authorization at admission.

Short-lived lifecycle-hook connections cannot call these operations. Hook-only
workspace operations use a separate hook token. Ordinary agent processes that
know or inherit the general token still cannot satisfy the pinned-process and
pinned-connection checks.

### Phase 8: only the supervisor applies or reverts data

The `TurnController` runs in the trusted supervisor process and serializes
operations with a lock. For commit it:

1. verifies every path is still currently remembered as kept;
2. resolves each canonical visible path to a configured protected root and
   relative path;
3. reads the latest content from the live BranchFS mount;
4. copies only the selected files/directories, or applies selected deletions, to
   the root's real base path;
5. records permission-denied paths as kept instead of pretending success;
6. records committed decisions and allowed scopes so finalization agrees; and
7. saves durable session events/audit state.

The MCP server does not receive a BranchFS commit handle and never writes the
base path. It only receives the supervisor's structured result.

For discard, the supervisor calls BranchFS `revert_path` for only the selected
live changes, verifies that those changes disappeared from live status, and
updates the decision map. A failed revert raises an error rather than reporting
success.

### Phase 9: abort and process-exit finalization

An approved `ccc_abort_session` does not tear down a live client underneath its
tool call. Instead, the supervisor atomically records:

```text
policy.mode = throwaway
policy.mcp_abort_requested = true
```

Subsequent Stop hooks are forced to hold all live changes and cannot auto-commit
anything. Kept-review hooks also stop re-prompting. The agent should then exit.
At process exit, authoritative finalization freezes/inspects as required,
unmounts, and aborts the still-live BranchFS branches.

Abort does not and cannot roll back changes committed by earlier completed turns;
the tool description and elicitation state that explicitly.

## Why spoofing attempts fail

| Attempt | Rejection mechanism |
|---|---|
| Use the control token from another child | MCP-only mutation also requires the pinned process and exact connection. |
| Launch a fake `ccc-agent mcp-server` | Its parent is not the exact registered initial client. |
| Rename a later process to `codex`/`claude` | Registration pins the initial direct child PID/start-time once; names are not sufficient afterward. |
| Reuse a dead PID | `/proc/<pid>/stat` start time is part of every fingerprint. |
| Open a second control socket | The supervisor compares the connection object, not just credentials. |
| Inherit MCP stdio in a shell/tool | Preload forces close-on-exec on new transport descriptors. |
| Reopen parent/MCP stdio via `/proc/<pid>/fd` | Both processes are non-dumpable; supervisor verifies denial and rechecks it. |
| Set a fake `LD_PRELOAD` or hardening environment variable | Runner uses only the configured protected library after ownership/path/digest checks; supervisor observes procfs behavior rather than trusting the variable. |
| Modify the hardening library | Library, manifest, and path chain must be non-agent-owned/non-writable and digest must match. |
| Forge “yes” in model output | Server requires a nested Form Mode response with exact ID, `action=accept`, and boolean `confirm=true`. |
| Ask to commit an arbitrary path | MCP server and supervisor both constrain resolution to current remembered-kept live paths. |
| Call `turn-resolve` from a lifecycle hook | MCP-only operation is rejected unless it arrives on the pinned MCP connection. |
| Lose hardening or process identity after admission | Every destructive control request revalidates identity, parentage, connection, and descriptor hiding. |
| Fail any prerequisite | Status remains available; mutation becomes pending external review or returns an error. |

## Relationship to automatic commits and operator commands

“Only trusted code commits” does not mean “every commit requires an MCP prompt.”
There are three commit paths, all implemented outside the untrusted process tree:

1. **Policy auto-commit:** the trusted supervisor may commit in-scope changes at
   a Stop/process-exit boundary according to configured CCC policy.
2. **Human-confirmed MCP resolution:** the admitted MCP server obtains nested
   human confirmation, then asks the supervisor to resolve selected kept paths.
3. **External operator review:** a human runs trusted `ccc-agent review/commit`
   commands outside the sandbox against persisted session state.

Only path 2 uses this MCP admission and elicitation protocol. In every path, the
actual underlay mutation is performed by trusted supervisor/operator code, not
by agent code.

## Failure behavior

The protocol intentionally prefers loss of convenience over unsafe mutation:

- no supported direct client: no destructive MCP authority;
- no system hardening library: external review only;
- unsafe library path or digest mismatch: library is not loaded;
- registration ambiguity/failure: MCP admission fails;
- MCP admission failure: tools are not safely initialized;
- no observed `/proc` descriptor protection: status only;
- no Form Mode capability: destructive call fails;
- malformed/negative human response: no mutation;
- path changed or disappeared: operation rejects or reports stale state;
- permission failure while applying: path remains kept/pending;
- process or connection identity changes: connection is rejected;
- unexpected exception: supervisor returns an error and preserves branch state.

The branch and durable review artifacts remain available for trusted external
inspection whenever safe automatic completion is not possible.

## Security assumptions and non-goals

This is Linux process-bound authorization, not cryptographic process attestation.
It assumes:

- the host kernel, procfs semantics, bwrap, BranchFS, and trusted supervisor are
  not compromised;
- the system installation owner/root and protected configuration path are
  trusted;
- the official Claude/Codex client correctly implements MCP stdio and presents
  Form Mode elicitation to the human;
- the agent remains non-root and capability-limited inside the CCC container;
- the real underlay and BranchFS control/store remain outside the sandbox; and
- the configured container/runtime exposure does not give the agent an
  independent host-level write path to the same data.

It does not defend against:

- compromised root, kernel, supervisor, official client, or system installer;
- an intentionally malicious official client that fabricates human Form Mode
  acceptance;
- alternate writable aliases to the real underlay exposed by operator
  misconfiguration;
- rollback of data committed by earlier completed turns when abort is requested;
- distributed live-FUSE state or cluster-wide mount replication; or
- an operator explicitly invoking trusted external commit commands.

`container_run_access` and ambient Docker/runtime sockets are separate
containment considerations. Enabling a path that independently grants writes to
the real underlay defeats the filesystem boundary regardless of MCP admission.
Use full isolation where the threat model requires it.

## Implementation map

| Responsibility | Implementation |
|---|---|
| PID-1 child spawn and registration | `ccc_agent/runner.py` (`BWRAP_AGENT_RUNNER`) |
| Hardening artifact build and manifest | `ccc_agent/setup.py` (`build_mcp_client_hardening`) |
| Preload behavior | `ccc_agent/assets/security/ccc_client_hardening.c` |
| Registration, `SO_PEERCRED`, process pinning, per-request checks | `ccc_agent/control.py` (`ControlServer`) |
| MCP tools and nested Form Mode validation | `ccc_agent/mcp.py` (`MCPServer`) |
| Live kept-path validation and selective apply/revert | `ccc_agent/turn.py` (`TurnController`) |
| Session-end freeze/status/policy/commit/abort | `ccc_agent/runner.py` (`finalize_session`) |
| Claude/Codex MCP/plugin declarations | `ccc_agent/assets/plugins/` |
| Admission and spoofing tests | `tests/test_mcp.py` |
| Preload, bwrap, digest, and runner tests | `tests/test_runner.py`, `tests/test_setup.py` |
| Abort/Stop/finalization tests | `tests/test_turn.py` |

## Review checklist for future changes

Any change to this protocol should answer all of these before merging:

- Is the exact initial client still registered by trusted launch code rather
  than inferred from a later process name?
- Is PID reuse still prevented with start-time pinning?
- Can a second connection or child use a token to invoke MCP-only operations?
- Is hardening loaded only from a non-agent-writable, digest-verified path?
- Does the supervisor observe descriptor protection rather than trust an
  environment claim?
- Are identity and hardening rechecked on every destructive request?
- Does human confirmation use nested Form Mode on the same admitted transport?
- Are action, paths, response ID, `action=accept`, and boolean confirmation
  validated exactly?
- Are paths re-read from current BranchFS/decision state before mutation?
- Does only trusted supervisor/operator code touch the real underlay?
- Do failures preserve the branch and move to external review?
- Does process-exit finalization remain authoritative?
- Does abort prevent later Stop hooks from committing live changes?
- Do docs and tests state that earlier committed turns are not rolled back?
