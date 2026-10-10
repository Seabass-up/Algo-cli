# Static Review Follow-up

Date: 2026-10-08. Local, uncommitted fixes in the actual editable v0.20.2
launcher checkout; the separate review workspace was not modified or merged.

## Coordinator Refusal

The pasted report's sentence does not identify a provider outage. The known
`online_coordinator_requires_remote_permission` path occurs when an online Jev
coordinator receives `local_only=true`, including the tool's deliberate default.
It makes no remote inference or external connectivity probe. The result now
reports `failure_kind=request_policy`, `provider_health=not_tested` and explicit
recovery guidance. Automatic checkpoints retain that distinction for the main AI.
No restriction is relaxed, default changed, or denied request retried remotely.

The current saved configuration enables automatic Jev coordination, and normal
preflight admits an explicitly online decision request under its current grant.
A fresh public synthetic Kimi K3 review received two validated automatic Jev
checkpoints: task 1,217.561 ms, evidence 1,136.237 ms, whole loop 9,732.768 ms.
It did not ask the main model to call Jev and left the fixture unchanged.
The original session's exact cause is unresolved without its tool/checkpoint
error; it could have different arguments, grants or already-loaded source.

### Repeated Policy Refusal: Root Inference Repair

The later runtime output confirmed the policy-refusal branch. A reproduction
then found the actual detector defect: a regex matched `local-only`, `offline`
or upload-prohibition words anywhere in the task, including a pasted report
about an earlier request. Updating the diagnostic alone had not fixed that logic.

Automatic locality now comes from the configured privacy setting or explicit
task directives, not mere topic mentions or quoted/fenced examples. Existing
egress grants remain required: `local_only=false` is not an authorization token.
Checkpoint state records the locality basis so an unintended restriction can be
diagnosed without guessing provider health. Online Jev and confirmed-offline
Clef-flash/Tev1 routing were not replaced or relaxed.

Twenty-six new regression cases cover policy discussion, quoted logs/examples,
explicit prohibitions, task modifiers and configured-local precedence. The
expanded focused run passed 341 tests; Ruff, scoped mypy and whitespace checks
passed. The live public review deliberately mentioned the prior local-only
failure and still received successful automatic Jev task/evidence checkpoints
with `normal_runtime_authority`: 1,215.542/1,132.292 ms, 11,694.859 ms whole loop.
The fixture remained unchanged. Final Python: 7,280 passed, 41 skipped, the same
12 failures in 132.521 seconds. This fixes the demonstrated false restriction;
it is not a guarantee that providers never fail or a live physical-offline test.

## Findings

- TeamExecutor timeout and post-return mutation were reproduced. It now uses one
  monotonic deadline, closes publication under a per-invocation lock, records
  unfinished members, uses daemon workers and returns on a race winner or total
  failure. Empty races also no longer wait for the default timeout. Timed-out
  callbacks are not forcibly cancelled and can still perform side effects;
  side-effecting users need cooperative cancellation or process containment.
- TokenBucket impossible requests were confirmed. Negative requests also created
  extra tokens. Finite positive capacity/refill and valid request/timeout checks,
  locked refill/debit, and a monotonic bounded wait repair these cases.
- FlowExecutor Python execution is unsandboxed, but source search found exports
  rather than a live model-callable execution path. This remains a trust-boundary
  limitation, not a confirmed remote-execution vulnerability. Trusted-local-only
  requirements are documented. Another reproduced logic error is repaired:
  failed/unsupported nodes no longer report success, even when evaluations pass.
- Six mojibake occurrences in the current harness source were replaced at their
  origin. The separate report referenced an older/different source snapshot.
- Kernel checks execute import initializers. Help and audit documentation now
  state that explicitly; import behavior is not a sandbox or authorization gate.
  No import-free custom-module audit or Python-flow sandbox was implemented.

## Verification And Limits

29 initial execution-boundary tests passed; the expanded focused review,
coordinator, kernel, command and harness run passed 294 tests. Repository Ruff,
whitespace checks and scoped five-file mypy passed. The final complete Python
run records 7,254 passed, 41 skipped and 12 failed in 129.257 seconds.

The remaining failures are native Continuum 15/16-operation drift, stale
M8/Nathan receipts, slash/scorecard expectations and seven logo tests. These
still block release qualification. No hosted CI, commit, push, tag, permission
change, private catalog publication or normal model switch occurred.

Jev 1.13 assisted the bounded team/rate repair choice in 362.422 ms; its advice
was verified with source and executable tests, not accepted as action authority.
The regression tests are `tests/test_intelligence_execution_bounds.py`,
`tests/test_decision_coordinator.py` and `tests/test_decision_checkpoints.py`.
Local receipts are retained under `/tmp/algo-continuum-cleanup.VJ38qP/`.
