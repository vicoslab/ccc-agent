---
name: ccc-containment
description: Always use in every session when ${CCC_AGENT_SESSION} env is non empty; check kept non-workspace files when finished/idling.
---

# CCC containment rules

Always use this skill when `${CCC_AGENT_SESSION}` is not empty. The CCC plugin also injects these rules at session/turn boundaries.

You are running in a contained filesystem. At each stop, the hook writes workspace/in-policy files through. Files outside workspace/policy stay separate until the user approves.

Workspace-scope changes are managed by trusted hooks; do not run workspace-scope commands yourself.

When you have finished your work and would otherwise idle, check kept files. The Stop hook runs `ccc-agent turn-review-kept`; if it reports kept files, ask briefly whether to commit, discard, or keep them, then run `ccc-agent turn-resolve <commit|discard|keep> --all-kept` unless a selective path decision is needed. You must run `ccc-agent turn-kept-status` if checking manually; use `--details` only when exact paths are needed.

Do not stop active loops/goals just to ask about commits unless approval is required to continue.
