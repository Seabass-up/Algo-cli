# index-compute-lab ↔ Algo CLI integration

**Lab path (default):** `~/index-compute-lab`; override it with `ALGO_CLI_INDEX_COMPUTE_LAB_ROOT`.

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

**Last updated:** 2026-07
