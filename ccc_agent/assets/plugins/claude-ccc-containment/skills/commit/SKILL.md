---
name: commit
description: Commit selected CCC kept files
disable-model-invocation: true
argument-hint: [paths or natural-language selector]
---

Use `$ARGUMENTS` to choose kept files. First run `ccc-agent turn-kept-status`. If arguments name exact paths, run `ccc-agent turn-resolve commit --paths <comma-separated-paths>`. If arguments are natural language, map them to exact kept paths from status; ask if ambiguous. Never commit paths the user did not select.
