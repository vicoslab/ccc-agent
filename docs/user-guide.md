# User guide

`ccc-agent` wraps an agent or command in a reviewable BranchFS session. The
wrapped command uses normal paths, but writes are captured in a branch until the
trusted supervisor commits or discards them.

## Basic command shape

```bash
ccc-agent run [run options] -- <command> [args...]
```

Common examples:

```bash
# One-shot agent tasks.
ccc-agent run -- codex exec "fix the failing test"
ccc-agent run -- claude -p "review this repository"
ccc-agent run -- hermes "summarize the project and update TODO.md"

# Explicit workspace and policy.
ccc-agent run \
  --workspace /home/$USER/Projects/my-project \
  --policy workspace-auto \
  -- codex exec "implement feature X"

# Interactive protected shell; defaults to the caller's shell.
ccc-agent run --workspace /home/$USER/Projects/my-project
```

Useful `run` options:

| Option | Purpose |
|---|---|
| `--workspace PATH` | Workspace used as the default allowed scope. Defaults to the current directory. |
| `--policy MODE` | `workspace-auto`, `manual`, `read-only-review`, `training-run`, or `throwaway`. |
| `--scope PATH` | Add another allowed scope. Repeatable. |
| `--hide PATTERN` | Add a run-specific deny/hide pattern for sensitive paths. Repeatable. |
| `--agent NAME` | Override agent kind/plugin inference (`codex`, `claude`, `hermes`, ...). |
| `--protect-agent-state` | Keep agent runtime state inside BranchFS review instead of the default shared bind. |
| `--full-isolation` | Do not bind ambient `/run`, `/var`, or `/dev` into the sandbox. |
| `-v`, `--verbose` | Print the full session event log. |

## What happens during a run

```text
create session
  -> create BranchFS branch(es)
  -> mount agent views
  -> run command in protected view
  -> turn hooks may finalize intermediate turns
  -> process exit finalizes the session
  -> freeze -> status -> policy
  -> auto-commit | pending-review | abort/no-op
```

The wrapped command receives `CCC_AGENT_SESSION` in its environment. If it starts
another wrapped agent, the nested invocation reuses the same session so the task
has one coherent review record.

## Session outcomes

| Outcome | Meaning |
|---|---|
| `auto-committed` | Policy-visible changes were safe and were applied to the real underlay; ignored runtime noise was discarded. No-op sessions also close here after discarding an empty branch. |
| `pending-review` | The branch is frozen and preserved for human review. Nothing unsafe was committed. |
| `committed` | A human/operator accepted a pending session. |
| `aborted` | The branch deltas were discarded. |
| `failed` | Mount, finalize, or commit failed; the branch is preserved for recovery when possible. |

Default `workspace-auto` policy auto-commits only when all policy-visible changes
are inside allowed scopes and no deny/hide pattern matches. Otherwise the session
becomes `pending-review`.

## Inspecting and reviewing sessions

List sessions:

```bash
ccc-agent list
ccc-agent ls
ccc-agent list <session-prefix>
```

Inspect one session:

```bash
ccc-agent show <session-id>       # full persisted session JSON
ccc-agent status <session-id>     # live BranchFS status for protected roots
ccc-agent diff <session-id>       # changed-path summary
ccc-agent diff <session-id> --show-ignored
ccc-agent diff <session-id> --show-file-diffs
ccc-agent diff <session-id> <path>
```

Review a pending/frozen session:

```bash
ccc-agent review <session-id>
ccc-agent review <session-id> --accept
ccc-agent review <session-id> --reject
ccc-agent review <session-id> --commit path/a,path/b
ccc-agent review <session-id> --emit-patch > review.patch
ccc-agent review <session-id> --apply-patch review.patch
```

Plain `ccc-agent review <session-id>` on an interactive TTY shows the summary and
prompts for:

- `yes` / `y`: commit the reviewable changes;
- `select` / `s`: open a file/folder selector and commit selected paths;
- `no` / `n`: discard the branch;
- `later` / `l` / Esc: keep the session frozen for later review.

The selector uses only the Python standard library: Up/Down move, Enter opens a
folder, Backspace goes up, Space selects a file or folder subtree, `c` commits
selected paths, and `q`/Esc returns to the prompt.

Scripted batch operations:

```bash
ccc-agent commit <session-id> [<session-id> ...]
ccc-agent abort  <session-id> [<session-id> ...]
```

## Live turn review commands

Native Codex/Claude/Hermes plugins call these commands automatically. Users and
operators normally only need them when resolving paths that were kept during a
live interactive session.

```bash
ccc-agent turn-kept-status [--details]
ccc-agent turn-review-kept [--details]
ccc-agent turn-resolve commit  --paths a,b
ccc-agent turn-resolve keep    --paths a,b
ccc-agent turn-resolve discard --paths a,b
ccc-agent turn-resolve commit --all-kept
```

A plugin may also expose agent-native commands such as `/ccc:status`,
`/ccc:commit`, `/ccc:discard`, and `/ccc:op`. Those commands translate the user's
request into the trusted `ccc-agent turn-*` control operations.

## Resuming and recovering

Resume an interrupted or reviewable session:

```bash
ccc-agent resume <session-id>                  # rerun stored command
ccc-agent resume <session-id> --cmd bash       # shell-style override
ccc-agent resume <session-id> -- bash          # exact argv override
ccc-agent resume <session-id> --allow-failed   # only after inspecting failure
```

Resume behavior by state:

| State | Resume behavior |
|---|---|
| `running` | Reuses the branch bundle after stale-mount cleanup. |
| `pending-review` | Thaws the preserved branch, runs more work, then finalizes again. |
| `aborted` | Recreates the branch under the same session id from the current base. |
| `failed` | Requires `--allow-failed`; use only after inspecting the recorded error. |

Use `--force` only after verifying no old agent process still uses the session's
mounts.

Other lifecycle controls:

```bash
ccc-agent thaw <session-id>       # reopen pending-review without running command
ccc-agent finish <session-id>     # force freeze/status/policy now
```

## Cleaning old sessions

```bash
ccc-agent cleanup --older-than 30 --dry-run
ccc-agent cleanup --older-than 30
ccc-agent cleanup -a -o 20        # include non-terminal/failed states too
```

By default cleanup removes only closed terminal bundles (`auto-committed`,
`committed`, `aborted`) older than the requested age. Pending review, running, and
failed sessions remain visible unless `--all-type` / `-a` is used.

## Practical recommendations

- Start with explicit `ccc-agent run -- ...` until the configuration is proven.
- Use transparent shims only after explicit runs work for your agents and shell
  environments.
- Keep the workspace narrow. Add `--scope` for deliberate extra output roots.
- Use `manual` or `read-only-review` for high-stakes data.
- Use `throwaway` for exploration where the default should be discard.
- Do not use low-level `branchfs commit-branch` for ordinary review; it bypasses
  `ccc-agent` ignores and selective commit policy.
