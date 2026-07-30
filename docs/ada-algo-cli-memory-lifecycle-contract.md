---
title: Ada Algo CLI Memory Lifecycle Contract
description: Durable placement, authority, capture, deduplication, retention, and readiness rules across Algo CLI memory systems.
status: active
updated: 2026-07-24
tags: [algo-cli, product-memory, memory-lifecycle, retrieval-authority, retention]
---

# Algo CLI Memory Lifecycle Contract

This record defines where durable context belongs and how each memory layer is
allowed to influence work. It is a placement and lifecycle contract, not a
product-identity page or a current-state dashboard.

## Placement matrix

| Information | Durable home | Rule |
|---|---|---|
| One stable fact or standing preference needed on most turns | Echo Veil scoped-v2 when protection is required; otherwise `memory.json` through explicit `remember` or bounded completion capture | Keep it atomic and concise. Required mode prohibits a plaintext shadow copy. |
| A behavioral lesson the user explicitly asks to retain | `lessons-learned.md` through `append_lesson` | Capture the lesson, not a transcript. |
| A multi-paragraph product decision or invariant | Curated harness memory | State the contract, rationale, failure behavior, and authoritative modules. |
| A procedure, runbook, dated audit, or current snapshot | Curated wiki | Make its date and status explicit; archive it when superseded. |
| Entity relationships and ranked associations | index-compute-lab | Use for navigation and discovery, not as proof of a live fact. |
| Associative/session recall | Echo Veil when protection is required; Intuition or Echo Veil in optional mode | Treat as a hint layer; it is never canonical by itself. |

Do not copy the same prose across tiers. Promote a small atomic fact to
always-on memory only when its value justifies prompt cost; keep supporting
detail in one RAG document.

## Authority and retrieval

User instructions and verified live files, endpoints, and tool results outrank
all persisted memory. Lessons, harness records, graph results, and optional
recall blocks are navigation evidence. Consequential claims must be checked at
the live source before action.

Retrieval must retain source identity and rank provenance. Incomplete vector
coverage is an availability condition, not a relevance signal: newly added
records must remain discoverable through lexical evidence while embeddings
catch up.

## Capture, cleanup, and retention

- Automatic capture runs only after a normally completed chat or runtime-agent
  turn and examines only the original user text. It requires an explicit durable
  marker, rejects quoted/code/transient/task/secret/PII input, normalizes before
  exact/Jaccard deduplication, and writes at most one entry per turn.
- Daily writes, fingerprint metadata, and total memory characters are capped.
  Rejected text is never persisted; the sidecar contains timestamps and hashes,
  not memory bodies. `/memory-auto status|on|off` exposes the persisted opt-out.
- Explicit `remember` and `append_lesson` requests suppress automatic capture in
  that turn so the same statement is not written twice.
- Batch reconciliation must hold the memory lock, write atomically, and retain a
  pre-change backup.
- Prefer consolidation and explicit historical status over silent deletion.
- Do not enable destructive time-based decay until records carry trustworthy
  timestamps and use/access evidence.
- A dated implementation observation belongs in a wiki or audit, not in this
  durable contract.

## Readiness vocabulary

Memory readiness is multi-dimensional. `installed`, `enabled`, `write_wired`,
`version_supported`, `crypto_initialized`, `index_wired`, `retrieval_wired`,
`persistence_wired`, `restart_restored`, `rotation_ready`, and `healthy` are
separate claims. A package or feature flag alone must never be presented as a
functioning write/recall path. Required protection fails closed unless every
fact needed by the requested operation is true. `healthy` describes the local
scoped-v2 path; it is not a production-enclave or external-review claim.

Authoritative implementation boundaries: `config.py`, `memory_candidates.py`,
`memory_runtime.py`, `context_budget.py`, `harness.py`, and
`ada_memory_echo_veil.py`. When this document and live behavior disagree, verify
the code and update this contract.

The per-entry-point protection classification and current release blockers are
maintained in `echo-veil-security-status.md`. Curated memory, lessons, harness
documents, wiki records, graph data, transcripts, and session history remain
outside Echo unless their implementation and tests are deliberately changed.
