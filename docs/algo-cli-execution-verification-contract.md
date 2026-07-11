---
title: Algo CLI Execution and Verification Contract
description: Durable rules for runtime entrypoints, policy preflight, mutation ownership, evidence, and failure handling.
status: active
updated: 2026-07-10
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
- Record Git status/diff evidence when a repository is available. If it is not,
  preserve an explicit manual-verification warning instead of inventing proof.
- Keep `worked`, `failed`, `skipped`, and `denied` outcomes distinct in the
  attempt ledger and telemetry.
- Do not cosmetically retry a recent identical failed signature; change the
  hypothesis, input, or tool, or report the blocker.

## Failure and fallback

Approval denial, unsafe input, unknown action, missing verification evidence,
and incompatible runtime state are explicit outcomes. Fallbacks must remain
bounded and observable; swallowed exceptions and silent policy downgrades are
not valid recovery.

Authoritative implementation boundaries: `tool_runtime.py`, `tool_policy.py`,
`agent_pipeline.py`, `agent_threads.py`, `runtime_services.py`, and
`session_commands.py`. Focused tests prove behavior; registry declarations only
describe the available contract.

