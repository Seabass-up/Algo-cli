---
title: Index Compute Lab Integration
description: Opt-in retrieval and graph-context integration between index-compute-lab and Algo CLI.
tags: [index-compute-lab, graph, retrieval, privacy, algo-cli]
status: active
updated: 2026-08-10
last_reviewed: 2026-08-10
runtime_version: "Algo CLI v0.18.0"
concept: "concept:algo-cli"
---

# Index Compute Lab Integration

**Lab path (default):** `~/index-compute-lab`; override it with `ALGO_CLI_INDEX_COMPUTE_LAB_ROOT`.

> **Evidence boundary:** Graph context is retrieval evidence, not proof. Verify consequential claims against the cited source files or live system before acting.

> **Echo authority boundary:** While Echo Veil is selected as the exclusive mutable-memory authority, Algo does not read, query, reindex, auto-inject, or expose the legacy lab path. `/icl status` reports the inactive boundary and `/icl off` may disable the saved flag; `/icl on|ask|path`, `query_knowledge_graph`, and `reindex_knowledge_graph` refuse before lab file or subprocess access. The harness also purges lab records and cannot rebuild them until Echo is disabled.

## Seamless context (every model / provider)

1. **Auto-inject (opt-in, default OFF)** — After `/icl on`, Algo CLI runs `query.py ask` and prepends a `## Knowledge Graph (index-compute-lab)` block to the turn. This content becomes part of the provider request, including when a cloud model is selected.

2. **Harness index** — `atoms/*.md` reports and notes are indexed dynamically under harness `index-compute-lab` (via `atoms_dir()` in `all_source_roots()`) and participate in vector RAG (`/harness refresh`). Do **not** also list the lab in `~/.algo_cli/harness_roots.json`; that path is for user extras only and previously double-indexed agent notes.

3. **Tool** — `query_knowledge_graph` for explicit follow-up questions.

## Configuration

| Setting | Default |
|---------|---------|
| `Config.index_compute_lab_auto_inject` | `false` |
| `ALGO_CLI_INDEX_COMPUTE_LAB_ROOT` | overrides path |
| `INDEX_COMPUTE_LAB_ROOT` | legacy env alias |

## Commands

- `/icl` — status (root, assets ready, auto-inject on/off)
- `/icl on` / `/icl off` — toggle auto-inject
- `/icl ask <question>` — one-off graph query
- `/icl path` — show resolved root

## First run

If a legacy `index-compute-lab` entry is present in `~/.algo_cli/harness_roots.json`, startup removes it (lab indexing is dynamic). Run `/harness refresh` once to drop any duplicate atom records, then `/harness embed` for vectors.
