---
title: Ada Algo CLI Memory Lifecycle Contract
description: Durable placement, authority, capture, deduplication, retention, and readiness rules across Algo CLI memory systems.
status: active
updated: 2026-09-21
tags: [algo-cli, product-memory, memory-lifecycle, retrieval-authority, retention]
---

# Algo CLI Memory Lifecycle Contract

This record defines where durable context belongs and how each memory layer is
allowed to influence work. It is a placement and lifecycle contract, not a
product-identity page or a current-state dashboard.

## Placement matrix

| Information | Durable home | Rule |
|---|---|---|
| One stable fact or standing preference needed on most turns | Native Continuum when explicitly selected; otherwise the standalone local catalog | Keep it atomic and concise. Selected Continuum prohibits a plaintext fallback or shadow copy. |
| A behavioral lesson the user explicitly asks to retain | Continuum through `append_lesson` when selected; otherwise `lessons-learned.md` | Capture the lesson, not a transcript. Continuum selection prohibits a plaintext lesson shadow and disables legacy lesson retrieval/reindexing. |
| A multi-paragraph product decision or invariant | Curated harness memory | State the contract, rationale, failure behavior, and authoritative modules. |
| A procedure, runbook, dated audit, or current snapshot | Curated wiki | Make its date and status explicit; archive it when superseded. |
| Entity relationships and ranked associations | index-compute-lab | Use for navigation and discovery, not as proof of a live fact. |
| Session recall | Bounded Continuum shared/private context when selected; legacy/Intuition sources only in standalone mode | Retain required records, provenance and withheld counts. Verify live facts before relying on them. |

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

- Automatic capture is off by default and requires `/memory-auto on` to record
  the current consent version. A legacy or unknown boolean is not consent. Once
  enabled, capture runs only after a normally completed chat or runtime-agent
  turn and examines only the original user text. It requires an explicit durable
  marker, rejects quoted/code/transient/task/secret/PII input, normalizes before
  exact/Jaccard deduplication, and writes at most one entry per turn.
- Daily writes, fingerprint metadata, and total memory characters are capped.
  Rejected text is never persisted; the sidecar contains timestamps and hashes,
  not memory bodies. `/memory-auto status|on|off` exposes the persisted opt-in.
- Explicit `remember` and `append_lesson` requests suppress automatic capture in
  that turn so the same statement is not written twice. With Continuum selected,
  `append_lesson` updates Algo's revision-bound private fact record;
  it never appends or reindexes `lessons-learned.md`. Runtime-agent
  blocks propagate content-free tool name/status receipts to this gate.
- Continuum verification and status preserve integrity refusals. Initialization,
  conflict resolution, restoration and revocation require explicit native
  operations. A context packet is a signed read snapshot, not action authority.
- Durable attempt records contain bounded status, size, and keyed-digest
  metadata only. Raw memory/tool result prefixes are never persisted there.
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
native service path; it is not a production-enclave or external-review claim.

Authoritative implementation boundaries: `config.py`, `julia_memory_candidates.py`,
`memory_runtime.py`, `context_budget.py`, `harness.py`, and
`continuum_memory.py` and `continuum_tools.py`. When this document and live behavior disagree, verify
the code and update this contract.

The per-entry-point protection classification and current release blockers are
maintained in `continuum-memory.md`. Legacy lesson files, curated
memory, harness documents, wiki records, graph data, transcripts, and session
history remain outside Continuum; while Continuum is selected those legacy lesson files
are excluded from model context and their index is inactive.
