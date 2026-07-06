# Integrating real agents (codex / claude / hermes) with ccc-agent

How each agent reaches the per-turn control channel through its **native plugin
mechanism**, how the plugin is injected, and how credentials are handled.

## Turn-boundary matrix

| Invocation | Turn boundary | How finalize happens | Approval flow |
|---|---|---|---|
| `codex exec "…"` | process exit (1 turn) | supervisor **process-exit finalize** (no hook) | session-end review |
| `claude -p "…"` | process exit (1 turn) | supervisor **process-exit finalize** (no hook) | session-end review |
| `claude` (interactive) | each Stop | plugin **Stop hook** → `ccc-agent turn-finalize --default-keep` | nonblocking: workspace commits; non-workspace kept |
| `codex` (interactive) | each Stop | plugin **Stop hook** → `ccc-agent turn-finalize --default-keep` | nonblocking when hook runs (see below) |
| `hermes` (interactive) | each turn / session end | plugin **`post_llm_call` / `on_session_end`** → `ccc-agent turn-finalize --default-keep` | nonblocking: workspace commits; non-workspace kept |

**Non-interactive (`exec`/`-p`) needs no hook** — one turn per process, so the
supervisor's existing end-of-process finalize is the per-turn commit.

**Claude interactive** loads a CCC plugin whose Stop hook reports the turn with
`--default-keep`: ordinary in-policy workspace changes are committed immediately.
New non-workspace/out-of-policy paths are kept in the BranchFS branch only, so
intermediate autonomous loops do not become approval gates. The user can resolve
those kept paths later during final/session review, or explicitly while the
session is still live with `turn-resolve`.

**Codex interactive** loads a CCC plugin whose `hooks/hooks.json` registers the
`Stop` event. Whether a given Codex build honours Stop hooks is
version-dependent; treat per-turn Codex handling as **best-effort**. If the hook
runs, workspace changes commit and non-workspace/out-of-policy paths are kept in
the branch. If the installed Codex never runs the hook, changes defer to
**session-end review** (`pending-review`) — they are never silently committed.

For manual/operator-driven checks, `ccc-agent turn-finalize` without
`--default-keep` returns an approval token and exit 2 for new
non-workspace/out-of-policy paths; `--default-keep-after SECONDS` prints that
prompt and then auto-keeps if no external decision arrives. Ordinary workspace
data that can be committed does not require a prompt. The approval response
supports four choices through the trusted control socket:

```bash
ccc-agent turn-approve <token>                 # commit all flagged paths
ccc-agent turn-approve <token> keep            # keep in the branch only
ccc-agent turn-approve <token> discard         # reject; agent must undo/revert
ccc-agent turn-approve <token> --commit a --keep b --discard c
```

`keep` is durable session state: the path remains in the BranchFS branch, is not
committed to the real underlay, and is not re-prompted on later turns or after a
control-server restart. The live state can be inspected any time from inside the
contained session:

```bash
ccc-agent turn-kept-status
```

When an agent is genuinely finished/idle (not still looping/thinking), the
bundled `branchfs-commit` skill tells it to run:

```bash
ccc-agent turn-review-kept
```

If that command reports kept paths, the agent asks the user what to do. If the
user later changes their mind while the session is still live, or answers that
final prompt, the agent relays the request explicitly:

```bash
ccc-agent turn-resolve commit --paths a,b
ccc-agent turn-resolve discard --paths c
ccc-agent turn-resolve keep --paths d
```

For live sessions, `discard` records the user's rejection and asks the agent to
undo the listed paths in its workspace; the supervisor does not surgically edit a
running FUSE branch under the agent. At process exit, any still-kept branch deltas
remain available through normal `pending-review` session review.

**Hermes** loads a CCC bundled plugin (`HERMES_BUNDLED_PLUGINS`) whose
`post_llm_call` / `on_session_end` hooks report turn boundaries with
`--default-keep`. Hermes hooks cannot block or feed instructions back, so kept
non-workspace paths are resolved through the same final/idle `turn-review-kept`
flow or normal session-end review.

Hooks are **best-effort turn-boundary signals only**. If a plugin fails to load,
a hook crashes, or an agent version changes the contract, the agent loses
per-turn convenience but the trusted **process-exit freeze → status → policy →
review** path still runs and never grants the agent commit authority.

## Plugin injection (no config-file overlay)

CCC hooks are delivered through each agent's **native plugin mechanism**, not by
overwriting the user's normal Codex/Claude/Hermes config. `ccc-agent setup`
records an `agent_plugins` entry per agent pointing at root-owned, read-only
package assets under `ccc_agent/assets/plugins/`. For a contained run only,
`ccc-agent run` bind-mounts the matching plugin read-only into the bwrap sandbox,
inserts any activation `argv` right after the agent executable, and exports any
`setenv`. Direct, uncontained `codex` / `claude` / `hermes` invocations load none
of this, and no user config file is edited or hidden.

When no explicit agent flag is provided, `ccc-agent run` infers the plugin from
the executable basename, including absolute paths such as `/opt/agents/bin/codex`:

```text
ccc-agent run -- codex exec "…"      # loads the Codex plugin
ccc-agent run -- /path/to/claude -p "…"  # loads the Claude plugin
```

Use `--agent <name>` only when you want an explicit override; explicit selection
wins over executable-path inference.

**Claude Code** — session-only plugin via the native `--plugin-dir` flag:

```text
ccc-agent run -- claude -p "…"
  → claude --plugin-dir /ccc-agent/plugins/claude-ccc-containment -p "…"
```

The plugin dir (`.claude-plugin/plugin.json` + `hooks/hooks.json` →
`${CLAUDE_PLUGIN_ROOT}/hooks/ccc-stop-hook.sh`, plus the `branchfs-commit` skill)
is a read-only bwrap mount of the package asset. `--bare` disables plugins/hooks,
so a contained `--bare` run skips injection and falls back to session-end review.

**Codex** — the plugin (`.codex-plugin/plugin.json` + `hooks/hooks.json` →
`./hooks/ccc-stop-hook.sh`, plus the `branchfs-commit` skill) is mounted
read-only at the in-sandbox Codex plugin path (`~/.codex/plugins/ccc-agent`). The
generated `argv` includes `--dangerously-bypass-approvals-and-sandbox` so Codex
does not start its own nested Linux/bwrap sandbox inside the existing ccc-agent
BranchFS/bwrap containment boundary.

**Hermes** — the bundled plugin (`plugin.yaml` + a `register()` module) is
mounted under a read-only bundle root and activated with
`HERMES_BUNDLED_PLUGINS=/ccc-agent/plugins/hermes` and `HERMES_ACCEPT_HOOKS=1`.

Disable all injection with `ccc-agent setup --no-agent-plugins` (alias
`--no-hooks`), which sets `agent_hook_mode: "disabled"`.

## Credentials and writable agent state

`~/.codex`, `~/.claude`, and `~/.hermes` are **agent/system state**, not trusted
plugin storage and not BranchFS-protected project data by default. `ccc-agent
run` direct-binds the real shared directories read-write over the BranchFS home
view so real agents can create logs, session files, caches, lock files, config,
and refreshed tokens. Changes there persist immediately and Codex/Claude/Hermes
own concurrent access across sessions and CCC nodes.

Use `ccc-agent run --protect-agent-state` or config `protect_agent_state: true`
when a user explicitly wants those directories inside BranchFS review. In that
mode ccc-agent will not try to understand or merge agent internals; the user must
handle any conflicts, especially SQLite/state databases.

System deployments protect the containment plugin by installing it outside
`$HOME`:

- package code, plugin manifests, and hook scripts live in a root-owned
  Python/package location under `/usr` (or another OS path exposed read-only by
  bwrap);
- `config.json` lives under `/etc/ccc-agent` and is root-owned;
- the per-agent CCC plugins are bind-mounted **read-only** into the sandbox only
  for a matching contained agent, so the untrusted agent can load but never edit
  the hook source;
- direct, uncontained `codex`/`claude`/`hermes` runs do not load CCC plugins.

The shared `~/.codex` / `~/.claude` / `~/.hermes` trees are outside BranchFS by
default. They are mounted directly from the real home and are therefore not part
of status, review, commit, or abort. The same ignored-runtime treatment still
applies to common shell/REPL history and cache files that land in protected
paths; startup/config files such as `~/.bashrc` remain reviewable deny matches,
not ignored noise.

`cred_mounts` remains available only for narrow special-case read-only overlays;
do **not** use it for whole agent config/state directories. `cred_mask` and
`cred_env` are for API-key deployments where an individual secret file can be
masked and the supervisor can pass the key via env. OAuth-subscription logins
(codex `auth.json` with `tokens`, claude `.credentials.json`) authenticate from
files, so those files must remain readable through the shared agent-state bind.

## Browsing / cleaning lingering sessions

A session that exits with un-committed deltas stays as a reviewable branch:

```bash
ccc-agent list                       # sessions + states (alias: ccc-agent ls)
ccc-agent review <session>           # browse, then accept/select/reject/later on a TTY
ccc-agent diff <session>             # read-only commit-set + ignored-change summary
ccc-agent diff <session> --show-ignored    # include full ignored/cache/runtime list
ccc-agent diff <session> --show-file-diffs # append hunks for changed text files; binary/non-text skipped
ccc-agent diff <session> <path>      # unified diff for one changed text file
ccc-agent review <session> --accept  # scripted commit policy-visible changes
ccc-agent review <session> --accept --include-ignored  # also commit ignored changes
ccc-agent review <session> --reject  # scripted discard all branch deltas
ccc-agent review <session> --commit a,b   # scripted commit only a,b (rest discarded)
ccc-agent review <session> --emit-patch > c.patch   # text hunks only: prune hunks…
ccc-agent review <session> --apply-patch c.patch    # …then apply
```

Interactive `review` prompts after showing the summary. The `select`/`s` choice
opens a stdlib tree selector: Up/Down move, Enter opens a folder, Backspace goes
up, Space selects a file or folder subtree, `c` commits selected paths, and
`q`/Esc cancels back to the prompt.

Or directly via the BranchFS CLI (the branch name is the session id):

```bash
branchfs list   --storage <store>
branchfs status <session> --storage <store> --json
branchfs commit-branch <session> --storage <store>   # low-level: applies all deltas, bypasses ccc-agent ignores
branchfs abort-branch  <session> --storage <store>   # discard the branch
```
