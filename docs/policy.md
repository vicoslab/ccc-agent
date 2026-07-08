# Path policy and secret handling

`ccc-agent` policy decides what to do with actual BranchFS changes after a turn
or session reaches a review point. It is a commit/review policy, not the
filesystem implementation itself.

## Three path mechanisms

| Mechanism | Layer | Purpose | Timing |
|---|---|---|---|
| `roots[].hide_paths` | BranchFS | Prevent the agent from reading/listing inherited sensitive literal paths. | Before and during the run. |
| `deny_patterns` / `hide_patterns` | Policy | Force review when changed paths match sensitive glob-like patterns. | After status. |
| `ignore_patterns` | Policy | Drop runtime/cache noise from review and commit. | After status, before decision. |

Keep these separate. Hiding prevents reads for known literal locations; denying
prevents auto-commit; ignoring removes non-deliverable noise.

## Decision model

After freeze or turn finalization:

1. `ccc-agent` reads BranchFS status for each protected root.
2. It maps paths into the agent-visible namespace and canonicalizes aliases such
   as `/home/<user>` and `/storage/user/...`.
3. It collapses raw status entries into net final changes.
4. It removes ignored runtime/cache changes.
5. It classifies remaining paths against allowed scopes and deny/hide patterns.
6. It chooses a decision.

```text
no policy-visible changes                 -> close as no-op and discard branch
policy mode = throwaway                   -> abort/discard
policy mode = manual/read-only-review     -> pending-review
policy mode = workspace-auto/training-run:
  all changes inside allowed scopes
  and no deny/hide pattern matches        -> auto-commit
  otherwise                               -> pending-review
```

`allowed_scopes` defaults to the run workspace. Add scopes with `ccc-agent run
--scope PATH` or config `policy.allowed_scopes`.

## Policy modes

| Mode | Behavior |
|---|---|
| `workspace-auto` | Default. Auto-commit only when all policy-visible changes are inside allowed scopes and no deny/hide pattern matches. |
| `manual` | Always preserve the branch for human review. |
| `read-only-review` | Report changes, never auto-commit. |
| `training-run` | Same decision structure as `workspace-auto`, intended for declared artifact/output scopes. |
| `throwaway` | Discard the branch at completion unless work was resolved manually before then. |

## Pattern semantics

`ccc_agent.policy.path_matches` supports:

- pattern without `/`: matches any single path component;
  - examples: `.env`, `id_rsa*`, `*.pem`
- relative pattern with `/`: matches that component sequence anywhere, including
  descendants;
  - example: `.git/hooks` matches `repo/.git/hooks/pre-commit`
- absolute pattern: matched against the whole canonical path, including
  descendants of a matched directory;
  - example: `/storage/group/private/*`

Default deny patterns include SSH/GPG material, `.env*`, private keys,
credential files, `.netrc`, `.aws`, `.kube/config`, `.docker/config.json`,
`.git/config`, `.git/hooks`, shell startup files, `.condarc`, and `.ccc-agent`.

## Preventive hiding

`roots[].hide_paths` are literal relative prefixes enforced by BranchFS. Example:

```json
{
  "roots": [
    {
      "name": "storage",
      "base": "/storage",
      "visible": "/storage",
      "hide_paths": [
        "user/my-home/.ssh",
        "user/my-home/.aws",
        "user/my-home/.netrc"
      ]
    }
  ]
}
```

Effects:

- inherited hidden paths cannot be read or listed by the agent;
- the real underlay is unchanged;
- if the agent creates a new file at a hidden path, that new branch delta is
  visible to the agent but policy deny patterns force review before commit.

`hide_paths` are not glob patterns. They are literal prefixes so branch creation
and path checks remain cheap. Use `deny_patterns`/`hide_patterns` for globs.

## Ignored runtime noise

Ignored changes are excluded from review and commit. They are discarded with the
branch unless an operator deliberately uses low-level BranchFS tools outside the
normal `ccc-agent` flow.

Default ignores cover common non-deliverables such as:

- NFS `.nfs*` silly-renames;
- generic cache/log directories like `.cache` and `.npm/_logs`;
- shell/REPL/client history files (`.bash_history`, `.zsh_history`,
  `.python_history`, `.sqlite_history`, `.lesshst`, fish history, IPython
  history, etc.);
- launcher-created bind/plugin/mask mountpoint paths when they happen inside a
  protected view.

Startup/config files such as `.bashrc`, `.profile`, `.zshrc`, and `.condarc` are
not ignored; they are deny matches that require review.

## Agent runtime state

By default, `~/.codex`, `~/.claude`, `~/.hermes`, and selected Claude runtime
paths are direct shared read-write binds outside BranchFS review. Changes there
persist immediately and are not part of policy status.

Use `ccc-agent run --protect-agent-state` or config `protect_agent_state: true`
only when you intentionally want agent internals to be branch deltas. In that
mode `ccc-agent` does not understand agent-specific databases or cache merge
semantics; the user must review conflicts/noise explicitly.

## Per-turn kept paths

Interactive plugins call `turn-finalize --default-keep` by default. That means:

- ordinary in-scope workspace changes can commit at turn boundaries;
- new out-of-scope or deny-matching paths are kept in the branch and remembered;
- the agent continues instead of turning every autonomous loop into an approval
  gate;
- the user can later resolve kept paths with `turn-resolve` or normal final
  review.

Useful commands inside a live session:

```bash
ccc-agent turn-kept-status --details
ccc-agent turn-review-kept
ccc-agent turn-resolve commit --paths a,b
ccc-agent turn-resolve discard --paths c
ccc-agent turn-resolve keep --paths d
```

Discarding a live kept path asks the trusted supervisor to revert the path in the
BranchFS branch. Added files disappear, modified inherited files fall back to the
base view, and deleted inherited files reappear.

## Multi-session conflicts

BranchFS branches are lazy live-base overlays, not frozen snapshots. A session's
own deltas/tombstones remain stable, while untouched inherited paths may reflect
commits from other sessions.

When a path changed in the base after this session first touched it, BranchFS can
record conflict information. Clean non-overlapping text merges are treated like
ordinary changes. Unclean overlaps, binary/type/delete conflicts, symlink/dir
cases, or missing merge-base content become review signals.

Conflict records must be visible to both:

- LLM repair paths (`turn-check` / turn hooks);
- human review artifacts and CLI summaries.

They are not a reason to let the agent bypass review or commit directly.

## Defense in depth

Use the mechanisms in order:

1. Hide known secret locations with BranchFS `hide_paths` so the agent cannot read
   inherited content.
2. Deny sensitive path patterns so any attempted modifications require review.
3. Keep review artifacts durable so humans can inspect exactly what changed.
4. Commit only through the trusted supervisor.
