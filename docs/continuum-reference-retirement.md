# Continuum And Reference Runtime Retirement

Date: 2026-10-08. Local working-tree repair, based on `6cac02a4`.

## Scope

The PATH launcher imports the editable `Algo-cli-launch-v0.20.2` checkout, not
the separate review workspace or clean release checkout. Existing unrelated
changes were preserved. No commit, push, tag, hosted CI or public release occurred.

Native Continuum remains the mutable memory backend. CLI reference lookup is
lexical. Main chat and Agent Blocks do not generate query/corpus embeddings,
recall Intuition vectors, or auto-inject Lab-index. Retired tools and commands
are absent from discovery; compatibility entry points reject before provider,
subprocess or graph-note IO. Retired configuration inputs cannot enable them,
and normal config serialization removes the old reference-provider fields.

Historical records, protected-root denials and retired-selector rejection remain.
Pure historical vector utilities and installed model weights were not deleted.
A decision model's internal encoder is not a CLI memory embedding API. The old
Lab guide is archived; a short retirement notice replaces its live resource.

Two retrieval defects were also repaired: malformed non-object cache rows are
dropped before consumers see them, and writes beside an exact-file catalog root
do not invalidate that root. Actual catalog edits still invalidate it.

## Verification

- Frozen isolated Python 3.11 development environment; no normal-env reinstall.
- Focused decision, authority, retirement and retrieval run: 564 passed.
- Separate pipeline, public-document and version-manifest run: 190 passed.
- `ruff check algo_cli tests`: passed. `git diff --check`: passed.
- Scoped mypy over main, tools, checkpoints and coordinator: four files passed.
- Runtime registry: ready, with 80 callable tools and 96 declared slash commands.
- Actual launcher: native Continuum selected; doctor integrity verification
  succeeded and a 25,399-byte native context packet was returned. Packet contents
  are not stored in this report or forwarded as a model-review fixture.
- Public synthetic automatic review: Kimi K3 received Jev task and evidence
  checkpoints without being instructed to call Jev. Native typed results reached
  its requests before the next step. The model read and identified the `+2`
  versus `+1` defect; the read-only fixture remained unchanged. Total 8,903.654 ms;
  task 1,257.945 ms; evidence 1,086.898 ms. No embedding startup step appeared.

The latest complete Python run records 7,220 passed, 41 skipped and 12 failed.
These remaining categories were already present before retirement: native
Continuum operation-count drift (15 versus 16), stale M8/Nathan receipts, slash
catalog consumption, a scorecard fixture and seven logo assertions. Focused green
checks are not an all-green release. Final-tree confirmation is retained in the
local test receipt, not inferred from older qualification artifacts.

## Lessons And Limits

Use a runtime-owned checkpoint when a required advisory check must not depend
on the main model remembering a tool. Background calls need live revocable
authority and bounded state; model votes do not grant permission or verification.
Signed Agent Block contracts receive no unbudgeted automatic model calls.

Retirement requires cleaning execution, discovery, policies, config persistence,
documentation and tests together. Keep provenance, denial and cancellation tests;
convert obsolete positive-feature expectations into no-IO refusal regressions.
Jev helped triage the migration but did not substitute for executable checks.

Existing processes must restart to load the edited runtime. Harness refresh only
refreshes reference data. Full-suite repair, exact release qualification, hosted
platform coverage, offline coordinator quality and public publication remain
separate work. No owner catalog, private kernels, credentials or context packet
was added to packaged resources.
