# Class A Harness Improvement Plan

## Contract

Follow `ALGO.md`: implementation, activation, correctness, measurement, and
comparative benefit are separate evidence states. A local score or a small
benchmark does not establish superiority over every harness. Preserve required
Echo authority, approval boundaries, source freshness, and external qualification
blocks while improving capability.

## Workstreams

1. Runtime recovery: bounded provider retries without replaying completed tools.
   Implemented and installed; verified with the actual Astra model after an
   injected reasoning-only response. Keep cancellation and partial-answer gates.
2. Retrieval reliability: CATALOG COVERAGE VERIFIED; SEMANTIC QUALITY OPEN.
   The protected-source baseline had 0/658 embedded records; the installed index
   now retains 788/788, including all 222 effective runtime capabilities and the
   recovery and supervised-review guides. Completed batches survive failure, cancellation,
   refresh, protected restart, and package reinstallation. Representative semantic
   recall and latency qualification remain open. Public vectors and query caches
   now bind the configured endpoint, observed model artifact, and requested dimensions.
3. Context and memory quality: validate dimension changes, cache identity, source
   invalidation, conflicting evidence, pattern applicability, and protected-memory
   isolation. Qualify Echo Contextual Logic independently of the disabled legacy
   graph; do not award readiness merely because an adapter is configured.
4. Runtime efficiency: measure cold and warm retrieval cost, repeated filtered
   workloads, cancellation, and oversubscription. Adopt vector sidecars or
   persistent scheduling only when measured bottlenecks justify the complexity.
5. Failure-driven learning: turn reproducible failures into deterministic tests,
   bounded recovery contracts, and executable pattern evidence. Keep operational
   memory in Echo; this plan contains public engineering work, not user memory.
6. Comparative evaluation: rerun revision-pinned, same-model baselines; expand
   beyond the existing four draft tasks to multi-file changes, recovery, security,
   and long-context work. Preserve raw runs, protected fixtures, held-out cases,
   repetitions, cost, latency, and failed runs. No universal ranking claim from a
   single local run or historical result.
7. Delivery and qualification: run focused and full tests, lint, default and CI
   typing gates; track the optional strict-typing backlog separately (565 errors
   in 73 files on September 7). Also run
   security checks, source-bound qualification, packaging, installed-source parity,
   and a real runtime smoke. Keep M8 external-browser requirements honestly blocked
   until their prerequisites are available. Commit/publish only when requested.

## Iteration Receipt

For each iteration record: observed failure, minimal reproduction, scoped change,
regression result, before/after runtime evidence, residual risks, and next test.
Refresh source-bound evidence only after the source stops changing. Failed gates
remain failed; retry only the operation whose contract permits retry.

### September 7 Discovery Pass

- Reproduced and repaired batch cardinality/cancellation checkpoint loss,
  invalid-vector false readiness, bulk source-change masking, and timestamp-only
  document invalidation. Added bounded restart reads for the public harness index:
  protected startup had deleted a valid vector index above the generic 16 MiB
  state limit. Config and credential limits remain unchanged.
- A live local build reached 658/658 protected-source records at 4,096 dimensions
  and retained all vectors through a fresh protected process. Full-coverage fusion
  exposed a missing canonical self-evaluation reference; explicit, filter-aware
  source selection now preserves it without altering the reported RRF scores.
- A package reinstall initially invalidated 94 unchanged capability vectors.
  Content-based reuse now retains them, while genuinely changed input still
  invalidates its vector. The installed package retained all 659 vectors through
  reinstall, refresh, and a fresh protected process.
- Authentication retrieval exposed a filename-filter conflict: the shipped public
  recovery guide was excluded as an auth file. An exact-path, non-link exception
  now covers indexing, freshness, and reading. External copies and credential,
  secret, and token files remain excluded. The actual installed CLI successfully
  searched and opened the guide; a search-only smoke had missed the reader defect.
- The final three-query canary returned canonical ALGO first for self-evaluation
  and the recovery guide first for authentication recovery. Cold reads took
  664-691 ms including query embedding and index/cache loading; warm medians were
  5.89-12.62 ms. These are canaries, not representative semantic-recall or latency
  qualification. Build a grounded retrieval set before tuning.
- The unchanged four-task draft coding baseline passed 0/4 external checkers.
  The first task exited zero after 7 successful and 20 denied tools without
  repairing the fixture. The other three reached 180-second timeouts before any
  tool call; their cause is not established. Preserve these failed runs.
  Per-action authorization is not supplied by `--approval-mode auto`; do not
  weaken that boundary to make a benchmark pass.

### September 7 Verification Receipt

- Final Python gate: 4,343 passed, 32 skipped, 68.15% branch-aware coverage.
  The four new retrieval regression files add 35 cases; the final focused slice
  passed all 126 cases, including actual recovery-guide read-through.
- Ruff, default mypy, CI mypy, compilation, package metadata, installed dependency
  compatibility, public source/history/artifact scan, and installed-source parity
  passed. The actual 0.18.0 launcher uses the repaired non-editable package.
  Optional strict mypy is still a separately recorded failing baseline.
- The real CLI passed protected startup, refresh, status, search, read, and quit.
  The final local embedding probe needed zero re-embeddings and retained 659/659
  vectors through a fresh protected process after package reinstall.
- O4-O8 source-bound test evidence passed; the local paired retrieval experiment
  completed 160 samples. This is not a model-quality or competitive ranking gate.
- M8: 9 local checks passed, 5 external checks blocked, 0 failed. The overall
  hardening audit retains 29 verified and 13 blocked requirements, with 0 failed.
  Neither report authorizes external-browser or public superiority claims.
- The four-task local-model baseline remains 0/4. Runtime recovery and retrieval
  improvements do not erase that failure or complete the overall Class A goal.

### September 7 Runtime Recovery Iteration

- Added failing-then-passing regressions for denied-only loops, empty completion,
  misleading finalization wording, skipped outcomes hiding uncertain effects,
  unrelated edits clearing nonretryable barriers, and missing extracted answers.
  The agent stops after three consecutive nonexecuting tool batches and exits
  partial when progress remains blocked. A permitted tool resets that budget.
- Uncertain and explicitly nonretryable attempts survive bounded ledger churn.
  Mutations reserve capacity under a lock before approval; full capacity blocks
  new effects but permits observations. Threaded and exception tests verify
  atomic admission and reservation release. This is not permission to reconcile
  an uncertain effect by clearing its record.
- One-shot output now explicitly reports model-turn completion, not independent
  task verification. The benchmark parser extracts actual final content deltas
  after the last tool call and ignores reasoning/tool-result bodies.
- A traced installed run reproduced a pre-tool stall in macOS Keychain item
  creation during optional argument redaction. Optional privacy projection now
  requests existing material only; required Echo provisioning is unchanged.
  Three new regressions failed before that fix and passed afterward. Locked
  backend reads remain a separate availability risk, not a qualified timeout fix.
- A repaired trace passed that stall, then timed out during a later model request.
  Ollama logs showed a concurrent embedding request evicting/reloading the coding
  model. A sequential repeat of the same frozen task, model, and 60-second bound
  finished in 11.467 seconds: observations executed, repeated approvals were
  blocked, and the process exited 2 with partial status. No timeout occurred.
  This supports recovery of the diagnosed stall, not a controlled speedup or
  functional checker pass. Original raw failed runs remain unchanged.
- Focused runtime, stream, Keychain, and privacy tests passed (132 cases), and
  default mypy remained clean after the capacity change. Final source-bound
  qualification and complete delivery gates are recorded separately below.

### Runtime Iteration Verification

- Full Python suite: 4,364 passed, 32 skipped, 68.24% branch-aware coverage
  in 87.35 seconds on macOS/Python 3.10.20. This iteration adds 21 regression
  cases above the previous 4,343-case passing baseline.
- Ruff, default mypy (276 modules), the exact CI mypy command (60 targets),
  compilation, source/history/artifact scans, locked dependency audit, package
  metadata, and installed dependency compatibility passed. Echo Veil's pinned
  source and installed RECORD audit also passed. Optional strict mypy remains
  the previously recorded, separately failing backlog.
- Installed 0.18.0 package/source parity passed for 290 source files and 276
  Python modules, digest
  `sha256:5174322332017ca5fc7334a7428332c1e02ade946683f418b40fbb6f7b1658b8`.
- A final installed-runtime smoke injected the reported reasoning-only failure,
  then used the real authenticated `gpt-6-astra` endpoint. Recovery reused the
  identical request, executed `git_status` exactly once, returned
  `ALGO_STREAM_RECOVERED`, and exited zero in 25.07 seconds. Required Echo
  protection stayed enabled; automatic memory capture was disabled for the
  probe, and saved configuration was restored. This is a bounded recovery
  qualification, not a provider-reliability or coding-quality benchmark.
- The final installed CLI also passed all seven protected startup, refresh,
  search, recovery-guide read-through, and clean-exit smoke assertions.
- O4-O8 source-bound test evidence passed, and the local paired comparison
  completed 160 samples. M8 retained 9 local passes, 5 external blocks, and
  zero failures. The overall audit retained 29 verified and 13 blocked
  requirements. The runtime benchmark passed 17 correctness probes and 31
  deterministic workloads; its wording no longer falsely infers an active
  hardening freeze from local evidence. Public-claim eligibility stays false.
- No commit, push, tag, approval relaxation, or credential reset was performed.
  The original four-task functional baseline remains 0/4, and the overall
  Class A goal is not complete.

### Supervised Approval Iteration

- Added explicit POSIX one-shot approval transport through
  `--approval-mode interactive --approval-fd FD`. The trusted supervisor receives
  exact runtime-defaulted arguments on a separate inherited Unix socket, then
  returns one nonce/digest-bound decision within a bounded deadline. Malformed,
  stale, disconnected, timed-out, and handoff requests fail closed; existing
  `never` and `auto` semantics are unchanged. No automated benchmark approval
  exception was added. See `docs/supervised-action-review.md`.
- Review completion now rechecks arguments and authority before issuing fresh
  single-use consent. Confirmation time is sampled after review rather than
  before an operator's wait. Socket descriptors are closed on exit and rejected
  descriptor paths; raw action content stays out of public NDJSON.
- Reproduced a separate approval-to-dispatch race: a nested `batch_edit` request
  was changed after approval and the unreviewed edit executed. The regression
  failed before repair. Dispatch now owns a deep argument snapshot and gives
  invokers a separate copy so callers/tools cannot corrupt the reviewed operation
  or its audit input. Both snapshot regressions pass.
- Full Python suite: 4,420 passed, 32 skipped, 68.33% branch-aware coverage in
  88.85 seconds. The 56 new cases include 53 channel/approval cases, two
  dispatch-snapshot cases, and one built-in guide discovery case. Channel tests
  include actual fixture-file writes and cancellation after approval but before
  invocation. POSIX tests do not qualify a Windows channel.
- Ruff, new-file formatting, default mypy (277 modules), the expanded exact CI
  mypy command (63 targets), compilation, source/history/artifact scans, locked
  dependency audit, wheel-from-sdist build, Twine, and installed dependency checks
  passed. Packaging initially rejected the missing sdist entry for the new guide;
  both packaging manifests are now consistent. Echo's pinned source/RECORD audit
  passed without changing required protection or credentials.
- Final installed 0.18.0 source parity passed for 291 package source files and
  277 Python modules with digest
  `sha256:54aad1d89e3220d34ece6f25bb88288e1be2c6cf3a3c74309208d0466f9296b9`.
  Separate byte comparisons verified installed ALGO, supervisor-review, and
  provider-recovery documentation. The initial new-guide read-through failed:
  its file shipped, but the curated wiki did not register it. A built-in-root
  regression reproduced that omission. Registering the guide also exposed an
  exact-inventory test mismatch; the expected list now includes the guide and
  verifies every registered wiki file exists. The failed M8 run was retained
  before rerunning. The rebuilt CLI passed protected startup, refresh, status,
  search, both guides' actual source/body read-through, and clean exit.
- An installed required-Echo stream canary injected one empty completed response,
  then used the actual authenticated Astra path. Exactly one retry returned the
  expected answer in 9.31 seconds, with no tool calls, a closed failed stream,
  automatic memory capture disabled, and all checked overrides restored. This
  qualifies bounded recovery of an injected empty response, not every possible
  provider failure or model task correctness.
- The final installed, required-Echo Astra probe received exactly one
  action-time `write_file` request. The supervisor explicitly denied it, no file
  was created, and Astra stopped with the expected answer in 15.95 seconds.
  Checked configuration overrides were restored and the descriptor closed.
  Positive approvals were tested in controlled fixtures, not presented as real
  operator approval or a passing coding benchmark.
- Current source-bound O4-O8 tests and the 160-sample local comparison passed.
  Agent runtime qualification retained 17 correctness passes and 31 passing
  deterministic workloads. M8 retained 9 local passes and 5 external blocks; the
  overall audit retained 29 verified and 13 blocked requirements, zero failures.
  The new approval transport, dispatcher, and authority engine are explicitly
  source-bound. No independent-review, production Echo, external-browser, or
  universal competitive claim is made. Functional baseline remains 0/4.
- The ten historical `.pytest_cache` failure entries were compared with all
  4,452 currently collected node IDs through pytest's collection hook. None is
  still collected. Those obsolete IDs were not treated as current failures or
  cleared to manufacture a passing result; the current full-suite run is the
  verification authority.
- Changes remain uncommitted and unpublished; the full Class A goal stays open.

### Terminal Reviewer Iteration

- Integrated `--review-actions` with explicit interactive one-shot approval.
  The parent retains its controlling terminal and reviewer socket; only the
  connected client descriptor reaches the agent. Full bounded actions are
  JSON-escaped, and approval requires a fresh request-specific line. Malformed,
  stale, duplicate, pipelined, expired, disconnected, and cancelled requests
  cannot authorize an action. Terminal or thread startup failure has no fallback.
- Added an Algo-only supervised benchmark lane with recorded decision counts and
  review time included in the run timeout and latency. It cannot mix with other
  adapters or be published as an unattended comparison. Corrected the benchmark
  README's false blanket-write-approval statement; `auto` remains unchanged.
- Initial runtime-frame tests exposed a digest-format mismatch in the new
  reviewer; it now validates the runtime's actual 64-character hexadecimal
  identity. Process-start callback and thread-start failures are cleanup paths,
  and ordinary client EOF no longer looks like a failed review. Positive fixture
  approvals are not represented as real operator consent.
- The focused integration gate passed 170 tests, including actual PTYs and a
  child process writing only its approved synthetic fixture. Default mypy passed
  278 modules; the exact blocking CI command passed 64 targets. The full suite
  passed 4,480 tests with 32 skips and 68.39% branch-aware coverage in 86.66
  seconds. The reviewer module itself reached 84% branch-aware coverage.
- Ruff, new-module formatting, compilation, public source/history/artifact scans,
  the locked dependency audit, wheel-from-sdist build, Twine, and installed
  dependency checks passed. Echo's pinned source/RECORD audit passed. Installed
  0.18.0 parity matched 292 package source files and 278 Python modules with digest
  `sha256:6b5a740291ebe8dcb3b61e095a1d28f0b7378de7584d5edbdc315a810fefc951`.
  Installed ALGO, supervised-review, and recovery guides also matched source bytes.
- A live installed Astra request used the new parent-held terminal reviewer with
  required Echo protection and automatic memory capture disabled. Its one exact
  file-write request was denied, no file was created, and the expected answer
  completed in 65.18 seconds, including interactive review time. Private review
  frames stayed out of NDJSON and stderr. All selected configuration fields were
  restored. The first probe had stopped before launching an agent because it
  incorrectly assumed the saved protection setting was `required`; the corrected
  probe temporarily required protection and restored the saved `optional` setting
  with Echo still enabled. It did not relax protection to make the test pass.
- The installed no-terminal probe exited 64 with no stdout and no approval
  fallback. Installed refresh, embedding, status, search, and guide read-through
  passed. A fresh required-protection process retained 660/660 finite vectors at
  4,096 dimensions with zero pending records. This proves current index coverage
  and persistence, not representative semantic recall quality.
- Source-bound runtime qualification retained 17 correctness passes and 31
  passing workloads. M8 retained 9 local passes, 5 external blocks, and zero
  failures. O4-O8 and the 160-sample local retrieval comparison passed. The
  original unattended coding baseline remains 0/4; no supervised coding checker
  passes, independent approval identity, external-browser readiness, production
  Echo readiness, or universal competitive superiority are claimed.
- Changes remain local and uncommitted. Real operator-reviewed task approval is
  still required before measuring the supervised coding lane; no real model
  mutation was approved by an automated loop.

### Source-Grounded Retrieval Iteration

The source-grounded retrieval iteration added an isolated 24-question public
development evaluation of real hybrid search and source-body read-through. Its
initial two-pass baseline passed 36/48 samples (18/24 questions), with labeled
recall 0.75 and MRR 0.6354. It missed five paraphrased authority/recovery queries
and one Spanish provider-privacy query. This failure replaces any implication
that complete embeddings or the six-record lexical fixture establish semantic
quality. Preserve these labels and failures before testing ranking hypotheses.
The baseline report is `/tmp/algo-cli-grounded-retrieval.wtxC81/baseline.json`;
it is development evidence, not a held-out comparison or protected Echo recall.

Reproduced 25 failing query-recovery cases before repair. Query embeddings are
now validated before caching, copied and safely normalized, and invalidated
with the index. Wrong dimensions, malformed responses, mutable provider lists,
and empty-slice calls no longer strand later retrieval. Cancellation still
propagates and lexical fallback never fabricates semantic success. The focused
evaluation/recovery suite passes 53 cases.

- Final full suite: 4,533 passed, 32 skipped, 68.44% branch-aware coverage in
  101.42 seconds. Ruff, new-file formatting, default mypy (279 modules), exact
  blocking CI mypy (66 targets), compilation, public source/history/artifact
  scans, locked dependency audit, wheel-from-sdist build, and Twine passed.
- Installed 0.18.0 parity matched 293 source files and 279 Python modules:
  `sha256:18e87476130ebe8381f30c8361fa9d33b7a2668bea148f2f75f1898c4dcee9ab`.
  Installed ALGO and both operator guides matched source bytes. Dependency
  compatibility and Echo's exact pin/source/RECORD audit passed.
- Installed startup, refresh, embedding, status, search, guide source/body read,
  and quit passed. A fresh required-Echo process retained 660/660 finite vectors
  at 4,096 dimensions. An injected malformed query response fell back to lexical
  search; the next query recovered using exactly one actual local embedding
  request. Its separate top-1 guide assertion failed: the guide ranked second
  behind pattern A11. That ranking miss is retained, not called a full probe pass.
  An initial probe used an incorrect config attribute and stopped before search;
  the corrected probe used the actual runtime field names without saving overrides.
- A tool-free installed `gpt-6-astra` canary returned the exact expected answer
  in 10.58 seconds with required Echo and no stderr or tool calls. Automatic
  memory capture was disabled for the probe; selected config fields were restored.
- The second independent isolated build again embedded 662 public source-tree
  records and passed 36/48 retrieval samples, with the same six failing questions.
  All label anchors were present and both source and model identity stayed stable.
  Report: `/tmp/algo-cli-grounded-retrieval.wtxC81/verified.json`, raw digest
  `sha256:9388f8b658ef0501cab1bf0d8c5072f835294523d97a54dfd3ce82da160b21be`.
  Corpus build is excluded from query timings; these development runs were not
  isolated latency experiments and do not qualify representative performance.
- Nathan retained 17/17 correctness probes and 31/31 deterministic workloads.
  M8 initially failed its source-closure invariant because the newly bound
  retrieval ranker and test were missing from M8's dependency list. The failed
  artifact was retained, the exact failure reproduced, and the closure repaired
  without changing thresholds. Final M8 is 9 local passes, 5 external blocks,
  zero failures; M9 is 29 verified, 13 blocked, zero failures. O4-O8 source-bound
  tests and the 160-sample local comparison passed; six validated reports were
  installed. None overrides the failing semantic development baseline.
- Changes remain local and uncommitted. The original coding baseline remains
  0/4; operator-reviewed coding, external-browser readiness, production Echo,
  and universal competitive superiority are still unproven.

### Next Qualification Work

1. Use the integrated operator-controlled reviewer to qualify real coding work.
   The inherited POSIX socket
   binds exact actions to fresh single-use confirmations and bounded review time;
   it is not an independent authority or an automatic benchmark exception.
   The current adapter's `auto` flag still cannot authorize
   `write_file`, `edit_file`, or `run_shell`.
   Do not relax production approvals or describe an authority-blocked run as
   comparable to an unrestricted competitor. Functional baseline remains 0/4.
2. Rerun revision-pinned frozen tasks with explicit authority and resource parity,
   no competing local inference workload, repetitions, and independent checkers.
   Keep completion, scope safety, latency, and task correctness separate.
3. Expand representative retrieval and coding evaluations. The scorecard must
   remain degraded without validated representative semantic retrieval evidence;
   the legacy knowledge graph is also intentionally unavailable under Echo
   authority. Do not turn configuration or lexical fixtures into readiness.
   M8 external-browser qualification, Echo production readiness, and universal
   competitive superiority remain unproven.
4. Investigate the fixed retrieval misses with ranker-level evidence before
   tuning: authority versus old notes, uncertain resume, pattern benefit,
   discovery versus permission, conflicting stores, and Spanish privacy.
   Keep labels and raw failures fixed, separate development tuning from held-out
   evaluation, and make quality/readiness consumers distinguish this real corpus
   result from the small offline lexical fixture. Do not raise top-k or insert
   query-specific aliases solely to make the development set pass.

### Effective Catalog and Restart Iteration

- Expanding the public development set exposed missing capabilities before any
  new query was ranked. The old discovery path indexed only 94 explicit specs;
  the effective registry contains 222, covering all 74 tool callables and 102
  distinct slash names. Discovery now uses that effective registry. A policy
  marker refreshes old indexes while retaining unchanged vectors. Unknown
  callables remain unclassified and denied; indexing never invokes them.
- Readiness now checks complete IDs and policy metadata rather than a nonzero
  count. The first installed restart probe caught a false stale-catalog warning:
  JSON had converted tuples to lists. Added a wire-value comparison and explicit
  tests for persistence, numeric policy booleans, malformed metadata, duplicates,
  unknown entries, and missing entries. The first failed probe is not a pass.
  All 17 capability-coverage regressions now pass.
- The first expanded-corpus measurement embedded 790 source-tree records and
  verified anchors for all 44 development questions before ranking. The original
  frozen set passed 34/48 samples (17/24 questions); the additional set passed
  30/40 (15/20). Sources and local embedding-model identity stayed stable during
  each run. Reports are in `/tmp/algo-cli-capability-recovery.InoL6U/` as
  `retrieval-original.json` and `retrieval-expanded.json`. This measurement
  preceded the metadata-only restart repair; it is not a final-source or installed
  semantic qualification. Neither set is independently held out, and no candidate
  ranking change was activated. The earlier 18/24 result used a smaller corpus.
- The six-record lexical fixture now explicitly reports its limited scope. Its
  raw result is preserved, but the scorecard warns instead of awarding full
  semantic-readiness credit. A validated representative-evidence input path for
  that gate is still open work. ALGO.md and the execution contract now distinguish
  coverage, lexical fixtures, semantics, provider-request retry, Agent recovery,
  and the live freeze policy.
- Final full gate: 4,597 passed, 32 skipped, 68.47% branch-aware coverage in
  95.54 seconds. All 94 stream-recovery cases passed. Ruff, default/CI mypy,
  compilation, source/history/artifact scans, locked dependency audit,
  wheel-from-sdist build, and Twine passed. Final logs and artifacts are under
  `/tmp/algo-cli-capability-recovery.InoL6U/restart-fix/`; first-round evidence
  remains in the parent directory. The ten historical pytest-cache entries refer
  to obsolete or platform-specific node IDs, not current local failures. The
  cache was not deleted to conceal them.
- Installed 0.18.0 parity passed for 294 source files and 280 Python modules:
  `sha256:c86ef91308a646a96ff9350831074daf2ad6f73ef8792510802960756d3515cf`.
  Installed documentation matches source bytes; dependency compatibility and
  Echo's exact pinned-source/RECORD audit passed. Actual launcher refresh, embed,
  status, search, source-body reads, and quit passed. A fresh process retained
  788/788 vectors at 4,096 dimensions and reported 222/222 current capabilities.
- The final installed required-Echo Astra canary recovered from one injected
  reasoning-only completion in 10.31 seconds. It reused the identical request,
  closed the failed response, returned the expected answer, and executed no
  tools. Saved settings were restored and automatic memory capture stayed off.
  This is a bounded recovery check, not a guarantee against provider outages.
- Current O4-O8 source-bound tests and the 160-sample local comparison passed;
  six validated reports were installed. Nathan retained 17 correctness passes
  and 31 passing workloads. M8 retained 9 local passes, 5 external blocks, and
  zero failures; M9 retained 29 verified and 13 blocked requirements. No approvals,
  external-browser prerequisites, or Echo protections were relaxed. Changes
  remain local and uncommitted; semantic quality, real coding qualification,
  and the overall Class A goal remain open.

### Source Selection and Label Eligibility Iteration

- Diagnosed the unchanged 790-record public corpus before editing the ranker.
  Physical-file source balancing improved the first development cases but
  regressed new natural-language web and Git multi-tool questions: unrelated
  tools share `action_registry.py`. The tested MMR variants also regressed.
  Both candidates were rejected, not promoted on their aggregate score.
- The narrower runtime policy uses exact pattern IDs and code-like capability
  names before the lexical candidate cutoff. Final hybrid selection discounts
  repeated document sources by `1 + 0.5 * prior_selected_from_source` while
  treating each runtime capability and explicitly named pattern as distinct.
  Kind filters retain relevance ordering. Raw RRF scores and all applicability,
  conflict, exclusion, and resource-budget checks remain intact; provenance
  discloses the selection policy. A zero-result request now exits before index
  or embedding-provider access. All 13 new selection regressions pass.
- Corrected the expanded evaluator's historical `reflex_spec` positive label.
  The source was explicitly historical and correctly excluded by production
  retrieval. Its exact query and anchor now form a separately scored exclusion
  check. Readability alone is no longer sufficient for positive-label validity;
  invalid inventories stop before provider calls. The original 24-question
  digest remains unchanged, and the first flawed report remains historical
  evidence. All 25 evaluator tests pass, including injected search failures,
  missing labels, source changes, and exclusion false positives.
- Final paired measurement used saved pre-change runtime bytes and current
  runtime bytes on the same final public index, local Qwen embedding-model
  digest, fixed top-five limit, 51 eligible positive questions, one exclusion
  question, and two repetitions. Baseline passed 78/102 positive samples
  (39/51 questions); candidate passed 86/102 (43/51), with no regressions. Both
  passed 2/2 exclusion samples, scored separately. Mean labeled recall improved
  from 0.7745 to 0.8497; MRR improved from 0.6967 to 0.7490. Median repeated
  search/read time increased from 88.22 to 94.99 ms; no speedup is claimed.
  Raw diagnostics, rejected candidates, frozen baseline bytes, paired samples,
  and source/model bindings are under
  `/tmp/algo-cli-ranking-selection.VZYjND/`. These are public development
  measurements, not independent held-out, generated-answer, operational Echo,
  or competitor qualification.
- Eight positive questions remain failing: `memory_authority`,
  `uncertain_resume`, `discovery_not_permission`, `conflicting_stores`,
  `provider_privacy_es`, `cloud_privacy_fr`, `cloud_privacy_de`, and
  `natural_file_tools`. Preserve these failures. Do not raise top-k, insert
  query-specific aliases, or weaken protected-memory exclusions to pass them.
  The component evaluator can read curated contracts that the protected
  model-facing entrypoints still exclude as `memory`; source typing and
  operational qualification remain separate work.
- ALGO.md now describes this limited runtime subset without claiming full L5
  facility-location, semantic redundancy, or token-cost optimization. It records
  the rejected fallback and the distinction between label corrections and
  measured ranking gains. O4/O5/O6/O7/O8 current source-bound checks passed
  17/104/13/125/28 tests respectively, and the 160-sample local interaction
  comparison completed. Six validated evidence reports were installed.
- Final full gate: 4,618 passed, 32 skipped, 68.50% branch-aware coverage;
  pytest reported 103.22 seconds. Ruff, default and exact CI mypy, scoped
  formatting, compilation, source/history/artifact scans, locked dependency
  audit, wheel-from-sdist build, and Twine passed. Logs and artifacts are in
  `/tmp/algo-cli-ranking-selection.VZYjND/delivery/`. The ten historical pytest
  cache entries were retained and are not current collected-test failures.
- Installed source parity passed for 294 files and 280 Python modules:
  `sha256:f461a1b3323110814d7afb06ce4ac73b3c56e951a3b63b2e8ec32140530814ee`.
  Installed ALGO.md and provider recovery documentation match source bytes;
  pip compatibility and Echo's pinned-source/RECORD audit passed. Actual CLI
  refresh, embed, status, search, read, and quit passed in 5.75 seconds. A fresh
  process retained 788/788 vectors at 4,096 dimensions and 222/222 current
  capabilities. Index readiness is not semantic qualification.
- Final installed required-Echo Astra canary recovered from an injected
  reasoning-only completion in 11.40 seconds, with exactly one retry of the
  identical request, a closed failed response, the exact expected answer, and
  no tools or errors. Saved settings were restored, Echo remained enabled,
  and automatic memory capture stayed off. The bounded provider-request retry
  remains separate from restarting an Agent or replaying completed tools.
- Nathan retained 17/17 correctness probes and 31/31 workloads, with zero
  duplicate mutations or policy escapes. M8 retained 9 local passes, 5 external
  blocks, and zero failures. M9 retained 29 verified and 13 blocked requirements.
  Current source-bound ledger rows were appended without erasing prior runs.
  The scorecard's representative semantic-evidence input, real coding
  qualification, Echo production readiness, M8 external-browser qualification,
  and the overall Class A goal remain open. Changes are local and uncommitted;
  no GitHub publication, release tag, or policy relaxation occurred.

### Protected Operational Retrieval and Tool Discovery

- Closed the gap between component retrieval and the model-facing tools. The
  old `harness_search` used keyword search and excluded all `memory` records,
  including three shipped governance contracts. Protected search now uses the
  local hybrid ranker over one authorized snapshot. These exact shipped
  contracts retain their IDs but are reconstructed from bounded,
  descriptor-bound source reads and labeled documentation, not agent memory.
  Runtime capability text is reconstructed from the code-owned registry.
  Protected reads reject unsafe links and changed pattern sources. Mutable
  host memory is still available only through Echo Veil; cached classification
  is not authority and ordinary public-index metadata is not fully authenticated.
- Query transport timeouts are bounded, keyword-only degradation is disclosed,
  and model-facing search does not start bulk embedding work. When no vector
  result is available, fallback retains lexical ordering without the hybrid
  source-repeat penalty. Tests cover one-snapshot ranking, forged metadata,
  source replacement, link refusal, size/encoding bounds, cancellation,
  unprepared protection, and vector invalidation.
- Measured a prototype cache regression before accepting the implementation.
  Separate repeated-query blocks measured protected search at 168.37 ms versus
  8.60 ms for the warmed raw ranker. Rechecking roots once per query and reusing
  an equal, freshly reconstructed projection reduced protected search to
  24.97 ms, with raw ranking at 8.32 ms. Source reads still occur on every
  request. These measurements use one public query and cached query vectors;
  they are not provider latency or a universal speedup. The earlier interleaved
  measurement rebuilt single-entry caches and is not a warm-path baseline.
- The initial paired driver omitted eight saved challenge vectors and therefore
  exercised fallback on those cases. Its report is retained as diagnostic
  evidence, not a complete semantic comparison. Final measurement prevalidates
  every query vector, binds both saved vector files and source bytes, and uses
  the same 790-record public corpus, fixed top-five budget, 51 positive
  questions, one separately scored exclusion, and two repetitions. Actual
  baseline tool functions passed 62/102 positive samples (31/51 questions);
  current tools passed 86/102 (43/51), with no regressions. Both passed 2/2
  exclusion samples. Recall improved from 0.6373 to 0.8497 and MRR from 0.5654
  to 0.7500. Repeated median search/read time rose from 4.14 to 26.75 ms;
  improved recall is not described as a latency win over keyword-only search.
  The eight previous semantic misses remain. Frozen labels were not changed.
- A real installed Astra canary initially failed: exact `harness_search` and
  `harness_read` identifiers did not satisfy specialist discovery gates, so
  neither schema was exposed and the answer had no source-read evidence.
  Exact names now satisfy discovery intent and receive the existing name
  relevance boost. Partial names do not; explicit class restrictions and
  schema/count budgets remain intact. Discovery does not invoke tools or grant
  approval. All 43 operational regressions and 14 selection regressions pass.
- The first post-fix source canary executed search/read successfully but failed
  an aggregate-content JSON check; it is retained as a failed overall check.
  The final checker separates the final answer after the last tool result from
  interim commentary. The identical prompt then passed in the installed runtime
  in 37.07 seconds: hybrid search discovered the contract, the next tool read
  its verified source, and the final JSON named that source and correctly
  described live-evidence precedence, default-off capture, and prohibited
  plaintext fallback under Echo. Interim content was present. This is one
  real generated answer with a deliberately narrowed read-only tool catalog,
  required Echo, and no automatic memory capture, not broad answer-quality or
  independent qualification.
- Final gates passed: 4,662 tests, 32 skipped, and 68.6077% branch-aware
  coverage. Ruff, default mypy across 280 modules, the exact 66-target CI mypy
  gate, scoped formatting, compilation, source/history/artifact scans, locked
  dependency audit, wheel-from-sdist build, and Twine passed. Pattern tests
  O4/O5/O6/O7/O8 passed 17/148/13/169/72 checks; the 160-sample comparison
  completed and all six source-validated reports were installed. Ten historical
  pytest cache entries remain historical, not current collected-test failures.
- Installed parity passed for 294 files and 280 Python modules with digest
  `sha256:6eb9ca1370a2983827e723949a3112412f6671f2e2e667d88917e25a4cb0aaf6`.
  Installed ALGO.md matches the source; dependency compatibility and Echo's
  pinned-source audit passed. Actual CLI refresh/embed/status/search/read/quit
  passed in 101.51 seconds after the source-runtime canary changed the indexed
  roots. A fresh process retained 788/788 vectors at 4,096 dimensions and
  222/222 current runtime capabilities. The required-Echo installed Astra
  stream canary recovered from an injected reasoning-only empty completion in
  11.36 seconds: one retry, identical request, failed response closed, exact
  answer, no tools or errors. Saved configuration was restored in both canaries.
- Evidence, rejected diagnostics, baseline bytes, paired samples, build logs,
  and installed receipts are under
  `/tmp/algo-cli-operational-retrieval.JIeFDc/`; final delivery artifacts are
  in its `delivery-final/` directory. Final paired source/index/code bindings
  are in `operational-paired-final.json`. ALGO.md O5/O8 now describe the actual
  source-role, revalidation, fallback, and operational verification contracts.
  Fresh Nathan/M8 ledger entries preserve prior runs. Nathan passes all local
  runtime gates; M8 has nine local passes and five external blocks; M9 has
  29 verified and 13 blocked requirements. Representative semantic quality,
  real coding qualification, Echo production readiness, external-browser
  qualification, and the overall Class A goal remain open. Changes remain
  local and uncommitted; no GitHub push, release tag, or policy relaxation.

### Model-Specific Query Input Qualification

- Rechecked the reported Responses failure before continuing retrieval work.
  All 145 stream, progress-recovery, and ChatGPT adapter tests passed. The
  installed Astra runtime recovered from a reasoning-only empty completion
  with one identical-request retry, closed the failed response, performed no
  tools, and restored configuration. The ten historical pytest cache IDs are
  renamed, superseded, or platform-specific cases, not fresh suite failures.
- A frozen public development experiment compared plain Qwen3 query embeddings
  with the retrieval instruction in the official Qwen model card. Documents
  were unchanged. Existing search/read questions improved from 43 to 47 of 51
  with no regressions; the separately scored exclusion passed twice per profile.
  The protocol, raw vectors, samples, model digest, and source bindings remain
  under `/tmp/algo-cli-query-profile.VSAgaw/`.
- Implemented `qwen3-instruct-v1` only for recognized `qwen3-embedding` names
  and tags. Other names and custom namespaces retain plain queries. The cache
  binds the actual embedding input and model, keeping old plain-query vectors
  separate. Raw lexical queries, document vectors, Echo memory, source filters,
  applicability, and action approval are unchanged. Rank provenance identifies
  the selected input profile separately from actual vector availability.
  Thirteen assertions failed before implementation; all 18 new regression
  cases pass afterward, including fallback, cancellation, and empty slices.
- Confirmed the improvement through actual local embedding transport and the
  protected-source search/read tools. Frozen 51-question results reproduced
  43 versus 47 passes over two repetitions; recall was 0.8497 versus 0.9281 and
  MRR 0.7500 versus 0.7794. Six additional safety questions were frozen before
  their first rankings: plain queries passed 3/6 and instructed queries 4/6.
  Across 57 questions there were five gains and no regressions. The exclusion
  passed twice per profile. All 116 cold embedding calls were checked for the
  exact intended input and valid 4,096-dimensional vectors; repeated queries
  made no additional provider calls. Both profiles used the same 790 records
  and pinned local model digest
  `64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b`.
- Existing-question median search/read latency was 132.33 versus 134.67 ms
  cold and 31.21 versus 31.58 ms warm. This is a recall improvement, not a
  latency win. Remaining misses are `memory_authority`, `provider_privacy_es`,
  `cloud_privacy_de`, `natural_file_tools`, `crashed_edit`, and
  `schema_permission`. Labels were not rewritten. Public developer-authored
  cases do not establish held-out quality, Echo-memory performance, or
  comparative superiority. Protocol digest:
  `sha256:0eced390fdc32dfffa0ffbe38e93f3cfffe05d729976529c4f7e9d7db0452027`.
- Full gate: 4,680 passed, 32 skipped, 68.6110% branch-aware coverage. Ruff,
  default and CI typing, scoped formatting, compilation, public source/history
  and artifact scans, locked dependency audit, wheel-from-sdist build, Twine,
  installed dependency compatibility, and Echo pin verification passed.
  Pattern O4/O5/O6/O7/O8 evidence passed 17/166/13/187/90 checks; the paired
  pattern comparison completed 160 samples. Six source-validated reports
  were installed. M8 remains nine local passes and five external blocks; M9
  remains 29 verified and 13 blocked, with no failed requirements.
- Installed parity passed for 294 files and 280 Python modules with digest
  `sha256:e9e0521962378c9274123bc7b51c174e837acabf999aca422a52fa05b2b07c6f`.
  Actual CLI refresh/embed/status/search/read/quit passed in 3.28 seconds.
  A fresh process retained 788/788 vectors and all 222 runtime capabilities.
  Required-Echo Astra searched for uncertain-crash replay guidance, read the
  verified execution contract, and returned correct source-bound JSON in
  40.21 seconds: reconciliation required, no automatic replay of unresolved
  mutations, and no replay of completed tools during provider retry. This
  was one generated-answer canary with a deliberately narrowed read-only
  catalog, not broad task qualification. A separate installed stream canary
  passed in 11.07 seconds with one identical-request retry, no tools/errors,
  and the exact expected answer. Both restored saved configuration.
- Actual-transport evidence, frozen additional cases, build outputs, and
  installed receipts are under `/tmp/algo-cli-query-profile-runtime.MwcTpG/`;
  final delivery gates are in `delivery-final/`. ALGO.md documents the input,
  cache, provenance, and measurement contract. Changes remain local and
  uncommitted, with no GitHub push, tag, policy relaxation, or overall Class A
  completion claim. The next semantic pass must retain all six misses and
  distinguish retrieval errors from generated-answer and authority failures.

### Stream Diagnosis and SQLite Lock Repair

- Rechecked the reported reasoning-only Responses failure, including a large
  `git_diff` result. The existing request-scoped retry budget remains two
  retries with 1/2-second backoff. New regressions verify that recovery and
  exhaustion preserve bounded prior tool results without replaying the tool.
  The focused stream/progress/client/retrieval/tool suite passed 417 tests
  with four skips. Historical pytest cache entries were not treated as proof
  of current failures.
- Fixed search-filter normalization and visibility. `harness_search` now
  normalizes supported facet inputs, discloses active filters and result kinds,
  and reports only authorized available facets on an empty match. Explicit
  filters never widen automatically, private facets stay hidden, and empty
  slices do not invoke the embedding provider. Seven inherited red regression
  cases pass. ALGO.md O8 records these operational contracts.
- A separate Python 3.14.6/SQLite 3.53.3 process failed natively in the WAL
  reader. Disposable fixtures then proved that the old Echo permission/profile
  checks could cancel a live SQLite writer lock by opening and closing an
  existing database or sidecar. This matches SQLite's documented POSIX lock
  hazard, but does not prove the exact cause of the earlier SIGBUS or establish
  that the operator database is corrupt. No operator database was opened
  directly for diagnosis, no WAL/SHM files were deleted, and no keys were reset.
- Repaired Echo inspection with metadata-only checks through pinned private
  directories, preserving ownership, permissions, link-count, namespace, and
  expected-identity rejection. New-file creation uses exclusive creation;
  existing database files are not reopened. Twenty-one regression cases cover
  process-level writer exclusion, unsafe leaves, namespace races, and
  existing-file inspection. Nine cases failed against the prior source.
- With explicit owner authorization, committed only this repair and its
  qualification metadata locally on Echo branch `codex/sqlite-lock-safety`:
  `4d1fc5ca7bdebac6db06effc54caf212972083bd`. The candidate starts from Algo's
  former exact pin, avoiding unrelated Echo checkout changes. Its full Python
  suite passed 764 tests with ten skips on each of Python 3.10-3.14. Ruff,
  formatting, typing, security/privacy/history scans, release checks, build,
  and Twine passed. Host authority remains release-pending, not promoted by
  source tests. Ambient `ECHO_VEIL_PROFILE` initially misdirected five fixture
  tests; the isolated rerun removed that override without altering runtime
  protection or the test assertions.
- Updated Algo's dependency, lockfile, runtime identity, and audit pins after
  qualification. Installed the canonical Git requirement using a command-local
  URL rewrite to the local Echo repository; no global Git setting or PEP 610
  record was forged. The revision is not yet available from public upstream.
  The installed audit verified commit identity, RECORD, and all 46 qualified
  Python sources against digest
  `81a73a8ffbae88ec6f4dba4c4092b311e78a05a8ecbdca38faa5f19b7f1570a1`.
- Final Algo checks passed 4,691 tests with 32 skips and 68.62% branch-aware
  coverage, Ruff lint, default/CI typing, compilation, source/history/artifact
  scans, the locked dependency audit, wheel-from-sdist build, and Twine. A
  separate whole-file formatter check requested formatting-only changes in
  three touched files; it is not reported as passing, and unrelated formatting
  churn was left out. O4/O5/O6/O7/O8 passed 17/173/13/194/97 checks; the paired
  pattern comparison completed 160 samples and six source-bound reports were
  installed. Installed parity verified 294 files and 280 Python modules with
  digest `sha256:4ead583f077ef81c0c207f697dfac7b5fd29f86600c834951cb3e86e3807d753`.
- Fresh processes using the actual installed Python preserved competing-writer
  exclusion before and after both agent/store validators in DELETE and WAL
  modes. Four fixture writers retained all 120 unique records with SQLite
  integrity `ok`. The stress driver reloads only after the existing explicit
  stale-generation rejection has rolled back before mutation; it never retries
  uncertain writes. Earlier driver API and stale-generation failures remain
  diagnostic failures, not product regressions or silently discarded evidence.
- Actual CLI refresh/embed/status/search/read/quit passed in 4.85 seconds.
  Restart retained 788 vectors at 4,096 dimensions and all 222 current runtime
  capabilities. Installed Astra, with required Echo and automatic capture off,
  recovered from an injected reasoning-only empty completion in 10.70 seconds:
  one identical-request retry, failed response closed, exact answer, no tools
  or errors, and saved configuration restored.
- The unchanged normal-catalog `crashed_edit` development prompt passed in
  62.56 seconds, reading and citing the execution contract. The unchanged
  `empty_stream_control` prompt returned correct values and a verified quote
  from that same authoritative contract in 62.01 seconds, but failed the frozen
  checker because it required the provider runbook specifically. Retain that
  formal failure; do not retroactively widen its labels. A future evaluation
  version should freeze acceptable equivalent authorities before running.
  Neither result establishes representative task quality or a new overall
  generated-answer pass rate; the earlier 7/8 baseline remains historical.
- Receipts, failed diagnostics, isolated Echo candidate, and exact dependency
  artifacts are under `/tmp/algo-cli-stream-and-filters.yEesW6/`; final Algo
  gates and installed checks are under `pinned-repair/delivery-final/` there.
  Current Nathan gates pass, M8 remains nine local passes/five external blocks,
  and M9 remains 29 verified/13 blocked. Other Echo consumers require separate
  installed qualification. Algo changes remain uncommitted. Nothing was pushed
  or tagged, no browser gate was relaxed, and the Class A goal remains open.

### Operator Review Failure and Concurrent Observation Repair

- The owner agreed to review one short run. The unchanged
  `code_repair_small_repo` fixture ran with `qwen3.6:35b-mlx`, a 360-second
  deadline, fresh isolated state, and the operator-owned Terminal reviewer.
  The frozen task digest was
  `aab3bd228c4f94a54318d897c0ba01e57350949259639361728e436b1406b473`;
  the runner digest was
  `d69cc316171c28760f2fb108d4099f45dea06699f66b7069020aa53e7a2a8ad0`.
  Both remained unchanged. Model warmup succeeded in 83.99 seconds outside
  scored time. The scored process stopped after 11.22 seconds with
  `review_protocol_error`, zero review requests, zero decisions, and no
  workspace changes. The checker retained its original one failure and three
  passes. This was a supervised workflow failure, not a user denial, coding
  success, or replacement for the historical unattended 0/4 baseline.
- Traced the first denial to simultaneous directory observations, before any
  shell review. `list_directory` omitted its path from policy target resolution,
  so different directories shared the working-directory target. Preflight also
  reused a one-use baseline grant across overlapping observations. After one
  observation consumed it, the other attempted a `confirmation_mode=none`
  review frame. The reviewer correctly rejected the frame and closed the
  channel; later shell requests could not reach the operator. Deterministic
  ordered-dispatch tests reproduced this exact zero-decision protocol failure.
- Bound directory listings to their requested path and aligned file policy
  resolution with execution's tilde expansion and literal surrounding spaces.
  Relative, absolute, and symlink targets outside the workspace no longer gain
  baseline directory authority. Every in-flight baseline observation now owns
  its own single-use grant, including reads of the same target. Consumed,
  revoked, or expired no-confirmation grants fail closed without prompting.
  The approval client also rejects non-reviewable modes locally without
  damaging the channel. Action-time approval, handoff, and reviewer validation
  remain unchanged; no automatic edit or shell retry was added.
- Added 26 regression cases; 24 exposed failures against the pre-repair source
  and two default-path controls already passed. The focused five-file suite
  passed 185 tests. The initial full run had one stale M9 artifact failure
  because its refreshed audit had been generated but not written before tests.
  That failed run is retained under `before-m9-refresh/`. After writing and
  verifying the audit, the full suite passed 4,717 tests with 32 skips and
  68.63% branch-aware coverage. Ruff lint, scoped formatting, default/CI typing,
  compilation, source/history/artifact scans, locked dependency auditing,
  wheel-from-sdist build, and Twine passed.
- Installed the qualified wheel and verified dependency compatibility, the
  unchanged exact Echo pin, and source parity for 294 files and 280 Python
  modules with digest
  `sha256:e3f07fb37671e955c4a05d316a3dab9f29efdf010dc70ae65560aaefadf33c51`.
  A disposable installed-Python-3.14.6 fixture verified independent one-use
  grants, path scope, and an invalid read followed by valid shell review with
  a synthetic denial. It made no model call and recorded no operator decision.
  Actual CLI refresh/embed/status/search/read/quit passed in 4.09 seconds;
  restart retained 788 vectors at 4,096 dimensions and all 222 capabilities.
  The installed required-Echo Astra stream canary passed with one identical
  request retry, no tools, and restored saved configuration.
- Refreshed O4/O5/O6/O7/O8 evidence passed 17/173/13/194/97 checks; the paired
  comparison completed 160 samples and six source-validated reports were
  installed. M8 remains nine local passes and five external blocks; M9 remains
  29 verified, 13 blocked, and zero failed requirements. ALGO.md and the
  supervised-review contract now record the concurrent-read grant invariant.
- The failed real run is retained under
  `/tmp/algo-cli-operator-coding.iLGXy3/`; repair qualification and installed
  receipts are under `/tmp/algo-cli-approval-grants.ngBK6W/`. No second operator
  run has been started yet; fresh availability was requested after the repair.
  Do not enter decisions on the owner's behalf. The next step is one freshly
  frozen supervised coding run, followed by checking actual test execution,
  changed files, and completion independently of a zero exit code. These local
  repairs do not establish representative task quality or Class A completion.
  Changes remain local and uncommitted; nothing was pushed or tagged.

### Checker Completion and False-Pass Repair

- While awaiting fresh availability for the supervised coding run, tested the
  independent grader rather than starting another agent. The prior checker
  incorrectly accepted zero-exit candidates that terminated during import,
  stopped after partial test execution, or printed a fabricated passing summary.
  A fourth regression showed that an exit hook could restore broken source
  after all tests passed. These were actual checker-process reproductions,
  not model benchmark results.
- Added a bounded completion observer and strict parent validation. Pytest
  must execute exactly the four frozen test IDs and every setup/call/teardown
  phase. Skips, xfails, duplicate or missing phases, malformed receipts, and
  early exits cannot qualify. Script checkers must return normally to pass;
  known failing fixture outcomes can qualify a baseline. Input digests must
  remain unchanged across evaluation, excluding existing generated-cache paths.
  A completed failing baseline is distinct from an interrupted checker or an
  already-passing fixture; neither unqualified state starts the agent or its
  reviewer. The runner retains structured completion evidence separately from
  stdout and binds the checker sources before and after each run.
- Advanced scoring to `algo-cli-cross-harness-v4-draft` without changing the
  four task fixtures. The publisher rejects older protocol results, incomplete
  baselines, and mismatched checker sources. Historical runs remain unchanged;
  there is no retroactive rescore or new cross-harness ranking. The qualified
  checker source digest is
  `5041c34e919f6a0adcabd9299377387ffb322b455d30c572c0cfb7fdcb5209a6`.
- The focused benchmark suite passed 87 tests. Thirteen standalone controls
  also passed using real checker subprocesses: all four pristine failing
  baselines, a valid repair, and eight early-exit, mutation, skip, or xfail
  cases. The controls made zero model calls and recorded no harness scores or
  operator decisions. Full qualification passed 4,770 tests with 32 skips and
  68.63% branch-aware coverage. Ruff lint, focused formatting, default/CI and
  benchmark typing, compilation, source/history/artifact scans, locked
  dependency auditing, wheel-from-sdist build, and Twine passed.
- Installed the qualified wheel. Package source parity remains 294 files and
  280 Python modules at
  `sha256:e3f07fb37671e955c4a05d316a3dab9f29efdf010dc70ae65560aaefadf33c51`;
  runtime Python was unchanged by this grader repair. The separately bundled
  ALGO.md matches the checkout at SHA-256
  `ce1b95fdf85ede8d19c226696693499c89d5c341871eaf63e0081f7e2ecf3253`.
  Actual CLI refresh/embed/status/search/read/quit passed in 4.24 seconds;
  restart retained 788 vectors at 4,096 dimensions and all 222 capabilities.
  Required-Echo Astra recovered from the injected empty response in 10.24
  seconds with one identical-request retry, no tools, and restored settings.
  The unchanged exact Echo pin and installed source audit passed.
- Refreshed O4/O5/O6/O7/O8 checks passed 17/173/13/194/97 tests; the paired
  comparison completed 160 samples and six source-validated reports were
  installed. The first supplementary check caught stale report hashes after
  the final benchmark-test edit; those checks are retained under
  `supplementary-before-pattern-refresh/`. Regenerated the evidence against
  the final test tree before revalidation. Current Nathan and M9 evidence
  verifies; M8 remains nine local
  passes and five external blocks, and M9 remains 29 verified/13 blocked.
  No browser qualification or representative coding performance is claimed.
- Receipts are under `/tmp/algo-cli-checker-completion.zyFGrk/`, including
  `grader-controls/summary.json` and `delivery-final/`. The observer is not an
  OS sandbox or independent attestation against compromised candidate code or
  a hostile host. Stronger evaluator isolation and representative task quality
  remain separate requirements. No fresh supervised run has started since the
  earlier zero-decision failure; do not approve actions on the owner's behalf.
  All Algo changes remain local and uncommitted; nothing was pushed or tagged.

### User Interruption and Effect Cleanup Repair

- Reproduced swallowed Ctrl-C in the canonical dispatcher for observations,
  local mutations, and external mutations. A one-shot regression demonstrated
  that the old path could invoke another tool, request another model round,
  and report a complete zero-exit run after interruption. The shell adapter
  also returned an ordinary error string after child cleanup instead of
  propagating cancellation. These were controlled runtime reproductions, not
  model coding results.
- Added a typed interruption carrying the finalized action result. Tool batches
  share a cancellation token; later actions receive noninvoked results and no
  later model round starts. Interrupted mutations retain unknown outcomes and
  their replay barriers. External effects do not start a postcondition verifier
  after an invocation interrupt; interruption within a verifier also preserves
  uncertainty. Effect leases and retry-capacity reservations are released.
  Shell cleanup now propagates Ctrl-C after stopping its child process group.
- Follow-up red tests caught incomplete cleanup after propagation: an Agent
  Block could remain running and an action program could lose its receipts.
  Blocks now become cancelled, provider histories remain balanced, and action
  programs persist available receipts before propagating interruption. Receipt
  storage failure cannot replace the interruption with an ordinary retryable
  failure. Three older pipeline test doubles needed the new cancellation
  argument; their approval assertions were preserved.
- Added 15 regression cases. The focused suite passed 444 tests with four
  skips, including an actual POSIX SIGINT and child-process stop check. Full
  qualification passed 4,785 tests with 32 skips and 68.70% branch-aware
  coverage. Ruff lint, focused formatting, default/CI and scoped typing,
  compilation, source/history/artifact scans, locked dependency auditing,
  wheel-from-sdist build, and Twine passed.
- Installed the qualified wheel and verified 294 source files and 280 Python
  modules at
  `sha256:066f3ec1f8b9b88b3e7827fe9eb69108a3986d173536f1ab5f7a8d3cd6de9ff8`.
  The unchanged exact Echo pin and installed dependency checks passed.
  Installed ALGO.md and the execution contract match the checkout; both now
  document interruption as control flow, uncertain-effect retention, and
  cooperative cancellation limits.
- Real SIGINT fixtures against installed Python 3.14.6 passed for both the
  shell runner and canonical dispatcher. Each exited 130 and stopped its
  child. The dispatcher retained one unknown attempt without automatic retry,
  released the effect lease, and released retry capacity. The first dispatcher
  fixture was denied before invocation because its private configuration still
  enabled safe mode. That failure and a diagnostic retry are retained; only
  the disposable fixture configuration was corrected. These tests used
  synthetic fixture authority, made zero model calls, and recorded zero
  operator decisions. The operator's approval policy was not changed.
- Actual CLI refresh/embed/status/search/read/quit passed in 7.22 seconds;
  restart retained 788 vectors at 4,096 dimensions and all 222 capabilities.
  The installed required-Echo Astra canary recovered from one injected empty
  response in 10.33 seconds with one identical-request retry, no tool calls,
  and restored saved settings. This is transport recovery, not permission to
  retry a cancelled action or an uncertain mutation.
- Refreshed O4/O5/O6/O7/O8 evidence passed 17/173/13/194/97 checks, with 160
  paired comparison samples. All six installed reports match current source
  bindings. Nathan's 17 correctness probes pass; M8 remains nine local passes
  and five external blocks, and M9 remains 29 verified/13 blocked with zero
  failed requirements. The prior 13 checker controls retain their unchanged
  checker-source binding; no new model benchmark score was recorded.
- Qualification receipts and retained failures are under
  `/tmp/algo-cli-interrupt-stop.7gLHq1/`, including `focused.xml`,
  `delivery-final/`, `installed-sigint-diagnostic/`,
  `installed-sigint-qualified/summary.json`, and `final-verification/`.
  Already-running arbitrary adapters are not forcibly preempted; collection
  can wait for them to return. Windows signal behavior and interrupts at every
  preflight, submission, or finalization instruction are not qualified by this
  pass. No fresh supervised coding run has started after the earlier
  zero-decision failure. All Algo changes remain local and uncommitted;
  nothing was pushed or tagged. This is runtime progress, not Class A completion.

### Completion Evidence Integrity Repair

- Eight initial regressions reproduced false completion: a real shell write
  followed by exit 1 left earlier reads fresh and no mutation pending; verifier
  commands in another checkout were credited to the active workspace; missing
  Git and non-repositories completed with manual warnings; an empty tracked
  diff qualified an untracked file containing structural problems.
- Dispatch now supplies the actual invocation bit to evidence recording.
  Invoked shell commands classified as mutating invalidate earlier reads and
  require later verification even on failure or unknown outcome. Noninvocations
  cannot supply mutation or verifier evidence. Uncertain retry barriers remain
  intact; a later passing workspace check does not authorize replaying a write.
- Verifier evidence now checks the executed workspace, leading directory
  changes, runner directory selectors, outside and symlinked targets, and
  explicit verifier-script paths. Normal interpreter selection, pytest report
  destinations, and valid root-scoped commands retain their supported behavior.
  Automatic Git fallback requires tracked coverage of outstanding known file
  mutations and refuses redirected Git state, unknown shell write sets, and
  unavailable evidence. Already-verified mutations do not block later checks.
- Added 35 cases and changed two legacy fallback expectations to match the
  existing fail-closed execution contract. The first 545-test focused run passed
  with four skips. Subsequent scope controls exposed an inherited Git redirect,
  outside script/symlink paths, and overly restrictive handling of valid options;
  their red and green results are retained. Final regressions passed 193 tests.
- Initial M8 qualification failed its focused slice. An exact diagnostic rerun
  found a missing transitive test-source binding and a one-shot fixture that
  patched a stale Config class after another test reloaded the module. The
  fixture now patches the active class, with a direct regression. The same
  preceding test order then passed 794 tests with 23 skips. No approval policy
  was relaxed, and the initial failed report remains retained.
- Full qualification passed 4,820 tests with 32 skips and 68.74% branch-aware
  coverage on Python 3.10.20. Ruff lint, focused formatting, default/CI and
  scoped typing, compilation, source/history/artifact scans, locked dependency
  auditing, wheel-from-sdist build, and Twine passed. Optional strict-typing
  debt remains separate from these passing gates.
- Installed the qualified wheel. Source parity passed for 294 files and 280
  Python modules at
  `sha256:c4db1f5f0d6c040e47871c22e521cbb1fd5e1115979d849fe5bc92b286927c2b`.
  Installed ALGO.md and the execution contract match the checkout. Their
  invocation/scope/coverage rules are source-bound by the refreshed reports.
  The unchanged exact Echo pin and installed dependency audit passed.
- Eleven installed-Python-3.14.6 completion assertions passed using disposable
  fixtures and actual shell/Git processes: partial writes stayed pending,
  outside checks executed without qualifying the write, an appropriate local
  assertion qualified the workspace, unknown retry barriers survived, and
  tracked/untracked controls behaved differently. Installed real-SIGINT tests
  also passed with child termination, lease release, and no automatic retry.
  These fixtures made zero model calls and recorded zero operator decisions.
- Real CLI refresh/embed/status/search/read/quit passed; restart retained all
  788 vectors at 4,096 dimensions and all 222 capabilities. Required-Echo Astra
  recovered from the injected empty response in 10.25 seconds with one
  identical-request retry, no tools, and restored settings. O4/O5/O6/O7/O8
  evidence passed 17/173/13/194/97 checks; 160 paired samples and all six
  installed reports were revalidated against current sources.
- Nathan retains 17 passing correctness probes. Refreshed M8 has nine local
  passes, five external blocks, and zero local failures; M9 has 29 verified,
  13 blocked, and zero failed requirements. Receipts and retained failures are
  under `/tmp/algo-cli-completion-integrity.4vzEzW/`, including
  `initial-qualification/`, `m8-triage.log`, `test-order-fixed.xml`,
  `delivery-final/`, `installed-completion/`, and `final-verification/`.
- Command recognition is not proof of test relevance, arbitrary script
  internals, complete test execution, or independent task correctness. General
  semantic coverage and representative supervised coding remain open; no new
  operator run or competitive score was recorded. Changes remain local and
  uncommitted, with no push or tag. The full Class A goal remains open.

### Retrieval Miss Diagnosis and Alias Repair

- Rebuilt an isolated 790-record source-tree public index, refreshing changed
  document vectors against the same local Qwen model digest. Kept the frozen
  57 positive questions, separate exclusion, five-result limit, and saved
  query vectors. Current tool search/read passed 51/57 questions. This is
  public development diagnosis, not held-out, live-latency, generated-answer,
  Echo-memory, or installed-corpus qualification.
- All six previous misses remain. The memory-authority contract ranks 33rd
  by vector and 146th by keyword. Spanish privacy ranks sixth by vector with
  no lexical match; German privacy ranks third by vector but 76th by keyword
  and is lost in fusion/selection. Natural multi-file tool discovery ranks
  write/edit/diff capabilities 34th/23rd/fourth by vector. The crash contract
  ranks 50th and the capability authority contract 61st. These are distinct
  representation, multi-result discovery, and fusion problems; increasing
  the output budget or changing the expected sources is not a repair.
- Found and repaired a separate operational bug: keyword and hybrid ranking
  resolved `openclaude`, `claude-code`, and `codex-cli`, but the final tool
  output filter compared the unresolved alias literally and discarded valid
  results. The filter now uses the shared alias resolver while retaining the
  authorized snapshot and requested kind. No external source is enabled by
  an alias. Twenty-five new controls cover both transports, direct names,
  aliases, empty scopes, and an out-of-scope ranker result. After correcting
  an invalid synthetic fixture setting, 13 cases reproduced the real bug;
  all 25 now pass. The focused suite passed 408 tests with four skips.
- The older ad hoc transport evaluator dropped IDs containing spaces, such
  as `/harness patterns`. The new diagnostic checks complete parsed IDs
  against the actual ranker output. On this current 57-question snapshot,
  that parser error inflated MRR from 0.709649 to 0.711111 without changing
  pass counts. Earlier artifacts remain historical; this measurement
  correction is not a semantic gain.
- Rejected an additional ALGO.md paragraph after measuring a regression:
  `stale_pattern` became a seventh miss, lowering the result to 50/57.
  Retained its exact document bytes, index, and failed report. Removing only
  that new paragraph restored 51/57 with every top-five list identical to
  the refreshed baseline. ALGO.md is byte-identical to the preceding
  qualified version. Keep this finding in dated evidence rather than
  diluting an otherwise focused catalog contract.
- Full delivery passed 4,845 tests with 32 skips and 68.75% branch-aware
  coverage on Python 3.10.20. Ruff, focused formatting, default/CI/scoped
  typing, compilation, source/history/artifact scans, locked dependency
  audit, wheel-from-sdist build, and Twine passed. Installed source parity
  passed for 294 files and 280 Python modules at
  `sha256:8ece55177d0469e25640ae375ad3f0d112020dd4c890d58b6d59d10c7c5c8fb9`.
  The exact Echo dependency audit passed unchanged.
- Installed synthetic alias checks passed 21 cases without model calls or
  operator decisions. Completion, real-SIGINT, and authority fixtures also
  passed. The real CLI refresh/embed/status/search/read/quit and fresh-process
  checks retained 788 vectors at 4,096 dimensions and all 222 capabilities.
  Required-Echo Astra recovered from an injected empty stream in 11.31
  seconds with one identical-request retry, no tools, and restored settings.
- O4/O5/O6/O7/O8 evidence passed 17/198/13/219/122 checks; 160 paired samples
  and all six installed reports remain source-valid. Nathan retains 17
  passing correctness probes; M8 retains nine local passes, five external
  blocks, and no failures; M9 remains 29 verified, 13 blocked, and zero failed.
  Receipts, raw ranks, rejected catalog bytes, and final verification are at
  `/tmp/algo-cli-retrieval-misses.3XAwVg/`. No new supervised coding run,
  competitive score, commit, push, or tag was recorded. Semantic quality and
  the full Class A goal remain open.

### Passage Retrieval, Installed Answers, and Context Cost

- Froze 14 additional public development questions before inspecting their
  rankings, extending the unchanged 57-question set to 71. Preserved the
  separate exclusion, five-result budget, two repeated search/read rankings,
  exact local Qwen digest, public-source projection, and source freshness.
  The protocol is
  `sha256:7016ca51a7b1967880090a759d1fc7ecc0882d2a93b7a8e475c4459b84362ef5`.
  These are authored development questions, not independent held-out evidence.
- An isolated prototype embedded bounded Markdown passages from actual source
  bodies while retaining canonical vectors and the production ranker. Eighty-five
  document passages improved the existing set from 51/57 to 54/57 and the new
  set from 8/14 to 12/14: 59/71 to 66/71 combined, with eight gains and one
  regression. `stale_pattern` lost its required source, so this variant was not
  promoted. Capability-purpose vectors added no case-level benefit. Expanding
  pattern passages also retained the regression while adding substantial index
  storage; that variant was not promoted either.
- The regressed O5 record remains inside the vector candidate pool. Other
  document scores and subsequent fusion/selection change its final position;
  this is not simply candidate truncation. Increasing the candidate multiplier
  from three to six or twelve produced 64/71 or 57/71 respectively, with more
  regressions. Retain those failed comparisons. Multi-capability discovery,
  capability authority, and source-freshness selection remain open; do not
  inflate the output budget, add query-specific aliases, or change labels to
  turn these failures into passes. ALGO.md and the installed ranker are unchanged.
- Repeated the existing eight-task installed Astra generated-answer protocol
  without changing questions, answer fields, required source groups, or grading.
  The protocol remains
  `sha256:db0ef8516bd12a1ab44ba63d37af0e1e69d0e1a6fce716efed1a2d627d336def`.
  It uses `gpt-6-astra`, low reasoning, required Echo, ordinary tool discovery,
  and only read-only harness search/read actions. The installed public index
  contains 788 records; the isolated source-tree retrieval corpus contains 790.
  Do not conflate those corpora or rescore single-query retrieval using a
  generated multi-search workflow with reformulations and kind filters.
- Formal generated-answer result: 7/8. All six original single-search misses
  passed this workflow. All eight final value sets were correct and their
  supporting quotes were checked against actually read source bodies. The
  `empty_stream_control` case still fails its frozen source-identity gate: it
  read and quoted the execution contract, which explicitly states two retries
  and no replay of completed tools, instead of the required provider-recovery
  guide. Keep that formal failure; it is not evidence of an incorrect answer
  or authorization bypass. Future equivalent-source groups must be frozen
  before the next run, not added retrospectively to improve this score.
- The eight runs took 531.47 seconds total, with a 67.445-second median, 36
  model rounds, and 175,496 reported tokens (173,013 prompt; 2,483 completion).
  Context construction accounted for 313.874 seconds and provider calls for
  189.130 seconds. No guard blocks, tool denials, process failures, or deadline
  expirations occurred. Settings and source identity were restored/retained.
  One repetition of this narrow protocol is not representative coding,
  competitor, independent memory-quality, or universal harness evidence.
- Profiled three additional installed context builds with required Echo and
  no generation. They took 9.77-9.90 seconds under instrumentation. Each made
  a fresh local embedding request; roughly 2.8 seconds was model loading and
  7.8 seconds was the embedding request overall. Profiled MMR selection also
  used approximately 117,784 cosine calls and 1.4 seconds per build. These are
  measurements of this query and live corpus, not an isolated algorithm
  benchmark. The report's saved settings describe the unchanged operator
  configuration; effective in-process settings were Astra and required Echo.
- A separate, predeclared seven-build experiment compared the current zero
  keep-alive with an in-memory 30-second candidate. Two cold baseline builds
  had a 10.335-second median; two warm candidate builds had a 5.839-second
  median. Cold candidate cost did not improve. Every build performed fresh
  protected recall, model-identity checks, record-integrity checks, and an
  embedding call. Ollama reported 8.43 GB of resident model size with zero GPU
  allocation, then no resident target model after a 35-second expiry wait.
  No persistent setting, default, GPU allocation, or memory-result cache was
  changed. Local-generation contention and near-threshold gating remain untested.
- Exact protected-output equality failed in five of those seven observations,
  including an unchanged baseline. A six-build diagnostic retained only scalar
  comparisons: all three returned record identities, order, payloads, and gates
  stayed equal, while repeated embeddings varied slightly (cosine agreement
  approximately 0.99935 or better; maximum relevance-score change 0.001205).
  Rendered score changes explain the observed string differences in that
  diagnostic. The original equality failures remain recorded, and the live
  protected corpus was not frozen. Do not infer gate stability near thresholds
  or general output equivalence from this one query. No private payloads,
  private identifiers, vectors, or their hashes were written by these profilers.
- Found a separate context-accounting bug at `main._round_context_sources`:
  when Echo owns memory, `memory_tokens` is forced to zero even though protected
  memory may be present in the request. Its estimated size is charged to
  `policy_and_runtime`. The total context timing above is still measured, but
  the zero memory category is not evidence that no memory was sent. Next repair:
  carry content-free counts from the actual rendered sections into telemetry,
  without another recall, private-content logging, or altered admission policy.
  This telemetry bug is diagnosed, not fixed in this iteration.
- Current installed/source parity still passes for 294 files and 280 Python
  modules at
  `sha256:8ece55177d0469e25640ae375ad3f0d112020dd4c890d58b6d59d10c7c5c8fb9`.
  All six installed pattern reports remain source-valid; Nathan and M9 report
  verification pass. M8 still has nine local passes and five external blocks.
  The preceding full gate remains 4,845 passed and 32 skipped; it was not rerun
  for these isolated experiments and this plan-only edit. No new runtime was
  installed, no supervised coding run or operator decision was recorded, and
  nothing was committed, pushed, or tagged. The overall goal remains active.
  Protocols, raw results, rejected variants, scalar-only profiling, and current
  source verification are under `/tmp/algo-cli-passage-retrieval.3TNUi7/`, including
  `answers-detail/`, `keepalive-check/`, `context-variation/`, and
  `final-verification/summary.json`.

### Rendered-Request Accounting Repair

- Reproduced the protected-memory accounting bug through the real agent loop
  with a deterministic provider fixture: its rendered memory section exceeded
  170 estimated tokens while the recorded memory category remained zero.
  The same telemetry path rebuilt identity text, counted whole optional blocks
  even when truncated, counted injected context again as conversation, and
  retained tool-schema cost during a tool-free finalization request.
- System-prompt assembly and optional-context fitting now optionally return
  numeric source counts from the text they actually render. No recalled text,
  identifiers, vectors, or payload hashes enter those counters. Counts are
  cleared on each build, including a compaction rebuild, so absent memory and
  omitted blocks cannot inherit stale estimates. Duplicate block names accumulate
  the actual admitted text, including separators and truncation markers.
  The original prompt strings, fitting order, and conservative admission budget
  are unchanged. Accounting never requests another memory or identity read.
- Model-round receipts carry `context_accounting_version: 2`. Categories
  partition the existing request-token estimate, including system-message
  framing and the schemas actually supplied to that request. Optional text is
  removed from the conversation subtotal and attributed once to its source.
  The memory category includes the rendered memory section's boundary metadata
  and applicable optional memory, not only recalled payloads. Tool outputs stay
  in `tool_results`: a shell or Git command name does not make the output a
  verification receipt. This path has no separately rendered receipt segment,
  so its `verification_receipts` count is zero, not a statement that no
  verification occurred. Estimates remain heuristic, not provider tokenization,
  billing counts, authority, or independent task-verification evidence.
- Added 25 controls and expanded the existing read-finalization test. Tests
  cover required/legacy modes, interactive/one-shot rendering, empty memory,
  numeric-only metadata, repeated names, truncation, clearing after compaction,
  no redundant reads, request-total partitioning, finalization schemas, and
  privacy allowlisting. The original separator-budget cases already passed;
  no truncation-admission defect or change is claimed. The initial red slice
  retained 14 failures, including the agent-loop zero-memory reproduction and
  missing optional count APIs. The final focused slice passed 183 tests.
- Full qualification passed 4,870 tests with 32 skips and 68.82% branch-aware
  coverage on Python 3.10.20. Ruff, focused formatting, default/CI/scoped typing,
  compilation, source/history/artifact scans, locked dependency auditing,
  wheel-from-sdist build, and Twine passed. The separately recorded optional
  strict-typing backlog was not relabeled as passing.
- Installed the qualified wheel and checked parity for 294 files and 280 Python
  modules at
  `sha256:1157c5a638e924d7e668ade843934cefdbc5621ed33d5802d632b792ef637977`.
  Installed Python 3.14.6 produced byte-identical text versus the retained
  pre-repair renderer across eight public prompt fixtures and 180 optional-fit
  fixtures. All source counts matched their actual admitted text. These fixtures
  made no model call and recorded no operator decision.
- The unchanged installed Astra `memory_authority` task passed in 54.60 seconds
  with three model rounds, two search/read actions, an exact canonical-source
  quote, and restored settings. Each request reported 1,369 estimated memory
  section tokens, and source totals matched the independently counted assembled
  requests: 4,626, 5,091, and 6,676 estimated tokens. Provider-reported usage was
  13,357 total tokens across the three rounds; that is a different measurement.
  Every round used accounting version 2, and instrumentation confirmed no
  additional recall for accounting. This is one required-Echo task, not a new
  eight-task score, speedup claim, or representative memory-quality benchmark.
- Installed alias, completion, real-SIGINT, and synthetic authority checks
  passed. The real CLI and fresh-process restart retained 788 vectors at 4,096
  dimensions and all 222 capabilities. The required-Echo empty-stream canary
  passed with one identical-request retry, no tools, and restored settings.
  O4/O5/O6/O7/O8 reports passed 17/198/13/219/122 checks; the comparison retained
  160 paired samples and all six installed reports are source-current. ALGO.md
  is unchanged. Nathan retains 17 passing correctness probes, M8 retains nine
  local passes and five external blocks, and M9 retains 29 verified, 13 blocked,
  and zero failed requirements.
- Receipts and the exact pre-repair renderer are retained under
  `/tmp/algo-cli-context-accounting.daRH98/`, including `red.xml`,
  `delivery-final/`, `installed-context-checks.json`,
  `installed-answer-accounting.json`, and `final-verification/summary.json`.
  Open work remains representative retrieval and coding quality, protected
  context correctness across continuation/compaction, resource-controlled
  context acceleration, and same-model comparative evaluation. No model
  keep-alive default, approval rule, memory authority, or M8 external gate was
  changed. No new supervised run, commit, push, or tag was made; the full goal
  remains active. Already-running CLI processes need a restart to load the
  installed repair.

### Active-Request Context Repair

- Reproduced three distinct continuation failures: memory recall followed a
  synthetic user-role control instead of the active request; optional context
  replaced the recovery control; and compaction could remove the active request
  entirely. Fixing recall alone left six failing integration cases, separating
  request assembly from recall selection. Older identical user text and
  user-written control-prefix lookalikes are not treated as runtime controls.
- The chat loop now passes its original request explicitly to required and
  optional Echo recall. `None` retains the standalone helper's history fallback;
  an explicit empty request does not silently select a later control message.
  Required closed-form tasks still use doctor-backed preflight, and required
  recall failures still stop model execution without plaintext fallback.
- The loop retains the active message by object identity. Optional context is
  applied only to its provider-facing copy. If real compaction drops that
  message, the provider request restores it ahead of retained history, charges
  its full estimated message cost before optional admission, and leaves runtime
  controls, tool boundaries, and stored history intact. This restores active
  intent, not the entire compressed conversation or an excluded protected
  summary. No new persistent control metadata or memory cache was introduced.
- Added 20 regression controls and extended the existing post-mutation
  completion-gate test. The expanded slice passed 339 tests with two skips.
  Tests cover optional/required Echo, interactive/one-shot builders, exact and
  empty queries, real compaction, duplicate text, denied actions, tool-free
  finalization, request restoration cost, and numeric request partitioning.
  Focused Ruff, formatting, and two-module mypy passed. ALGO.md A11 now states
  the active-request preservation contract and its limits.
- Refreshed O4/O5/O6/O7/O8 evidence passed 17/198/13/219/122 checks and retained
  160 paired samples; all six reports remain source-bound. Nathan retains 17
  passing correctness probes and all deterministic gates. M8 retains nine
  local passes and five external blocks; M9 retains 29 verified, 13 blocked,
  and zero failed requirements. These are local checks, not new external
  browser, representative model-quality, or independent-review evidence.
- The first full gate caught a bookkeeping error in this pass's new HARD-080
  row: it was labeled `benchmark` instead of the required `qualification`.
  That run retained 4,889 passes, 32 skips, and one M9 failure. Corrected only
  that new row, regenerated M9, and passed its exact-report regression.
  The failed log, XML, and coverage remain under
  `/tmp/algo-cli-active-request.5bDweB/delivery-final/failed-ledger-entry/`.
- The corrected full gate passed 4,890 tests with 32 skips and 68.83%
  branch-aware coverage on Python 3.10.20. Ruff, default/CI/scoped typing,
  focused formatting, compilation, public source/history/artifact scans,
  release-version checks, locked dependency auditing, wheel-from-sdist build,
  and Twine passed. The optional strict-typing backlog remains separate.
- Installed the qualified wheel and verified 294 source files and 280 Python
  modules at
  `sha256:51cfef93b1b0c3ef1b11389c014b8f0e26751e5d05894c732d9ea54881de4a41`.
  The packaged ALGO.md matches the updated A11 source. Installed Python 3.14.6
  passed 20 isolated active-request controls with zero real model calls and
  zero operator decisions. Eight fixed-section prompt fixtures and 180 fitting
  fixtures remained byte-identical to the retained pre-repair implementation;
  memory selection is stubbed only in those eight comparison fixtures and
  is qualified separately by the active-request controls.
- The existing required-Echo Astra `memory_authority` task passed in 45.19
  seconds with three rounds, two public search/read actions, and an exact
  canonical-source quote. Each recall query matched the original task and
  accounting caused no extra recall. Rendered memory estimates were 1,369
  tokens per round; source totals matched assembled request estimates of
  4,626, 5,079, and 6,650. Provider-reported usage was 13,311 tokens, a different
  measure. Settings were restored. This is one existing read-only development
  task, not a representative quality score or a controlled speedup comparison.
- Installed alias, completion, real-SIGINT, and synthetic authority checks
  passed. Real CLI startup and fresh-process restart retained 788 vectors at
  4,096 dimensions and all 222 capabilities. The required-Echo empty-stream
  canary passed in 9.98 seconds with one identical-request retry, no tools,
  and restored settings. Final verification rechecked installed/source parity,
  packaged ALGO.md, six source-current pattern reports, Nathan, M8, and M9.
- Receipts, pre-repair source snapshots, red tests, and final validation remain
  under `/tmp/algo-cli-active-request.5bDweB/`, including
  `installed-active-request-checks.json`, `installed-answer-accounting.json`,
  and `final-verification/summary.json`. Representative retrieval/coding
  quality, resource-controlled context acceleration, and same-model comparative
  evaluation remain open. No approval rule, Echo authority, or M8 external gate
  was weakened. No new supervised coding run, operator approval, memory write,
  commit, push, or tag was performed. The broad goal remains active; existing
  CLI processes need a restart to load the installed repair.

### MMR Pair-Reuse Dependency Qualification

- The exact pinned Echo ranker repeatedly compared every remaining candidate
  with the entire selected prefix. At 128 distinct-topic candidates this made
  349,504 cosine calls for 8,128 unique pairs. The isolated candidate maintains
  each remaining maximum only within the current ranking call, preserving the
  original weight, current-record priority, effective-time and input-order
  ties, case-folded topic exclusions, and first-value/max behavior. It changes
  neither protected-memory policy nor cross-call caching.
- Added 43 differential, malformed-vector, freshness, and pair-count cases.
  Three count controls failed the old ranker; all new tests pass the repair.
  Full Echo suites passed 807 tests with ten skips on each of Python 3.10-3.14.
  The inherited profile override reproduced five fixture failures against the
  unchanged pin; removing it from test processes, not runtime configuration,
  gave the unchanged baseline 764 passes and ten skips. No assertion was relaxed.
- With explicit owner approval, committed only the ranker, regression tests,
  changelog, and source-evidence binding on local Echo branch
  `codex/mmr-pair-reuse`: `cbee525687ac03c830d4b6632ff1d044b4b838fc`, directly
  atop the qualified SQLite repair. Updated Algo's exact dependency, runtime
  identity, audit, test, and lock pins afterward. The lockfile changed only
  Echo's revision. Canonical Git installation used a command-local URL mirror;
  no global Git setting or PEP 610 metadata was rewritten.
- Nine frozen public-vector groups used 32/64/128 candidates, unique/mixed/same
  topics, 1,024 dimensions, and six timing samples per variant. Every complete
  rank order matched. At 128 unique topics the median ranker time changed from
  2.5418 seconds to 0.0615 seconds, with 349,504 versus 8,128 cosine calls.
  These are synthetic ranker-only timings, not end-to-end recall, model quality,
  or competitive superiority. ALGO.md A3 records the optimization boundaries.
- Echo's locked lint, format, typing, security/privacy/history, metadata, build,
  archive-content/source parity, and Twine checks passed. The full release
  archive check rejected the changed wheel against OpenClaw's older deployment
  lock as expected; that external deployment lock was not updated or bypassed.
  All existing host release-pending states and exclusions remain unchanged.
- Algo passed 4,890 tests with 32 skips and 68.84% branch-aware coverage, Ruff,
  default/CI typing, compilation, source/history/artifact scans, the dependency
  audit, wheel-from-sdist build, and Twine. Whole-file formatting still requests
  changes in two previously unformatted pin-bearing files; the same failures
  occur with the pre-edit pins and are not reported as passing. O4/O5/O6/O7/O8
  passed 17/198/13/219/122 checks, with 160 paired samples and six installed
  source-bound reports. M8 remains nine local passes/five external blocks;
  M9 remains 29 verified/13 blocked, with zero failed requirements.
- Installed Algo source parity passed for 294 files and 280 Python modules,
  digest `sha256:c462780eb9b1e6d661d24cbd00821ef054330343cf2af2218e7d587411b671b6`.
  The exact installed Echo audit verified 46 Python sources against
  `fe94fbba3686ca44c2cb0274a8a4baaeedbb85c04e261c6f3d240e9e77f934f0`.
  Python 3.14 replayed all nine frozen ranking fixtures with identical order and
  unique-pair counts. DELETE/WAL writer exclusion, four-worker stress with all
  120 public records retained, active-request/context controls, aliases,
  completion, real interruption, and synthetic authority checks passed.
- Actual CLI refresh/search/read/quit passed; a fresh process retained 788
  4,096-dimensional vectors and all 222 runtime capabilities. Required-Echo
  Astra recovered from an injected empty completion in 11.62 seconds with one
  identical-request retry, no tools/errors, and restored configuration. The
  unchanged public `memory_authority` task passed in 42.81 seconds over three
  rounds, retaining original recall queries, canonical read/quote evidence,
  and exact request-accounting partitions without another recall. This is one
  bounded development check, not a representative quality score.
- Evidence and retained failed diagnostics are under
  `/tmp/algo-cli-mmr.MuAzOQ/`. Only the focused Echo commit was made; Algo's
  existing dirty work and updated pins remain uncommitted. Nothing was pushed
  or tagged, and a fresh public clone cannot fetch this local-only revision.
  Other Echo consumers need separate qualification. No new operator approval,
  supervised coding run, automatic memory capture, or relaxed external gate was
  introduced. The broad goal remains active; restart existing Algo processes
  to load the qualified installed dependency.

### Requested Embedding Dimension Binding

- Reproduced a real failure in the installed runtime using disposable public
  fixtures: changing the requested width from two to three left readiness true,
  triggered no rebuild, and reused a warm query's old vector. A fresh query then
  lost semantic retrieval. Model-name-only matching was insufficient both for
  index coverage and for the query cache.
- Bound runtime indexing, incremental progress, status, tool/slash dispatch,
  retrieval eligibility, and query caching to the requested dimension mode.
  Explicit `None` means a model-default request and differs from an explicit
  numeric width, even when their actual widths coincide. Legacy records with no
  request metadata remain unqualified for a configured runtime request. Low-level
  callers omitting the new argument retain their legacy unbound semantics.
- Refresh, Rust grafting, generated capabilities, pattern projections, and
  checked shipped contracts preserve request metadata only with eligible source
  reuse. Partial rebuilds resume matching batches; incompatible vectors remain
  outside semantic ranking, and public keyword fallback retains source filters.
  Wrong-width provider batches cannot mark records ready. A4/A5 in ALGO.md now
  describe these contracts and the current WindowTinyLFU cache policy.
- Added 41 dimension regressions and two capability-metadata parameter cases.
  The initial new suite failed the old implementation, including both actual
  automatic and slash rebuild paths. Existing protected-source fixtures were
  updated to represent explicitly bound model-default vectors. A broader gate
  exposed three old no-config test doubles; these now assert runtime config
  forwarding without weakening their original output or policy assertions.
  The dimension and recovery tests are included in source-bound qualifications.
- Real local Qwen transitions 1024 -> 2048 -> default -> explicit 4096 -> default
  passed in both source and installed runtimes. Each mode rebuilt three public
  documents, used one query call for cold/warm repeats, and survived refresh and
  reload. The unchanged model digest was
  `64b933495768fbd3b87c20583d379728a07471e0c66733a9df87cd1901b3c44b`.
  No operator setting or Echo embedding profile was changed.
- A frozen historical public-vector replay retained all 71 exact rank lists,
  scores, and rank-source sets, with the existing 59/71 evidence-ID result.
  Its first setup lacked the checkout's `repository-tests` capability in the
  archived module; supplying the same explicit context to both versions resolved
  that mismatch. Both diagnostics are retained. This is differential regression
  evidence, not current-source relevance, answer quality, or competitive proof.
- Full Algo qualification passed 4,933 tests with 32 skips and 68.92% branch-aware
  coverage, Ruff lint, default/CI typing, compilation, source/history/artifact
  scans, dependency audit, wheel-from-sdist build, and Twine. Nine scoped files
  passed formatting; existing whole-file formatting debt, including the prior
  harness.py findings, was not relabeled as passing or refactored away.
  O4/O5/O6/O7/O8 passed 17/198/13/219/122 checks, with 160 paired samples and six
  source-bound installed reports. M8 remains nine local passes/five external
  blocks; M9 remains 29 verified/13 blocked, with zero failed requirements.
- Installed parity verified 294 files and 280 Python modules at
  `sha256:6586ae7bb55aac88368fcc0522aa4a786fba5bec058a8e1faf8691e1b020e8a2`.
  Echo's exact `cbee525687ac03c830d4b6632ff1d044b4b838fc` pin and 46 Python
  sources passed their audit unchanged. Eleven installed checks passed. The CLI
  completed its one-time migration in about 120 seconds; restart retained all
  788 vectors at model-default 4096 dimensions and all 222 current capabilities.
  The required-Echo Astra retry canary passed in 10.78 seconds with one identical
  retry and no tools. The unchanged public memory-authority task passed in 41.08
  seconds over three rounds with exact request accounting and no extra recall.
- Evidence is under `/tmp/algo-cli-embedding-dimensions.ccZMGz/`. Same-tag model
  artifact changes, provider endpoint identity, the frozen retrieval misses,
  supervised coding quality, and external M8 qualification remain separate open
  work. No new coding approval, memory write, commit, push, or tag was performed;
  the previously authorized Echo commit remains local and Algo's accumulated
  changes remain uncommitted. The broad goal remains active. Restart existing
  Algo processes to load this installed build.

### Passage-Ranking Development Study

- Refreshed and embedded a disposable 790-record current-source public index
  with explicit model-default dimensions and the unchanged local Qwen artifact.
  No protected-memory payloads were copied into the study. The existing 71
  positive cases and one exclusion retained their queries and expected IDs.
  A versioned protocol rebased only A4's obsolete heading anchor after the
  preceding cache-title correction; the original failed preflight is retained.
- With production source filtering, fusion, search/read tools, and a top-five
  budget, the refreshed baseline passed 58/71. A half-weight passage blend
  passed 63/71 and maximum-passage scoring passed 66/71. Both lost the existing
  `stale_pattern` case, so neither met the frozen zero-regression acceptance
  rule. The exclusion passed in every arm. These are development retrieval-ID
  checks using frozen query vectors, not held-out or generated-answer quality.
- A four-arm follow-up added source-verified passages for long algorithm
  entries. It embedded 238 additional passages, for 333 total, without changing
  the parent record identities or their authority. Balanced/max scoring still
  passed 63/71 and 66/71 with the same regression. No passage candidate was
  accepted, installed, or added to the production ranker.
- The candidate-window hypothesis was disproved: O5's vector rank moved from
  seven to nine/ten, remaining inside the fifteen-result component window.
  O5 and H2 initially had equal reciprocal-rank sums; changes in other records'
  vector ranks broke that tie. This diagnostic does not establish a production
  cutoff bug, and no rank-window workaround or case-specific override was added.
- Corrected a separate factual error in ALGO.md A4: the cache exposes the
  instance methods `clear()` and `snapshot()`, not the previously named
  module-level reset/stats helpers. Production Python sources are unchanged.
  Study protocols, public vectors, failed diagnostics, and delivery receipts
  are retained under `/tmp/algo-cli-passage-quality.mnCOzc/`.

### Shipped-Resource Parity Qualification

- The A4 documentation-only build exposed a real checker blind spot: the
  installed-source digest remained unchanged because it covered only files
  physically inside `algo_cli/`, not the wheel's declared `force-include`
  resources. Its previous 294-file pass did not bind shipped ALGO.md or skills.
- The checker now reads the existing wheel mapping with the TOML parser and
  compares declared files and directory trees at their installed paths. Those
  resources participate in missing/divergent checks and both content digests.
  Invalid configuration, path escapes, linked or unavailable sources, and
  conflicting destinations fail closed. The existing exception for undeclared
  generated non-Python extras is unchanged and is stated in ALGO.md O3.
- Added 18 regressions; all 23 focused tests pass on the repair. The first 15
  failed against the original checker, including false passes for missing and
  stale documents and changed skills. The installed negative control now
  correctly rejects the older catalog at `resources/docs/ALGO.md` and counts
  320 expected files instead of 294. Source-package Python contents are unchanged.
- The initial documentation delivery and its eleven installed checks are
  retained as limited evidence, not retroactively labeled resource-qualified.
  Fresh repair qualification and the real negative control are recorded under
  `/tmp/algo-cli-passage-quality.mnCOzc/parity-repair/`.
- Full qualification passed 4,951 tests with 32 skips and 68.92% branch-aware
  coverage, Ruff, default/CI typing, compilation, public source/history/artifact
  scans, dependency audit, wheel-from-sdist build, and Twine. The two changed
  checker/test files pass formatting; the checker also passes focused typing.
  Existing unrelated whole-file formatting debt was not relabeled as passing.
- After reinstall, expanded parity verified 320 files and 280 Python modules
  at `sha256:0e1b1bb8b8f7b5d7838d3846462ed7c4c6da03938372e2eabbb442e43b201096`.
  Echo's exact local `cbee525687ac03c830d4b6632ff1d044b4b838fc` revision and all
  46 Python sources passed their audit. The commit is retained in the canonical
  Echo repository's Git object store; its candidate worktree remains clean.
- All eleven installed checks passed. Restart retained 788 model-default
  4,096-dimensional vectors and all 222 capabilities. Required-Echo Astra
  recovered the injected empty completion in 9.26 seconds using one identical
  request retry and no tools. The unchanged public memory-authority task passed
  in 39.84 seconds over three rounds, with canonical source quotes, exact
  request accounting, and no extra recall. An additional installed source read
  verified the corrected cache API and shipped-resource parity contracts.
- O4/O5/O6/O7/O8 passed 17/198/13/219/122 checks, with 160 paired samples and
  six refreshed source-bound installed reports. M8 remains nine local passes
  and five external blocks; M9 remains 29 verified and 13 blocked, with no failed
  requirements. These local results do not qualify production browsers,
  representative model quality, or competitive superiority.
- The requested Echo local commit and exact pin remain in place. Algo's
  accumulated changes, including this checker repair, remain uncommitted.
  Nothing was pushed or tagged; a fresh public clone cannot fetch the local-only
  Echo revision yet. No protection, approval, automatic capture, or operator
  embedding setting changed. Passage candidates remain unshipped. The broad
  goal remains active, and another supervised coding benchmark still needs
  fresh operator readiness.

### Provider-Bound Embeddings and Gateway Routing

- Confirmed that the embedding gateway could return vectors from a different
  upstream than the configured Ollama endpoint. Model-name/dimension checks also
  reused document and query vectors after an equal-width provider or artifact
  change. Red controls against the archived implementations remain preserved.
- Added an observed endpoint/model-digest identity for configured public harness
  requests. Embedding batches validate it before and after provider calls; query
  caches, shared per-turn memo hits, readiness, progress, status, and retrieval
  now use the same binding. Changed or unavailable identities cannot reuse old
  vectors. Completed batches retain their truthful identity for later rebuilds.
- The versioned gateway endpoint rejects an upstream mismatch before forwarding
  input. Python requires the matching response binding and falls back to the
  selected direct SDK endpoint for old or mismatched gateways. Direct embedding
  requests disable ambient proxies and redirects. Existing gateway deployments
  were not replaced; actual old/new Go processes verified compatibility locally.
- Follow-up controls caught stale vector contributions after ranking, premature
  readiness after a progress callback, a per-turn memo validation gap, and an
  uncaught truncated-metadata response. The public tool wrapper also still
  probed metadata for empty filtered searches. Three public-API cases reproduced
  that last issue after the first full qualification; the final guard performs
  no provider work for an empty index or unmatched harness/kind slice.
- Added 48 Python regressions and Go upstream-binding controls. The final
  focused wrapper/retrieval pass had 359 passes and four skips. Nine live checks
  used real Go processes and the actual SDK with disposable public HTTP
  providers, plus a stable real-Qwen batch. Proxy/redirect targets received no
  input, and the empty-filter check observed zero provider requests.
- A frozen public development replay preserved complete ranked results and
  provenance, except the added identity field, across 144 cold/warm comparisons.
  Its initial failure came from loading the baseline module under a different
  package location, changing source-root eligibility. Aligning the package
  location fixed the fixture without changing queries, vectors, expected output
  equality, or production ranking. This is not a new semantic-quality score.
- Final local qualification passed 4,999 tests with 32 skips and 69.00%
  branch-aware coverage, Go race/vet/build checks, Ruff, default/CI typing,
  compilation, source/history/artifact scans, the locked dependency audit,
  wheel-from-sdist build, and Twine. Go formatting and the new Python module/test
  formatting checks passed; no repository-wide formatter pass is claimed.
- Installed-source parity verified 321 files and 281 Python modules at
  `sha256:2b535d55893e965cd797f36499b7b5a637bda82a271468602b7e8090157472e4`.
  All 13 final installed checks passed. Fresh protected startup retained all
  788 provider-bound, model-default 4,096-dimensional vectors and all 222
  capabilities. Five real-Qwen dimension-mode transitions rebuilt correctly,
  retained identity through refresh/restart, and used one query embedding for
  each cold/warm pair. The CLI exercised refresh/embed/status/search/read/quit.
- Required-Echo Astra recovered the injected empty completion in 10.42 seconds
  with one identical-request retry and no tools. The unchanged public
  memory-authority task passed in 42.23 seconds over three rounds, with canonical
  read/quote evidence, exact request accounting, and no extra recall. Operator
  configuration was restored and no memory capture was enabled.
- A4/A5 in ALGO.md now describe the binding and its limits. O4/O5/O6/O7/O8 passed
  17/198/13/219/122 checks; 160 paired samples and six installed source-bound
  pattern reports were refreshed. M8 remains nine local passes/five external
  blocks, and M9 remains 29 verified/13 blocked with zero failed requirements.
- Before/after metadata probes are not attestation or an atomic model snapshot;
  a dishonest endpoint or an artifact changing away and back between probes is
  outside this contract. Artifact changes were exercised with synthetic public
  providers, not by altering operator model tags. Legacy low-level callables and
  Echo's independently governed memory embedding profile are separate scopes.
- Receipts and failed controls are under
  `/tmp/algo-cli-embedding-identity.povQiP/`; final wrapper qualification and
  installed checks are under `final/` there. Algo changes remain uncommitted.
  The authorized local Echo revision/pin is unchanged; nothing was pushed or
  tagged, and no global Echo deployment or browser/approval gate was changed.
  Representative quality, competitive evaluation, and external qualification
  remain open. Another supervised coding benchmark needs fresh operator readiness.

### Bounded Retrieval-Slice Reuse

- Profiled the actual installed public search boundary with ten frozen
  development queries across five filter scopes. The 788-record index was
  initialized outside query timings; the embedding model could already be
  warm. Immediate repeats reused the one-entry lexical/vector caches, but all
  40 alternating-filter requests rebuilt both derived indexes. No operator
  embedding, protection, capture, or model settings were changed.
- Replaced single-slice retention with source-scoped LRU reuse. Each cache has
  eight entry slots and at most 4,096 input-row references; normalized matrix
  payloads have an additional 32 MiB ceiling. Oversized entries execute without
  caching or evicting useful small entries. These are derived-cache bounds,
  not whole-process memory guarantees. Original input rows, including numeric
  rejects, stay referenced until eviction so identity keys cannot recycle.
  Invalidation clears every slice and rejects an older concurrent build.
- Added 20 regressions covering alternating scopes, entry/row/payload limits,
  LRU promotion, replacement accounting, concurrent invalidation, reordered and
  replaced inputs, numeric-reject retention, and oversized-result correctness.
  Two initial reuse controls failed the prior implementation. Two further red
  controls caught a candidate diagnostic error: an oversized miss could leave
  the previous slice looking reused. Misses now clear that observation without
  evicting retained entries. Existing diagnostic probes still inspect the
  actual most-recent derived object, not an always-identical cache container.
- Sequential old/candidate/candidate/old live runs retained all formatted
  results. Pooled alternating-filter medians were 101.86/68.08 ms over 80
  samples per variant, with 80/0 rebuilds of each derived index. Fresh-query
  medians were 223.02/192.49 ms; immediate repeats 76.21/70.13 ms; empty filters
  11.21/12.73 ms. Provider checks were unchanged: six per new query vector,
  four per cached-vector request, and zero for empty scopes. These are local
  diagnostic timings, not representative task latency or competitive evidence.
- The exact cross-process numeric comparison failed on three vector scores
  differing by 0.0001. Two unchanged baseline runs showed the same differences;
  their cause was not established. That failed comparison remains retained.
  Separately frozen public query vectors gave exact complete-output and
  ranking-provenance parity on 40 paired requests, including the final source.
  No assertion or old result was relabeled to hide the live numeric difference.
- The first M8 run caught two patch-integration mistakes: a stale statistics
  schema expectation and a duplicate source/focused-test manifest entry. Both
  were corrected while retaining exact-schema and disjoint-manifest checks.
  A broader diagnostic also ran post-write checks before artifact refresh;
  those stale-artifact failures were not treated as runtime defects.
- Final qualification passed 5,019 tests with 32 skips and 69.02% branch-aware
  coverage. Go race/vet, Ruff, default/CI typing, compilation, source/history/
  artifact scans, locked dependency audit, wheel-from-sdist, and Twine passed.
  The new test file passes formatting; no repository-wide formatter claim is
  made. O4/O5/O6/O7/O8 passed 17/218/13/239/142 checks, with 160 paired pattern
  samples and six installed source-bound reports. M8 remains nine local
  passes/five external blocks; M9 remains 29 verified/13 blocked, zero failed.
- Installed-source parity verified 321 files and 281 Python modules against
  `sha256:f94c84c5a7329797c43d139df7a2b31848b796786e290b35971b1c45718d23d8`.
  The unchanged Echo pin/source audit and all 13 installed smoke checks passed.
  Restart retained 788 vectors at 4,096 dimensions and all 222 capabilities.
  Required-Echo Astra recovered the injected empty response in 11.68 seconds
  with one identical-request retry and no tools. The unchanged public
  `memory_authority` development task passed in 50.34 seconds over three
  rounds, with canonical quotes, exact accounting, and no additional recall.
- A further actual-installed replay made five builds per index, then zero
  rebuilds across 40 alternating-filter requests; median 68.92 ms. Five cached
  scopes retained 1,554 input-row references per cache and 25,460,736 matrix
  bytes. Its corpus includes the newly documented A1 contract, so it is a
  separate installed check, not the unchanged-corpus comparison above.
- ALGO.md A1 now specifies bounded slice reuse and clarifies active RRF fusion;
  O7 describes bounded cache misses rather than the old single-entry behavior.
  Raw runs, retained failures, frozen parity, and final qualification are under
  `/tmp/algo-cli-retrieval-latency.2rz8tN/`, with final gates in `qualified/`.
  Algo changes remain uncommitted. No push, tag, Echo pin change, global Echo
  deployment, model-setting change, or approval/browser gate relaxation occurred.
  Representative task quality, competitive evaluation, and external
  qualification remain open. Fresh readiness for supervised coding was requested.

### Claim-Grounded Answer Qualification

- Reproduced a false acceptance in the original generated-answer checker without
  a model call. A correct retry answer passed every legacy check while citing an
  unrelated, genuine sentence about model routing from the required guide. The
  original grader copy matches SHA256
  `fae35ee5580f881e0aae7e42b662c76ba2e348e7d583b04a38b2bfd35f162da3`.
  A new regression uses that same public quote and requires rejection.
- Added `evals/grounded_answers.py` and
  `scripts/grounded_answer_qualification.py`. The versioned development protocol
  freezes answer types and per-claim supporting spans, including predeclared
  equivalent retry authorities. Its JSON parser rejects duplicate keys,
  nonfinite constants, oversize answers, malformed evidence, unread quotes,
  unrelated citations, and incomplete claim coverage. The runner uses the actual
  non-editable installed interpreter and normal catalog, but permits execution
  only of public harness search/read. Required Echo is enforced per run without
  changing the operator's saved protection preference; capture stays off.
- The runner freezes source, prompt, and case hashes before any model call,
  bounds each worker, and checks source/configuration stability. Receipts contain
  fixed checks, numeric metrics, and allowlisted public IDs, not arbitrary model
  output, queries, or memory payloads. This is a strict annotated-span evaluator,
  not a general semantic-entailment engine or a normal-chat answer filter.
  ALGO.md records that distinction and prohibits retrospective label changes.
- Found and repaired a second observation defect: a short reread overwrote the
  longer source body already observed, making an earlier valid quote fail. The
  runner now retains the longest compatible prefix and invalidates conflicting
  views instead of joining them into invented evidence. Both failing controls
  pass after repair; longer, shorter, identical, and conflicting views are covered.
  An earlier runner precondition also incorrectly required a globally saved
  `required` preference despite its per-run override; its red/green regression
  verifies that an existing optional preference is preserved, not changed.
- Final delivery passed 5,114 tests with 32 skips and 69.05% branch-aware
  coverage, including 95 new evaluator/runner checks. Go race/vet, Ruff,
  default/CI typing, compilation, source/history/artifact scans, locked dependency
  audit, wheel-from-sdist build, and Twine passed. The four new files pass scoped
  formatting. Earlier candidates passed 5,108 and 5,110 tests, but their receipts
  are retained separately and do not substitute for this final source.
- O4/O5/O6/O7/O8 passed 17/218/13/239/142 checks, with 160 paired pattern samples
  and six source-bound reports installed. M8 remains nine local passes and five
  external blocks; M9 remains 29 verified and 13 blocked, with zero failed
  requirements. No external-browser or comparative-claim threshold was relaxed.
- Installed-source parity verified 322 files and 282 Python modules against
  `sha256:a3a6429060fe5672cd4cc183f0115ae7072460d3109d67168cb0e07f10accef4`.
  The final script-only read-history correction did not change those package
  bytes. Twelve installed smoke checks passed on that same package, including
  required-Echo empty-stream recovery in 10.38 seconds. The unchanged Echo pin
  `cbee525687ac03c830d4b6632ff1d044b4b838fc` and all 46 qualified Python sources
  passed the installed integrity audit again.
- The frozen eight-task v2 run scored 6/8, not a pass. It took 506.217 seconds
  across 35 completed model rounds and 178,291 provider-reported tokens.
  Protocol digest:
  `sha256:9f61c0f49be0cff7029390063056b50c16274e0d248f12e963677837de5b3f43`.
  The Spanish privacy task timed out at 120 seconds after eight rounds. The
  pattern-effectiveness task returned the correct value and a source-bound quote
  but missed the frozen span annotation. Source and saved configuration remained
  stable in every case. This run preceded the final read-history correction;
  neither it nor the older evaluation was rescored after that correction.
- Three separately frozen diagnostic repeats retained the same prompts, model,
  reasoning, and limits, adding only numeric observations and validated public
  quote offsets. The repeated pattern answer quoted two genuine table rows that
  distinguish catalog/import readiness from execution and benefit. Those rows
  support the conclusion, but v2's single accepted passage does not cover that
  equivalent evidence. This diagnoses an annotation coverage gap, not proof of
  an incorrect model conclusion. Keep the strict failed scores unchanged and
  preregister fuller, conjunctive alternatives in a future evaluation version.
- One Spanish repeat timed out at 120.033 seconds after seven completed rounds;
  the second passed at 109.98 seconds after eight. Both read the entire 7,972-
  character privacy guide, including both predeclared supporting claims, early
  in the run. Completed-round context preparation totaled 59.331 and 67.727
  seconds respectively, generally 8-9 seconds per round. This is measured
  context-path cost, not attribution to a particular subcomponent or evidence
  for raising the deadline. Profile that path before changing caching, provider,
  protected-memory freshness, or operator compute settings.
- The final installed controlled read-history probe passed in 59.919 seconds
  over four rounds: the actual tool returned 5,724 characters, then 20 from the
  same contract; the answer still cited the earlier supporting passage. Required
  Echo, source stability, both child and parent configuration checks, and all
  answer checks passed. Its protocol is
  `sha256:4fc109bbfbf05b7244b030e4d228122ed0cf50d0662b695ce41da927a60c3645`.
  This qualifies the changed observation boundary, not a new eight-task score.
- Artifacts and retained failures are under
  `/tmp/algo-cli-answer-grounding.JRhuFa/`; final gates are in `delivery/`, the
  original v2 run in `answers-v2-final/`, repeats in `diagnostic-repeats/`, and
  the final controlled probe in `read-view-live/`. Algo changes remain
  uncommitted. No push, tag, Echo pin change, global Echo deployment, operator
  model/embedding setting change, or approval relaxation occurred. Representative
  coding and competitive qualification, annotation calibration, context-path
  optimization, and external qualification remain open.

### Hosted Root-Relative Read Repair

- Published candidate `c36f06352614b45ac3dccf016b8c26a5845236f7` in PR #33.
  Its local gate passed 5,114 tests, but hosted run `34236382090` failed the
  same 12 retrieval cases on Linux Python 3.10 and 3.12. macOS, native helpers,
  Swift, website, and hardening-policy checks passed. The Windows cell exceeded
  its 25-minute limit; the runner's log archive was unavailable. Do not call
  that timeout a Linux-path failure or a completed Windows qualification.
- Reproduced the exact 12 failures on macOS by placing the unchanged tests
  under a `/tmp` ancestor: 12 failed, 108 passed. Discovery and source freshness
  already used root-relative exclusions, while reading rejected every absolute
  ancestor, including valid temporary checkouts and virtual environments.
- Repaired the reader using the current configured source roots and lexical
  relative components. It still rejects nested excluded directories, credential
  filenames, parent traversal, and link aliases through excluded components.
  Added complete relative-path credential checks: three negative controls proved
  that a stale record could previously expose a benign filename under
  `credentials`, `secrets`, or `tokens`. Cached `relative_path` values grant no
  exception. Protected source validation and the exact runbook exception remain
  unchanged. Two public-document fixtures now register their actual source root.
- Added 39 deterministic read-path cases; 19 failed against the original reader.
  The repaired focused set passed 159 cases under `/tmp`, and the expanded
  source, pattern, workflow, and dependency set passed 286. CI now names each
  running test, reports slow tests, and dumps thread stacks after 120 seconds;
  the job deadline, matrix cells, test assertions, and skips were not relaxed.
- Full local delivery passed 5,153 tests with 32 skips and 69.06% branch-aware
  coverage. Go race/vet, Ruff, default/CI typing, compilation, public
  source/history/artifact scans, locked dependency auditing, wheel-from-sdist
  build, and Twine passed. The new regression module passes scoped formatting.
- Source-bound O4/O5/O6/O7/O8 reports passed 17/218/13/239/142 checks; 160 paired
  samples completed, and six current reports were installed. M8 remains nine
  local passes and five external blocks; M9 remains 29 verified and 13 blocked,
  with zero failed requirements. These reports do not qualify external browsers
  or establish representative task quality.
- Installed the qualified wheel and verified all 322 files and 282 Python
  modules at
  `sha256:beb6288be8d4075b54b3278e1caf43d7bd55cfa6c0d6df735d034d087c793f26`.
  Nineteen installed read-boundary probes and all twelve installed smokes passed.
  Required-Echo Astra recovery passed in 14.62 seconds with one identical-request
  retry, no tools, and restored settings. Echo's published exact `cbee525` pin
  and all 46 qualified Python source files remain unchanged and audited.
- Retained red tests, expanded regressions, installed probes, and delivery logs
  are under `/tmp/algo-cli-ci-paths.By3JWO/`. This iteration repairs a real read
  boundary; it does not resolve the earlier context-preparation bottleneck,
  recalibrate the 6/8 answer evaluation, or complete the broad Class A goal.
  Fresh hosted results must qualify this follow-up separately from the failed
  initial run. No release tag or merge is authorized by these local results.

### Bounded Hosted Watchdog Diagnostics

- Hosted run `34246814890` reached a terminal cancelled state. The quality
  job exceeded its 15-minute limit; Linux, macOS, and Windows matrix cells
  each exceeded their 25-minute limit. GitHub supplied those annotations but
  no detailed log archive. The empty `--log-failed` result was not evidence of
  success: the jobs were cancelled, and direct job-log retrieval failed.
- Reproduced a diagnostic gap independently of the unknown hosted stall.
  The existing pytest timer printed thread stacks but did not stop a blocked
  worker. Four pre-repair checks failed, including two actual subprocesses
  that required their separate 20-second parent limit. This is not proof
  that the hosted stalls occurred inside a test rather than collection,
  session shutdown, or descendant cleanup.
- Both CI test commands now enable the locked pytest runtime's
  `faulthandler_exit_on_timeout` setting. The 120-second test timer, outer job
  deadlines, matrix cells, disabled matrix fail-fast, coverage floor, and
  blocking failure policy are unchanged. Twelve checks cover both job
  contracts, actual stalls in setup/call/teardown, normal completion, and
  ordinary assertion failure. The expanded workflow set passed 120 tests.
  `ALGO.md` now documents this bounded-watchdog contract and its limits.
- Full local delivery with the stricter watchdog passed 5,165 tests with
  32 skips and 69.06% branch-aware coverage. Go race/vet, Ruff, default and
  CI typing, compilation, public source/history/artifact scans, locked
  dependency auditing, wheel-from-sdist build, and Twine passed. The new
  test module passes scoped formatting; no repository-wide formatting claim
  is made.
- Source-bound O4/O5/O6/O7/O8 checks remain 17/218/13/239/142, with 160 paired
  samples and six current reports installed. M8 remains nine local passes
  and five external blocks; M9 remains 29 verified and 13 blocked, with no
  failed requirements. The M8 snapshot binds the changed workflow; the new
  watchdog tests have separate full-suite evidence, not an invented M8 binding.
- Installed parity verified 322 files and 282 Python modules at
  `sha256:c31971bbb70eaf589480a5b5971de00ffc3ba5c5c599f2e6efae0ee21f09dc21`.
  All twelve installed smokes passed, including required-Echo Astra stream
  recovery in 11.44 seconds. The exact Echo `cbee525` pin and its 46 qualified
  Python sources remain unchanged and audited.
- Retained evidence is under `/tmp/algo-cli-ci-deadlock.ZgODrn/`. The initial
  new-test collection error and missing disposable gateway in the first smoke
  attempt are retained as fixture-setup failures, not successful runtime
  checks. After correcting the fixture setup, the complete installed smoke
  sequence passed in `smoke-qualified/delivery-final/`.
- These are local diagnostic results. A separate hosted run must verify the
  changed workflow and expose any remaining stall. No timeout cause, hosted
  pass, browser qualification, release, or universal harness ranking is claimed.

### Installed Context Preparation Profile

- Before the watchdog-only follow-up, measured the installed `2a90269` runtime
  using the unchanged v2 Spanish privacy prompt and fresh required-Echo reads.
  One unprofiled build took 8.05 seconds. Two profiled builds took 8.24 and
  8.35 seconds; retrieval-query embedding consumed about 93% of each, dominated
  by waiting for the local service response. Memory-layer initialization was
  about 0.38 seconds, including about 0.10 seconds for its security doctor.
- A separately frozen service-timing probe took 14.29 seconds overall. Its
  single batched embedding request took 8.68 seconds, with 3.07 seconds of
  service-reported load time. The remaining build time was not profiled in
  that sample and remains unattributed. Do not treat this separate sample as
  a controlled regression comparison or sum overlapping cumulative timings.
- The runners retained only numeric measurements and sanitized public code
  symbols. They checked source and saved-setting stability, made no model-
  generation call, and introduced no protected-result cache or persistent
  profile lease. Artifacts remain in `/tmp/algo-cli-context-profile.7KD91N/`
  and `/tmp/algo-cli-embedding-timing.JO16O2/`.
- Investigate repeated identical query work within the Echo-owned boundary.
  Preserve fresh protected recall, semantic availability, and integrity checks;
  do not move private query vectors into public harness caches or silently
  change saved embedding/compute settings. A performance change still needs
  negative controls and same-protocol before/after qualification. The strict
  eight-task answer score remains 6/8; this profile does not rescore it.

### Bounded CI Test Metadata

- Authenticated live logs for hosted run `34252238007` exposed a common
  output boundary. Linux 3.12, Linux 3.10, and macOS 3.12 stopped immediately
  before a provider-metadata case with a 2,097,267-byte generated test ID.
  Windows stopped earlier, before a completion-receipt case with a
  65,618-byte ID. All four jobs subsequently exceeded their existing job
  limits; the completed quality log archive was again unavailable (HTTP 404).
- A clean, isolated Linux probe ran the original 71 embedding/anchor tests
  successfully in 1.85 seconds with file-backed output. Its longest log line
  was 2,097,282 bytes. This identifies oversized diagnostic output as a real
  defect and a hosted-stall suspect, not proof of a parser deadlock or a
  completed hosted fix. Six oversized IDs were found across five test files.
- Added short explicit IDs without changing the adversarial inputs or their
  assertions. A collection hook rejects node IDs over 1024 UTF-8 bytes and
  emits at most five bounded examples without parameter contents. Eight new
  tests cover the real subprocess behavior, byte boundaries, diagnostic
  bounds, and the complete collected suite. The pre-repair run had seven
  failures; the repaired focused set passed 315 tests. Maximum collected ID
  size fell from 2,097,267 to 500 bytes; only the eight new cases were added.
- Full local delivery passed 5,173 tests with 32 skips and 69.06%
  branch-aware coverage. Go race/vet, Ruff, default and CI typing, compilation,
  source/history/artifact scans, locked dependency auditing, wheel-from-sdist
  build, Twine, and scoped new-test formatting passed. No test, payload,
  watchdog deadline, outer timeout, or coverage floor was relaxed.
- O4/O5/O6/O7/O8 checks remain 17/218/13/239/142, with 160 paired samples and
  six source-validated reports installed. M8 remains nine local passes and
  five external blocks; M9 remains 29 verified and 13 blocked with no failed
  requirements. M8 binds the collection guard and affected embedding/answer
  metadata; the new output-budget tests and ALGO contract have separate
  full-suite evidence, not an invented M8 binding.
- Installed parity verified 322 files and 282 Python modules at
  `sha256:9d499f8cdc085706c6504f7179fb915509e813dc444b318827fd7048d6e5b416`.
  All twelve installed smokes passed, including required-Echo Astra retry
  recovery in 11.48 seconds. The exact Echo `cbee525` pin, saved settings, and
  46-source qualification are unchanged. Artifacts are retained under
  `/tmp/algo-cli-linux-ci.JXC3da/`; fresh hosted qualification remains separate.
- The isolated Linux x86_64 image runs under Rosetta on this arm64 host.
  After correcting its Git ownership, user, tooling, and stale-report setup,
  the full suite completed with 5,169 passes, 35 skips, and one failure in
  186.27 seconds. The remaining benchmark failure reports a Rosetta anonymous
  memory-map error under the existing 512 MB subprocess limit; a focused
  pytest reproduction confirms it. This is not a green Linux qualification.
  Preserve the failed runs and the memory cap; native hosted execution must
  independently verify this patch. No installed runtime implementation changed.

### Byte-Stable Catalog Portability

- Hosted run `34256348318` at `6add6e2` completed without the earlier output
  stall. Linux quality and Python 3.10 each passed 5,170 tests with 35 skips;
  macOS passed 5,173 with 32 skips. Native helper, Swift, website, packaging,
  and installed-wheel jobs also passed. Windows completed with 4,284 passes,
  824 skips, and three failures. The overall run is failed, not qualified.
- Two Windows failures used an implicit cp1252 default to read UTF-8 source
  documents. The third was a real protected-read inconsistency: indexing
  normalized CRLF to LF, while the descriptor-bound read hashed original bytes.
  Ordinary reads also failed to invalidate an index after a newline-only edit.
- Pattern indexing and ordinary reads now preserve UTF-8 line endings. The
  protected reader, strict decoding, byte limits, link checks, and source digest
  comparison are unchanged. Two document tests now name their UTF-8 encoding.
  An opt-in fixture exercises both host defaults without globally hiding
  encoding errors. Exact LF/CRLF/CR fixtures cover digest identity, valid reads,
  link replacement, and stale rejection in both read modes.
- After correcting a missing digest-prefix assertion in the first test draft,
  the retained pre-repair run had 10 failures and eight passes. The repaired
  focused suite passed 213 checks. Full local delivery passed 5,187 tests with
  32 skips and 69.06% branch-aware coverage, plus Go race/vet, Ruff, default
  and CI typing, compilation, source/history/artifact scans, locked dependency
  audit, wheel-from-sdist build, Twine, and scoped test formatting.
- O4/O5/O6/O7/O8 source-bound checks are 17/231/13/252/155, with 160 paired
  samples and six current reports installed. M8 remains nine local passes and
  five external blocks. M9 remains 29 verified and 13 blocked, with no failed
  requirements. No approval, browser qualification, coverage, watchdog,
  benchmark, or source-authorization requirement was relaxed.
- Installed-source parity covers 322 files and 282 Python modules at
  `sha256:88fe8c7dcd7ab8f780deae6de6eb6d777ae576d0fc23fe2a5a37c3a92d1bc742`.
  Thirteen installed smokes passed, including 21 byte-stability/link checks
  and required-Echo Astra stream retry in 11.73 seconds. The exact Echo
  `cbee525` pin and its 46-source audit are unchanged. Artifacts are retained
  under `/tmp/algo-cli-windows-text.UfEgJM/`; new native hosted qualification
  remains separate from these local and installed results.

### Operator Verification Loop (Open)

- A supplied operator transcript exposes a separate incomplete coding turn:
  a file mutation succeeded, but shell verification was prohibited by Echo
  policy, browser service was unavailable, and the temporary workspace was
  not a Git repository. Repeated discovery and transform probes did not
  satisfy the completion gate. Do not weaken the gate or retry a policy denial.
- Three isolated offline regressions reproduce current defects: discovery
  advertises `available_actions` for a program runtime that forbids it; Echo
  shell policy is checked after approval instead of during preflight; and
  browser unavailability is classified as successful tool execution. These
  intentionally failing probes are retained separately in the iteration's
  `transcript-repro/` directory. The passing portability suite does not fix
  or qualify these failures.
- The transcript also records program validation errors as uncertain effects,
  and the completion nudge requests verifier routes without checking their
  availability. Next iteration: preserve typed pre-dispatch denial versus
  post-dispatch uncertainty, expose executable discovery paths, and bound
  verification recovery even when successful discovery calls keep resetting
  the nonexecuting-batch counter. Retain per-action approvals and return an
  honest partial result when verification cannot run. No real coding benchmark
  pass or representative quality gain is claimed from this transcript.
- Correction to earlier smoke receipts: configuration comparisons covered
  model/Echo preferences but omitted `cwd`. The real launcher's `--cwd` was
  persisted, so a smoke test changed the next normal launch's workspace.
  The runner now uses and verifies the existing saved workspace. The operator
  selected the canonical repository for restoration; only that field changed,
  and the generated file remains unchanged. Earlier claims of unchanged
  settings must not be interpreted as complete configuration parity.

### Admission and Verification Recovery Repair

- The next Windows run, `34260295375` at `cb448d4`, removed all three original
  portability failures but remains failed: 4,297 passed, 824 skipped, and four
  failed. One model-free benchmark exceeded unchanged p95 limits: 6,538 ms
  workload total against 3,500 ms and 1,072 ms first-event time against 1,000 ms.
  Three artifact/receipt tests depend on that failed report. All correctness
  probes passed; the timing cause is not established. Other executed CI jobs
  passed, and protected-browser jobs remain skipped and unqualified.
- Reproduced non-composable discovery, late Echo refusals, invalid-program
  uncertain effects, browser error false successes, and unbounded post-nudge
  recovery. Known admission failures now precede approval and dispatch.
  Discovery intersects the canonical composable set with runtime authority and
  globally applicable Echo denials. Static programs are validated before
  starting and again during normal step dispatch.
- A negative control exposed an additional authority defect: browser mutations
  were classified as pure observations. Six stateful browser actions now require
  action-time approval and at-most-once dispatch. Success strings do not replace
  independent effect verification, and uncertain attempts cannot be retried.
  All eight unqualified browser actions are refused under Echo before readiness
  probing. This does not qualify browser containment or reconcile external state.
- Chat and Agent Blocks share a four-round post-nudge recovery ceiling and a
  protection-aware verifier prompt. Successful discovery, rereads, and new
  mutations do not reset that window. A final permitted verifier allows a
  tool-free answer; otherwise work is retained with partial status. Existing
  global iteration limits, tracked-path Git rules, and approval gates remain.
- Early isolated regressions retained their failures, including the browser
  metadata defect and a corrected configured-root fixture assumption. The first
  combined recovery slice passed 442 tests; full delivery and installed
  qualification remain separate and must be recorded after source freeze.
- The first broader local qualification failed because the new discovery
  configuration argument was not hidden from the provider schema. Conversion
  dropped `action_search`, leaving 73 schemas for 74 callables. The focused
  hardening failure and token-efficiency failure share this cause. Apply the
  existing runtime-parameter hiding helper and test both provider-neutral and
  Responses schemas; retain the failed report and unchanged completeness gate.
- Open work: native Windows timing diagnosis, a permitted contained verifier
  for protected new-project coding, representative coding/answer evaluation,
  and a byte bound for search output (line limits alone admitted a multi-megabyte
  result). The original generated application and normal-launch workspace are
  unchanged. No representative quality improvement or M8 completion is claimed.

### Admission Repair Delivery Receipt

- Final source delivery passed 5,246 tests with 32 skips in 108.31 seconds and
  69.16% branch-aware coverage. There are 59 new admission/recovery regression
  cases. Go race/vet, Ruff, default and CI typing, compilation, public
  source/history/artifact scans, locked dependency audit, wheel-from-sdist build,
  Twine, and new-test formatting passed without lowering any existing gate.
- The corrected schema conversion retains all 74 runtime callables. O4-O8
  source-bound checks remain 17/231/13/252/155; 160 paired samples completed,
  and six current reports were installed. M8 retains nine local passes and five
  external blocks; M9 retains 29 verified and 13 blocked requirements, with no
  failed requirements. The earlier failed local qualification is retained.
- Installed parity covers 322 files and 282 Python modules at
  `sha256:271a08a312a03410789ed589163ac949137c787b005b670396c13326351e5f41`.
  Fourteen installed smokes passed, including actual launcher refresh/search/read,
  fresh-process retention, 21 byte/link checks, five isolated admission/recovery
  controls, and required-Echo Astra empty-stream recovery in 10.59 seconds.
  The new fixture reproduces the original denial on the previous installed
  package and passes against the repair; it makes no real model tool calls or
  operator decisions and is not a representative coding qualification.
- The first installed context fixture failed after early admission correctly
  rejected its workspace inside protected state. Moving that fixture also exposed
  its noncanonical `/tmp` symlink ancestry. The corrected fixture uses a canonical
  sibling workspace, asserts protected paths remain denied, and supplies a
  synthetic privacy key. All failed fixture attempts are retained; no runtime
  path protection was relaxed to make the smoke pass.
- The exact Echo `cbee525` dependency and 46-source audit remain unchanged.
  Saved workspace/model/Echo settings and the operator-generated application
  were checked unchanged. Receipts live under `/tmp/algo-cli-verification.xyESly/`.
  A ten-sample local workload profile is diagnostic only: it does not explain or
  clear the prior native Windows latency failure. Hosted qualification of this
  source, representative coding quality, and M8 external requirements remain
  separate from this local delivery result.

### Search Execution Candidate and Open Boundaries

- Hosted run `34265899529` at `d785584` also failed the unchanged Windows
  workload latency gates: total p95 5,135.5303 ms against 3,500 ms, and
  first-event p95 1,825.3686 ms against 1,000 ms. All correctness probes passed.
  Windows completed with 4,356 passes, 824 skips, and four failures, including
  the three dependent artifact checks. Other executed jobs passed; four
  protected-browser jobs were skipped and remain unqualified. Preserve both
  this run and the earlier timing failure; no rerun or local pass diagnoses it.
- Seventeen initial search regressions reproduced option-shaped patterns being
  interpreted as ripgrep flags, inherited configuration changing the command,
  unbounded single-line results in both backends, silent line truncation, and
  invalid limits. The candidate disables inherited rg configuration and
  separates positional data from options. A bounded pipe collector stops at
  global byte/line limits instead of capturing all output first.
- The Python fallback and single-file regex path now run in a cancellable
  child with explicit UTF-8 output. Focused tests cover stuck regexes, unlimited
  stdout/stderr, cancellation, child reaping, exact-limit results, byte/chunk
  boundaries, legitimate regex/glob behavior, and malformed input. Post-patch
  negative controls also caught and corrected malformed-Unicode exceptions
  and a reported search failure being hidden by truncation. The first draft's
  missing import and oversized parameter IDs are retained as failed attempts;
  their correction did not shrink payloads or weaken the collection gate.
- An independent synthetic fixture confirms a separate open security defect:
  both backends return a private marker from a protected subtree when the model
  searches its permitted ancestor. Direct access to the protected root is
  correctly denied. Root admission is therefore not recursive read authority.
  The next repair must authorize descendants and bind reads across symlink,
  hardlink, reparse, and path-replacement races; output truncation is not a fix
  for this boundary. The fixture reads no real protected records.
- Artifacts for this candidate are under `/tmp/algo-cli-search.iFua3caS/`.
  Full qualification and installation are not implied by focused passes.
  Keep the installed runtime and saved workspace unchanged until the recursive
  boundary is repaired and the combined candidate is qualified. No new release,
  Windows performance qualification, representative task-quality gain, or M8
  external completion is claimed.
- The first full suite had 5,301 passes, 32 skips, and one stale M9 ledger-digest
  failure after refreshing source-bound reports. Appending current content-bound
  evidence and regenerating the still-blocked M9 report produced 5,302 passes
  with 32 skips. A final portability check then reproduced two presentation
  defects in binary capture: CRLF was no longer normalized, and Unicode line
  separators could bypass the requested display-line limit. Their corrections
  require a new source-bound qualification; the earlier green run is retained,
  not relabeled as proof for later source.

### Search Execution Local Receipt

- The final matcher candidate passed 5,304 tests with 32 skips in 104.84 seconds
  and 69.17% branch-aware coverage, including 58 dedicated search regressions.
  Go race/vet, Ruff, default and CI typing, compilation, public source/history
  and artifact scans, the locked dependency audit, wheel-from-sdist build, and
  Twine passed. The original failures and intermediate source results remain
  under `/tmp/algo-cli-search.iFua3caS/`; final receipts are in `final-source/`.
- Five alternating baseline/candidate pairs used the same twelve synthetic
  large-line files. The prior implementation returned 10,801,463 UTF-8 bytes;
  the candidate returned 19,809 bytes with truncation disclosed. Maximum parent
  Python allocation was about 32.4 MB versus 0.13 MB. This diagnostic does not
  measure child or total RSS, representative search quality, or competitive
  superiority; its source hashes and all ten samples are retained.
- The built wheel contains the exact fallback helper source. Six isolated
  packaged-helper checks passed across Python 3.10 and 3.14, covering Unicode
  matches, invalid regexes, and no-match results. This is not an installed
  harness or native Windows qualification. The normal installed environment,
  Echo pin, saved workspace, and operator-generated application remain unchanged.
- M8 retains nine existing local metric passes and five external blocks. M9
  retains 29 verified and 13 blocked requirements within its defined checklist.
  Their current digest bindings explicitly retain the independently reproduced
  recursive protected-search defect and both failed native Windows timing
  runs. Neither checklist is exhaustive proof of harness safety or readiness.
- These changes remain a local uncommitted candidate. Before installation or
  publication, repair descendant read authorization, add race and alias controls
  to the owning regression suite, and qualify the combined source. The Windows
  latency cause, contained protected-coding verifier, representative evaluations,
  and M8 external qualification also remain open.

### Protected Search Boundary Local Receipt

- Seven synthetic red tests confirmed that allowed-ancestor searches exposed
  protected descendants, hardlink aliases, and migration residue. The protected
  search path now carries immutable deny paths and root identities into a
  deadline-isolated worker, binds root handoff and descendant reads, and gives
  the matcher only authorized in-memory text. Runtime configuration is injected
  by the dispatcher and hidden from all provider schemas.
- A separate post-patch race probe caught a real remaining route: renaming a
  protected directory under a permitted path reached the content reader. Adding
  registered protected-root filesystem identities closes that reproduced route
  before bytes are read. Its red result remains alongside the original seven
  failures. The initial oversized test ID and typing errors are also retained;
  correcting them did not reduce fixture payloads or weaken any gate.
- The original two-backend reproducer now returns the public control without
  its fictional private marker, and direct protected-root access stays denied.
  Fifty protected-search cases and three new bounded-stdin cases cover ordinary
  globs, local ignore/negation rules, native regex semantics, links, replacements,
  policy changes, budgets, Unicode limits, schemas, and child cleanup. The native
  Windows DOS-alias case is skipped on this Mac, not counted as a native pass.
- Combined source validation passed 5,356 tests with 33 skips in 111.49 seconds
  and 69.15% branch-aware coverage. Go race/vet, Ruff, default and exact CI typing,
  compilation, public source/history/artifact scans, the locked dependency audit,
  wheel-from-sdist build, and Twine all passed. Runtime dependencies now explicitly
  include pathspec 1.1.1 and wcmatch 10.2.1, with bracex 3.0.1 in the exact lock;
  the Echo pin is unchanged.
- Two disposable non-editable wheel environments, Python 3.10 and 3.14, each
  passed seven synthetic smoke checks. Both match 324 packaged source files,
  including 284 Python modules and the updated ALGO.md, at
  `sha256:c00a304a6caabecbb0fd5abf379cfa62cf2cdc20d7cc3022a22f8882f9a55f4c`.
  This verifies the packaged worker handoff, not the user's normal installation,
  native Windows execution, required-Echo live-provider integration, or coding quality.
- M8 remains nine local passes and five external blocks; M9 remains 29 verified
  and 13 blocked requirements within its non-exhaustive checklist. Current M8
  source binding is
  `sha256:379ddcbb155e103f78186b86a7c2d606fdf6ed2e043debd6fd97e86f90f7f28b`.
  Both previous native Windows timing failures remain failed and unexplained.
  No benchmark threshold, browser approval/signing gate, or protection setting
  changed. Artifacts are under `/tmp/algo-cli-protected-search.k7ZTMrg9/`.
- The combined candidate remains uncommitted, uninstalled in the normal runtime,
  and unpushed. The installed launcher's saved workspace was verified as the
  canonical Algo-cli checkout; this iteration changed no saved settings. Next:
  qualify the final installed runtime and native Windows boundary, then publish
  through the authorized feature-branch workflow without implying a main release,
  M8 completion, or competitive superiority. Keep the separate Windows timing,
  protected-coding verification, and representative-evaluation work open.

### Protected Search Delivery And Hosted Findings

- Committed and pushed the qualified search repair as
  `ce45748b3a2424b5e603db4c3b347373a9ca4db3` to `feat/pattern-runtime`, PR #33.
  No main merge, tag, or release was performed. The normal non-editable runtime
  now matches the 324-file candidate digest above. Only the three newly pinned
  search dependencies were added; the exact Echo pin and its source/RECORD
  verification remain unchanged.
- All 15 installed smokes passed, including the actual launcher, restart,
  protected-search canaries, and required-Echo Astra empty-stream recovery.
  Nineteen tracked settings remained unchanged, including the canonical saved
  Algo-cli workspace. Synthetic authority checks did not approve real actions.
  The existing interactive process was not restarted and will need a fresh launch
  to load the new code. Evidence: `/tmp/algo-cli-search-delivery.chGHcTj6/`.
- Refreshed O4-O8 and their 160-sample, model-free retrieval comparison against
  the delivered source. The records-plus-rules cell reached 1.0 fixture accuracy
  with no boundary violations; records without rules reached 0.75 with ten
  violations. This measures frozen retrieval tasks only, not generated answers
  or representative coding quality. Later source edits invalidate these receipts
  and require another final-source refresh.
- Hosted run `34276924298` completed with a Windows failure. The macOS matrix
  passed 5,322 tests with 67 skips; Linux passed 5,319 with 70 skips. Each executed
  76 of the 111 search cases and skipped 35: 34 required unavailable ripgrep,
  and one required native Windows.
  Windows executed 74 search cases and skipped 37, including platform exclusions.
  No executed search case failed. This was incomplete native-backend coverage,
  not successful qualification of both implementations.
- Windows reported 5 failures, 4,429 passes, and 861 skips in 867.30 seconds.
  Four failures derive from the workload p95 of 3,659.8754 ms exceeding the
  unchanged 3,500 ms ceiling. First-event p95 was 794.0687 ms, and all 17
  correctness probes passed. The fifth failure was a stored source-digest
  mismatch. Log SHA-256:
  `1b661a79391a3bb5252f5d66ade815e85d38fc3a49561095b644a7bb9337f090`.
- A separate red test reproduced Git converting the newly source-bound `uv.lock`
  to CRLF under `core.autocrlf=true`; `.gitattributes` had no LF rule for it.
  The candidate pins checkout bytes instead of weakening hashing. Two other red
  checks preserve the missing CI backend prerequisite. CI now builds an exact
  locked ripgrep and rejects missing/wrong versions before collection. A
  failure-only Windows profiler will retain bounded, source-bound diagnostic
  JSON without changing the failed gate or its thresholds. Native qualification
  of these changes and the Windows latency cause remain open.

### Native Windows Profile And Static Binding Repair

- Run `34280924721` at `2d7c2bd8d9e40c36c894ffc52440be97e7a1eaa5` ended
  with five Windows failures: four assertions from the unchanged 3,500 ms
  workload p95 limit, and one CRLF anchor expectation. All other executed jobs,
  including the three installed-wheel jobs, passed. Four external Boron jobs
  remained skipped and unqualified. Windows recorded 4,481 passes, 827 skips,
  and workload p95 4,527.6351 ms; all 17 correctness probes passed. Log SHA-256:
  `d24c87fcfc3c26adb12cb4d02c29f7e4db63b6d9cc1a5aa05c02d0d8c846a491`.
- The failure-only profile is diagnostic, not replacement gate evidence. Its
  source digest is
  `sha256:a311c8accbf418573d2a20e97c169ef73c1737f629b4018ddcd240265eb17e75`;
  its merge revision `b2839dec70be88578bdcef04503889634c87d9e6` has the
  tested candidate and main as parents. Artifact `10078187231`, JSON SHA-256
  `5d62feb4717309249922b5b05f5484cf179f11ef74bc04f0f767cbcf192a6347`,
  records 9,000 ACL checks and 9,010 identity lookups over five workloads.
  These repeatedly rebuilt ctypes structures and signatures. The cumulative
  timings overlap and must not be summed or treated as isolated cost estimates.
- The candidate reuses only static API/type metadata. Identity, owner, DACL,
  allocations, native handles, ancestry checks, locks, and flushes stay fresh.
  The pre-fix regression made 12 library-wrapper constructions where two suffice;
  it still required two actual ACL queries and four actual token queries.
  Warm identity/ACL mutation and API-failure controls passed before the change
  and must continue passing after it. Native latency improvement remains unproven
  until the new exact-source Windows gate executes with unchanged limits.
- The anchor failure was an implicit Windows CRLF fixture, not a matcher defect:
  direct ripgrep and protected search agreed on all nine explicit-byte oracle
  cases. Tests now cover LF, CRLF, and missing final newline without changing
  runtime matching. The current candidate requires fresh local reports, pattern
  receipts, installed smoke, and native CI before delivery claims.
