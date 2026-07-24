---
title: Algo CLI Execution and Verification Contract
description: Durable rules for runtime entrypoints, policy preflight, mutation ownership, evidence, and failure handling.
status: active
updated: 2026-07-23
tags: [algo-cli, product-memory, execution-verification, tool-policy, mutation-owner]
---

# Algo CLI Execution and Verification Contract

This record describes the runtime invariants that must survive UI, model, and
pipeline changes. It explains ownership and evidence; procedural tool-selection
skills may provide narrower how-to guidance.

## Execution surfaces

- Normal model-callable tools perform work and return structured results.
- `session_slash` owns deterministic working-directory navigation commands.
- `session_command` owns non-file session controls and registered slash routes.

Every model-invoked tool path must pass through the shared runtime preflight
before execution. The preflight resolves runtime arguments, capability tier,
policy-chain verdict, QoS metadata, and approval requirements. Unknown or
disallowed actions fail closed; a parallel path may not bypass the same gate
used by a serial path.

## Immutable Agent Run Contract

Every user-facing Agent pipeline compiles a runtime-owned contract before the
first model call. Its canonical digest binds:

- the task, route, pipeline shape, block prompts, models, and effort;
- the live approval mode, safe-mode state, and session preapproval state;
- configured, admitted, denied, and approval-required tools per block;
- read-only versus workspace-mutation scope;
- initial workspace identity;
- per-round and total prompt, model-round, tool-call, block, parallelism, and
  wall-time ceilings;
- required verifiers and the only permitted typed recovery categories.

The model cannot widen this envelope. Approval-mode drift, prompt drift, model
fallback, tool-set drift, mutation-shape drift, resource exhaustion, and
workspace drift stop execution. `never`, `interactive`, and `auto` retain
distinct semantics: read-only tools remain usable without prompts in every
mode, `never` never gains session preapproval, and `auto` does not silently
rewrite another mode's authority.

## Durable Agent checkpoints

Agent runs use a private append-only, hash-chained journal. The runtime writes
content-free intent before dispatch and records outcome, verifier, and
workspace evidence afterward. Raw prompts, arguments, tool-call identifiers,
targets, context bodies, and model output are represented by bounded digests
rather than copied into the journal.

The journal validates both bytes and execution grammar. A valid hash is not
enough: model rounds must balance their declared tool batch, every result must
match one intent, recovery must be contract-bound, and a verified block must
carry its passed output verifier plus post-mutation evidence when required.
A rehashed but semantically impossible event sequence is corrupt.

Resume reconstructs counters and the last contiguous verified block boundary.
It rejects approval, task, prompt, pipeline, context, workspace, or contract
drift. An unresolved or unverified mutation blocks automatic replay and
requires reconciliation.

## Mutation ownership

A multi-agent team has one write owner. Specialists receive bounded read-only
contexts and return evidence; the integration pipeline alone may edit the
workspace. Child work does not recursively delegate or create competing
writers. The integration owner remains subject to the same policy, approval,
required-change, and verification rules as an ordinary run.

## Evidence for completed work

- Use file mutation tools for edits; use shell execution for tests, lint,
  type-checking, builds, status, and diff evidence.
- A mutation is not complete merely because a tool returned text that sounds
  successful. Verify the changed artifact and the smallest relevant behavior.
- Record Git status/diff evidence when a repository is available. If required
  evidence is unavailable, the result is partial or failed; it is not complete
  with a manual-verification warning.
- Keep `worked`, `failed`, `skipped`, and `denied` outcomes distinct in the
  attempt ledger and telemetry.
- Do not cosmetically retry a recent identical failed signature; change the
  hypothesis, input, or tool, or report the blocker.

## Failure and fallback

Approval denial, unsafe input, unknown action, missing verification evidence,
and incompatible runtime state are explicit outcomes. Fallbacks must remain
bounded and observable; swallowed exceptions and silent policy downgrades are
not valid recovery.

Recovery is typed and single-attempt. Only a contract-listed failure code may
open a reduced-budget plan/retry cycle, and high-risk mutations do not recover
automatically. Provider fallback never replays an uncertain mutation.

## Context and provider protocol

The Agent context broker prioritizes verified handoffs, governed memory,
user-provided resume direction, and heuristic memory under one explicit token
budget. Every source carries a provenance/trust label and is evidence only; no
context source can expand scope, tools, approvals, or policy. The journal stores
the fitted-context digest and inclusion/truncation/omission receipt, not its
body.

One provider-neutral state machine requires exactly one result per tool call,
rejects duplicate or orphan results, and prevents a new model round until the
previous tool batch balances. Provider-specific streaming remains an adapter
around that invariant.

## Qualification

The model-free `nathan-agent-runtime-hardening-v1` workload exercises approval
separation, read-only containment, authority and workspace drift, prompt/token
binding, context provenance, provider/tool balancing, journal tampering,
semantic checkpoint forgery, uncertain mutation, verified resume, output
verification, and multi-signal risk routing. It also reports p50/p95 contract,
context, and durable checkpoint/resume latency.

Run and validate it with:

```bash
.venv/bin/python scripts/nathan_agent_runtime_qualification.py --refresh
.venv/bin/python scripts/nathan_agent_runtime_qualification.py
```

This is deterministic local runtime evidence, not model-quality, production
power-loss, or cross-harness superiority evidence. The active hardening freeze
still blocks tagging, releases, publication, and public benchmark claims.

Authoritative implementation boundaries: `run_contract.py`,
`agent_run_journal.py`, `agent_context.py`, `agent_pipeline.py`,
`agent_threads.py`, `nathan_runtime.py`, `samuel_policy.py`,
`task_router.py`, `theodore_runtime_services.py`, and `session_commands.py`.
Focused adversarial tests prove behavior; registry declarations only describe
the available contract.
