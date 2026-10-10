# Runtime Reliability Goal

**Independent candidate update, 2026-10-09:** Local candidate commits were
explicitly authorized after the runtime qualification below. This independent
copy passes source/history/distribution privacy checks and a complete frozen
noneditable Python coverage run (7,382 pass, 42 skip, zero failures/errors;
73.22% combined coverage). The additional macOS alias skip passed separately.
The subsequent authorized local repair applies Go 1.26.9 pins to the original
checkout too and removes all 17 website advisory findings through patched
versions and a tested, bounded synchronous glob adapter. Latest complete
installed Python: 7,382 pass / 42 skip / zero failures, 212.663 seconds,
70.92% combined coverage. Original history, unrelated edits and normal
launcher/settings remain preserved. External release gates remain open; the
approved history-replacement plan is not executed under local-CI-only authority.
No public release or original history replacement occurred.
See [candidate report](clean-history-candidate.md). Older results below retain
their original checkout/time scope rather than becoming candidate release proof.

Date: 2026-10-09. Local, uncommitted active-launch development; not a public
release qualification or a claim that every runtime error is eliminated.

## Verified Repairs

- Assemble SSE events at blank-line boundaries, including multiple `data:` lines,
  CRLF, event labels and Unicode text. Do not invent a terminal event at EOF.
  A valid multiline completion reproduced the reported `truncated_stream`
  symptom before the repair; the regression now completes without another call.
- Opt the main Codex loop and unsigned Agent Blocks into one bounded, tool-free
  continuation after retryable partial text. Preserve completed tool results;
  buffer/validate the suffix and reject actions, empty/oversized text and a second
  interruption. Keep low-level no-replay retries, cancellation, permanent-error
  handling, signed dispatch budgets and post-mutation verification intact.
- Cap continuation text at 64 KiB, use a 30-second receive deadline with socket
  interruption for keepalive-only streams, and retain safe missing-terminal
  diagnostics without storing response bodies or credentials.
- Reuse only successful companion readiness briefly; share one fresh Ollama
  discovery snapshot per routing operation. Never cache permission or inference.
- Bound initial preapproved checkpoint waiting and make evidence follow-ups
  nonblocking. Poll at model-round boundaries, identify superseded evidence,
  suppress late publication and prohibit later specialist dispatch after closing.
  Cancellation cannot retract a call already sent.
- Reject oversized integer confidence/probability values without an overflow
  crash; preserve strict numeric types. Capture checked subprocess pipe handles
  and use explicit dynamic imports for the separately installed CLM companion.

## Observations

The original supplied disconnect has no wire capture, so its exact upstream cause
remains unknown. A fresh real Astra response included `response.completed` with
zero malformed data lines (3,941 ms). A separate live fault-injection cut after
its first text chunk recovered through one real text-only request (5,724 ms).
This injected disconnect is not proof of another natural service failure.

Status-only discovery measured 726 ms before these changes. In a separate
same-configuration repaired sample, cold discovery took 863 ms and two warm
checks took 323/264 ms. These are local observations, not a controlled end-to-end
speedup or a provider-quality benchmark. Unready Decider/CLM were still excluded;
installed-but-unloaded Clef variants/Tev1 were not counted as successful inference.

A real unpinned Kimi K3/Jev agent review consumed an initial routing deferral and
a validated evidence judgment, preserved fixture bytes and completed in 8,412 ms.
Initial route probability 0.76 remained below 0.80; thresholds were not lowered.
The owner-selected Astra model, the Kimi fixture agent and replayed failure tests
are distinct evidence scopes.

## Qualification And Remaining Work

### Installed-Environment Follow-up

The three package typing errors are repaired; mypy now passes all 271 modules.
The native catalog now has all 16 operations. Applicability remains an approved
mutation with exact readback, not an access grant. The active scorecard is schema
3 with nine one-point checks and maximum nine; missing checks remain fail-closed.
Focused discovery consumers and non-hermetic logo/private-path fixtures are fixed.

The frozen noneditable environment exposed a protected-search worker importing a
different package tree. Its bootstrap now selects the parent's trusted runtime
while keeping isolation and the exact root check. Installed-source parity passed.
The narrow urllib3 2.8.0 lock update cleared three dependency advisories. Current
source privacy scan passes; reachable history still reports five old findings.
History rewriting/publication is outside this goal's current authority.

Live Astra actual-agent cases passed: explicit Jev typed decision consumed before
the read (14,095 ms); injected stream loss after a completed text frame recovered
through one tool-free continuation with one read (12,811 ms); injected provider
timeout was reported without retry or a fake validated answer (15,882 ms).
Automatic unrestricted routing separately demonstrated legitimate deferral. Late
evidence results were recorded but were not consumed by an already-running model
request. These are control-flow fixtures, not calibration or production outages.

Earlier twelve-failure counts below are retained as history, not current readiness.
Qualification refresh and final full-suite results remain independently tracked.

Frozen noneditable qualification now verifies the current source: Nathan's 17
correctness probes and 31 workloads passed with no duplicate mutations, policy
escapes or unverified completions. M8's nine local metrics pass, five external
browser metrics remain blocked and none fail. New evidence rows bind the actually
generated artifacts without changing requirement statuses; M9 remains honestly
blocked, not lifted by these local results. All three stored-artifact checks pass.
Public source, locked dependencies, installed-source parity, Ruff and all-package
mypy pass. Reachable-history privacy findings still block publication; no history
rewrite or release is authorized or claimed.

Final frozen noneditable Python: **7,379 passed, 41 skipped, zero failures** in
145.432 seconds. The prior twelve failures and newly exposed installed-search
failures are resolved; the old failed receipts remain available. Qualification
checks: 152 passed. Core focused checks: 373 passed, one skipped. Go race and seven
Rust tests pass. Source still matches the qualified frozen product fingerprint;
the owner launcher still imports this checkout with Astra/Jev/0.80 unchanged.
Publication remains unqualified due to reachable-history privacy and external
browser/signing requirements, not a passing local runtime test disguised as one.

Regression coverage includes multiline/unfinished SSE, EOF/timeout/provider
failure, repeated-prefix deduplication, cancellation, malicious tool output,
keepalive-only socket deadlines, missing verification after mutation, signed
Agent Block exclusions, cache expiry/identity changes and late checkpoint results.
Commands use the frozen dev/supply-chain test environment; exact JUnit receipts
and final counts are recorded in the lessons document: 589 focused passes and
7,340 complete-suite passes, 41 skips and the same 12 failures (151.120 seconds).

Go race tests and seven Rust tests passed. Earlier broad local Python testing
still reported twelve failures: native Continuum 15/16-operation drift, stale
hardening artifacts, slash discovery, scorecard fixtures and seven logo tests.
Do not regenerate receipts or weaken tests to conceal those discrepancies.
Package-wide mypy also found pre-existing sticky-footer/display/TOML typing
issues; the seven repaired decision/stream source modules pass scoped mypy.

Remaining goal work includes the complete exact-source local CI gates, a live
main-agent provider-failure/continuation qualification, remaining relevant defects
and final source-bound artifact generation. Historical green runs and isolated
tests cannot establish that the complete goal or a public release is ready.
Normal configuration, model selection, permissions and launcher are unchanged.
Existing processes require restart to load Python edits; reference-index refresh
does not reload runtime code. No commit, push, tag or publication occurred.

## Sources

Final edge-case audit: a timeout/done race no longer falsely fails a checkpoint;
turn closure cannot publish unconsumed advice; continuation deadlines are checked
again after silent terminal events. Latest full frozen Python: **7,383 passed,
41 skipped, zero failures** (138.627 s), with 184 checkpoint/stream and 117
protection checks. Current qualification artifacts/ledger/audit agree. See
[completion audit](runtime-goal-completion-audit.md) for the original full goal's
requirement-by-requirement evidence and the still-blocked publication boundaries.

- [SSE framing standard](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream)
- [OpenAI streaming guide](https://developers.openai.com/api/docs/guides/streaming-responses)
- `algo_cli/chatgpt_client.py`, `decision_readiness.py`, `decision_checkpoints.py`
- `tests/test_chatgpt_stream_recovery.py`, `test_decision_readiness.py`,
  `test_decision_checkpoints.py`, `test_jev_kernel.py`, `test_agent_pipeline.py`
