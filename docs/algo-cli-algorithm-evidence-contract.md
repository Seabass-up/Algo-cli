---
title: Algo CLI Algorithm Evidence Contract
description: Durable distinction between catalog, kernel readiness, runtime use, and measured algorithm effectiveness.
status: active
updated: 2026-07-10
tags: [algo-cli, product-memory, algorithm-evidence, kernel-readiness, effectiveness]
---

# Algo CLI Algorithm Evidence Contract

Algo CLI records ideas, contracts, runtime capabilities, and empirical evidence
at different layers. Those layers must not be collapsed into one “active” claim.

## What each layer proves

| Layer | It proves | It does not prove |
|---|---|---|
| `ALGO.md` catalog | An algorithm or pattern has a reviewed contract and intended use. | That normal runtime traffic executes it. |
| Kernel manifest and `/kernel check` | Modules import, slash roots resolve, metadata is valid, and active action declarations have risk contracts. | Workload execution or benefit. |
| Action registry | A callable/command has coverage, risk, provider, and approval metadata. | Algorithmic correctness or effectiveness. |
| Tests and runtime evidence | The real boundary behaves as specified for measured inputs and fallbacks. | General benefit beyond the evaluated corpus. |

Use precise states: `planned`, `preview`, `contract-ready`, `observed`,
`effective`, `limited`, or `regressing`. Contract readiness is valuable, but it
must never be rendered as empirical effectiveness.

## Evidence rule

An effectiveness claim requires all of the following:

1. The actual production boundary is exercised, not a duplicate test-only
   implementation.
2. Deterministic positive, negative, and fallback cases pass.
3. The result exposes enough provenance or telemetry to explain the decision.
4. A pinned baseline or parity oracle is used when improvement is claimed.
5. Missing prerequisites and runtime errors are reported as `unavailable`,
   `error`, or `fail`; they never become a fabricated pass.

Critical safety bypasses are vetoes. Adding many trivial declarations cannot
average away one failed required check.

## Bounded live evidence

The local algorithm-effectiveness probe uses the persisted canonical catalog
embedding as a deterministic query vector and makes no model or network call.
It exercises the production hybrid retrieval path twice and checks lexical
provenance and corpus-cache reuse, exact-vector and normalized-matrix reuse,
fusion mode against embedding coverage, stable top-k parity above its measured
crossover, bounded TinyLFU reuse, and value-aware embedding-tier arithmetic.
It also runs the production durable-memory admission path in an isolated
temporary state and requires one safe write, duplicate/secret rejection, and
metadata-only persistence. The probe passes only when every required check
passes.

Do not preserve transient benchmark numbers here. Measurements belong in probe
output or dated audits; this document preserves the evidence contract.

Authoritative implementation boundaries: `evals/algorithm_effectiveness.py`,
`kernels/manifest.py`, `action_registry.py`, `perf_telemetry.py`, and
`docs/ALGO.md`.
