---
name: ccc-containment
description: Use in every contained CCC session; operate live containment state only through the official CCC MCP tools.
---

# CCC containment

Always use this skill when `${CCC_AGENT_SESSION}` is set. You are working in a contained filesystem. Trusted lifecycle hooks maintain workspace scopes and finalize each turn; do not invoke hook lifecycle commands yourself. Process-exit finalization remains authoritative.

For direct Claude/Codex clients, use only the `ccc` MCP server for agent-facing decisions:

- `ccc_status`: compact kept/committed counts.
- `ccc_list_kept`: exact currently kept non-workspace paths.
- `ccc_commit_kept`: commit currently kept paths after nested human form elicitation.
- `ccc_discard_kept`: discard currently kept changes after nested human form elicitation.
- `ccc_keep_kept`: leave currently kept paths for later review.

Codex policy must prompt per-tool for every CCC MCP call; never add a blanket allow rule. Commit/discard fail closed without affirmative confirmation and accept only currently remembered kept paths. Never use ordinary `ccc-agent turn-resolve` or `turn-approve`; they are not an agent authority path. In unsupported wrapper/Hermes modes, leave resolution to external session review.

Do not stop active loops/goals merely to ask about kept paths. When finished or otherwise idling, call `ccc_status`; use `ccc_list_kept` and a resolution tool only when needed.
