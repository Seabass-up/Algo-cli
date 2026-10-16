---
name: smart-error-recovery
description: Recover from structured tool outcomes without replaying uncertain mutations or bypassing prerequisites.
tags: [algo-cli, error-handling, recovery, reflexion]
created: 2026-06-09
runtime_contract: mutation-recovery-v1
---

# Smart Error Recovery

## Trigger

Whenever a tool fails, is denied, times out, or loses its acknowledgement.

## Steps

1. **Inspect the structured outcome and attempt ledger first.** Check status,
   mutation risk, whether execution began, retry_allowed and reconciliation
   requirements. Error text alone does not establish whether an effect landed.
2. **Reconcile unknown_outcome before any retry.** For a possibly executed
   mutation, perform an authorized read-only observation of its exact target.
   Compare with prior state and intended postcondition. If applied, do not
   replay; if definitely not applied, retry only under normal authorization.
   If neither is established, stop and report uncertainty. A timeout is not
   proof of non-execution; a different tool can duplicate the same effect.
3. **Bound safe retries.** A failed read or definitely unexecuted operation may
   be retried once when the runtime permits it and the cause is transient.
   Never increase deadlines past runtime limits or retry denied operations.
4. **Satisfy prerequisites.** For read-before-write errors, read the exact file
   first. Then choose the appropriate edit under the same checks. Do not switch
   tools, modes or authority paths to evade a protection or a denial.
5. **Keep the handoff compact.** Summarize consequential recovery and unresolved
   effects, not every tool call. Verify the final outcome with normal tools.

## Key Discoveries

- For large matches, narrow the search with `glob=` and `path=` arguments.
  Use current tool metadata for limits rather than remembered numeric caps.
- `harness_search` returns "No harness matches" for both "nothing in
  the index" and "all matches were excluded by kind filter". Try
  `kind=None` first to see what's there.
- `run_shell` enforces its own timeout ceiling. Use an authorized bounded
  runner for longer work; do not spawn untracked background side effects.
- For an ambiguous edit match, re-read and choose a unique anchor. A validation
  failure is not evidence of partial execution unless the runtime reports it.
- EOF or connection loss does not prove a mutation failed or a user exited.
  Establish session and operation state before deciding what can be retried.

## Environment

Algo CLI only. Structured runtime outcomes govern recovery; this guidance
does not grant permissions or relax verification requirements.
