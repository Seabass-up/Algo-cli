# Memory Facts — Algo CLI Rebrand (for ~/.algo_cli/memory.json)

These are atomic, durable facts optimized for the `remember` tool and memory.json format. Keep this always-on set small because every persisted fact is injected into the system prompt.

Copy these directly or feed them one-by-one via the `/remember` command inside Algo CLI.

---

- Algo CLI was rebranded from ollama-cli in version 0.3.0 (June 2026).
- The primary command is `algo-cli`; `ollama-cli` remains a temporary compatibility shim.
- Algo CLI stores configuration and durable state under `~/.algo_cli/`.
- Algo CLI-specific environment variables use the `ALGO_CLI_*` prefix; legacy `OLLAMA_CLI_*` names remain transition compatibility.
- The primary Python package is `algo_cli`; `ollama_cli` remains a compatibility package.
- Harness project records use the `algo-cli` source root, and the canonical knowledge-graph concept is `concept:algo-cli`.

Migration mechanics, release history, and implementation details belong in the indexed rebrand docs rather than always-on memory.

## RAG-only reference: hot-path performance (June 2026)

- Embed responses are unpacked through `tools.unpack_embed_response()` so `/embed` and the `embed_text` tool stay in sync.
- `agent_loop` builds the system prompt once per tool iteration; context compaction uses that same string for token estimates.
- Stale tool-message pruning is O(n) via a tool_call_id → assistant index (not repeated reverse scans).
- `refresh_runtime_status` is not called at the end of `agent_loop` (avoids duplicate model/context work per turn).
- Local model names are listed once per agent turn and passed into harness index embedding setup.
- Context usage cache keys include identity file mtimes so footer estimates track SOUL/USER/lessons edits.
- Harness cold start skips a full source-tree walk when no index file exists yet; embed batches reuse the loaded index watermark.

## RAG-only reference: index-compute-lab integration

- Algo CLI auto-injects index-compute-lab ranked graph context on every user turn when `index_compute_lab_auto_inject` is true (default).
- Lab path resolves from `ALGO_CLI_INDEX_COMPUTE_LAB_ROOT`, `INDEX_COMPUTE_LAB_ROOT`, or `~/index-compute-lab`.
- `/icl on|off` toggles auto-inject; `/icl ask` runs a one-off graph query.
- Harness indexes `index-compute-lab` markdown under `atoms/` for vector RAG alongside the live graph query.

---

**Usage Tip:**
You can paste these one at a time using:
```
/remember Algo CLI was rebranded from ollama-cli in version 0.3.0 (June 2026).
```

Or bulk-import them if you have a script that calls the `remember` tool.
