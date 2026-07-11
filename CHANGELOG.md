# Changelog

All notable changes to Algo CLI are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release tags use the package version prefixed with `v`.

## [Unreleased]

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
