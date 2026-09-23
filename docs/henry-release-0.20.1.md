# 0.20.1 Release Scope

State: prepared release candidate. Publication requires exact-source local and
hosted qualification, an immutable `v0.20.1` tag, protected release-authority
approval, exact artifact verification, public registry verification, and clean
install and predecessor-upgrade checks.

## Included Scope

- Bug, quality-of-life and terminal UI fixes from a Jev-ranked codebase review,
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

- The Jev Shadow routing layer is not part of this release.
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

Not yet published. Record the exact source and publisher revisions, CI and
release workflow runs, artifact sizes and SHA-256 digests, GitHub immutable
release state, PyPI index state, and public install/upgrade results here only
after each result is observed.
