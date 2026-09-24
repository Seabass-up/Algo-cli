# 0.20.1 Release Scope

State: published on 2026-09-23 from the immutable `v0.20.1` tag through the
protected release workflow with trusted PyPI publishing.

## Included Scope

- Bug, quality-of-life and terminal UI fixes from a codebase review,
  each with a regression test: Rich markup safety in tool display, tool-failure
  classification, partial ripgrep results and fallback globs, `run_shell`
  whitespace, schemeless Ollama hosts, agent-block routing, one-shot `done`
  events, `/agent` parsing and cancellation, memory file safety, lessons
  indexing, memory-capture filtering, compaction fallback reporting, code-RAG
  embedding identity and deletions, and harness future-mtime handling.
- Terminal UI: Ctrl+C line clearing, immediate footer refresh, elapsed-time
  model wait, tail-following thinking panel, and classified error hints.
- Predecessor upgrade smoke baseline moved to public `0.20.0`.

## Excluded Scope

- M8 external-browser and native authority work that remains blocked is not
  represented as complete.
- Published `v0.20.0` artifacts and its immutable tag remain unchanged.

## Required Qualification

- Frozen non-editable dependency sync, installed-source parity, lint, typing,
  source and dependency scans, full local tests, and reproducible distributions.
- Source-bound Nathan, M8 and M9 evidence regeneration with every blocked
  metric retained as blocked.
- Exact protected-main CI on Linux, macOS, and native Windows, including public
  browser qualification and attestation.
- Build, wheel-from-sdist, metadata, SBOM, provenance, checksum, and immutable
  release-asset verification.
- Clean public installation of `0.20.1` and an isolated real update from public
  `0.20.0`, preserving configuration, credentials, memory, workspace settings,
  private files, and SQLite integrity.

## Publication Receipt

- Source: tag `v0.20.1` -> merge commit
  `2fae0bfbf915af0cd204ad81e171d499333efc82` (PR #69), which is also the
  publisher revision. Push CI run
  [35914889350](https://github.com/Seabass-up/Algo-cli/actions/runs/35914889350)
  succeeded on that commit, including Boron public-browser qualification and
  attestation.
- Release run
  [35917346731](https://github.com/Seabass-up/Algo-cli/actions/runs/35917346731)
  uploaded both distributions to PyPI, then failed its immediate visibility
  query with `release_pypi_missing` after six observations. The files became
  visible shortly afterward. This failure remains recorded as failed.
- Recovery run
  [35919389045](https://github.com/Seabass-up/Algo-cli/actions/runs/35919389045)
  took the `draft-exact` path, observed the exact PyPI file set, skipped
  re-upload, revalidated policy, and published the immutable GitHub release at
  2026-09-23T21:18:51Z. The post-publication policy drift check passed.

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `algo_cli_runtime-0.20.1-py3-none-any.whl` | 1317884 | `5c897032c12cf2be568059e3c3ecf70681e25615358f01cfe82d50da9fd260cd` |
| `algo_cli_runtime-0.20.1.tar.gz` | 1188889 | `bbeaba8ab33b5d2ad65aecad0e0e9c319e7f742f5fad2ada461a67b1a62608ba` |

The PyPI files are non-yanked and byte-identical to the GitHub release assets.
A clean `pip install algo-cli-runtime==0.20.1` in an isolated Python 3.12
environment reported `Algo CLI v0.20.1`. The downloaded wheel and sdist passed
`check_public_release.py --artifacts-only`. In push CI run 35914889350 the
installed-wheel jobs on Linux, macOS and Windows upgraded from public `0.20.0`
(recorded `baseline_version` 0.20.0 with the published wheel digest) through
pip, pipx, pipx with the uv backend, uv, and a pip-free `uv pip` environment
without changing user state. Their workflow step labels still name 0.18.0.
