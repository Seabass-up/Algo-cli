# Algo CLI Competitive Rating Contract

The competitive rating is a reproducible comparison, not a branding claim and
not the same thing as Algo CLI's internal `/harness score`.

## Source arithmetic

The attached 2026-07-10 matrix declares five equally weighted axes. Algo CLI
always recomputes their arithmetic mean:

| Project | Architecture | Quality | Tests | Safety | Local-first | Mean |
|---|---:|---:|---:|---:|---:|---:|
| QodeX | 8 | 8 | 8 | 9 | 9 | 8.4 |
| Algo CLI | 8 | 8 | 9 | 7 | 9 | 8.2 |
| OpenAgentd | 9 | 9 | 7 | 8 | 7 | 8.0 |

The source's reported OpenAgentd value of 8.8 is arithmetically invalid and is
never used for ranking. No local evidence is allowed to add axis points.

## Leader gates

All ten gates are critical and worth one point:

1. strict architecture dominance,
2. strict code-quality dominance,
3. strict test-coverage dominance,
4. strict security/safety dominance,
5. strict local-first dominance,
6. a reproducible benchmark covering every project with at least three runs,
7. live production-path receipts for every required Algo CLI algorithm check,
8. identical workload, hardware, and model with raw per-project results,
9. a clean landed commit with a verification artifact, and
10. revision-pinned source artifacts for every competitor.

Ties do not establish dominance. Missing, malformed, dirty, local-only, or
unknown evidence fails closed. A leader claim is permitted only when Algo CLI
is the unique corrected rank 1 and all ten gates pass.

## Runtime commands

- `/harness score`: internal index, retrieval, memory, benchmark, and algorithm readiness.
- `/harness compare`: corrected external matrix plus strict leader gates and local probe receipts.
- `harness_competitive_rating`: model-callable JSON form of `/harness compare`.

Local retrieval and algorithm probes are included in the comparison report so
a local regression is visible, but they are explicitly labeled as insufficient
cross-harness evidence.

The release workflow additionally treats Ruff, the core-runtime mypy contract,
the full test suite, and a measured 57% combined line/branch coverage floor as
blocking. Coverage XML is retained as a CI artifact; Linux, Windows, macOS,
Rust-indexer, and Go-gateway checks are separate release evidence.
