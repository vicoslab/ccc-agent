---
name: discard
description: Discard selected CCC kept files
disable-model-invocation: true
argument-hint: [all | paths | natural-language selector]
---

Use `$ARGUMENTS` to choose kept files. If arguments are `all` or the user clearly means every pending kept file, run `ccc-agent turn-resolve discard --all-kept` without listing paths. For exact-path or natural-language selection, run `ccc-agent turn-kept-status --details`, map to exact kept paths, then run `ccc-agent turn-resolve discard --paths <comma-separated-paths>`. Ask if ambiguous. Do not discard unselected paths. Discard is active: the supervisor drops the selected kept delta/tombstone, so added files disappear, modified inherited files restore the base view, and deleted inherited files/directories reappear.
