# Astra and Harness Pattern Review

## Plan

- [x] Repair version-gated discovery, subscription transport, aliases, model
  capabilities, and reasoning controls. Test future catalog entries as well.
- [x] Reproduce and fix nearby provider and pattern-evaluation bugs.
- [x] Audit catalog structure and active pattern correctness; compare retrieval
  and cache behavior against deterministic baselines.
- [x] Update ALGO.md with evidence-bounded patterns and current audit results.
- [x] Run full tests, lint/type checks, compilation, public-data scan, build,
  affected qualification refresh, installed-source checks, and a bounded live
  Astra smoke with no repository content or tools.

## Evidence Rules

A catalog heading, imported kernel, passing unit test, and measured runtime
improvement are different claims. Proposed and untested entries remain explicitly
unverified. Synthetic benchmarks establish fixture-specific behavior only. M8
external qualification remains separate from local checks.

## Initial Findings

- Discovery used Codex protocol 0.144.2. The authenticated catalog exposes Astra
  at protocol 0.153.1 and declares a minimum client version of 0.153.0.
- The static subscription transport registry omitted Astra; the installed
  executable uses a separate v0.18.0 virtual environment.
- Catalog status parsing searched arbitrary prose and truncated the search to
  500 characters; test evidence was tested for truthiness rather than exact bool.
- Tool-name cadence alone was mislabeled as proof that verification occurred.
- The M8 report hard-coded an active-freeze statement after the freeze was
  lifted. It now states the actual limit: local evidence does not override
  outstanding external qualification gates.
- Responses function-argument events used item IDs distinct from call IDs;
  deltas were disconnected from the named tool call and final arguments ignored.
  The event shape is confirmed by the
  [OpenAI SDK schema](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_function_call_arguments_delta_event.py).

## Results

- The source and actual installed v0.18.0 CLI both discover `gpt-6-astra` at
  protocol 0.153.1 and return `ASTRA_OK` through native Responses Lite, without
  tools or CLI fallback. No repository content was sent in either smoke.
- Astra aliases, 272,000-token context metadata, vision support, and supported
  low/medium/high/xhigh/max settings are covered. Ultra remains explicitly
  unsupported because this adapter does not implement its orchestration contract.
- Discovered future model IDs use validated catalog capabilities; malformed,
  duplicate, and hidden entries are excluded. Credential reset clears discoveries.
- Stream argument correlation, strict catalog evidence, and cadence diagnostic
  regressions reproduce the prior defects and pass after repair.
- The final catalog contains 531 structurally valid entries: 13 explicitly
  implemented, 512 without explicit status, four planned, one partial, and one
  proposed. Missing status does not prove a missing implementation; it means no
  status was inferred. This review does not certify 531 runtime algorithms.
- Four patterns were added (O1-O4), with implementation/partial/proposed states,
  explicit failure behavior, source/test pointers, and acceptance criteria.
- The active-pattern regression selection passed 111 tests, including hybrid
  fusion, exact-vector/scalar parity, top-k tie stability, cache admission,
  CUSUM, kernel contracts, and the catalog/diagnostic paths.
- The bounded 896-record persisted snapshot benchmark passed 15/15 canary
  observations. Reusable BM25 measured 0.966 ms median versus 45.135 ms rebuild
  median (46.73x); seven frozen quality fixtures met all thresholds. This measures
  ranking, not generation latency or real-world answer quality.
- Equal-capacity TinyLFU versus LRU replay: 88/96 versus 0/96 hot-key hits over
  576 requests. This is evidence for scan resistance on that frozen workload.
- The live seven-check hybrid probe initially reported unavailable because the
  canonical ALGO record lacked an embedding. A single-record local Ollama embed
  repaired that prerequisite; all seven checks then passed. This is a self-vector
  and wiring check, not an independent semantic-quality evaluation. The remaining
  index still has partial embedding coverage; no full-index migration was claimed.
- The wheel and source distribution build; isolated wheel installation/entrypoint
  smoke passed. The installed package and bundled ALGO.md match source hashes.
  The prior installed package was archived locally before replacement; no user
  credentials, configuration, or session history were included in that backup.
- Final full suite: 4,163 passed, 32 skipped in 77.58 seconds; branch-inclusive
  coverage 67.73%, above the 57% gate. Ruff, targeted mypy (nine changed runtime
  modules), compileall, public-data scan, diff whitespace, wheel/sdist build,
  isolated installed-wheel smoke, and installed-source parity passed.
- Earlier suite runs caught stale/intermediate qualification digests while the
  source/evidence refresh was in progress. The final suite used unchanged,
  reconciled artifacts and passed, without weakening the freshness checks.
- Refreshed Nathan evidence passes 17/17 correctness probes and 31/31 workloads
  with no policy escapes, duplicate mutations, or unverified completions. M8
  passes nine local metrics and keeps five external-browser metrics blocked.
  The evidence ledger and exact M9 report verify: 29 requirements verified,
  13 blocked, zero failed. The hardening gate confirms the freeze is lifted.
- At completion of this local review, changes were installed but not yet
  committed or pushed. Subsequent integration is tracked in PR #22. Version
  0.18.0 is retained; no release tag was created by this review.

## Limits

M8 external browser qualification remains blocked independently of local test
success. No release tag, public benchmark claim, or cross-harness superiority is
asserted. O4 is a proposed diagnostic improvement, not silently activated behavior.
Existing processes retain imported Python code; restart Algo CLI before using
`/model astra` or selecting Astra in `/models`.
