# 0.20.2 Release Scope

State: local candidate, not yet qualified. Publication requires exact-source
local and hosted qualification, an immutable `v0.20.2` tag, protected
release-authority approval, exact artifact verification, public registry
verification, and clean install and predecessor-upgrade checks.

## Included Scope

- Security: captured `session_command` output is stripped of terminal control
  sequences before secret redaction, and the secret and Bearer patterns no
  longer depend on a leading word boundary (PR #81). The published 0.20.1 is
  affected when `FORCE_COLOR` is set.
- Runtime reliability (PR #73): reachable `jev_kernel_status` grant,
  actionable missing-grant denials, no in-turn retry of identical refusals,
  slash-command capabilities in `action_search`, clearer Jev tool text, and
  embedding-backlog explanations. No permission guard is weakened.
- Terminal UI slices 1 and 2 (PRs #72, #74): one semantic token table with
  contrast floors, themed Markdown and code, a single detected color profile
  applied to output, prompt and footer, 16-color and no-color modes.
- Boron managed-browser pin refreshed to Chrome `154.0.8037.57`.
- Predecessor upgrade smoke baseline moved to public `0.20.1`.

## Excluded Scope

- Open PRs #75 through #80 (config backups, release friction, Windows parity
  fixtures, scenario harness, capability readiness, cooperative cancel) are
  not part of this release.
- M8 external-browser and native authority work that remains blocked is not
  represented as complete.
- Published `v0.20.0` and `v0.20.1` artifacts and their immutable tags remain
  unchanged.

## Required Qualification

- Frozen non-editable dependency sync, installed-source parity, lint, typing,
  compilation, source and dependency scans, full local tests, and reproducible
  distributions.
- Source-bound Nathan, M8 and M9 evidence regeneration with every blocked
  metric retained as blocked.
- Exact protected-main CI on Linux, macOS, and native Windows, including public
  browser qualification and attestation on the refreshed Chrome pin.
- Build, wheel-from-sdist, metadata, SBOM, provenance, checksum, and immutable
  release-asset verification.
- Clean public installation of `0.20.2` and an isolated real update from public
  `0.20.1`, preserving configuration, credentials, memory, workspace settings,
  private files, and SQLite integrity.

## Publication Receipt

Not yet published. Record the exact source and publisher revisions, CI and
release workflow runs, artifact sizes and SHA-256 digests, GitHub immutable
release state, PyPI index state, and public install/upgrade results here only
after each result is observed.
