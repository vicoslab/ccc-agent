---
name: discard
description: Discard selected CCC kept files
disable-model-invocation: true
argument-hint: [all | paths | natural-language selector]
---

Use `$ARGUMENTS` to choose kept files. First run `ccc-agent turn-kept-status`. If arguments are `all`, discard every kept path. If arguments name exact paths, run `ccc-agent turn-resolve discard --paths <comma-separated-paths>`. If natural language, map to exact kept paths; ask if ambiguous. Do not discard unselected paths.
