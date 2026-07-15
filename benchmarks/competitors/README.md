# Competitor harness benchmark

This suite compares agent harnesses separately from model quality. A fair
same-model cell uses the same Ollama model, task fixture, machine, time limit,
permission boundary, and external checker for every harness. Native/default
model comparisons belong in a separate lane and must not be merged into the
same-model ranking.

The suite is intentionally fail-closed:

- every run receives fresh workspace, artifact, and harness-state directories;
- task definitions, prompts, and protected fixture inputs are hashed;
- generated caches are ignored, but unexpected edits fail the file-scope gate;
- the checker must fail before the agent runs and pass afterward;
- missing auth, unsupported headless modes, failed isolation, and license gates
  are reported as blocked instead of receiving invented scores;
- a broad "better than" claim is never inferred from this small draft corpus.

## Safety and prerequisites

The runner does not install competitors, authenticate accounts, accept license
terms, or download models. Install and authorize only the harnesses you intend
to measure, review their terms, and start Ollama with the selected model before
running a cell. `runner.py --list` reports the locally discoverable matrix.

Measured agents receive automatic write approval because every run gets a
disposable workspace and isolated home/config directories. The public runner
passes only a small allowlist of system environment variables and replaces
provider credentials with local Ollama placeholders where an adapter requires
one. Parent API keys, cloud credentials, Git credentials, and proxy settings
are not inherited.

This is process-level isolation, not a hardened VM boundary. Known web tools
are disabled where a harness supports it, but the runner does not enforce
OS-level read or network isolation. Run third-party agents on a disposable
machine or VM when stronger containment is required. Raw artifacts may contain
prompts, model output, command text, and absolute paths; inspect them before
sharing.

## Tasks

The v3 draft corpus currently covers:

1. a minimal Python code repair;
2. recovery from misleading documentation and a decoy configuration file;
3. a stale-memory/RAG conflict where live files must win;
4. a medium-repository evidence-reconciliation rollout with three differently
   shaped service configs, an authoritative registry, protected live inputs,
   stale decoys, and an artifact receipt.

Every summary records `task_suite_sha256`, a digest of the selected prompts and
fixtures. Freeze and publish that digest before interpreting a new task's
results so the corpus cannot be tuned after scores are known.

These tasks are useful release regressions, but they are not yet a representative
sample of software engineering. Add multi-file implementation, refactoring,
security, long-context, cross-language, and recovery tasks before using results
for broad marketing.

## Commands

```bash
# Show every requested product and why it is runnable or blocked
python3 benchmarks/competitors/runner.py --list

# One run per task/harness cell
python3 benchmarks/competitors/runner.py \
  --harness algo_cli,codex_cli,claude_code,opencode,pi \
  --repetitions 1

# Release-candidate minimum: three repetitions per cell
python3 benchmarks/competitors/runner.py \
  --harness algo_cli,codex_cli,claude_code,opencode,pi \
  --repetitions 3 \
  --warmup-model
```

`--warmup-model` performs one short direct Ollama generation before any scored
harness process starts and keeps the model loaded for two hours by default.
The warm-up duration is excluded from every harness score, and its result,
duration, keep-alive setting, and output digests are recorded in
`warmup_receipt.json` and the summary protocol. A failed warm-up aborts the
benchmark before the first scored cell.

Results default to `benchmark-results/`, which is ignored by Git. Each run keeps
raw stdout/stderr, the rendered prompt, checker receipts, file-scope evidence,
and normalized metrics. Algo CLI's one-shot stream additionally reports one
content-free `model_round` receipt per request, including trigger, provider
timings, prompt/completion tokens, schema count, context-source estimates, and
supersession savings. These diagnostics are not ranking inputs. Review and
sanitize raw artifacts before publishing.

After a complete release cell, publish only validated aggregate data:

```bash
python3 benchmarks/competitors/publish_website.py \
  benchmark-results/<cell>/summary.json \
  --source-revision "$(git rev-parse HEAD)" \
  --hardware-description "Apple M5 Max (18-core CPU, 48 GB unified memory)" \
  --os-description "macOS 27.0"
```

The publisher requires the full warmed 11-harness, four-task, three-repetition
v3 cell and rechecks baseline failures, protected-input receipts, cell completeness,
and the task digest. It writes the website summary and CSV without exporting
raw prompts, model output, executable paths, or local workspace metadata.

## Competitor classification

`runner.py --list` includes the entire requested comparison set. Terminal tools
with deterministic headless modes can enter measured cells. Desktop-only apps,
an unidentified Mercury harness, auth-gated Grok Build, a crashing Cline binary,
and Pool before the user accepts its EULA remain explicitly blocked.

`Assistants` is an Ollama documentation category, not a separate harness.
