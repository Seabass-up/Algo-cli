# Changelog

All notable changes to Algo CLI are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release tags use the package version prefixed with `v`.

## [Unreleased]

## [0.15.1] - 2026-07-13

### Changed

- Typed action programs now clamp timeout-aware nested actions to the remaining wall-clock budget and accurately document the cooperative limit; semantic supersession preserves read snapshots across intervening mutation epochs.

### Added

- `algo-cli update` upgrades the published distribution through pipx, uv tool, or the current Python environment's pip without initializing or changing user runtime state.

## [0.15.0] - 2026-07-13

### Added

- OAuth-backed Codex model discovery for GPT-5.6 Sol, Terra, and Luna, plus independent per-model reasoning effort controls through `/thinking effort`.
- Deferred BM25 action discovery and a bounded typed program runtime with runtime-owned capability ceilings, canonical per-action approvals, content-addressed intermediate artifacts, and immutable hash-chained mutation receipts.
- A repeated context-efficiency release benchmark covering schema conversion/recall, selected-schema tokens, typed-program intermediate-result compression, and semantic supersession.
- Query-aware structural repository maps with weighted personalized CodeRank, token-budgeted symbol outlines, and score provenance in code-RAG results.
- A deterministic baseline-versus-structural retrieval benchmark with ambiguous central-module cases and semantic-specificity guardrails.
- Worktree-backed durable agent threads with `/worktree`, `/agent switch`, repository/branch/HEAD validation, collision-safe repository-hashed paths, and isolated forks by default.
- A structured `/ship` workflow for fingerprinted and scoped commit, freshly checked push, and idempotent draft pull-request creation.
- Active `worktree-isolation` and `pre-push-gate` kernel contracts with explicit approval and safety metadata.

### Changed

- Ordinary turns now expose a bounded task-relevant tool catalog instead of all schemas; request budgeting includes the visible schema cost, and older successful resource snapshots are replaced with compact provenance receipts while mutation and verification evidence stays protected.
- Code RAG now fuses embedding similarity with structural importance while retaining a semantic-only baseline mode; project-graph construction reuses the consent-filtered file inventory instead of walking the repository twice.
- Agent-thread records now persist bounded workspace and Git-digest evidence while omitting absolute paths from model handoffs.
- Direct `/model NAME` selection now reconciles the provider route instead of inheriting stale Ollama Cloud state; dashboards label xAI and ChatGPT routes correctly.
- The runtime overview now advertises only controls the prompt loop actually implements.

### Security

- One-shot `approval-mode=never` now derives denials from the central Action Registry rather than a stale hard-coded mutation list.
- Structured publishing blocks stale plans, protected branches, path escapes, oversized or secret-bearing security inputs, Git object overlays, unresolved LFS payloads, mismatched upstreams, and remote divergence. Bounded disk-backed scans cover every outgoing commit's raw metadata, paths, and delta; immutable object-ID refspecs and destination binding prevent prefix-truncation, metadata-secret, remote-default, and remote-rewrite bypasses.
- Repository-provided release scripts are never executed implicitly by `/ship`; the public-release scanner remains an explicit CI/release gate and normal user-configured Git hooks retain standard Git semantics.
- Worktree removal refuses ignored files as well as tracked and untracked changes, derives its removal boundary independently of editable records, and validates live Git identity. Resume, switch, and isolated fork require the exact recorded HEAD plus working-state digests; dirty state must stay in the same worktree or continue in a new worktree/thread after commit.
- Raw Git mutation classification now handles bounded global options and fails closed for aliases or unknown subcommands instead of allowing `git -C`, `git -c`, or configured-alias bypasses.

### Fixed

- GPT-5.6 subscription requests now use the Codex Responses Lite contract, preserve `max` effort, surface reasoning-summary deltas, and avoid forwarding unsupported Ollama output-limit fields. Short or previously persisted `sol`, `terra`, `luna`, and `lunna` names are canonicalized before provider routing instead of falling through to Ollama.
- NDJSON tool events now report non-zero shell exits as failures.
- Resumed threads no longer lose their restored workspace to heuristic project resolution.

## [0.14.0] - 2026-07-11

### Added

- Persistent runtime-agent threads with `/agent resume`, `/agent fork`, and bounded handoff context.
- `/agent team` fan-out for two to four read-only specialists followed by a single write-owning integration pipeline.
- Kernel listing, contract inspection, and runtime wiring checks.
- A ten-gate harness scorecard, offline retrieval benchmark, competitive evidence contract, and production-path algorithm-effectiveness probes.
- Bounded, privacy-gated automatic memory capture with timestamps, fingerprints, deduplication, and `/memory-auto` controls.
- Runtime execution guardrails, Git-evidence capture, performance telemetry, and explicit verification contracts.
- `python -m algo_cli`, a side-effect-free `doctor`, public release scans, isolated-wheel smoke tests, and trusted-publishing workflows.

### Changed

- Renamed the public package, command, configuration directory, and environment variables to Algo CLI. The `ollama-cli`, `~/.ollama_cli`, and `OLLAMA_CLI_*` forms remain temporary migration aliases.
- Packaged the curated algorithm, memory, wiki, and skill corpus inside the wheel so an installed harness has the same built-in knowledge as a source checkout.
- Made external agent stores and index-compute-lab explicit opt-ins. Changing either setting rebuilds the index without disabled records.
- Moved SciPy and TurboVec to the optional `quantization` extra and PDF rendering to the `pdf` extra.
- Single-sourced the version from `algo_cli.__version__` and updated package, command, documentation, and release metadata to `0.14.0`.
- Published the PyPI distribution as `algo-cli-runtime` because `algocli` already occupies the conflicting namespace; the product and installed command remain `algo-cli`.
- Reduced repeated prompt construction, context scans, vector work, and cache churn on hot runtime paths.
- Restricted experimental plugin status checks to manifest inspection; loading a plugin no longer claims unsupported dynamic registration.

### Security

- Removed personal profiles, business-specific integrations, machine paths, generated inventories, and private project examples from the public tree and distributions.
- Added metadata-only connector/MCP indexing, credential redaction, source and artifact privacy scans, and a separate full-history privacy gate.
- Kept local external context out of model requests until the user explicitly enables the corresponding source.
- Kept the unauthenticated Go harness gateway loopback-only unless remote binding is explicitly allowed.

### Fixed

- Failed or cancelled first-run model selection no longer marks onboarding complete.
- `algo-cli --version` and `algo-cli doctor` no longer build an index, scaffold identity files, or otherwise mutate a fresh home.
- Installed wheels now retain the reviewed algorithm catalog and all required curated product-memory categories.
- Harness readiness now reports optional or unavailable subsystems accurately instead of inflating the score.
- Unknown slash commands are rejected instead of falling through to the model.
- Runtime agent commands, slash-command ownership, kernel actions, memory paths, and retrieval algorithms are covered by the release test suite.
