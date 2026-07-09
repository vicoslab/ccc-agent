# Agent integration

`ccc-agent` can wrap any command. Native integrations for Codex, Claude Code, and
Hermes add turn-boundary convenience, but the authoritative safety path remains
process-exit freeze/status/policy review.

## Invocation matrix

| Invocation | Boundary | Plugin behavior | Review behavior |
|---|---|---|---|
| `ccc-agent run -- codex exec "..."` | Process exit | Codex plugin may be injected, but one-shot exit is enough. | Session-end finalize. |
| `ccc-agent run -- claude -p "..."` | Process exit | Claude plugin may be injected, but one-shot exit is enough. | Session-end finalize. |
| `ccc-agent run -- hermes "..."` | Process exit / Hermes hooks | Hermes plugin can report turns/session end. | Turn and session finalize when hooks run; process exit remains authoritative. |
| `ccc-agent run -- codex` | Interactive turns + process exit | Codex plugin Stop hook, version-dependent. | Workspace changes may commit per turn; kept paths reviewed later. |
| `ccc-agent run -- claude` | Interactive Stop hooks + process exit | Claude plugin via `--plugin-dir`. | Workspace changes may commit per turn; kept paths reviewed later. |
| `ccc-agent run -- <other command>` | Process exit | No native plugin required. | Session-end finalize. |

## Plugin injection model

For a matching contained run, `ccc-agent run`:

1. identifies the agent from `--agent` or the executable basename;
2. validates the configured plugin asset directory on the trusted host;
3. bind-mounts that asset read-only into the bwrap sandbox;
4. inserts activation argv or environment variables for the contained command;
5. starts a trusted control socket for turn operations when enabled.

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
cache path. `ccc-agent setup` also maintains a narrow marked block in
`~/.codex/config.toml` so Codex 0.136+ treats the plugin as enabled/trusted when
that read-only plugin cache is present.

For contained Codex commands, `ccc-agent` also inserts:

```text
--dangerously-bypass-approvals-and-sandbox
```

Codex is already running inside the `ccc-agent` BranchFS/bwrap boundary. Disabling
Codex's nested Linux sandbox avoids incompatible nested-bwrap behavior while
preserving the outer filesystem containment and review boundary.

Interactive Codex Stop hooks are version-dependent. If the hook runs, it calls
`turn-finalize --default-keep`. If it does not run, changes are handled at
session end.

## Claude Code

Contained Claude Code receives the bundled Claude plugin by adding a session-only
plugin directory:

```text
claude --plugin-dir /ccc-agent/plugins/claude-ccc-containment ...
```

The plugin directory is a read-only bwrap mount from package assets. The Stop
hook reports turn boundaries to the trusted supervisor. `--bare` disables
plugins/hooks, so a contained `--bare` run falls back to process-exit review.

## Hermes

Contained Hermes receives a bundled plugin through environment variables:

```text
HERMES_BUNDLED_PLUGINS=/ccc-agent/plugins/hermes
HERMES_ACCEPT_HOOKS=1
```

The plugin injects CCC review/commit reminders, reports turn/session boundaries,
and surfaces kept-file choices in final responses when needed. It still does not
own commit authority; it calls trusted `ccc-agent turn-*` operations.

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
