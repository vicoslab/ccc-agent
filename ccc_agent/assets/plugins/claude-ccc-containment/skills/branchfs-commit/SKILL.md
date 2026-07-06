---
description: When finishing ccc-agent BranchFS work, run final kept-path review
---

You are running inside ccc-agent with BranchFS. Workspace/in-policy changes are committed each turn. Non-workspace or out-of-policy paths are kept in the branch only until the user confirms.

While still working/thinking, do not ask. When finished and before final response, run:

```bash
ccc-agent turn-review-kept
```

If it lists paths, ask the user whether to commit, discard, or keep them. Then run one of:

```bash
ccc-agent turn-resolve commit --paths PATHS
ccc-agent turn-resolve discard --paths PATHS
ccc-agent turn-resolve keep --paths PATHS
```

Status anytime:

```bash
ccc-agent turn-kept-status
```
