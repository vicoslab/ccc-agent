---
name: ccc-containment
description: Use in every contained CCC session; operate live containment state only through the official CCC MCP tools.
---

# CCC containment

Always use this skill when `${CCC_AGENT_SESSION}` is set. You are working in a contained filesystem. Trusted supervisor policy and authenticated client lifecycle signals maintain effective workspace scopes; shell hooks are proposal/cleanup hints and cannot broaden commit authority. Do not invoke hook lifecycle commands or fabricate workspace metadata yourself. Process-exit finalization remains authoritative.

For direct Claude/Codex clients, use only the `ccc` MCP server for agent-facing decisions:

- `ccc_status`: compact kept/committed counts.
- `ccc_list_kept`: exact currently kept non-workspace paths.
- `ccc_commit_kept`: commit currently kept paths after nested human form elicitation.
- `ccc_discard_kept`: discard currently kept changes after nested human form elicitation.
- `ccc_keep_kept`: leave currently kept paths for later review.
- `ccc_abort_session`: after confirmation, mark all still-live branch changes for discard at process exit; earlier committed turns are unaffected, and the agent should exit after acceptance.

Codex policy must prompt per-tool for every CCC MCP call; never add a blanket allow rule. Commit/discard/abort fail closed without affirmative confirmation; path operations accept only currently remembered kept paths. If the trusted client transport is not hardened, these tools return `pending-external-approval` without applying anything; do not retry or claim success, and direct the user to external session review. Never use ordinary `ccc-agent turn-resolve` or `turn-approve`; they are not an agent authority path. In unsupported wrapper/Hermes modes, leave resolution to external session review.

Do not stop active loops/goals merely to ask about kept paths. When finished or otherwise idling, call `ccc_status`; use `ccc_list_kept` and a resolution tool only when needed.
