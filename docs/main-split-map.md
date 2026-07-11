# main.py decomposition map

`algo_cli/main.py` is the CLI orchestrator. Split it incrementally; keep `from algo_cli import main` working for tests and scripts.

## Done

| Module | Responsibility |
|--------|----------------|
| `chat_protocol.py` | `get_attr`, `normalize_tool_call`, `serialize_tool_call`, `collapse_tool_history_for_gemini`, `normalize_message` |
| `model_routing.py` | Cloud/local/xAI host routing and model name classifiers |
| `runtime_services.py` | `create_client`, `client_for_model`, Ollama serve, harness-gateway lifecycle, `apply_tool_runtime_env`, readiness caches |
| `context_budget.py` | Token estimates, `build_system_prompt`, pruning, compaction, `context_status`, `unpack_embed` cache keys, `estimate_usage_with_system_prompt` |
| `perf_telemetry.py` | Perf JSONL buffer, chat/tool/compaction metrics, embed perf log, `/perf` summary |
| `tool_runtime.py` | `run_tool`, approval, attempt ledger, reflex augmentation, reflection checkpoint, pipeline tool execution |
| `agent_pipeline.py` | `run_agent_block`, `run_agent_pipeline`, required-change contract, recovery/replan, session pipeline buffer |
| `slash_dispatch.py` | `SLASH_COMMANDS`, `SlashCommandCompleter`, `handle_command` (delegates to `main` for handlers) |
| `session_commands.py` | Model-invokable `/read`, `/ls`, `/cd`, `/cwd` |
| `oneshot.py` | `--oneshot --json` NDJSON mode |
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