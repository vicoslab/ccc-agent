---
name: ccc-commit
description: Commit selected CCC kept files
disable-model-invocation: true
argument-hint: [paths or natural-language selector]
---

Use `$ARGUMENTS` to choose kept files. If arguments are `all` or the user clearly means every pending kept file, run `ccc-agent turn-resolve commit --all-kept` without listing paths. For exact-path or natural-language selection, run `ccc-agent turn-kept-status --details`, map to exact kept paths, then run `ccc-agent turn-resolve commit --paths <comma-separated-paths>`. Ask if ambiguous. Never commit paths the user did not select.
