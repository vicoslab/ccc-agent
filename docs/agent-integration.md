# Agent integration

`ccc-agent` can wrap any command. Native integrations for Codex, Claude Code, and
Hermes add turn-boundary convenience, but the authoritative safety path remains
process-exit freeze/status/policy review.

## Invocation matrix

| Invocation | Boundary | Plugin behavior | Review behavior |
|---|---|---|---|
| `ccc-agent run -- codex exec "..."` | Process exit | Codex plugin cache may be mounted; one-shot exit is enough. | Session-end finalize. |
| `ccc-agent run -- claude -p "..."` | Process exit | Claude plugin seed may be mounted, but one-shot exit is enough. | Session-end finalize. |
| `ccc-agent run -- hermes "..."` | Process exit | No default Hermes per-run plugin env. | Session-end finalize. |
| `ccc-agent run -- codex` | Interactive turns + process exit | Codex plugin Stop hook, version-dependent. | Workspace changes may commit per turn; kept paths reviewed later. |
| `ccc-agent run -- claude` | Interactive Stop hooks + process exit | Claude hooks from the enabled `ccc@ccc-agent` plugin, if active. | Workspace changes may commit per turn; kept paths reviewed later. |
| `ccc-agent run --serve codex -- <ssh/app-server wrapper>` | Server process + inner sessions | Treats the contained command as a server/runtime wrapper for the named agent. | `ccc-agent` prints nothing on the SSH stream; at process exit it commits workspace changes and keeps other paths for later review. |
| `ccc-agent run -- <other command>` | Process exit | No native plugin required. | Session-end finalize. |

## Plugin/config model

For default setup-generated configs, `ccc-agent run` does **not** append
agent-specific argv. Instead:

1. setup writes persistent tool config where the tool supports it;
2. `ccc-agent run` may bind trusted package assets read-only when a matching
   contained agent needs files inside its runtime state;
3. it may set non-interactive plugin-discovery environment such as Claude's seed
   path for an explicitly identified agent or server wrapper;
4. the trusted control socket is available for best-effort turn operations when
   hooks run.

Manually configured `agent_plugins` may still specify `argv` or `setenv`.
Argument activation remains restricted to direct agent CLI invocations; safe
asset-discovery environment can reach an explicitly identified server wrapper.

Use `--serve AGENT` for server-style entrypoints such as Codex app-server, Claude
remote server wrappers, or a Hermes gateway launched through SSH. Persisted
server sessions use the `AGENT-remote` label (for example `codex-remote`) in
`ccc-agent list`. Server mode is intended for protocols that parse stdout/stderr
themselves: `ccc-agent` emits no banner, finish line, review text, or prompt during
the wrapped server lifecycle, and it does not inject agent-interactive argv into
the server command. Without an explicit `--workspace`, the SSH launch directory
is used only as the server process cwd and is not an auto-commit scope. Trusted
inner-session hooks establish and own the active workspace scopes; changes made
before a workspace hook arrives are kept rather than auto-committed. Before the
final freeze server mode applies the same default as turn
hooks: commit in-workspace changes, remember non-workspace changes as kept in the
branch, and leave the session reviewable if anything still needs later attention.
The SSH shell router uses this mode automatically for detected Codex/Claude/Hermes
remote commands.

## Adaptive SSH process lifecycle

`--serve` controls protocol-safe output and review defaults; it does not decide
whether the command is foreground or a daemon. The packaged SSH router invokes
all broadly detected Claude, Codex, and Hermes requests with:

```text
--lifecycle adaptive
```

The router does not inspect private operations such as `--serve`, `--bridge`,
`app-server`, or `proxy`. Inside bwrap, namespace PID 1 classifies the opaque
process behavior:

- a command that remains alive through the bootstrap window is permanently
  foreground; when it exits, leaked helpers are killed with the PID namespace;
- an early nonzero exit fails normally;
- an early zero exit with no descendants is a one-shot command;
- an early zero exit with descendants becomes a handoff candidate and is accepted
  only after descendant stability plus authoritative stdout and stderr EOF;
- candidates that retain either output stream are rejected and cleaned up within
  the detach timeout.

A small trusted supervisor owns bwrap and BranchFS after a clean handoff, while
the bootstrap SSH command returns. That true service remains visible as an
`AGENT-remote` session. A foreground-locked or rejected adaptive server-mode run
is classified as `AGENT-remote-bridge`: it is omitted from `ccc-agent list`, its
branch is aborted rather than committed when the bridge exits, and its session
bundle is removed immediately after a successful discard. Bridge/proxy commands
still use independent containment while running; there is no active-lane or
namespace-attachment router. Multiple true services therefore keep independent
session IDs and branch views.

Interactive SSH commands with a TTY stay on the existing foreground lifecycle so
terminal ownership/job control is preserved. Ordinary local `ccc-agent run`
commands also remain foreground unless `--lifecycle adaptive` is explicitly
selected.

If no plugin matches, the plugin directory is missing, the command uses a mode
that disables plugins, or the agent version ignores hooks, the run degrades to
session-end review. Hook failure never grants commit authority.

Disable plugin injection at setup/config time with:

```bash
ccc-agent setup --system --no-agent-plugins
ccc-agent setup --user --no-agent-plugins
```

Configuration-level disabling uses `agent_hook_mode: "disabled"` and an empty
`agent_plugins` map. A run with disabled plugins still finalizes at process exit.

## Codex

Contained Codex receives the bundled Codex plugin mounted at its in-sandbox plugin
cache path. `ccc-agent setup --system` writes the enable/trust block to
`/etc/codex/config.toml`; `ccc-agent setup --user` writes the same marked block to
`~/.codex/config.toml`. Codex 0.136+ then treats the read-only cache bind as an
enabled/trusted installed plugin when the contained run provides it.

`ccc-agent` no longer adds `--dangerously-bypass-approvals-and-sandbox` or any
other Codex sandbox/Yolo flag by default. If you want Codex's danger-full-access
mode inside the outer CCC boundary, pass the Codex flag yourself; otherwise Codex
owns its own sandbox behavior and any nested-sandbox incompatibility is surfaced
by Codex.

Interactive Codex Stop hooks are version-dependent. If the hook runs, it calls
`turn-finalize --default-keep`. If it does not run, changes are handled at
session end.

## Claude Code

Contained Claude Code uses the packaged `ccc@ccc-agent` Claude plugin, not
settings-level hook duplication and not a session-only `--plugin-dir` flag.
The production path is Anthropic's container/CI seed mechanism:
`CLAUDE_CODE_PLUGIN_SEED_DIR` points to a read-only, pre-populated plugin tree.
System setup uses `/opt/claude-seed`; user setup uses
`~/.local/share/ccc-agent/claude-seed`.

The pip package owns all static plugin files. `ccc-agent setup` materializes the
complete seed directly without invoking Claude or requiring an image rebuild.
An image can optionally create the system seed in a separate layer:

```bash
python -m ccc_agent.claude_plugin --seed-dir /opt/claude-seed
```

Setup-managed settings enable `ccc@ccc-agent` but do not duplicate hooks or
declare another marketplace source. Setup initializes the user's Claude plugin
metadata from the seed so hooks are active on the first invocation while
preserving unrelated plugins. At runtime, `ccc-agent run` mounts the seed
read-only and sets `CLAUDE_CODE_PLUGIN_SEED_DIR` inside bwrap. Claude remote
servers may rebuild the environment before starting an inner `ccd-cli` session.
For that path, the launcher mounts a mode-0600, read-only CCC session/control
handoff at `/tmp/ccc-agent/session-env.json`; the plugin's `SessionStart` hook
restores those narrow values and appends them to Claude's documented
`CLAUDE_ENV_FILE`, making `CCC_AGENT_SESSION` available to subsequent Bash tool
calls. The host-side handoff is removed when the outer session exits. If the seed
is absent, Claude does not load the plugin, or SessionStart does not run,
contained Claude runs still fall back to process-exit review.

## Hermes

`ccc-agent` no longer injects `HERMES_BUNDLED_PLUGINS` or `HERMES_ACCEPT_HOOKS`
by default. Hermes runs still get the BranchFS/bwrap boundary and process-exit
freeze/status/policy review. Operators who want a Hermes native plugin can
configure Hermes explicitly; commit authority remains in the trusted supervisor,
not in the plugin.

## OpenCode and generic commands

OpenCode and arbitrary commands can be wrapped even without native plugins:

```bash
ccc-agent run -- opencode run ...
ccc-agent run -- python train.py
ccc-agent run -- bash scripts/do-work.sh
```

They get process-exit review. If the command writes only policy-safe files, those
changes can auto-commit. Otherwise the session remains reviewable.

## Transparent shims and nested agents

Optional shims can expose the usual command names:

```text
codex   -> ccc-agent run --agent codex -- <real codex> ...
claude  -> ccc-agent run --agent claude -- <real claude> ...
hermes  -> ccc-agent run --agent hermes -- <real hermes> ...
opencode -> ccc-agent run --agent opencode -- <real opencode> ...
```

When `CCC_AGENT_SESSION` is already set, a nested invocation reuses the current
session rather than creating another branch. This is important when one agent
starts another agent or helper script: the task remains one review unit.

## Live review through MCP

The Claude and Codex plugins each use the client's supported plugin `.mcp.json`
to launch `ccc-agent mcp-server` over stdio. The dependency-free server exposes:

```text
ccc_status
ccc_list_kept
ccc_commit_kept
ccc_discard_kept
ccc_keep_kept
ccc_abort_session
```

The four protected user-operation skills (`ccc`, `ccc-status`, `ccc-commit`, and
`ccc-discard`) have been replaced by one `ccc-containment` instruction skill.
Lifecycle hooks are retained. Codex deployments must keep per-tool prompting for
`mcp__ccc__*` enabled and must not add a blanket allow rule. Claude's destructive
tools carry `anthropic/requiresUserInteraction` metadata.

`ccc_commit_kept`, `ccc_discard_kept`, and `ccc_abort_session` send a
nested MCP form-mode `elicitation/create` request on the same connection **only
after** transport hardening is verified. Abort records throwaway policy and is
applied by authoritative process-exit finalization rather than tearing down the
live mount; the client should exit after an accepted abort. No advertised
elicitation capability, an MCP error,
malformed content, decline, cancel, EOF, or anything other than
`action=accept` plus `confirm=true` fails closed. Every resolution is restricted
to the supervisor's current remembered-kept path set. If hardening is absent,
the tool returns `pending-external-approval` and does not open an elicitation or
apply data; resolution remains available through trusted external session
review. The old `turn-resolve` and `turn-approve` CLI protocol verbs remain for
compatibility, but the production supervisor admits their mutating operations
only on the pinned, destructively-authorized MCP connection.

## MCP process and transport admission

This section is the operational summary. The complete security protocol,
including trust boundaries, exact registration/admission checks, human
elicitation, supervisor signaling, spoofing defenses, failure behavior,
assumptions, implementation map, and review checklist, is documented in
[Trusted MCP commit protocol](trusted-mcp-commit-protocol.md).

The supervisor authenticates the Unix control peer with Linux `SO_PEERCRED`; it
does not trust environment claims or a token by itself. The trusted namespace
PID-1 runner registers the exact initial Claude/Codex child immediately after
spawning it. The supervisor accepts registration only from that launch's PID 1,
resolves its direct child with host `/proc`/`NSpid`, and pins the client PID and
start time. Before tools are advertised/model work starts, the bundled stdio MCP
process opens one persistent control connection. The supervisor admits it only
when `SO_PEERCRED` shows that the registered initial client is its direct parent,
then pins the MCP PID/start time and connection object.

A later shell or malicious descendant cannot become eligible by naming itself
`claude` or `codex`; the first registration is one-time and process-bound. PID
reuse, missing process state, non-bwrap debug launches, and server/SSH wrapper
modes where the exact initial client cannot be registered all fail closed.

Process lineage alone is insufficient: on ordinary same-UID procfs mounts, a
malicious child can reopen its parent's MCP pipe through `/proc/<pid>/fd` and
forge traffic. `ccc-agent setup --system` therefore compiles a tiny
architecture-local preload library from packaged source into the root-owned
configuration directory. User-mode setup deliberately does not authorize
destructive MCP operations. For direct Claude/Codex launches the
runner uses it only when the library, its setup-generated SHA-256 manifest, and
every parent directory are not owned or writable by the agent UID and the
digest matches, then binds it read-only into bwrap. The library:

- sets `PR_SET_DUMPABLE=0` in the client and MCP subprocess;
- forces close-on-exec on newly created pipes and socketpairs; and
- exits immediately if the dumpability restriction cannot be installed.

At MCP admission the supervisor independently verifies that both the client and
MCP process descriptor directories reject access. Only then does it mark the
pinned connection as destructive-authorized and treat same-connection Form Mode
elicitation as authoritative. A missing compiler/library, user-writable setup,
unsupported/static loader behavior, or failed descriptor probe leaves status
available but commit/discard external-review-only.

This is process-bound authorization in the normal Linux process model, not
cryptographic process attestation. It assumes the trusted Claude/Codex client
itself is not compromised and that the attacker cannot escape containment,
replace root-owned runtime assets, or obtain privilege that bypasses the
non-dumpable process restriction.

## Authenticated live workspace scopes

Workspace scope affects automatic commit policy, so the hook token is not treated
as authentication against same-UID agent code. The detailed state model,
process/transport checks, per-client protocols, attack analysis, failure behavior,
and implementation map are in the
[trusted workspace-scope protocol](trusted-workspace-scope-protocol.md).

The bundled lifecycle commands remain:

```bash
ccc-agent turn-add-workspace --agent-session <inner-session-id> [PATH]
ccc-agent turn-remove-workspace --agent-session <inner-session-id> [PATH]
```

They are proposal and cleanup signals. A hook path already under the static
operator ceiling may refine that existing authority. A new/broader hook path
returns `workspace-proposed`, remains absent from `allowed_scopes`, and waits for
an authenticated client root signal. Hook removal can remove its own proposal or
hook-owned sub-scope, but cannot remove static or authenticated roots. These
commands do not `cd`, remount BranchFS, expose the underlay, or commit data.

Authenticated dynamic roots use complete replacement sets:

- **Claude:** the pinned, descriptor-hardened CCC MCP server requests standard
  MCP Roots after initialization and root-change notifications. Only absolute
  local `file:` URIs are forwarded.
- **Codex app-server:** trusted PID 1 observes transparent JSONL requests and
  matching successful responses, tracks roots per thread, and confirms their
  union. Failed requests grant nothing. Foreground and adaptive/server PID-1
  paths both use this mechanism.
- **Hermes:** the exact registered hardened Hermes process opens one pinned
  in-process workspace channel. Framework-owned workspace kwargs are preferred;
  an exact leading `Workspace::v1` tag is accepted only from the WebUI/API
  adapter (`platform=api_server`, with `webui` retained for compatible runtimes).
  Missing metadata does not fall back to process cwd. A locked per-session map
  confirms the active-session union, and session end removes only its own root.
- **Explicit direct launch:** trusted PID 1 may confirm launch cwd only when the
  outer operator selected a workspace; incidental server/bootstrap cwd is not
  authority.

Each privileged replacement rechecks PID/start-time, parentage, pinned connection,
and `/proc/<pid>/fd` protection, then validates every path beneath configured
protected roots. A copied token, forged hook, renamed descendant, second socket,
unhardened client, malformed protocol message, or path outside protected storage
cannot broaden policy. Failure leaves changes out of scope for normal review.

## Environment propagation

Contained commands inherit the complete environment of the `ccc-agent run`
invocation by default. This is intentional for CCC images: integrations may rely
on container identity and node variables, `CCC_FUSE_SIDECAR_SOCKET`, CUDA/NVIDIA
settings, Conda activation, `SSH_AUTH_SOCK`, library paths, and future
image-provided feature variables. External credential variables are inherited as
well; ccc-agent does not guess which application tokens are valid.

The launcher then applies a narrow trusted policy:

1. discard stale ccc-agent session/control/hook/lifecycle values from an enclosing
   or failed invocation;
2. assign the new session identity and fresh control credentials;
3. keep the host-side `CCC_AGENT_STATE_DIR` out of the bwrap process;
4. override sandbox invariants such as `HOME`, `USER`, `LOGNAME`, `PATH`, `SHELL`,
   `TERM`, and `PWD` with the correct contained values;
5. apply configured credential/plugin values and `bwrap_setenv` overrides.

Fresh ccc-agent control tokens, extracted credential values, plugin values, and
`bwrap_setenv` overrides are carried in bwrap's process environment, not in its
command-line arguments, so values are not exposed through bwrap's
`/proc/<pid>/cmdline`.

Operators can explicitly remove deployment-specific variables:

```json
{
  "bwrap_unsetenv": ["AWS_SECRET_ACCESS_KEY", "SOME_UNUSED_TOKEN"],
  "bwrap_setenv": {"FEATURE_MODE": "contained"}
}
```

`bwrap_unsetenv` is denylist-only. An empty list means inherit all non-internal
variables. `bwrap_setenv` is applied after removal, so a trusted override can
reintroduce a name deliberately.

## Security rules for integrations

- Plugin assets must be package/root-owned and read-only in the sandbox.
- Agent runtime state may remain writable; trusted plugin source must not.
- Hooks report lifecycle events and user choices; they do not commit real data.
- Direct low-level BranchFS commit APIs are not exposed inside the agent mount.
- Process-exit finalization must remain correct even if all plugins are disabled.
