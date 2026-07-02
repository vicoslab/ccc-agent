# ccc-agent path policy and secret hiding

Two distinct mechanisms, often confused — keep them apart:

1. **Hide paths** (BranchFS, *preventive*): literal relative paths masked from
   the agent's branch view. The agent can never read them. Configured per
   protected root (`roots[].hide_paths`), enforced by the BranchFS resolver
   (`branchfs create --hide ...`).
2. **Deny/hide patterns** (policy engine, *detective*): globs evaluated at
   freeze time against the canonicalized change list. A match never blocks the
   agent while it runs; it downgrades the decision to `pending-review`.

## Decision model

After freeze, every changed path (from `branchfs status`, mapped into the
agent-visible namespace) is canonicalized — `/home/$USER/...` and
`/storage/user/...` collapse to one namespace — then:

```text
no changes                                      -> no-op close (abort branch)
mode=throwaway                                  -> abort
mode=manual | read-only-review                  -> pending-review
mode=workspace-auto | training-run:
    every path within allowed_scopes
    and no deny/hide pattern matches            -> auto-commit
    otherwise                                   -> pending-review
```

`allowed_scopes` defaults to the declared workspace. Scopes may be declared
via either alias (`/home/...` or `/storage/user/...`); canonicalization makes
them equivalent.

## Multi-session conflicts and LLM/human handling

BranchFS provides lazy live-base views, not frozen snapshots. A session keeps its
own branch deltas/tombstones, but inherited paths it never touched may reflect
newer commits from other sessions.

When a parent/base path changed after this session first touched the same path,
BranchFS/ccc-agent should first try a git-style 3-way merge for regular text
files. Clean non-overlapping merges are committed as merged content and treated
like disjoint-path changes for policy. Unclean overlaps, binary files,
delete-vs-modify, type changes, symlinks/directories, or missing merge-base
content become conflict records.

Conflict records are **review signals**, not generic commit failures. Normal
latest-session-wins policy lets commit proceed while recording the conflict. The
same records must be usable in two paths:

- **LLM-handled:** `check-before-final` / turn hooks print concise conflict
  summaries so the agent can reconcile in the still-running branch before
  finalization.
- **Human-handled:** review artifacts and CLI output list conflicts separately
  from ordinary changes and ignored paths, so a human can inspect or request
  follow-up repair.

## Pattern semantics (`ccc_agent.policy.path_matches`)

- pattern without `/` — matches any single path component:
  `.env`, `id_rsa*`, `*.pem`
- pattern with `/`, not absolute — matches that component sequence anywhere,
  plus everything below it: `.git/hooks` matches `proj/.git/hooks/pre-commit`
- absolute pattern — fnmatch against the whole canonical path:
  `/storage/group/*`

Default deny set (see `policy.DEFAULT_DENY_PATTERNS`): SSH/GPG material,
`.env*`, key/credential files, `.netrc`, `.aws`, `.kube/config`,
`.docker/config.json`, `.git/config`, `.git/hooks`, shell startup files,
`.condarc`, and `.ccc-agent` (supervisor state). Override per deployment via
`deny_patterns` in policy config; extend per run with `ccc-agent run --hide`.

Default ignore set (see `policy.DEFAULT_IGNORE_PATTERNS`): launcher/runtime noise
that should never become a deliverable, including NFS `.nfs*` silly-renames,
caches/logs such as `~/.cache` and `~/.npm/_logs`, and interactive history files
such as `~/.bash_history`, `~/.zsh_history`, `~/.python_history`, `~/.lesshst`,
and common database/REPL histories. Ignored changes are removed before turn
checks, review artifacts, auto-commit, and manual `ccc-agent commit/review
--accept`; they are discarded with the branch unless a human uses lower-level
BranchFS tools outside the normal supervisor flow.

Agent tool homes are handled separately: by default `~/.codex`, `~/.claude`, and
`~/.hermes` are **direct shared rw binds outside BranchFS**, not ignored deltas.
They are system/agent state, so changes there persist immediately and are owned
by Codex/Claude/Hermes concurrency semantics. Use `ccc-agent run
--protect-agent-state` or config `protect_agent_state: true` only if you want
those directories inside BranchFS status/review/commit.

`ccc-agent run` also adds per-session ignores for its own bwrap/plugin plumbing
when a bind target lands inside a BranchFS-backed view: read-only `bwrap_ro_binds`,
`cred_mounts`/`cred_mask`, and the matched agent plugin's `sandbox_path` and
`ensure_dirs`. Prefer mounting trusted runtime/plugin assets outside the view
(`/ccc-agent`, `/opt/...`) when the agent supports it; when a native agent (for
example Codex plugin discovery) requires an in-home path, those mountpoint deltas
are treated as infrastructure and ignored.

## Secret hiding: what is and is not guaranteed

With `hide_paths` on a root (e.g. `.ssh`, `.netrc`, `.aws`):

- the agent cannot read or list those inherited paths — resolution and
  readdir treat them as nonexistent (subtree included);
- main and other branches are unaffected; the underlay is untouched;
- if the agent *creates* a file at a hidden path, that is its own delta
  (shadow): visible to the agent, reported by status, and **flagged by the
  matching deny pattern** so it cannot auto-commit over the real secret.

Not guaranteed:

- secrets inside the workspace the user explicitly exposed (an `.env` the
  project itself contains is readable unless listed in `hide_paths`; the
  deny pattern still forces review if the agent modifies it);
- patterns in `hide_paths` — BranchFS hiding is literal-prefix only by
  design (O(1), no tree scans). Use well-known literal locations there and
  globs in `deny_patterns`/`hide_patterns`;
- anything outside the protected roots.

Defense in depth, in order: hide at the filesystem (can't read), deny at
policy (can't auto-commit), review artifacts (humans see exactly what was
touched).

## Policy modes

| mode | use case |
|---|---|
| `workspace-auto` | default: agent edits its project, auto-commit when clean |
| `manual` | high-stakes data; always a human decision |
| `read-only-review` | audits/dry-runs: report, never commit automatically |
| `training-run` | scopes = declared artifact dirs (checkpoints, logs) |
| `throwaway` | exploration; discard at completion unless a human commits first |

## Bounded self-repair (`ccc-agent check-before-final`)

When a harness supports a blocking Stop hook (Claude Code, Codex), the hook
runs `ccc-agent check-before-final <session>` before reporting the turn.
The check classifies **live** status — no freeze, no commit — and only looks
at scope and deny/hide hygiene; mode semantics (`manual`,
`read-only-review`, ...) still apply at finalize:

- **clean** → exit 0 (`check-clean` event); the stop proceeds and the normal
  finalize flow decides per mode;
- **dirty, repair budget left** → exit 2 with the offending paths printed;
  the harness blocks the stop and the agent reverts in-session
  (`repair-requested` event, `repair_attempts` incremented);
- **dirty, budget exhausted** → exit 0 (`repair-budget-exhausted` event) so
  hooks can never livelock the agent; the violations simply land in
  `pending-review` at finalize.

`max_policy_repair_attempts` (default 2) bounds the loop; the count is
stored per session, in supervisor state the agent cannot touch.
