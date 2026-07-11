# Algo CLI — Hot-path performance fixes (June 2026)

**Purpose:** Harness RAG, wiki, and memory reference for agent_loop / context / embed behavior after the anti-pattern review.

## Summary

Eight targeted fixes reduce duplicate work, improve context-estimate accuracy, and speed cold harness indexing. No user-facing command changes.

## Changes

| Area | Fix |
|------|-----|
| Embed API | `tools.unpack_embed_response()` — single unpack for `/embed` and `embed_text` via `get_attr` |
| Agent loop | One `build_system_prompt()` per iteration; compaction uses `precomputed_used`; rebuild only if compaction runs |
| Pruning | `prune_stale_tool_messages` — O(n) `call_id_to_assistant` map (was O(n²) reverse scans) |
| Runtime status | Removed duplicate `refresh_runtime_status` at end of `agent_loop` (REPL still refreshes) |
| Models | `local_model_names` resolved once per turn for harness embed setup |
| Context cache | Key includes `identity.identity_mtime_key()` and session mode |
| Harness watermark | No full filesystem walk when index file missing; embed batches use indexed watermark; skip missing record paths on stat failure |

## Modules

- `algo_cli/context_budget.py` — pruning, compaction, cache, `estimate_usage_with_system_prompt`
- `algo_cli/tools.py` — `unpack_embed_response`
- `algo_cli/harness.py` — `_source_watermark_ns`
- `algo_cli/main.py` — `agent_loop` ordering

## Verification

```powershell
python -m ruff check algo_cli tests
python -m pytest -q
```

## Search terms

`hot path`, `build_system_prompt`, `unpack_embed`, `prune_stale_tool_messages`, `context usage cache`, `harness watermark`

**Last updated:** 2026-06