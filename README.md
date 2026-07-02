# ccc-agent — BranchFS containment for agents in CCC containers

Run Codex / Claude Code / Hermes / OpenCode (and any command) against CCC
storage **without giving them direct write access to real data**. The agent
works in a node-local BranchFS branch view; a trusted supervisor freezes the
branch when the agent finishes, classifies the changes against a path policy,
and only then commits to the real NFS-backed underlay — or parks the session
for human review, or discards it.

```text
ccc-agent run -- codex exec "implement feature X"
   create session -> branch bundle -> (bwrap) agent run ->
   freeze -> status -> policy -> auto-commit | pending-review | abort
```

Everything in this directory is **non-invasive scaffolding**: nothing here is
wired into CCC image startup by default. Deploy by copying to `/opt/ccc-agent`
(see *Deployment*).

## Quick start (explicit wrapper)

```bash
# config: protected roots, state dir, branchfs binary (see config/config.example.json)
export CCC_AGENT_CONFIG=/etc/ccc-agent/config.json

# run any command contained; workspace defaults to $PWD
ccc-agent run --workspace /home/$USER/Projects/foo \
              --policy workspace-auto \
              --agent codex \
              -- codex exec "implement feature X"

# omit the command to open the invoking shell (bash/sh/zsh/...) inside containment
ccc-agent run --workspace /home/$USER/Projects/foo
```

Outcome per policy:

- all changes inside the workspace, no deny rule hit → **auto-committed**;
- anything outside scope / deny match (`.ssh`, `.env`, `.git/hooks`, ...) →
  **pending-review** (branch stays frozen, nothing touched the underlay);
- `--policy throwaway` → branch aborted at completion;
- no changes → session closes as a no-op.

### Per-turn review (interactive, in the agent UI)

At each Stop boundary the agent's hook calls `ccc-agent finalize-turn` over
the control socket. In-scope changes auto-commit and the agent continues;
out-of-scope changes are reported to the user, who responds (relayed by the
agent) with one of:

```bash
ccc-agent approve-turn <token>            # accept all flagged changes
ccc-agent approve-turn <token> keep       # keep deltas, don't commit (continue)
ccc-agent approve-turn <token> revert     # reject; the agent undoes them
ccc-agent approve-turn <token> --paths a,b # commit only a,b (file-by-file)
```

### Post-session review (operator) + lingering sessions

A session that exits with un-committed changes stays as a reviewable branch:

```bash
ccc-agent list                              # sessions + states
ccc-agent review <session>                  # browse the diff
ccc-agent review <session> --accept         # commit policy-visible changes; ignored runtime noise is discarded
ccc-agent review <session> --reject         # discard everything
ccc-agent review <session> --commit a,b     # commit only a,b (file-by-file)
ccc-agent review <session> --emit-patch > c.patch  # line-by-line: prune hunks…
ccc-agent review <session> --apply-patch c.patch   # …then apply
ccc-agent commit <session> [<session> ...]  # commit one or more pending/frozen sessions
ccc-agent abort <session> [<session> ...]   # discard one or more sessions
ccc-agent cleanup --older-than 30           # remove old closed session bundles
ccc-agent cleanup --older-than 30 --dry-run # preview without deleting
```

If the node/container reboots while a session is `running`, the agent process and
FUSE mounts are gone but the session bundle and BranchFS branch remain. Resume it
from the durable metadata:

```bash
ccc-agent resume <session>                  # re-run the exact stored command
ccc-agent resume <session> -- bash          # custom recovery command/shell
ccc-agent resume <session> -- sh -lc '...'  # any custom exec after --
```

`resume` only accepts sessions still marked `running`, reuses the existing branch
(no new branch is created), re-mounts the saved roots, runs the stored command by
default, and then performs the normal freeze/status/policy finalization. If you
pass a custom command, the original `agent_command` stored in `session.json` is
preserved; the custom exec is recorded as a resume event. Use `--force` only
after verifying that no old agent process/mount is still alive.

`cleanup` only removes closed session bundles (`auto-committed`, `committed`,
`aborted`) older than the requested age. Pending review, running, and failed
sessions stay visible for human review/recovery.

Or directly via the BranchFS CLI (branch name == session id):

```bash
branchfs list   --storage <store>
branchfs status <session> --storage <store> --json
branchfs commit-branch <session> --storage <store>   # low-level: applies all deltas, bypasses ccc-agent ignores
branchfs abort-branch  <session> --storage <store>   # discard the branch
```

Durable review artifacts (summary.md, per-root status JSON, policy decision)
land under `<state_dir>/<session-id>/reviews/`. Other non-store runtime data
for the same run is bundled nearby, e.g.
`<state_dir>/<session-id>/session/session.json`,
`<state_dir>/<session-id>/mounts/`, and
`<state_dir>/<session-id>/control/control.sock`. BranchFS stores/deltas stay at
the configured root `store` paths.

### Credentials and agent state

`~/.codex`, `~/.claude`, and `~/.hermes` are **agent/system state**, not
BranchFS-protected project data by default. `ccc-agent run` direct-binds those
real shared directories read-write over the BranchFS home view so the tools can
manage their own config, sessions, caches, locks, and token refreshes. Changes
there persist immediately and are not committed, reviewed, or rolled back by
BranchFS; Codex/Claude/Hermes own their concurrent access semantics across
sessions/nodes.

Use `ccc-agent run --protect-agent-state` or config `protect_agent_state: true`
only when you intentionally want those dirs inside BranchFS review and are
prepared to handle tool-specific merging/conflicts yourself.

The containment plugin itself must not be writable agent state. In system deployments,
install the package/hooks as root-owned files under `/usr` (or another OS path
that bwrap exposes read-only) and write `config.json` under `/etc/ccc-agent`.
Do not edit the user's normal Codex/Claude/Hermes config or globally install
direct-run managed settings just to enable containment hooks. For a contained
run only, the trusted launcher mounts CCC plugin assets read-only, e.g. Claude's
`--plugin-dir`, Codex's in-sandbox plugin path, or Hermes'
`HERMES_BUNDLED_PLUGINS`.

`cred_mounts` remains available only for narrow special-case read-only overlays;
do **not** use it for the whole `~/.codex`, `~/.claude`, or `~/.hermes`
directories. API-key deployments can still combine `cred_mask` (overmount an
individual secret file with `/dev/null`) and `cred_env` (supervisor reads a
credential and passes it as env). OAuth-subscription logins (ChatGPT / Claude
account — the common case) authenticate from token files, so those files must
remain readable through the shared agent-state bind.

## Layers

| Piece | Role |
|---|---|
| `bin/ccc-agent` / `ccc-agent run` | trusted launcher: session + branch bundle + agent/shell + finalize; also used by transparent shims (workspace = `$PWD`) |
| `ccc-agent list/show/diff/...` | operator + in-sandbox control ops: list/show/diff/commit/abort/review/finalize-turn/approve-turn; `cleanup` prunes old closed session bundles |
| `ccc-agent setup` | installer/wiring op: config, plugin entries, optional transparent PATH shims |
| `ccc_agent/` | stdlib-only Python: session store, policy engine, BranchFS driver, bwrap assembler, control socket + per-turn handler |
| `shims/ccc-agent-shim.sh` | transparent `codex`/`claude`/... PATH shims |
| `assets/plugins/` | native CCC lifecycle-hook plugins (`claude-`/`codex-`/`hermes-ccc-containment`) injected read-only per contained run |
| `hooks/` | standalone Stop-hook / `notify` adapter scripts (manual/fallback registration) |
| `config/` | runtime config example + legacy claude/codex overlay examples (fallback only) |

Confinement modes (`confinement` in config.json):

- **`bwrap`** (default, the real boundary): runs the agent in a rootless
  bubblewrap user+mount+pid namespace — OS read-only, the BranchFS view
  read-write at its visible path, the real underlay/store hidden. By default it
  also binds the existing CCC/container `/run` runtime namespace so the agent can
  use sockets that the container deployment already exposes (Docker, ssh-agent,
  etc.). This is intentionally not a full container-escape-prevention boundary;
  use `ccc-agent run --full-isolation` or config `container_run_access: false`
  to omit ambient `/run` access and restore the older stricter mode. No container
  `CAP_SYS_ADMIN` needed (just unprivileged user namespaces). `bwrap_bin` and
  `bwrap_proc_mode` (`bind`|`ro`|`fresh`) are configurable.
- **`none`** (debug only — *not* a security boundary): runs the agent with its
  cwd inside the view but nothing else isolated; absolute-path writes bypass the
  view. Use only to exercise the policy/commit pipeline without bwrap.

Design references: `docs/architecture.md` (trust boundaries, sandbox layout),
`docs/policy.md` (path policy + secret hiding), and the workspace-level
`CCC_AGENT_BRANCHFS_PROTECTION_REVIEW_DESIGN.md`.

## Install

It's a pip package (stdlib-only, no dependencies) with one `ccc-agent` console
script and the shell hooks/shims bundled as package data.

```bash
# system (CCC images / shared): install into the SYSTEM python so the entry
# points are conda-independent (shebang pinned to /usr/bin/python3) and visible
# inside the bwrap sandbox (which only exposes /usr):
/usr/bin/python3 -m pip install --break-system-packages \
    "git+https://github.com/vicoslab/ccc-agent.git@master"
ccc-agent setup --system --branchfs-bin /usr/local/bin/branchfs --bwrap-bin "$(command -v bwrap)"

# user / dev:
python3 -m pip install --user "git+https://github.com/vicoslab/ccc-agent.git"
ccc-agent setup --user
```

### Shell completion

`pip install ccc-agent` installs shell completion files into standard
`share/...` locations, so bash/fish/zsh can pick them up the same way Git
completions are picked up by package installs:

- `share/bash-completion/completions/ccc-agent`
- `share/zsh/site-functions/_ccc-agent`
- `share/fish/vendor_completions.d/ccc-agent.fish`

With `pip install --user`, those land under the user base (normally
`~/.local/share/...`). With the system install command above, they land under the
system Python prefix. Start a new shell after install; no `source <(...)` step is
part of normal setup. `ccc-agent completion <bash|zsh|fish>` remains available
only as a debug / one-off fallback.

The completion hook reads the configured session store and completes optional
`session-id` prefixes for `list`, plus required `session-id` arguments for
`show`, `status`, `diff`, `review`, `commit`, `abort`, `thaw`, `finish`,
`finish-turn`, and `check-before-final`. It also completes `cleanup` options
such as `--older-than` and `--dry-run`.

`ccc-agent setup` does what pip can't: writes `config.json` with the
`agent_plugins` map, makes the bundled plugin hook scripts executable, and
optionally installs the transparent PATH shims (`--enable-shims`). It does
**not** edit the user's `~/.codex/config.toml`, `~/.claude/settings.json`, or
live Claude managed settings. Instead, for a contained run only, `ccc-agent run`
loads CCC hooks through each agent's **native plugin mechanism**: it bind-mounts
the matching read-only plugin (`ccc_agent/assets/plugins/…`) into the sandbox,
adds Claude's `--plugin-dir`, drops the Codex plugin at the in-sandbox Codex
plugin path, and sets Hermes' `HERMES_BUNDLED_PLUGINS`. Direct, uncontained
`codex`/`claude`/`hermes` runs load none of this. By default `ccc-agent run`
infers the plugin from the command executable basename, including absolute paths.
For example, these load the matching plugin:

- `ccc-agent run -- codex exec ...`
- `ccc-agent run -- /path/to/claude -p ...`

Use `--agent <name>` only for an explicit override; explicit selection wins over
executable-path inference. Disable injection with `--no-agent-plugins` (alias
`--no-hooks`). Agent state is shared pass-through by default; pass
`ccc-agent run --protect-agent-state` for one run, or generate/set
`protect_agent_state: true` in config, to keep `~/.codex`, `~/.claude`, and
`~/.hermes` inside BranchFS review.

The plugins live under the installed package (`ccc_agent/assets/plugins/…`),
which is under `/usr` for a system install and therefore exposed read-only
inside the sandbox. See `docs/agent-integration.md` for the per-agent activation
and turn-boundary matrix.

The branchfs binary and bwrap are separate (not pip-installable); in CCC images
the runit startup installs them and runs the two commands above (see the CCC
image repo's `06_ccc_agent.sh`).

## Deployment

Hard rules:

- `config.json`, the state dir pointer, and the plugin/hook source assets
  must **not** be writable by the agent user (mounted read-only in the sandbox);
- the BranchFS store and real underlay paths must not be visible inside the
  agent's view (bwrap mode enforces this by exposing only the view; in `none`
  debug mode the `.ccc-agent` deny pattern is the only fallback);
- hooks report and at most request self-repair (`ccc-agent
  check-before-final` exits 2 with the offending paths while the per-session
  repair budget lasts); only the supervisor/operator commits.

Shims (optional, after the explicit wrapper works for you):

```bash
# Simple case: no conda env shadows /usr/local/bin.
ccc-agent setup --system --enable-shims

# Conda-compatible case: use a dedicated trusted shim directory and install
# activation hooks in the env that contains codex/claude. On every `conda
# activate`, the hook re-prepends the shim directory ahead of $CONDA_PREFIX/bin;
# the shim then skips itself and resolves the real binary from the active env.
conda activate my-agent-env
ccc-agent setup --user --enable-shims \
    --link-dir "$HOME/.local/share/ccc-agent/shims" \
    --conda-activate-shims --conda-prefix "$CONDA_PREFIX"

# Verify precedence: this should print the shim path, not $CONDA_PREFIX/bin/codex.
command -v codex
```

Do **not** install the transparent `codex`/`claude` shim directly into the same
`$CONDA_PREFIX/bin` that contains the real binary: that overwrites or masks the
real executable. Use a separate shim directory and the conda activation hook so
PATH becomes:

```text
<trusted-shim-dir>:$CONDA_PREFIX/bin:...
```

The same generic shim is symlinked as each agent name:

```bash
ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/codex
ln -s /opt/ccc-agent/shims/ccc-agent-shim.sh /usr/local/bin/claude
# nested agent calls reuse the outer session via CCC_AGENT_SESSION
```

## Tests

```bash
python3 -m unittest discover                    # all non-FUSE, stdlib-only
```

Integration against a real `branchfs` binary (still no FUSE — daemon socket
only) runs automatically when the binary is found (or set
`CCC_AGENT_BRANCHFS_BIN`). Real FUSE mount + bwrap end-to-end validation
(`scripts/test-e2e-bwrap.sh`) needs a host with `/dev/fuse` and unprivileged
user namespaces; see *Runtime validation* in `docs/architecture.md`.
