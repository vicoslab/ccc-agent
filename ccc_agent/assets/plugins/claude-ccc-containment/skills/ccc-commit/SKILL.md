---
name: ccc-commit
description: Always use in every session when ${CCC_AGENT_SESSION} env is non empty; check kept non-workspace files when finished/idling.
---

# CCC commit check

Always use this skill in every session when `${CCC_AGENT_SESSION}` is not empty. The Claude CCC plugin also injects these rules at session start and before each user prompt so they are active even when model-invoked skill loading does not trigger.

You are running in a contained filesystem. At each stop, the hook writes workspace/in-policy files to the underlying filesystem. Files outside the workspace or policy stay separate and are not written there until the user approves.

When you have finished your work and would otherwise idle, check kept files. The Stop hook runs `ccc-agent turn-review-kept`; if it reports kept files, ask the user whether to commit, discard, or keep them and then run the shown `ccc-agent turn-resolve ...` command. You must run `ccc-agent turn-kept-status` if checking manually.

Do not stop active loops/goals just to ask about commits unless approval is required to continue.
