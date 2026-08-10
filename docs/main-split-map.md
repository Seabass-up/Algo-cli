---
title: main.py Decomposition Map
description: Current architecture status and limited roadmap for decomposing the Algo CLI orchestrator.
tags: [architecture, roadmap, main, modules, algo-cli]
status: architecture-status
updated: 2026-07-24
last_reviewed: 2026-07-24
runtime_version: "Algo CLI v0.18.0"
verification_revision: "215d7cb044fd530809af7c9d0a375e1d3bb792d5"
---

# main.py Decomposition Map

`algo_cli/main.py` is the CLI orchestrator. Split it incrementally; keep `from algo_cli import main` working for tests and scripts.

This page is a current architecture-status record with one optional roadmap item. It is not a promise that the next slice will be implemented. `tests/test_julia_curated_memory_contracts.py` verifies that every module named in the completed-module table still exists.

## Done

| Module | Responsibility |
|--------|----------------|
| `chat_protocol.py` | `get_attr`, `normalize_tool_call`, `serialize_tool_call`, `collapse_tool_history_for_gemini`, `normalize_message` |
| `model_routing.py` | Cloud/local/xAI host routing and model name classifiers |
| `theodore_runtime_services.py` | `create_client`, `client_for_model`, Ollama serve, harness-gateway lifecycle, `apply_tool_runtime_env`, readiness caches |
| `context_budget.py` | Token estimates, `build_system_prompt`, pruning, compaction, `context_status`, `unpack_embed` cache keys, `estimate_usage_with_system_prompt` |
| `dorothy_perf_telemetry.py` | Allowlisted perf JSONL buffer, chat/tool/compaction metrics, embed perf log, `/perf` summary |
| `nathan_runtime.py` | `run_tool`, scoped authority, attempt ledger, reflex augmentation, reflection checkpoint, pipeline tool execution |
| `agent_pipeline.py` | `run_agent_block`, `run_agent_pipeline`, required-change contract, recovery/replan, session pipeline buffer |
| `oliver_slash_dispatch.py` | `SLASH_COMMANDS`, `SlashCommandCompleter`, `handle_command` (delegates to `main` for handlers) |
| `session_commands.py` | Model-invokable `/read`, `/ls`, `/cd`, `/cwd` |
| `oliver_oneshot.py` | `--oneshot --json` NDJSON mode |
| `agent_blocks.py` | Pipeline definitions and TOML loading |

`main.py` re-exports moved symbols so `tests/test_main_helpers.py` and `test_verify.py` need no churn.

## Next slices (recommended order)

1. **Handler extraction** — move `handle_*_command` and auth helpers out of `main.py` so `slash_dispatch` does not lazy-import `main` (optional cleanup).

## Keep in `main.py`

- `main()` REPL loop, `parse_args`, migration/onboarding hooks
- `agent_loop` until `tool_runtime` exists (then thin wrapper only)

## Tests

- Prefer unit tests on new modules (`test_chat_protocol.py`, `test_model_routing.py`) over growing `test_main_helpers.py`.
- After each slice: `ruff check algo_cli tests` and `pytest -q`.
