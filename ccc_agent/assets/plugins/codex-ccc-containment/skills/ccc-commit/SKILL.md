---
name: ccc-commit
description: Always use in every CCC contained session; check kept non-workspace files when finished/idling.
---

# CCC commit check

Always use this skill in every `ccc-agent` contained session. You are running in a contained filesystem. At each turn, the hook writes workspace/in-policy files to the underlying filesystem. Files outside the workspace or policy are kept separate and are not written there until the user approves.

You must run `ccc-agent turn-kept-status` when you have finished your work and would otherwise idle. If you are still running loops/goals, wait until they finish. Do not stop active loops/goals just to ask about commits unless approval is required to continue.

If status shows kept files, ask the user whether to commit, discard, or keep. Then run `ccc-agent turn-review-kept` for the exact prompt or run the shown `ccc-agent turn-resolve ...` command.
