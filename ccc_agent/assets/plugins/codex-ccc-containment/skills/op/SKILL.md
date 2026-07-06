---
name: op
description: Run a CCC contained-session turn operation from natural language
disable-model-invocation: true
argument-hint: [status|review|commit|discard|keep ...]
---

Interpret `$ARGUMENTS` as a CCC contained-session request. `status` -> `ccc-agent turn-kept-status`; `review` -> `ccc-agent turn-review-kept`; `commit|discard|keep` -> inspect status, map any path/natural-language selector to exact kept paths, then run `ccc-agent turn-resolve <decision> --paths <comma-separated-paths>`. Example: "discard all files in this folder but keep those" means split kept paths into discard/keep sets and run specific commands; ask if ambiguous.
