---
name: status
description: Show CCC contained-session committed and kept files
disable-model-invocation: true
argument-hint: [optional filter]
---

Run `ccc-agent turn-kept-status` and summarize the output. It shows paths already written to the underlying filesystem and paths still kept separate until user approval. If `$ARGUMENTS` is non-empty, filter or focus the summary on that text, but do not hide kept/uncommitted paths that may need action.
