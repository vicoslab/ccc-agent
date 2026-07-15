# ccc-agent

`ccc-agent` runs AI agents and other commands in a reviewable filesystem session.
The command sees normal project paths, but its writes land in a BranchFS branch
first. When the command or turn finishes, a trusted supervisor freezes the
branch, checks what changed, and either commits safe changes to the real files,
keeps the session for review, or discards it.

Use it when you want autonomous tools such as Codex, Claude Code, Hermes, or
OpenCode to edit files without giving the agent direct final-write authority over
important storage.

```text
ccc-agent run -- codex exec "fix the parser"
  -> create branch session
  -> run the agent inside the protected view
  -> freeze and classify changes
  -> commit | pending review | discard
```
## How it works

`ccc-agent` is a small Python supervisor around three ideas:

1. **Branch first**: protected roots are mounted as [BranchFS](https://github.com/vicoslab/branchfs) branch views.
2. **Run contained**: the command runs in a rootless bubblewrap user/mount/PID
   namespace where writable project paths resolve to the branch view, not the
   real underlay.
3. **Commit only after review**: the trusted supervisor freezes the branch,
   reads real BranchFS status, applies policy, and selectively applies approved
   changes.

The agent can create branch deltas. It cannot directly commit them through the
agent-visible filesystem view but must call `ccc-agent turn-*`.


## Quick start

Install the supervisor only when you will provide `branchfs` yourself:

```bash
pip install 'ccc-agent @ git+https://github.com/vicoslab/ccc-agent.git'
```

Install with the matching vicoslab BranchFS release binary bundled from the
`vicoslab/branchfs` GitHub release:

```bash
pip install 'ccc-agent[branchfs] @ git+https://github.com/vicoslab/ccc-agent.git'
```

The `branchfs` extra downloads the prebuilt `vicoslab-branchfs-bin` wheel. It
does not compile Rust and does not bundle `libfuse3`; install `libfuse3-3`/`fuse3`
or the equivalent host package separately. If the extra is not installed,
`ccc-agent setup` records the `branchfs` binary it finds on `PATH` or at
`/usr/local/bin/branchfs`, and you can override it with `--branchfs-bin`.

Run agent in current directory:

```bash
ccc-agent run codex
ccc-agent run claude
```

Run a one-shot agent task in the current directory:

```bash
ccc-agent run -- codex exec "implement feature X"
ccc-agent run -- claude -p "review this repository"
ccc-agent run -- hermes "summarize and clean up the TODOs"
```

Run in a specific workspace:

```bash
ccc-agent run \
  --workspace /home/$USER/Projects/my-project \
  --policy workspace-auto \
  -- codex exec "fix the failing tests"
```

Open an interactive shell inside a protected session:

```bash
# current dir as main workspace
ccc-agent run 

# in explicity folder as main workspace 
ccc-agent run --workspace /home/$USER/Projects/my-project
```

Default behavior with `workspace-auto`:

- changes inside the declared workspace auto-commit when no deny rule matches;
- changes outside the workspace, or to sensitive paths such as `.ssh`, `.env`,
  `.git/hooks`, credentials, or shell startup files, become `pending-review`;
- ignored runtime/cache noise is discarded with the branch;
- `--policy manual` always requires review;
- `--policy throwaway` discards the branch at completion.

## Reviewing sessions

After session finishes, you can review it / inspect it before committing:

```bash
ccc-agent list
ccc-agent diff <session-id>
ccc-agent diff <session-id> --show-file-diffs
ccc-agent review <session-id>          # interactive accept/select/reject/later
ccc-agent review <session-id> --accept # scripted accept
ccc-agent review <session-id> --reject # scripted discard
```

Other useful operations:

```bash
ccc-agent resume <session-id>          # continue after crash or pending review
ccc-agent thaw <session-id>            # reopen a pending branch for more work
ccc-agent finish <session-id>          # freeze/status/review a live session now
ccc-agent cleanup --older-than 30      # remove old closed session bundles
```

Each session keeps durable metadata and review artifacts under the configured
`state_dir`, including the command, protected roots, status JSON, policy decision,
and a human-readable summary.

## Agent integrations

`ccc-agent run` already knows how to integrate with common agent CLIs:

- **Codex**: setup enables/trusts the bundled Codex containment plugin in Codex
  config; contained runs only receive a read-only plugin-cache bind. `ccc-agent`
  no longer adds Codex YOLO/sandbox-bypass flags by default.
- **Claude Code**: `ccc-agent setup` materializes the complete bundled plugin
  seed from pip package data—no image rebuild or runtime marketplace install is
  required. System mode uses `/opt/claude-seed`; user mode uses
  `~/.local/share/ccc-agent/claude-seed`. Contained runs mount the seed read-only
  and do not receive `--plugin-dir`. Hooks remain inside the plugin.
- **Hermes**: no default per-run plugin environment is injected; process-exit
  review remains authoritative until Hermes is configured explicitly.
- **OpenCode or any other command**: still benefits from process-exit review even
  without a native turn hook.

Interactive Codex/Claude/Hermes sessions use best-effort turn hooks. Ordinary
workspace changes can be committed at turn boundaries; out-of-scope changes are
kept in the branch and surfaced for review. Server-style SSH/app-server launches
should use `ccc-agent serve <agent> -- ...` (the SSH router does this
automatically): `ccc-agent` stays silent on the protocol stream, labels true
service records as `<agent>-remote`, does not inject interactive-agent argv/env
activation into the server command, and keeps non-workspace changes for later
review. An implicit server launch does not treat its SSH current directory as a
workspace; trusted inner-session hooks establish auto-commit scopes. `serve`
uses adaptive lifecycle by default, so
foreground bridge/proxy runs are classified as `<agent>-remote-bridge`, hidden
from `ccc-agent list`, aborted without commit at exit, and immediately removed
after successful cleanup. For server-style runtimes, distinguish the outer
`ccc-agent serve` containment session from inner agent sessions (for example a
Codex app-server client session, Hermes gateway conversation, or Claude agent
command). Hooks may register one current workspace per active inner agent
session; when that inner session ends, only the workspace that hook session added
is removed. If a hook
does not run, process-exit finalization still provides the authoritative
freeze/status/policy path.

Optional transparent shims can wrap `codex`, `claude`, `hermes`, and `opencode`
so users can keep invoking the normal command names. Nested agent calls reuse the
current `CCC_AGENT_SESSION` instead of creating a new branch for every subcommand.

## Installation

Managed CCC images include integration for installing and wiring `ccc-agent` as
part of image/runtime setup. Administrators and developers should see:

- [Installation and build](docs/installation.md)
- [Dependencies](docs/dependencies.md)
- [Configuration](docs/configuration.md)

For local development from this repository:

```bash
python3 -m pip install --user -e .
ccc-agent setup --user
```

`BranchFS` and `bubblewrap` are runtime prerequisites and are configured outside
this Python package.

## Documentation

Start with [docs/README.md](docs/README.md):

- [User guide](docs/user-guide.md)
- [Agent integration](docs/agent-integration.md)
- [Installation and build](docs/installation.md)
- [Configuration](docs/configuration.md)
- [Path policy and secret handling](docs/policy.md)
- [Architecture](docs/architecture.md)
- [Design decisions](docs/design-decisions.md)
- [Dependencies](docs/dependencies.md)
- [Development](docs/development.md)

## Important limits

`ccc-agent` protects configured filesystem roots and review/commit authority. It
is not a full VM or hostile-container escape boundary. If the surrounding runtime
intentionally exposes powerful sockets or devices, the agent may be able to use
those capabilities. Use `ccc-agent run --full-isolation` when you want to omit
ambient `/run`, `/var`, and `/dev` access from the sandbox.
