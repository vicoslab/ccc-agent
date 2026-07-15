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
the server command. Before the final freeze it applies the same default as turn
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
`CLAUDE_CODE_PLUGIN_SEED_DIR` points to a read-only, pre-populated
`~/.claude/plugins` tree baked into the image (normally `/opt/claude-seed`).

The pip package owns the static plugin files. During the image build, materialize
the local marketplace source and let Claude Code perform the one-time install:

```bash
mkdir -p /opt/claude-seed/marketplaces
python -m ccc_agent.claude_plugin \
  --write-to /opt/claude-seed/marketplaces/ccc-agent
CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
  claude plugin marketplace add /opt/claude-seed/marketplaces/ccc-agent
CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-seed \
  claude plugin install ccc@ccc-agent
```

Setup-managed settings enable `ccc@ccc-agent` but do not duplicate hooks or
declare another marketplace source. Setup initializes the user's Claude plugin
metadata from the seed so hooks are active on the first invocation while
preserving unrelated plugins. At runtime, `ccc-agent run` mounts the seed
read-only and sets `CLAUDE_CODE_PLUGIN_SEED_DIR` inside bwrap. If the seed is
absent or Claude does not load the plugin, contained Claude runs fall back to
process-exit review.

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

## Live review commands exposed to agents

Bundled plugins include user-facing command skills where the agent supports them:

```text
/ccc:status [filter]
/ccc:commit [paths|prompt]
/ccc:discard [all|prompt]
/ccc:op <natural-language request>
```

The concrete trusted CLI operations are:

```bash
ccc-agent turn-kept-status [--details]
ccc-agent turn-review-kept [--details]
ccc-agent turn-resolve commit|keep|discard --paths a,b
ccc-agent turn-resolve commit|keep|discard --all-kept
```

Hook-only workspace operations are reserved for trusted lifecycle hooks:

```bash
ccc-agent turn-add-workspace --agent-session <inner-session-id> [PATH]
ccc-agent turn-remove-workspace --agent-session <inner-session-id> [PATH]
```

Server-mode integrations may call the workspace commands when the agent runtime
starts, resumes, updates, or ends an inner agent session inside the outer
`ccc-agent run` containment session. Each inner agent session has one current
workspace. Starting/updating that workspace releases only the previous workspace
owned by that same inner session; ending it removes only the workspace that the
hook actually added. The commands require the hook token and update only dynamic
policy scopes used by later turn/session finalization. They are not a process
`cd`, and they do not expose the real underlay. Changes outside the active
workspaces and static scopes remain review/keep candidates.

Bundled lifecycle coverage:

- Hermes: first-turn `pre_llm_call` adds the current workspace; `on_session_end`
  removes it.
- Claude: `SessionStart` adds the current workspace; `SessionEnd` removes it.
  `Stop` remains a turn-boundary finalize/review hook.
- Codex: documented `SessionStart` adds the root thread workspace;
  `SubagentStart`/`SubagentStop` add/remove subagent workspaces. Codex does not
  currently document a root `SessionEnd` event, so the root thread workspace is
  cleared by the outer `ccc-agent run` lifecycle/resume reset rather than by an
  in-Codex end hook.

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
