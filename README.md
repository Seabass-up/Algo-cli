# Algo CLI

[![CI](https://github.com/Seabass-up/algo-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/Seabass-up/algo-cli/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/algo-cli-runtime.svg)](https://pypi.org/project/algo-cli-runtime/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Website](https://algo-cli.com) · [Documentation](https://algo-cli.com/docs) · [Security](SECURITY.md)

An agent runtime for tools, durable context, and verified work. Algo CLI combines direct tool use, routed agent pipelines, persistent memory, repository intelligence, and a searchable cross-agent harness. Run locally with Ollama or connect Ollama Cloud, xAI Grok, and ChatGPT/Codex.

- **Act:** inspect files, edit code, run commands, and work with connected services.
- **Remember:** carry identity, lessons, memories, and reusable skills across sessions.
- **Route:** use direct chat for ordinary work or Agent Blocks for larger tasks.
- **Verify:** ground claims in tool results, Git evidence, and explicit safety policies.

## Install

Algo CLI requires Python 3.10 or newer. The PyPI distribution is named
`algo-cli-runtime` and installs the `algo-cli` command:

```bash
pipx install algo-cli-runtime
# or: uv tool install algo-cli-runtime
algo-cli doctor
algo-cli update
```

`algo-cli update` upgrades the published `algo-cli-runtime` package using the
installation's owning tool: pipx, uv, or the current Python environment's pip.
It leaves configuration, credentials, memory, and other files under
`~/.algo_cli` untouched. Set `ALGO_CLI_UPDATE_MANAGER=pipx`, `uv`, or `pip`
only when automatic installation detection needs an explicit override.

To install a reviewed source checkout instead, clone the repository and run
`pipx install .` from its root.

Optional extras are available for PDF rendering (`algo-cli-runtime[pdf]`) and experimental vector quantization (`algo-cli-runtime[quantization]`). The distribution installs the `algo-cli` command. Run `algo-cli doctor` for a side-effect-free readiness report.

## Quick Start

```bash
# Local Ollama
ollama pull qwen3
algo-cli

# Ollama cloud model through local Ollama login
ollama signin
ollama pull qwen3:235b-cloud
algo-cli --model qwen3:235b-cloud

# Direct Ollama Cloud API and web tools
export OLLAMA_API_KEY="..."
algo-cli --cloud --model qwen3

# ChatGPT/Codex subscription (authenticate once outside the chat REPL)
algo-cli config setup chatgpt
algo-cli --model gpt-5.6-sol
# Short aliases also work: sol, terra, luna
algo-cli --model terra

# One-shot non-interactive JSON event mode (for bridges, scripts, CI)
algo-cli --oneshot --json "summarize this folder"
echo "what changed in main.py?" | algo-cli --oneshot --json
algo-cli --oneshot --json --approval-mode auto --cwd /path/to/project "fix the failing test"
```

Signed-in local Ollama can run `:cloud` models without `OLLAMA_API_KEY`. `OLLAMA_API_KEY` is only required for the direct Cloud API route (`--cloud` / `/cloud on` when an API key is present) and web tools. Embedding indexes currently use local Ollama models only; a configured cloud embedding backend falls back to local until Ollama Cloud exposes embedding models.

`--oneshot --json` emits one JSON object per line to stdout (NDJSON), suitable for subprocess consumption. `--approval-mode never` (default) denies approval-required tools and emits a `tool_denied` event; pass `--approval-mode auto` to grant them. Skill crystallization is disabled in one-shot mode. Event types: `session_start`, `thinking`, `content`, `tool_call`, `tool_result`, `tool_denied`, `error`, `done`. The bridge can rely on `session_start` first and `done` last as framing.

Runtime environment values can be stored in `~/.algo_cli/env` — the CLI loads them automatically. `algo-cli config` shows safe provider status, and `algo-cli config setup PROVIDER` writes only the selected setting with private file permissions. Point to a different file with `ALGO_CLI_ENV_FILE`. Legacy `~/.ollama_cli` locations are supported as migration aliases.

### Provider setup

Keep account setup outside the interactive slash palette:

```bash
# Safe, redacted readiness summary
algo-cli config status

# xAI API: prompts for XAI_API_KEY without echoing it
algo-cli config setup xai
algo-cli config auth xai verify

# Google Workspace: prompts for a Google Desktop-app OAuth client ID,
# then opens the PKCE loopback login flow
algo-cli config setup google

# ChatGPT/Codex browser or device OAuth
algo-cli config setup chatgpt
```

xAI's public API uses `XAI_API_KEY`; API calls can consume paid usage, so setup never makes a request until you explicitly verify or use a Grok model/tool. Google Workspace uses OAuth 2.0 + PKCE with a local loopback callback. Create a **Desktop app** OAuth client, enable only the Workspace APIs you intend to use, and keep its consent screen/test-user policy aligned with your account. The normal Google operations remain under `/google ...`; only credentials and login moved to `algo-cli config`.

### Privacy-safe context defaults

Every normal chat request sends the active conversation and the assembled Algo CLI system context to the selected inference provider. That context includes `SOUL.md`, `IDENTITY.md`, `USER.md`, saved memories, and relevant retrieved lessons when available. With a cloud model, this content may leave the machine. Automatic memory capture is on by default; `/memory-auto off` stops new automatic captures (it does not delete existing memories).

The installed harness corpus contains only public Algo CLI docs, skills, and runtime metadata. Other local agent stores, working-directory code retrieval, index-compute-lab, and skill run-history capture are off by default because retrieved or summarized content can become part of a model request.

- `/harness external on` opts in to supported Codex, Claude, OpenClaw, Mercury, and shared agent stores.
- `/icl on` opts in to `~/index-compute-lab`; override its root with `ALGO_CLI_INDEX_COMPUTE_LAB_ROOT`.
- `/code-rag on` opts in to indexing source files beneath the active cwd and adding relevant snippets to model requests. `/code-rag off` disables retrieval and purges persisted code indexes.
- `/skills on` opts in to bounded run-summary capture under `~/.algo_cli/private/run_history.jsonl` and automatic skill-candidate crystallization by a genuinely local, non-embedding Ollama model. There is no cloud fallback.
- `/harness external off` and `/icl off` rebuild the generated index without those records.

Common credential forms are redacted and connector/MCP JSON is metadata-only, but redaction is not a substitute for reviewing the sources you enable. See [Privacy and local context](docs/privacy-and-context.md).

## Commands

| Command | Description |
|---------|-------------|
| `/help` | Show help |
| `/model NAME` | Switch model |
| `/models` | List local models |
| `/host URL` | Set local Ollama host |
| `/cloud [on\|off\|status]` | Set/toggle direct Ollama Cloud API mode; local Ollama login can run `:cloud` models with `/cloud off` |
| `/config [status\|setup PROVIDER\|auth PROVIDER ACTION]` | Focused provider setup from the REPL; prefer `algo-cli config ...` in a normal terminal |
| `algo-cli config setup xai` | Store a redacted xAI API key; API calls may consume paid usage |
| `algo-cli config setup google` | Configure Google Desktop-app OAuth and launch the PKCE login flow |
| `algo-cli config setup chatgpt` | Authenticate ChatGPT/Codex outside the normal slash palette |
| `/x-account status` | Check separate X account OAuth status through `xurl` |
| `/x-account draft-post` / `draft-reply` | Create browser drafts for X posts/replies without publishing |
| `/auto [on\|off\|status]` | Set/toggle auto-approve for tool calls |
| `/safe [on\|off\|status]` | Set/toggle safe mode for shell/file tools |
| `/policy [on\|off\|status]` | Toggle Agent Block tool-policy enforcement (off by default) |
| `/thinking [on\|off\|status]` | Set/toggle reasoning-summary display |
| `/thinking efforts` | Show independent Sol, Terra, and Luna reasoning settings |
| `/thinking effort [MODEL] LEVEL` | Set Codex reasoning effort (`low`, `medium`, `high`, `xhigh`, or GPT-5.6 `max`) |
| `/agent team ...` | Algo's multi-agent counterpart to Codex Ultra; fan out specialists, then integrate and verify |
| `/memory-auto [on\|off\|status]` | Inspect or toggle bounded, privacy-gated automatic memory capture after successful turns |
| `/code-rag [on\|off\|status]` | Opt in or out of cwd source indexing and prompt retrieval; `off` purges persisted indexes |
| `/skills [on\|off\|status\|crystallize]` | Opt in to local run-history capture and review local-only skill candidates |
| `/remember FACT` | Save a long-term memory |
| `/intuition [on|off|status|add|list|forget|reindex]` | Manage embedded recall blocks |
| `/icl [on\|off\|status\|ask\|path]` | index-compute-lab knowledge graph (auto-injected each turn when on) |
| `/harness external [on\|off\|status]` | Opt in or out of indexing other local agent stores |
| `/route TASK` | Preview task classification, pipeline, advisory budget, and tool policy |
| `/agent init` | Write a starter `~/.algo_cli/blocks.toml` without overwriting an existing file |
| `/agent [--pipeline NAME] TASK` | Run a named Agent Blocks pipeline and record a resumable thread |
| `/agent team [--roles A,B[,C,D]] TASK` | Run 2–4 independent read-only specialists, then one integrating pipeline |
| `/agent threads` / `/agent show ID` | List persistent parent/child runs or inspect their evidence |
| `/agent switch ID` | Restore a thread's recorded repository, branch, HEAD, and working directory after identity checks |
| `/agent resume ID [TASK]` / `/agent fork ID [--same-worktree] TASK` | Continue an exact recorded thread state or create an isolated child worktree; dirty parents require `--same-worktree`, or a new worktree/thread after commit |
| `/worktree status` / `/worktree list` | Inspect the active Git workspace or list Algo-managed worktrees |
| `/worktree new NAME [--from REF]` | Create and activate a collision-safe feature worktree outside the source repository |
| `/worktree use ID` / `/worktree remove ID` | Activate a verified worktree or remove one only when tracked, untracked, and ignored-file gates pass |
| `/ship status` | Preview branch, HEAD, divergence, remote, diff checks, and a state fingerprint without mutation |
| `/ship commit [--expect HASH] [--files PATHS] MESSAGE` | Scope, scrub, stage, and commit feature-branch changes |
| `/ship push [--expect HASH]` | Refresh the remote, reject divergence, scrub every outgoing commit and its metadata, then push the reviewed object ID |
| `/ship pr [--ready]` / `/ship all [--ready] MESSAGE` | Reuse or create a draft PR; or resume the guarded commit → push → PR stack |
| `/kernel list` / `/kernel show NAME` | Inspect kernel contracts without executing workloads |
| `/kernel check [NAME]` | Validate kernel imports, slash routes, metadata, and active action contracts |
| `/status` | Show current model, context usage, and active features |
| `/verify [on\|off\|status]` | Set/toggle claim-grounding verification |
| `/reason status\|guide\|react\|reflexion\|tot\|got\|mcts\|qcr\|neuro_symbolic` | Inspect or set reasoning posture |
| `/memories` | List memories |
| `/forget ID` | Delete a memory |
| `/clear` | Clear current conversation |
| `/context [status\|rebuild\|clear]` | Inspect or manage context compression |
| `/save NAME` / `/load NAME` | Save/load conversation |
| `/cd PATH` | Change tool working directory |
| `/ctx NUM` | Set context window (`256`–`2,000,000`) |
| `/temp NUM` | Set temperature (`0.0`–`2.0`, finite values only) |
| `/toolmax NUM` | Set max tool iterations (`1`–`128`) |
| `/thinkevery NUM` | Set tool-call reflection interval (`1`–`128`) |
| `/pdf [--pages N] [--chars N] PATH` | Extract local PDF text |
| `/theme NAME` | Switch visual theme |
| `/info` | Show configuration |
| `/actions [TOPIC]` | Show commands, tools, and harness stats |
| `/selfcheck` | Show improvement-relevant skills and harness stats |
| `/reload` | Reload config, tools, and harness index |
| `/exit` | Exit |

Typing `/` in the prompt shows inline slash-command completions.

For the assistant/model, slash commands are session controls, not prose. The model is prompted to use `session_slash` for deterministic `/read`, `/ls`, `/cd`, and `/cwd`; `session_command` for non-file session controls such as `/status`, `/mode`, `/context`, `/code-rag status`, `/harness refresh`, `/route`, and `/agent`; and normal model-callable tools for actual work (`write_file` for edits, `run_shell` for tests/builds). Read-only/status `session_command` calls run without approval; state-changing commands, including `/code-rag on` and `/code-rag off`, and agent execution require approval. Runtime-triggered `/agent` calls are allowed from the parent chat loop, but recursive delegation from inside an Agent Blocks run is rejected. Toggle commands accept explicit idempotent forms such as `/auto on`, `/safe off`, `/code-rag status`, `/thinking status`, `/verify on`, and `/cloud off`.

`/reason` is called out in the model prompt as a reasoning-posture control. Use `/reason status` or `/reason guide` before changing it. Keep `react` for ordinary tool loops and simple edits; use `reflexion` after failed/partial attempts; `tot`, `got`, or `mcts` for ambiguous multi-path planning/search; `qcr` for comparing candidate solutions; and `neuro_symbolic` for verification-heavy logic, code invariants, contracts, math, or claim checking. Changing `/reason` does not replace evidence gathering or tool verification.

`/route` also shows an advisory Agent Blocks budget (block count, per-block iterations, and parallel-work signal). It never starts or truncates a pipeline automatically. Agent Block panels show tool-policy decisions. By default policies are previews only; use `/policy on` to restrict Agent Block tools to the displayed policy. Existing safe-mode and approval behavior remains authoritative.

Use `/agent init` to create a starter `blocks.toml` defining `default`, `code-change`, `research`, and `review` pipelines. Configured blocks use tool groups (`read`, `web`, `write`, `shell`) rather than raw tool names; set `requires_change = true` on blocks that must produce a verified file change. Invalid configuration falls back to the built-in pipeline with an error message.

`/agent team` follows the Algo delegation loop: classify the task, choose two to four explicit specialist roles, fan them out with fresh read-only contexts, join their evidence in deterministic role order, and hand that bounded context to one normal integration pipeline. Child threads never mutate the shared workspace. The integration pipeline is the sole write owner and retains the existing approval, safe-mode, required-change, Git-evidence, review, and recovery gates. This gives parallel exploration without parallel-edit conflicts or unbounded recursive swarms.

Agent runs are recorded in `~/.algo_cli/agent_threads.json` (up to 100 recent records, with bounded outputs and turn history). Records include bounded workspace identity, the initial/current HEAD, branch, and full-state Git digests without persisting diff contents or injecting absolute paths into model handoffs. `/agent resume` and `/agent switch` restore only the exact recorded state. `/agent fork` creates a collision-safe linked worktree by default when the parent has Git metadata, uses the verified immutable HEAD as its base, and refuses to silently drop dirty tracked or untracked files. Dirty state can continue only with `--same-worktree`; after committing it, create a new worktree and agent thread rather than rebinding the old record to a different state. Managed worktrees are recorded separately in `~/.algo_cli/worktrees.json`, active records are never evicted by the history cap, and cleanup refuses tracked, untracked, or ignored data. Custom team roles must be unique short names, and team size is structurally capped at four.

`/ship` is the terminal equivalent of a one-action Git publish control: status is read-only; every mutating phase remains approval-gated when model-invoked. The workflow blocks detached/protected branches, stale reviewed fingerprints, path escapes, high-confidence secrets, oversized security inputs, dirty pushes, missing/ambiguous remote defaults, non-feature upstreams, and branches behind their freshly fetched base or upstream. Before push, it reviews every immutable outgoing commit's raw metadata, paths, and delta with bounded disk-backed capture, rejects unresolved Git LFS payloads and Git object overlays, revalidates the destination, and pushes the reviewed object ID through an isolated destination binding. Repository-provided scripts are never executed implicitly, while normal user-configured commit hooks retain Git semantics and are verified after they run. Project-specific public-release scans remain separate CI gates. New pull requests default to draft, and the state-derived phases resume after partial failure without creating a duplicate commit.

See [T3 Code parity review](docs/t3-code-parity-2026-07.md) for the revision-pinned comparison, verified advantages, and remaining gaps.

When a block reaches its iteration budget after gathering evidence, the CLI requests a tool-free partial summary and allows the finalizer to report those incomplete findings instead of discarding the run.

Partial and failed Agent Block panels include an explicit state reason, and partial reasons are passed to downstream blocks separately from the model's output text.

When a `requires_change` implementation becomes partial for a recoverable execution reason (iteration exhaustion, missing write evidence, failed `write_file`, or no verified final delta), the CLI may run exactly one tool-free recovery plan and one focused implementation retry capped at eight iterations. Policy denials, unsafe Git attribution, errors, and cancellation do not trigger retry.

After an agent pipeline run, `/diff` shows the most recent verified Git diff captured by a `requires_change` block (with status, status reason, verification warning, and recorded writes), and `/changes` summarizes per-block activity (role, status, duration, tool calls, evidence). Both commands read session-scoped state — cleared by `/clear`, overwritten by the next pipeline run, never persisted to disk.

Every turn semantically supersedes older successful snapshots of the same resource—such as repeated file reads, directory listings, status calls, and identical searches—with compact SHA-256 receipts while preserving the latest result and the assistant/tool protocol pair. Mutation results, failures, shell verification, and Git-diff evidence are protected. Once `cfg.messages` exceeds `prune_after_messages` (default 80), count-based cleanup may remove only older allowlisted snapshots outside `prune_keep_recent` (default 40); it strips the matching call entry so providers never receive orphaned tool messages. Token savings are emitted as `semantic_supersession` and `prune` performance events.

When a `requires_change` block completes on recorded `write_file` evidence but Git verification is unavailable, the manual-confirmation notice is carried on a dedicated verification field. The block's output stays untouched; the warning is rendered on the completion panel and passed to downstream blocks in a separate `## Verification` section.

Agent Block runs use enabled intuition recall before the first block and display any recalled context. For medium-risk coding tasks with policy enforcement enabled, write and shell tools remain available subject to approval; high-risk tasks remain read-only.

The built-in `code-change` plan block may perform up to four local read-only inspection turns before planning, so implementation starts from observed Python paths instead of guessed project structure.

## Model-Callable Tools

The model can call these tools during a conversation:

**Files:** `read_file`, `read_pdf`, `render_pdf_pages`, `edit_file`, `write_file`, `list_directory`, `search_files`, `find_unique_anchor`, `batch_edit`

**Shell:** `run_shell`

**Web:** `web_search`, `web_fetch` (requires Ollama Cloud + API key)

**X:** `x_search` uses optional xAI API-key access for read/search; configure it with `algo-cli config setup xai`. xAI API calls may consume paid usage. `x_account_status`, `x_account_draft_post`, `x_account_draft_reply`, `x_account_post`, `x_account_reply`, and `x_account_post_action` use the separate X API OAuth lane through `xurl`; write actions require explicit confirmation.

`/x-account` requires the official X API CLI, `xurl`, to be installed and authenticated separately. The CLI only runs `xurl auth status` for status checks and never reads `~/.xurl` directly.

**Memory:** `remember`, `append_lesson`, `update_user_profile`

**Multimodal:** `embed_text`, `vision_describe`

**Programmatic actions:** `action_search` retrieves a small set of exact deferred action schemas; discovery does not bypass the active capability ceiling, runtime policy, or approval. `action_program` compiles a bounded typed dataflow plan—never arbitrary Python or JavaScript—and routes every nested action through its existing ActionSpec policy, guardrails, approval, attempt ledger, and telemetry. Its wall-clock budget is cooperative: the remaining budget is propagated into timeout-aware actions and checked around every step, while actions without a timeout contract can only be marked over-budget after they return. Large intermediate values are content-addressed in the private runtime store; compact results retain hash-chained mutation and verification receipts. Successful program verifiers reconcile into the outer completion ledger, so the latest passing post-mutation check supersedes earlier failures without relying on model phrasing.

**Models:** `model_show`, `model_pull`, `model_copy`, `model_create`, `model_delete`

**Files and Git:** `read_file`, `read_pdf`, `render_pdf_pages`, `edit_file`, `write_file`, `list_directory`, `search_files`, `find_unique_anchor`, `batch_edit`, `git_status`, `git_diff`

**Harness:** `available_actions`, `harness_stats`, `harness_search`, `harness_read`, `harness_refresh`, `query_knowledge_graph`

After `/icl on`, `query_knowledge_graph` reads the configured index-compute-lab ranked association index through its public query CLI. Set `ALGO_CLI_INDEX_COMPUTE_LAB_ROOT` to use a different local checkout.

`write_file`, `run_shell`, `model_delete`, and `model_create` require approval unless `/auto` is enabled. A typed program is not blanket-approved: each nested mutation or external action keeps its own approval decision, and restricted Agent Blocks supply their own runtime-owned capability ceiling. Safe mode blocks destructive shell patterns by default. Agent Blocks with `requires_change = true` are instructed prescriptively that file creation, edits, appends, and deletes must go through `write_file`; `run_shell` is restricted to read-only verification (status, tests, lint, diff, grep, ls). Shell-based file mutation (heredocs, output redirection, `sed -i`, `Set-Content`, `Out-File`, etc.) is not counted as evidence of a change, and the model is told to stop and report rather than route edits through shell when `write_file` is unavailable.

Run the deterministic context-efficiency release gate with `python -m algo_cli.evals.tool_context_efficiency`. It repeats tool selection, verifies required-action recall and schema conversion, measures semantic supersession, and compares raw intermediate data with compact typed-program output.

Agent Block runs automatically snapshot `git status` and tracked `git diff` around implementation blocks, so review and final blocks receive verified change evidence rather than relying only on model narrative.

Blocks without `requires_change` are not gated, but if they execute a successful `write_file` action or a shell command classified as mutating, the CLI captures read-only Git audit evidence and passes it to downstream blocks.

## Harness Bridge

The CLI retrieves relevant records from a local index. The public built-in corpus is enabled by default; external agent stores require `/harness external on`. Indexing reads content but never executes external agent tools.

**Default sources:** packaged Algo CLI docs and skills, user-created `~/.algo_cli` records, and explicit `harness_roots.json` entries.

**Opt-in sources:** Codex, Claude, OpenClaw, shared agents, Mercury, CLI Agent, Pi, and index-compute-lab.

**Harness commands:**
```
/harness status
/harness score
/harness compare
/harness refresh
/harness external status
/harness build-rust
/hsearch retrieval benchmark
/hread algo-cli:memory:algo-cli-memory-lifecycle-contract.md
```

`/harness score` grades Algo CLI's internal retrieval/runtime readiness.
`/harness compare` recomputes the declared five-axis competitor matrix and
applies ten stricter leader gates. It will not claim leadership from a dirty
worktree, missing revision-pinned competitor evidence, local-only benchmarks,
ties, or a second-place corrected score.

### Rust Indexer (optional)

For faster cold-start index builds:

```bash
cd harness-indexer
cargo build --release
```

Native helpers are source-checkout features and are not bundled into the Python wheel. When built from a checkout, Algo CLI uses the Rust indexer for opted-in cold-start discovery only. Warm refreshes use the Python incremental scanner. Point to a custom binary:

```bash
export ALGO_CLI_HARNESS_INDEXER=/path/to/harness-indexer
```

Legacy `OLLAMA_CLI_HARNESS_INDEXER` is still accepted as a fallback.

### Go Gateway (optional)

Exposes the harness index over localhost for bridge integrations:

```bash
cd harness-gateway
go run . -addr 127.0.0.1:8765
```

Endpoints: `GET /healthz`, `GET /harness/stats`, `GET /harness/search`, `POST /supplemental/embed`

Override: `ALGO_CLI_GATEWAY_URL`, `ALGO_CLI_GATEWAY_BIN` (legacy `OLLAMA_CLI_*` names are still accepted).

## Configuration

Config is stored in `~/.algo_cli/` by default (data from `~/.ollama_cli` is auto-copied with backup on first run). Runtime environment values load from `~/.algo_cli/env`. Override with `ALGO_CLI_CONFIG_DIR` or `ALGO_CLI_ENV_FILE`. Legacy `OLLAMA_CLI_*` variables are still read for one release.

| Variable | Purpose |
|----------|---------|
| `ALGO_CLI_CONFIG_DIR` | Override config directory |
| `ALGO_CLI_ENV_FILE` | Override runtime env file path (`env` is preferred; `.env` is supported as a fallback) |
| `ALGO_CLI_MODEL` | Default model |
| `ALGO_CLI_THEME` | Default theme (`tokyo-night`, `catppuccin-mocha`, `dracula`, `nord`, `gruvbox`, `dolphie`) |
| `ALGO_CLI_GATEWAY_URL` | Go gateway URL |
| `ALGO_CLI_HARNESS_INDEXER` | Optional Rust harness indexer binary |
| `OPENAI_OAUTH_CLIENT_ID` | Optional override for the bundled ChatGPT/Codex browser OAuth client |
| `OPENAI_CODEX_CLIENT_ID` | Optional override for the bundled Codex OAuth client |
| `XAI_API_KEY` | Optional xAI API key for Grok models and `x_search`; configure with `algo-cli config setup xai` |
| `GOOGLE_OAUTH_CLIENT_ID` | Google Desktop-app OAuth client ID for Workspace access; configure with `algo-cli config setup google` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Optional Google client secret for a client type that requires it; Desktop apps normally omit it |
| `OLLAMA_HOST` | Local Ollama host |
| `OLLAMA_API_KEY` | Direct Ollama Cloud API/web tools key; not required for signed-in local `:cloud` models |

Legacy `OLLAMA_CLI_*` equivalents are read for compatibility during the rebrand window.

## Development

```bash
python -m pip install -e ".[dev]"

ruff check algo_cli tests
python scripts/check_public_release.py
python -m compileall -q algo_cli      # compile check
pytest tests --cov=algo_cli --cov-branch --cov-fail-under=57
```

The test suite stubs all model calls and runs against a temporary config directory — no running Ollama instance or network access required.

Optional native components:

```bash
# Rust indexer
cd harness-indexer
cargo fmt --check && cargo clippy --release -- -D warnings && cargo test --release

# Go gateway
cd harness-gateway
gofmt -l . && go vet ./... && go test ./...
```

CI runs Python on Linux, Windows, and macOS; validates the wheel and source distribution; and tests the Rust and Go helpers.

## License

MIT — see [LICENSE](LICENSE).
