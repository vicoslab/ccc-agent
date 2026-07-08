# Design decisions

This page records the main design choices behind `ccc-agent` and the rationale for
keeping them this way.

## BranchFS branch before agent start

**Decision:** create a BranchFS branch and replace writable protected roots before
starting the agent.

**Why:** hooks are too late to be the protection boundary. A hook can detect a
turn ending, but it cannot undo the fact that the agent already had direct write
access if the real underlay was visible. The filesystem view must be established
first; hooks only improve review timing.

## Trusted supervisor owns commit authority

**Decision:** agents may write branch deltas, but only the trusted `ccc-agent`
supervisor can freeze, inspect, commit, or abort.

**Why:** rollback alone is insufficient if the agent can also commit or hide what
it changed. The important boundary is authority: untrusted code creates deltas;
trusted code decides whether those deltas become real storage.

## Process-exit finalization is authoritative

**Decision:** every session finalizes on process exit even when native turn hooks
exist.

**Why:** process exit is universal and agent-agnostic. Codex, Claude, Hermes, and
other tools change hook contracts over time. Hooks are best-effort convenience;
final freeze/status/policy based on real BranchFS status is the correctness path.

## Hooks are best-effort turn signals

**Decision:** plugins call `turn-finalize` or related `turn-*` commands, but they
never directly commit data.

**Why:** native plugins provide useful interactive behavior, especially committing
safe workspace changes between turns and surfacing kept paths. They must remain a
UX layer. If a hook fails, the worst acceptable result is delayed review at
session end, not unsafe commit.

## Rootless bubblewrap instead of a new privileged container

**Decision:** use rootless bwrap user/mount/PID namespaces for the command
sandbox.

**Why:** the required boundary is hiding real writable roots and BranchFS control
state from the agent. Rootless bwrap can assemble that view without granting the
application container `CAP_SYS_ADMIN`. A separate full container is heavier and
not necessary for the first-order filesystem review goal.

## Not a full escape-prevention boundary

**Decision:** default bwrap mode protects configured filesystem roots, but may
preserve ambient runtime access such as `/run`, read-only `/var`, and device-capable
`/dev` when the outer runtime exposes them.

**Why:** real agent workflows often need runtime sockets/devices intentionally
provided by the surrounding environment. Removing all of them breaks useful work.
Users who prefer stricter isolation can use `--full-isolation` or
`container_run_access: false`.

## One BranchFS root for aliased storage

**Decision:** if `/home/<user>` and another visible path refer to the same backing
files, they are exposed from one branch, not branched independently.

**Why:** two branches over the same bytes create inconsistent views and ambiguous
commits. One root plus alias binding preserves path compatibility while keeping a
single branch state.

## Lazy live-base branches, not snapshots

**Decision:** BranchFS branches inherit lazily from the current base/parent rather
than snapshotting entire trees.

**Why:** protected roots can contain millions of files. Branch creation must be
O(1). Untouched inherited paths may observe newer committed base content, while
paths the session touched remain defined by the branch's own deltas/tombstones.
Same-path issues are handled at commit/review time.

## No distributed live-FUSE replication

**Decision:** live FUSE mounts are local implementation state. The durable/global
boundary is the reviewed commit to the real underlay.

**Why:** a FUSE mount is kernel/userspace state on one node. Replicating live
mount state across nodes would add complexity in the wrong layer. Other nodes need
committed results, not the live private branch of a running local agent.

## Sidecar stays policy-free

**Decision:** the FUSE sidecar only brokers privileged local mount operations.

**Why:** mixing path policy and review decisions into the privileged mount broker
would enlarge the trusted surface and couple deployment plumbing to user policy.
`ccc-agent` owns sessions and review; BranchFS owns filesystem semantics; the
sidecar owns mount mechanics.

## Selective commit instead of raw `branchfs commit-branch`

**Decision:** normal `ccc-agent` auto-commit/review applies only reviewed
policy-visible changes, then discards the remaining branch.

**Why:** a low-level branch commit would also apply ignored runtime noise and any
unreviewed deltas. Selective commit lets policy-visible project changes land while
cache/history/plugin mountpoint churn is dropped.

## Agent runtime state is shared by default

**Decision:** Codex/Claude/Hermes state directories are direct shared read-write
binds by default, outside BranchFS review.

**Why:** agent tools maintain caches, logs, sessions, locks, token refresh files,
SQLite databases, and installed binaries. Treating all of that as project output
creates noisy reviews and hard merge problems. Users can opt into
`--protect-agent-state` when they deliberately want those paths reviewed.

## Trusted plugin assets are read-only

**Decision:** bundled plugin/hook files live in package/root-owned locations and
are mounted read-only only for matching contained runs.

**Why:** user-home hook files are easy for the user/agent to replace or disable.
The runtime must load trusted hook code that the contained agent can read but not
edit. At the same time, normal direct agent runs should not be forced through
containment hooks.

## JSON configuration and session records

**Decision:** use JSON rather than YAML for trusted config/session artifacts.

**Why:** the supervisor is stdlib-only Python. JSON avoids adding parser
dependencies to the trusted runtime and is easy to inspect with common tools.

## Generated review artifacts are snapshots

**Decision:** `reviews/` contains generated artifacts for frozen/completed review
points, not a live status database.

**Why:** pending branches can be thawed and edited again. Cached status from a
previous freeze becomes stale as soon as the branch is mutable. Live sessions must
read BranchFS status; finalization rewrites generated artifacts from fresh status.
