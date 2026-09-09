---
title: Provider Authentication Recovery
description: Recovery runbook for invalidated OAuth sessions and provider configuration failures.
tags: [provider, oauth, authentication, recovery, operations]
status: active
updated: 2026-09-07
---

# Provider Authentication Recovery

Treat HTTP 401, invalidated-token, expired-token, and refresh failures as authentication state problems rather than model failures. Algo CLI should attempt one bounded refresh when a refresh credential exists, persist the replacement atomically, and retry once. It must not loop indefinitely or print credentials.

If recovery fails, clear only the invalid access session and direct the operator to the provider-specific setup command. Use `algo-cli config setup <provider>` for guided setup and `algo-cli config auth <provider> login` when a new login is required. Confirm provider readiness before retrying a model request.

For model-not-found errors, distinguish authentication from model routing: resolve aliases to provider model IDs, check that the authenticated account exposes the model, and report the selected provider and resolved model without leaking tokens.

## Codex Responses Stream Recovery

`Codex Responses stream completed without text or a valid tool call` is not an
OAuth error. A provider can finish with only a reasoning summary, or the
connection can end before a usable answer. The adapter also reconciles final
text/refusal and function-call snapshots, which may contain output not present
in the deltas. Do not clear credentials or disable Echo Veil for these failures.
Responses Lite can also send `response.completed.output: []` after complete
function-call item events. Reconcile those snapshots without discarding the
already streamed calls; an empty terminal list alone does not mean empty output.
Match text snapshots to their stable message item IDs as well: a sparse terminal
list can move a message to another position without making it new answer text.

- Retry only the current Responses request, at most twice, after 1 and 2 seconds.
  Retryable cases are empty/reasoning-only completion, premature EOF, interrupted
  stream transport, and explicit `server_error`/`internal_error` events.
  Opening the request, reading the stream, and reconnecting share this one
  budget. Wrapped timeouts/resets, unexpected TLS EOF, and HTTP 408/500/502/503/504
  qualify; certificate verification and other permanent transport errors do not.
- Retry only before any answer text or tool calls have been delivered. Keep the
  same request and previous tool results; never restart the agent or tool batch.
  The interactive and JSON paths announce retries without adding them to model
  conversation history. Completed failed attempts retain their reported usage.
- Buffer function calls until a completion marker; require a nonempty name and
  JSON-object arguments. A truncated, malformed, or explicitly incomplete batch
  must not run even if an earlier function call in the batch looks complete.
- Do not automatically retry partial answer text, authentication failures,
  quota/invalid-request failures, token-limit/content-filter incompletions, or
  cancellation. Preserve the existing partial-response behavior for these cases.
- Exhaustion reports the retry count and retains existing tool results. It is
  not a successful turn and must not trigger completed-turn memory capture.

To diagnose a recurrence, record the model, failure category, retry count, and
whether any answer/tool output was delivered. Do not log OAuth tokens, raw
prompts, tool contents, or encrypted reasoning payloads. A past `.pytest_cache`
failure list is only a hint; rerun the current tests before declaring a regression.

Offline regression gate:

```sh
.venv/bin/pytest -q -o addopts= tests/test_chatgpt_stream_recovery.py tests/test_chatgpt_client.py tests/test_main_helpers.py tests/test_oliver_oneshot.py
```

After installing the repair in the actual launcher environment, run a bounded
authenticated model smoke with required Echo protection and memory auto-capture
disabled. A version/help check alone does not qualify stream recovery.

## Native SQLite Failures Are Separate

A native `SIGBUS` in SQLite's WAL reader is not an empty provider response.
The prior pinned Echo dependency reproduced loss of SQLite writer locks when
permission or profile-health checks opened and closed database/sidecar files.
The replacement exact pin uses metadata inspection through pinned directories
and preserves existing-file locks. This does not prove that every native crash
has that cause or that an operator database is corrupt.

Do not add a provider retry, remove WAL/SHM files, reset keys, or disable Echo to
hide this failure. Verify the installed dependency with
`scripts/henry_echo_veil_dependency_audit.py`, retain the crash receipt, and use
disposable cross-process fixtures before a bounded required-protection model
smoke. Other Echo consumers need their own qualified dependency rollout.
