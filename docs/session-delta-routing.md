# Per-logical-session delta routing

`session_delta_routing` is an optional provenance layer inside the existing
outer BranchFS+bwrap session. It can separate supported vendor conversations
into nested BranchFS branches, but it is **not** an additional security
boundary. The outer session remains authoritative for containment, complete
status, review, and writes to the real underlay.

## Scope and defaults

```json
{
  "session_delta_routing": false,
  "session_delta_routing_vendors": ["codex"]
}
```

Routing is disabled by default. The initial supported vendor is direct
`codex app-server` running in bwrap with per-turn control enabled. Claude Code
remains shared/unattributed because its current official root/session signals
do not establish a sufficiently stable logical-session-to-bwrap mapping.
Hermes has per-conversation workspace authority but no vendor bwrap adapter;
its writes therefore remain in the outer delta.

If capability detection fails, ccc-agent launches normally and records routing
as unavailable. It never blocks the agent merely because attribution is
unavailable.

## Trust and authority model

The routing path has three separate properties:

1. **Workspace authority.** A process-pinned trusted client replaces one
   logical session's complete root set with a strictly monotonic generation.
   Raw logical IDs are accepted only on the trusted channel and are persisted
   as digests, never as branch names or capabilities.
2. **Filesystem routing.** A read-only bwrap adapter asks the supervisor for an
   already-provisioned route and appends bind mounts to the vendor's bwrap
   argv. It cannot create routes, widen admitted roots, approve changes, or
   receive real-underlay paths.
3. **Commit authority.** Only the outer supervisor can freeze, reconcile, and
   selectively apply changes. Nested branches merge only into the outer branch,
   never directly into the real base.

Possession of a route hint, route ID, control token, environment variable, or
bwrap command line is not commit authority.

## Workspace session protocol

The privileged operation is `workspace-session-replace`:

```text
(source, trusted process incarnation, logical session ID, generation,
 complete root set, active|ended)
```

The control server overwrites caller-supplied source/incarnation fields from
its pinned process/connection identity. The session record stores:

- a schema version;
- a key derived from source, process incarnation, and a SHA-256 logical-ID
  digest;
- the last generation and state;
- exact admitted root records, including canonical paths and identity
  snapshots; and
- no raw vendor/conversation ID.

Same-generation retries are accepted only when byte-equivalent to the previous
complete replacement. Older generations cannot restore an ended session.
Ending or losing one trusted transport revokes only records owned by that
logical session or transport incarnation. `mcp_workspace_roots` is retained as
a derived compatibility union; it is not the source of truth.

Every admitted root must be an existing non-symlinked directory beneath both a
protected BranchFS root and the configured admission ceiling. Identity is
revalidated before automatic apply.

## Codex observation and bwrap interception

Trusted PID 1 observes successful Codex JSONL responses for thread start,
resume, fork, turn updates, and archive/delete. It emits typed per-thread
replacement events. Failed, stale, unmatched, malformed, or out-of-order
responses grant no authority.

When routing is enabled, the outer launcher:

1. resolves the real Codex runtime and adjacent vendor `bwrap`;
2. binds the real binary to a private `/dev/shm/ccc-agent-<session>/...` path;
3. overlays the package-owned `ccc-bwrap-route` script read-only at the vendor
   bwrap path after the outer BranchFS views are installed;
4. exposes only the opaque nested-route mount parent and the existing control
   socket; and
5. removes the private runtime directory during cleanup.

The adapter recognizes only documented logical-session environment hints (for
Codex, `CODEX_THREAD_ID`), sends a bounded read-only lookup, validates the
response shape and route-source prefix, appends route binds last before the
bwrap command separator, and `exec`s the real bwrap. Missing hints, unknown
routes, probes, malformed argv, lookup timeouts, and protocol errors delegate
to the real bwrap unchanged.

Route lookup is tokenless but not anonymous: `SO_PEERCRED`, exact registered
client PID/start-time, ancestry, launch boundary, vendor match, and the expected
interposed bwrap argv path are checked on every request. The response contains
only opaque paths already visible inside the outer sandbox.

## Nested route lifecycle

Each route has an opaque random `route-<hex>` ID and one nested BranchFS branch
per outer protected root:

```text
provisioning -> active -> quiescing -> frozen -> merged
                                      \-> pending-review
```

Before provisioning, the supervisor captures a fingerprinted baseline of the
outer delta. Provisioning creates and mounts child branches over the live outer
branches, then publishes lookup bindings only after every root is active.

At logical-session end or supervisor recovery:

1. route lookup stops;
2. every child is frozen;
3. status and route metadata are written under
   `reviews/routes/<route-id>/`;
4. child mounts are removed;
5. each child is committed into its live outer parent; and
6. merge outcomes/conflicts are persisted before the route becomes `merged` or
   `pending-review`.

A restart recovers provisioning/active/quiescing/frozen records conservatively.
Missing snapshots, cleanup failures, malformed metadata, or unresolved
conflicts remain reviewable rather than being guessed away.

## Reconciliation and safe apply

Final outer status is complete and authoritative. For each path it is reconciled
with route snapshots, outer baselines, merge outcomes, and post-merge
fingerprints. Review categories include:

- `attributed`;
- `shared/unattributed`;
- `attributed-out-of-scope`;
- `multiply-influenced`;
- `conflicted`; and
- `conflicted-after-merge`.

Only an `attributed` path beneath that route's exact admitted roots receives
path-exact automatic apply authority. Its fingerprint must still match at final
freeze. A pre-existing/shared outer delta on the same path, multiple route
contributors, a post-merge mutation, an unresolved route state, or an admission
identity change forces pending review. Apply preflight also rejects symlinked or
non-directory underlay parents.

Artifacts include:

```text
reviews/
  route-reconciliation.json
  routes/<route-id>/route.json
  routes/<route-id>/status.json
  routes/<route-id>/merge.json
```

`summary.md` reports path-category counts, routed versus bypassed/unattributed
bwrap coverage, and review blockers.

## Limitations

- The adapter covers supported vendor bwrap call paths, not arbitrary custom
  launchers. Unintercepted writes still land safely in the outer branch and are
  labeled shared/unattributed.
- Same-UID code inside a compromised official client can influence provenance;
  attribution is a review aid, not process attestation.
- Large-file fingerprints use metadata rather than full content hashing.
- Route data is node-local live state. Only reviewed outer commits are globally
  durable across CCC nodes.
- `container_run_access` and the outer bwrap threat model are unchanged.
