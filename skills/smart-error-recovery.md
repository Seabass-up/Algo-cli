---
name: smart-error-recovery
description: When a tool returns an error, classify it and pick the right recovery path: tighten input, change tools, ask the user, or escalate to /reason reflexion.
tags: [algo-cli, error-handling, recovery, reflexion]
created: 2026-06-09
---

# Smart Error Recovery

## Trigger

Whenever a tool call returns a string starting with `Error:`.

## Steps

1. **Read the error string verbatim.** It usually contains a hint
   (e.g. "matched 2 locations" means "tighten old_string" or
   "pass replace_all=True"; "old_string not found" means "re-read
   the file and check whitespace").
2. **Classify** the error:
   - *Transient* (network, model timeout): retry once with same args,
     then with timeout bump, then escalate to user.
   - *Input validation* (empty, ambiguous, missing file): adjust the
     input and retry.
   - *Permission denied* (`tool_denied` event, approval needed):
     stop and ask the user, or check whether `/auto` is on.
   - *Logical* (no search results, hypothesis wrong): switch tools
     or change approach.
3. **If the same tool fails twice in a row:** switch tools or change
   strategy. Don't keep retrying the same broken call.
4. **If 3+ different tools have failed on the same goal:** switch to
   `/reason reflexion` mode, or stop and ask the user for guidance.
5. **Always narrate the recovery** in the user-facing answer:
   "I tried X, got Y, so I tried Z instead."

## Key Discoveries

- The 2,000-character tool-result cap in `tools.MAX_TOOL_RESULT` is
  per-call. For large matches, narrow the search with `glob=` and
  `path=` arguments rather than asking for everything at once.
- `harness_search` returns "No harness matches" for both "nothing in
  the index" and "all matches were excluded by kind filter". Try
  `kind=None` first to see what's there.
- `run_shell` has a hard 120s cap regardless of `timeout=` argument.
  Long-running commands need to be backgrounded and polled, not timed
  out at 600s and hoped for.
- `edit_file` is the highest-leverage recovery tool: when a `write_file`
  call returns "file already exists" or "you need to read the file first",
  switch to `edit_file` with the exact existing string instead.
- `session_command` errors with EOFError mean the user typed `/exit` —
  do not retry, the session is over.

## Environment

algo-cli only. These heuristics are encoded in the system prompt
verification_layer; this skill captures the long-form reasoning.
