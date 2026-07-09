# Agent integration

`ccc-agent` can wrap any command. Native integrations for Codex, Claude Code, and
Hermes add turn-boundary convenience, but the authoritative safety path remains
process-exit freeze/status/policy review.

## Invocation matrix

| Invocation | Boundary | Plugin behavior | Review behavior |
|---|---|---|---|
| `ccc-agent run -- codex exec "..."` | Process exit | Codex plugin cache may be mounted; one-shot exit is enough. | Session-end finalize. |
| `ccc-agent run -- claude -p "..."` | Process exit | Claude standalone hooks may be configured, but one-shot exit is enough. | Session-end finalize. |
| `ccc-agent run -- hermes "..."` | Process exit | No default Hermes per-run plugin env. | Session-end finalize. |
| `ccc-agent run -- codex` | Interactive turns + process exit | Codex plugin Stop hook, version-dependent. | Workspace changes may commit per turn; kept paths reviewed later. |
| `ccc-agent run -- claude` | Interactive Stop hooks + process exit | Claude hooks from persistent settings, if active. | Workspace changes may commit per turn; kept paths reviewed later. |
| `ccc-agent run -- <other command>` | Process exit | No native plugin required. | Session-end finalize. |

## Plugin/config model

For default setup-generated configs, `ccc-agent run` does **not** append
agent-specific argv or set agent-specific plugin environment variables. Instead:

1. setup writes persistent tool config where the tool supports it;
2. `ccc-agent run` may bind trusted package assets read-only when a matching
   contained agent needs files inside its runtime state;
3. the trusted control socket is available for best-effort turn operations when
   hooks run.

Manually configured `agent_plugins` may still specify `argv` or `setenv`, but
those launch mutations are opt-in/operator configuration. Mount-only specs can be
selected for SSH/server wrapper commands because they do not alter argv/env;
argv/env activation is restricted to direct agent CLI invocations.

If no plugin/config matches, the asset directory is missing, the command uses a
mode that disables hooks, or the agent version ignores hooks, the run degrades to
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

Contained Claude Code uses persistent standalone hook settings instead of a
session-only plugin directory. `ccc-agent setup --system` writes a managed drop-in
under `/etc/claude-code/managed-settings.d/`; `ccc-agent setup --user` writes the
same hooks to `~/.claude/settings.json`. The hook commands point at the installed
ccc-agent package assets.

`ccc-agent run` does not append `--plugin-dir` to Claude by default. If Claude
settings/hooks are absent or disabled, contained Claude runs fall back to
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
- Claude: `SessionStart` adds the current workspace; `SessionEnd`/`SessionStop`
  are wired to remove it when those events are emitted. `Stop` remains a
  turn-boundary finalize/review hook.
- Codex: documented `SessionStart` adds the root thread workspace;
  `SubagentStart`/`SubagentStop` add/remove subagent workspaces. Codex does not
  currently document a root `SessionEnd` event, so the root thread workspace is
  cleared by the outer `ccc-agent run` lifecycle/resume reset rather than by an
  in-Codex end hook.

## Security rules for integrations

- Plugin assets must be package/root-owned and read-only in the sandbox.
- Agent runtime state may remain writable; trusted plugin source must not.
- Hooks report lifecycle events and user choices; they do not commit real data.
- Direct low-level BranchFS commit APIs are not exposed inside the agent mount.
- Process-exit finalization must remain correct even if all plugins are disabled.
