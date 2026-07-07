---
name: status
description: Show CCC contained-session committed and kept files
disable-model-invocation: true
argument-hint: [optional filter]
---

Use `$ARGUMENTS` as an optional filter. Run `ccc-agent turn-kept-status` and summarize the compact counts. Do not dump long path lists to the user. Run `ccc-agent turn-kept-status --details` only when exact paths are needed for a selective decision or the user explicitly asks for details.
