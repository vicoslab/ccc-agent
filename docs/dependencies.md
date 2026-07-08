# Dependencies

## Python package

`ccc-agent` itself is stdlib-only Python:

- Python `>= 3.9`;
- `setuptools>=61` only for building/installing from source;
- no runtime Python package dependencies.

This is intentional: the trusted supervisor should run in minimal system Python
environments without pulling a dependency tree into the protected runtime.

## Required runtime components

| Component | Why it is needed |
|---|---|
| BranchFS CLI | Creates branches, mounts agent views, freezes/thaws, reports status, commits/aborts low-level branches. Configure with `branchfs_bin`. |
| bubblewrap (`bwrap`) | Builds the rootless user/mount/PID namespace in `confinement: "bwrap"`. Configure with `bwrap_bin`. |
| Unprivileged user namespaces | Required by rootless bwrap. The kernel/container runtime must allow them. |
| FUSE support | Required for real BranchFS mounts. In managed containers this is usually provided through a FUSE sidecar and `/dev/fuse`. |
| Writable state directory | Holds session records, mountpoints, control socket paths, and review artifacts. Must be outside the agent-visible protected view or hidden from it. |
| Branch store directory | Holds BranchFS deltas/tombstones/metadata. Must not be visible inside the agent sandbox. |

## Optional runtime components

| Component | Used for |
|---|---|
| `ccc-fuse-sidecar` | Privileged local FUSE/mount brokering when the application container does not have `CAP_SYS_ADMIN`. The sidecar remains policy-free. |
| Codex CLI | Agent command and native plugin/Stop-hook integration. |
| Claude Code | Agent command and native `--plugin-dir`/Stop-hook integration. |
| Hermes Agent | Agent command and bundled-plugin integration. |
| OpenCode | Command can be wrapped and reviewed at process exit; native plugin integration is not part of the current bundled set. |
| Docker/ssh-agent/runtime sockets | Available inside the sandbox only if the surrounding runtime exposes them and `container_run_access` remains enabled. |

## Build and test components

| Component | Used for |
|---|---|
| `python3 -m unittest` | Main non-FUSE test suite. |
| Shell (`bash`) | Script syntax and smoke tests under `scripts/`. |
| Real `branchfs` binary | Optional integration tests that exercise daemon/status paths without necessarily requiring FUSE. |
| A FUSE-capable host | End-to-end BranchFS mount + bwrap validation. Local unprivileged development containers may fail with `Operation not permitted`; do not treat unit tests as proof of runtime FUSE support. |
| `python3 -m build` | Wheel/sdist packaging checks. |

## Deliberate non-dependencies

`ccc-agent` does not depend on:

- a distributed live-FUSE replication layer;
- agent self-reporting for correctness;
- a full extra container per agent session;
- Python libraries beyond the standard library;
- low-level BranchFS commit authority inside agent plugins.

Those omissions are part of the design. The trusted supervisor owns review and
commit, while plugins only provide lifecycle signals and user-facing convenience.
