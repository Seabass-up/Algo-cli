# CLAUDE.md — Algo CLI Codebase Guide

## Project Overview

`algo-cli` is a local-first agentic terminal assistant (Python package `algo_cli`) that supports local Ollama models, Ollama Cloud, and xAI Grok. It features structured tool use, reflection checkpoints, adaptive context compression, a harness RAG bridge over local agent ecosystems, skill crystallization, and an identity layer.

**Package:** `algo-cli` v0.14.0 | **Python:** 3.10+ | **Entry point:** `algo_cli.main:main`

---

## Deferred Work

- **Phase 3 — `AgentBlock` metadata refactor.** The class is accumulating specialized fields (`status_reason`, `verification_warning`, `audit_evidence`, `mutation_actions`, `git_evidence`, etc.). Introduce a `BlockResult` / metadata structure and standardize naming across these fields. Cross-cutting; do as its own contained slice after current enforcement/audit work stabilizes.

## Future Work (Next Cycle Priorities)

Ordered by leverage, smallest viable slice first. Each item should be its own contained slice; do not stack.

1. **~~One-shot non-interactive JSON mode (`--oneshot --json`)~~ — SHIPPED.** `algo-cli --oneshot --json [--approval-mode never|auto] [PROMPT]` emits one JSON event per line to stdout (NDJSON), framed by `session_start` / `done`. Event types: `session_start`, `thinking`, `content`, `tool_call`, `tool_result`, `tool_denied`, `error`, `done`. `--approval-mode never` (default) denies protected tools; `--approval-mode auto` preapproves only session-preapproval actions and cannot bypass action-time confirmation or handoff. Skill crystallization is disabled in oneshot. Implementation is in `algo_cli/oliver_oneshot.py`, with scoped grants enforced by `algo_cli/nathan_runtime.py`.

2. **Embedding model / hardware fit (local infrastructure shipped; cloud blocked by platform).** `make_embed_fn`, model-aware index accounting, and backend telemetry are implemented. Ollama Cloud authentication was verified for model listing and web search, but its catalog exposes no embedding models and direct `embed()` requests fail; embedding routing therefore remains local-only with a visible fallback from a configured cloud backend. Local `all-minilm:latest` is now the default and was verified at 125.6 ms/record. Earlier benchmark results:
   - `nomic-embed-text-v2-moe:latest` (retired default): single-record 1,475 ms; batch 12,138 ms/rec. MoE batches catastrophically on CPU.
   - `qwen3-embedding:0.6b`: OOM (needs 5.3 GiB, available 5.2 GiB).
   - `embeddinggemma:latest`: single-record 1,017 ms; batch 8,074 ms/rec.

   The usable route today is the verified lightweight local model; cloud embedding can be revisited only when Ollama publishes supported embedding models.
3. **~~Replan-on-failure for `requires_change` blocks~~ — SHIPPED.** Recoverable implementation partials (`max_iterations`, `no_write_evidence`, `write_blocked`, `no_verified_delta`) receive exactly one tool-free `recovery-plan` block and one focused `implement-retry` capped at eight iterations. Policy denial, unsafe attribution, model errors, and cancellation do not retry. Original and retry outputs/evidence remain visible to review/final blocks.
4. **~~Interactive evidence commands (`/diff`, `/changes`)~~ — SHIPPED.** `/diff` shows the most recent verified Git diff from a `requires_change` block; `/changes` summarizes per-block activity from the most recent pipeline run. Both read the session-scoped `_session_pipeline_blocks` buffer in `main.py`. Cleared on `/clear`, overwritten on each pipeline run, never persisted.
5. **~~Intra-session message pruning~~ — SHIPPED.** `prune_stale_tool_messages(cfg)` runs every agent_loop iteration alongside `maybe_compact_context`. Strict FIFO over `role == "tool"` messages only, gated on `cfg.prune_after_messages` (default 80) with `cfg.prune_keep_recent` (default 40) protected. Strips orphaned `tool_calls` from originating assistant messages. Emits a `prune` perf event only when at least one message was removed.
6. **Parallel read-only sub-agents.** Build on the existing isolated AgentBlock execution model. Synthesis layer is the harder part; parallelism itself is straightforward.
7. **Code-native RAG depth.** Tree-sitter pass over `cfg.cwd` indexed alongside Harness — symbol- and AST-aware retrieval without a full LSP integration.
8. **Larger TUI / UX overhaul.** Defer until interaction patterns from 3–6 have settled; do not polish the linear Rich flow before the planning layer matures.
9. **`AgentBlock` metadata refactor** (see Deferred Work above) — pair with the long-term contracts module extraction (policy + evidence + contract rules into `agent_contracts.py`).

---

## Repository Layout

```
algo-cli/
├── algo_cli/           # Main Python package
│   ├── main.py           # CLI loop, slash-command dispatch, agent loop
│   ├── config.py         # Config dataclass + persistent state
│   ├── tools.py          # Model-callable tool definitions (ALL_TOOLS, TOOL_MAP)
│   ├── harness.py        # Read-only bridge into local agent harness assets + RAG
│   ├── identity.py       # Identity layer (SOUL/IDENTITY/USER/lessons) + lessons RAG
│   ├── skills.py         # Skill crystallization (run recording + crystallizer pass)
│   ├── display.py        # Rich terminal display + theme engine
│   ├── model_info.py     # Ollama model metadata cache + markdown harness records
│   ├── verify.py         # Tier-3 claim-grounding hallucination verifier
│   ├── xai_auth.py       # xAI OAuth 2.0 + PKCE flow (SuperGrok subscription)
│   ├── xai_client.py     # xAI chat adapter (api.x.ai/v1 → ollama-shaped chunks)
│   ├── x_account.py      # Separate X account actions through official xurl CLI
│   └── __init__.py
├── tests/                # pytest test suite (offline, no Ollama needed)
│   ├── conftest.py       # Shared fixtures; sets ALGO_CLI_CONFIG_DIR before imports
│   ├── test_config.py
│   ├── test_harness.py
│   ├── test_identity.py
│   ├── test_skills.py
│   ├── test_tools.py
│   └── test_main_helpers.py
├── harness-indexer/      # Optional Rust cold-start indexer (cargo)
│   └── src/main.rs
├── harness-gateway/      # Optional Go localhost gateway for bridge integrations
│   └── main.go
├── algo-cli.ps1        # Windows PowerShell launcher
├── algo-cli.cmd        # Windows CMD launcher
├── pyproject.toml        # Build system (hatchling), deps, ruff + pytest config
├── AGENTS.md             # Repository guidelines
└── .github/workflows/oliver-ci.yml
```

---

## Development Commands

Install with dev tools (run from repo root):

```bash
python -m pip install -e ".[dev]"
```

**The full CI check suite (run these before committing):**

```bash
python -m ruff check algo_cli tests   # lint: pyflakes + syntax errors only
python -m compileall -q algo_cli      # compile check
python -m pytest                        # offline test suite
```

**Optional native components:**

```bash
# Rust indexer
cd harness-indexer
cargo fmt --check
cargo clippy --release -- -D warnings
cargo build --release
cargo test --release

# Go gateway
cd harness-gateway
gofmt -l .
go vet ./...
go build ./...
go test ./...
```

**Run the CLI:**

```bash
algo-cli                                  # local Ollama
algo-cli --cloud --model qwen3            # Ollama Cloud
```

---

## CI Pipeline (`.github/workflows/oliver-ci.yml`)

Three parallel jobs run on every push and PR to `main`:

| Job | Matrix | Checks |
|-----|--------|--------|
| `python` | Ubuntu + Windows × py3.10 + py3.12 | ruff lint, compileall, pytest |
| `rust` | ubuntu-latest | fmt, clippy (-D warnings), build, test |
| `go` | ubuntu-latest | gofmt, vet, build, test |

Superseded runs on the same ref are cancelled (`concurrency.cancel-in-progress: true`).

---

## Module Responsibilities

### `config.py` — Config and Persistent State

- `Config` dataclass holds all runtime state: model, theme, auto/safe modes, context window, temperature, messages, memories, context compression state, etc.
- Config is persisted to `~/.algo_cli/config.json` (with legacy migration support from `~/.ollama_cli`); memories to `~/.algo_cli/memory.json`.
- Key paths: `CONFIG_DIR`, `MEMORY_FILE`, `MEMORY_CANDIDATE_STATE_FILE`, `HISTORY_DIR`, `CONTEXT_ARCHIVE_DIR`, `PROMPT_HISTORY_FILE`.
- Override config dir: `ALGO_CLI_CONFIG_DIR` env var (legacy `OLLAMA_CLI_CONFIG_DIR` still supported during transition).
- `load_runtime_env()` loads `~/.algo_cli/env` (or `ALGO_CLI_ENV_FILE`) before setting env vars — supports `export KEY=VALUE` and bare `KEY=VALUE` syntax. Legacy `OLLAMA_CLI_*` files/vars are still read for compatibility.

### `tools.py` — Model-Callable Tools

All tools are in `ALL_TOOLS` (list) and `TOOL_MAP` (name → fn dict). Each function has a docstring Ollama uses to build tool schemas.

| Tool | Notes |
|------|-------|
| `read_file` | Reads text, capped at 50k chars |
| `read_pdf` | PyMuPDF → PyPDF2 fallback; reports image-only PDFs |
| `render_pdf_pages` | Renders pages to PNG in temp dir (PyMuPDF required) |
| `write_file` | Requires `overwrite=True` for existing files; needs approval |
| `list_directory` | Sorted; dirs first; 200-entry limit |
| `search_files` | `rg` if available, else Python fallback (5k-file limit, 2 MB/file limit) |
| `run_shell` | `safe_mode` blocks destructive patterns via `DENY_COMMAND_RE`; needs approval |
| `web_search` / `web_fetch` | Requires Ollama Cloud + `OLLAMA_API_KEY` |
| `x_account_*` | Separate X API account lane through `xurl`; writes require explicit confirmation |
| `remember` | Atomically persists an explicit durable fact through `Config.remember_fact()` |
| `append_lesson` | Non-destructive append to `lessons-learned.md` |
| `update_user_profile` | Overwrites `USER.md`; requires approval |
| `embed_text` | Tries Go gateway first, falls back to direct Ollama |
| `vision_describe` | Passes image to a local vision model via Ollama |
| `available_actions` | Returns JSON of all commands, tool groups, harness stats |
| `harness_refresh/stats/search/read` | Delegate to `harness.py` |

**Safe-mode deny list** (`DENY_COMMAND_RE`): `rm`, `del`, `erase`, `rd`, `rmdir`, `format`, `diskpart`, `shutdown`, `restart-computer`, `stop-computer`, `git reset`, `git checkout`, `Remove-Item`.

### `harness.py` — Harness RAG Bridge

Read-only bridge into local agent ecosystem assets. Never executes external agent tools.

**Indexed harness roots (`SOURCE_ROOTS`):**
- `algo-cli`: crystallized skills in `~/.algo_cli/skills/`
- `codex`: skills, scripts, memories, extensions in `~/.codex/`
- `claude`: skills, extensions in `~/.claude/`
- `openclaw`: skills, plugin-skills, prompts (SOUL/IDENTITY/USER/lessons), wiki, memory in `~/.openclaw/`
- `agents`: shared `.agents` skills in `~/.agents/`
- `mercury`: skills, soul prompts, harness workflows in `~/.mercury/`
- `cli-agent`: skills in `~/.cli-agent/`
- `pi`: prompts and package metadata in `~/pi-mono/`

**Index file:** `~/.algo_cli/harness_index.json` (legacy data from `~/.ollama_cli` is migrated on first use)

**Indexing strategy:** On cold start (no index file), tries the Rust binary first, falls back to Python. On warm refresh (index exists), always uses Python incremental scanner (mtime/size reuse skips unchanged files).

**RAG layer:** Each record gets an optional `embedding` field. At turn start, the user message is embedded locally; top-3 cosine-ranked records are injected as `## Relevant Context` in the system prompt. Keyword search backs `/hsearch` and `harness_search`.

**Secret exclusion:** `SECRET_RE` skips paths containing `secret`, `token`, `credential`, `auth`, `password`, `key`, `.env`. Email folders, `sessions/`, `.tmp/`, `logs/` are also excluded.

**Harness alias:** `openclaude` expands to `{claude, openclaw}`.

### `identity.py` — Identity Layer

Four files in `~/.algo_cli/identity/` are mtime-cached and prepended to the system prompt every turn:

| File | Purpose | Editable by |
|------|---------|-------------|
| `SOUL.md` | CLI voice and operating values | User only |
| `IDENTITY.md` | CLI persona declaration | User only |
| `USER.md` | User profile | User + `update_user_profile` tool |
| `lessons-learned.md` | Accumulated lessons | User + `append_lesson` tool |

Lessons RAG: chunks lessons on `## ` headings, embeds with local model, injects top-5 cosine-nearest chunks based on the user message. Falls back to full inline when embeddings unavailable.

### `memory_candidates.py` / `julia_memory_runtime.py` — Automatic Memory Admission

After a normal chat completion or a completed `/agent` pipeline, the runtime evaluates only the original user-authored text. Explicit durable markers pass through privacy, task/transience, code/quotation, length, exact/Jaccard duplicate, daily-write, fingerprint, and total-character gates. At most one entry is written per turn. Partial streams, exhausted tool loops, failed agents, and turns with a successful explicit `remember`/`append_lesson` tool call are skipped. The sidecar stores UTC days and SHA-256 fingerprints only; telemetry contains aggregate counts only. Use `/memory-auto status|on|off` to inspect or change the persisted setting.

### `skills.py` — Skill Crystallization

After every `skill_crystallize_every` (default 3) substantive agent runs:
1. Recent runs are loaded from `~/.algo_cli/run_history.jsonl` (capped at 60).
2. A crystallizer LLM call reviews runs and extracts reusable skill candidates (paths, configs, command sequences, workarounds).
3. New `SKILL.md` files are written to `~/.algo_cli/skills/` — never overwriting existing ones.
4. Skills are picked up by the harness indexer and retrieved into future context.

A run is "substantive" if it used more than 2 tool calls.

### `display.py` — Rich Terminal Display

- Themes: `tokyo-night` (default), `catppuccin-mocha`, `dracula`, `nord`, `gruvbox`, `dolphie`.
- All display goes through the singleton `console = Console(theme=...)`.
- Theme switching uses `console.push_theme()` / `console.pop_theme()`.
- Streaming responses use `rich.live.Live` with `Markdown` rendering at 12 fps.
- Footer toolbar is rebuilt by `build_prompt_style(palette)` in `main.py` on each prompt cycle.

### `xai_auth.py` — xAI OAuth 2.0 + PKCE

Optional OAuth Authorization Code + PKCE (S256) flow for the xAI subscription path. API-key fallback is intentionally disabled; auth is browser-driven against `https://auth.x.ai`. Tokens persist to `CONFIG_DIR/xai_auth.json` with POSIX 0600 permissions. Algo CLI does not bundle a client identity; the user must provide an OAuth client ID they are authorized to use through `XAI_CLIENT_ID`.

| Constant | Value |
|---|---|
| `XAI_CLIENT_ID` | Required user-provided client ID; no bundled default |
| `XAI_AUTHORIZE_URL` | `https://auth.x.ai/oauth2/authorize` |
| `XAI_TOKEN_URL` | `https://auth.x.ai/oauth2/token` |
| `XAI_REDIRECT_URI` | `http://127.0.0.1:56121/callback` |
| `XAI_DEFAULT_SCOPE` | `openid offline_access api:access` |
| `XAI_API_BASE` | `https://api.x.ai/v1` |

Public API: `begin_login(no_browser=…)`, `run_loopback_capture()`, `complete_login(verifier, state, callback)`, `get_valid_token()` (silent refresh inside the 60 s expiry window), `auth_status()`, `clear_tokens()`, `port_is_free()`.

Slash commands (in `main.py`): `/xai-login [--no-browser]`, `/xai-logout`, `/xai-status`. Headless usage: `/xai-login --no-browser` prints the auth URL and an `ssh -N -L 56121:127.0.0.1:56121 …` tunnel hint so the loopback callback can be forwarded from a remote host.

The OAuth client ID is resolved from the runtime environment for authorization, code exchange, and refresh. Missing configuration fails before a browser is opened. Algo CLI does not reuse a third-party client ID, and client identifiers are redacted from reported OAuth errors. Tokens are never logged or printed and are excluded from `Config` serialization.

### `xai_client.py` — xAI chat adapter

`XaiClient.chat(...)` mirrors the `ollama.Client.chat()` signature. Standard Grok chat routes to `https://api.x.ai/v1/chat/completions` with OAuth bearer auth only; `grok-*-multi-agent*` routes to `https://api.x.ai/v1/responses` because xAI does not support multi-agent on Chat Completions. Streams OpenAI-style Server-Sent Events for Chat Completions and re-emits ollama-shaped chunks (`{"message": {"content": ..., "tool_calls": [...], "thinking": ...}}`), so `agent_loop` runs unchanged.

Key behaviors:
- **Tool-call delta accumulation:** OpenAI streams `tool_calls[i].function.arguments` as partial JSON strings across many chunks. The adapter buffers by `index` and emits one complete `tool_calls` chunk at `finish_reason="tool_calls"` to match agent_loop's "tool_calls in a chunk are complete" assumption.
- **Schema conversion:** reuses `ollama._utils.convert_function_to_tool` to convert our Python tool callables to OpenAI tool schemas. (Private API; pinned to current ollama dep.)
- **Message translation:** ollama tool-result messages identify tools by `name`; OpenAI uses `tool_call_id`. The adapter tracks ids per assistant turn and re-links them.
- **`reasoning_content` → `thinking`:** Grok's reasoning trace surfaces in the same field as Ollama thinking.
- **Multi-agent Responses routing:** multi-agent requests use `/v1/responses` and omit local client-side tools, which xAI does not currently support for that model. Prior local tool-call history is folded into text context.
- **Fail-closed OAuth policy:** `XAI_API_KEY` is not read by this provider. Billing, credits, API-key-required, quota, 402, or 403 responses stop the request instead of falling back to pay-per-token API-key usage.
- **`search()` helper:** convenience method for `x_search` tool — POSTs to `/v1/responses` with `tools: [{type: x_search}]` and returns `{content, citations}`.

`create_client(cfg)` in `main.py` returns an `XaiClient` when `is_xai_model(cfg.model)` is true; otherwise it returns an Ollama `Client` as before. The agent loop also skips `ensure_model_info()` for Grok (no `client.show()` on xAI) and uses `synthesize_xai_info()` to produce a minimal info dict.

### `x_search` tool

`x_search(query, max_results=10)` in `tools.py`. Calls `XaiClient.search()`, formats the result, and writes a markdown record to `CONFIG_DIR/x_search_cache/{safe_query}_{ts}.md`. The cache dir is registered as a SourceRoot in `harness.py`, so search results are indexed, embedded, and RAG-retrieved in future turns. Requires `/xai-login` first.

### `x_account.py` — X account lane

X account operations are intentionally separate from xAI Grok OAuth. `x_account.py` shells out to the official `xurl` CLI, which owns X API OAuth tokens under its own config. The CLI never reads or prints `~/.xurl`; `/x-account status` runs `xurl auth status`.

Slash commands: `/x-account status`, `/x-account draft-post`, `/x-account draft-reply`, `/x-account post --confirm`, `/x-account reply --confirm`, and confirmed post actions such as `like`, `repost`, `bookmark`, and `delete`. Model-callable tools include status and draft helpers plus guarded write helpers. Writes are blocked unless `confirm=True`, and callers must only set that after explicit user approval of the exact action and text.

Runtime prerequisite: `xurl` must be installed and authenticated outside Algo CLI. If it is missing from PATH, the integration reports that state instead of falling back to browser scraping or token-file parsing.

---

## Configuration Reference

| Config Field | Default | Description |
|---|---|---|
| `model` | `qwen3` (env: `ALGO_CLI_MODEL`) | Active model |
| `host` | `http://localhost:11434` | Local Ollama host |
| `cloud` | `False` | Ollama Cloud mode |
| `theme` | `tokyo-night` | Visual theme |
| `auto_mode` | `False` | Auto-approve all tool calls |
| `safe_mode` | `True` | Block destructive shell commands |
| `num_ctx` | `8192` | Context window tokens |
| `temperature` | `0.4` | Sampling temperature |
| `max_tool_iterations` | `24` | Max tool calls per turn |
| `tool_think_every` | `10` | Reflection checkpoint interval |
| `memory_auto_capture_enabled` | `True` | Capture explicit, privacy-safe durable markers after successful turns |
| `memory_auto_daily_limit` | `5` | Requested automatic writes per UTC day; may lower but not exceed hard max 5 |
| `memory_auto_entry_limit` | `64` | Requested fingerprint entries; may lower but not exceed hard max 64 |
| `memory_auto_char_limit` | `12000` | Requested memory-growth budget; may lower but not exceed hard max 12000 chars |
| `skill_crystallize_enabled` | `True` | Auto-crystallize skills |
| `skill_crystallize_every` | `3` | Runs between crystallize passes |

**Environment overrides:**

| Variable | Purpose |
|----------|---------|
| `ALGO_CLI_CONFIG_DIR` | Override config directory |
| `ALGO_CLI_ENV_FILE` | Override runtime env file path |
| `ALGO_CLI_MODEL` | Default model |
| `ALGO_CLI_THEME` | Default theme |
| `ALGO_CLI_GATEWAY_URL` | Go gateway URL |
| `ALGO_CLI_GATEWAY_BIN` | Custom gateway binary path |
| `ALGO_CLI_HARNESS_INDEXER` | Custom Rust indexer binary path |
| `OLLAMA_HOST` | Local Ollama host |
| `OLLAMA_API_KEY` | Ollama Cloud API key |

---

## Testing

The test suite runs fully offline — every model call (embedders, crystallizer LLM) is stubbed. No running Ollama instance or network access required.

**Key fixture (`conftest.py`):**
- `ALGO_CLI_CONFIG_DIR` is set to a temp directory **before any `algo_cli` imports** — this is critical because `CONFIG_DIR` is resolved at module import time. The legacy variable/package are also exercised for compatibility.
- `clean_state` (autouse) wipes the temp config dir and clears all module-level caches (`identity._CACHE`, `identity._LESSONS_INDEX`, `harness._INDEX_CACHE`) around every test.
- `make_fake_embed(dims)` returns a deterministic keyword-biased embedder for retrieval tests.

---

## Code Style Conventions

- **Python 3.10+**: use `X | Y` union syntax, `match`, `from __future__ import annotations`.
- **Formatting**: ruff, 120-char line length, `snake_case` functions/variables, `PascalCase` classes, `UPPER_CASE` module constants.
- **Lint rules**: pyflakes (`F`) + syntax errors (`E9`) only — no style enforcement beyond that.
- **Go**: `gofmt`-formatted; minimal stdlib-only module.
- **Rust**: `cargo fmt`, clippy with `-D warnings`.
- **Comments**: only when the WHY is non-obvious. No docstring blocks on internal helpers.
- **No new features** without a corresponding test; no feature flags or backwards-compat shims.

---

## Commit and PR Conventions

- Short imperative subjects: e.g. `Add identity layer and harness RAG`, `Fix footer toolbar theme inversion`.
- One focused change per commit; body only when details add clarity.
- PRs describe user-facing behavior changed, list manual verification commands, call out config/env impacts, include terminal screenshots only for display changes.

---

## Security Constraints

- **Never commit:** API keys, `~/.algo_cli/` data, memory contents, generated credential files, `.env` files.
- Keep secrets in environment variables or the ignored `~/.algo_cli/env` runtime file.
- `run_shell` and `write_file` require explicit user approval unless `auto_mode` is on.
- The Go gateway must not bind non-loopback addresses without `-allow-remote`; it has no authentication.
- Harness source integrations are read-only; maintenance writes only generated index and performance data and never executes external agent tools.

---

## Known Upstream Issues

### Gemini 3 + tool calls via Ollama (issue #14567 / PR #14676)

Gemini 3 models (`gemini-3-flash-preview:cloud`, `gemini-3-pro-preview`, etc.) routed through Ollama Cloud or the local Ollama daemon require an opaque `thought_signature` to be round-tripped on every `functionCall` part. The Ollama Python SDK does not currently expose this field, so the second round of any tool-call sequence returns:

```
Function call is missing a thought_signature in functionCall parts.
(status code: 400)
```

Setting `think=False` does not help — Gemini 3 thinks internally even at MINIMAL levels and still enforces the requirement.

**Our workaround** (in `main.collapse_tool_history_for_gemini`): when `is_gemini_model(cfg.model)` is true, every chat request rewrites prior assistant `tool_calls` + tool result messages into a single assistant content turn before sending. The model keeps continuity (knows what it called and what came back) without triggering the signature requirement. A one-time per-session notice fires on first use so the user knows the workaround is in effect.

Tradeoff: Gemini sees stringified tool history instead of native `functionCall` / `functionResponse` parts. Small reasoning-quality hit, but tool use works end-to-end.

**Remove the workaround when:**
1. Ollama PR #14676 merges and adds `Signature` to `api.Message` and `ThoughtSignature` to `api.ToolCallFunction`
2. The Ollama Python SDK releases a version that exposes those fields
3. Verify the existing forward-compat capture in `serialize_tool_call` round-trips them

Tracking:
- https://github.com/ollama/ollama/issues/14567
- https://github.com/ollama/ollama/pull/14676
- https://ai.google.dev/gemini-api/docs/thought-signatures (Google's spec)

---

## Windows Notes

The CLI is tuned for lower-resource Windows machines:
- Default context is `8192` (not `32768`).
- File reads, tool results, directory listings, and search results are all capped.
- `search_files` prefers `rg` and skips heavy directories.
- Optional integrations resolve from the current user's home and configured environment variables; never add developer-specific absolute paths.
