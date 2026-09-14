# 0.19.2 Release Scope

State: prepared release candidate. Publication requires exact-source local and
hosted qualification, an immutable `v0.19.2` tag, protected release-authority
approval, exact artifact verification, public registry verification, and clean
install and predecessor-upgrade checks.

## Included Scope

- Detect standalone `uv pip` installations from installed distribution
  metadata and upgrade them with `uv pip --python`, without requiring a bundled
  `pip` module.
- Preserve custom `uv tool` ownership and configured tool directories.
- Retry only absent or partial-but-exact PyPI visibility after upload, with a
  fixed six-observation ceiling and 30 seconds of scheduled backoff.
- Reject malformed successful PyPI responses and missing SHA-256 fields instead
  of treating them as an absent release.
- Refresh the Boron managed-browser pin, Debian package digest and lock, hosted
  evidence fixtures, and documentation for Chrome `153.0.8010.36`.
- Capture the protected `v0.19.2` draft through fixed GET requests, deriving the
  GitHub-assigned release ID from exactly one bounded tag match while retaining
  source, asset, digest, redirect, freshness, and approval controls.

## Excluded Scope

- The unfinished contained test runner is not stable release functionality.
- M8 external-browser and native authority work that remains blocked is not
  represented as complete.
- Historical `v0.19.0` and `v0.19.1` drafts remain untouched and unpublished.
- Published `v0.19.1.post1` artifacts and its immutable tag remain unchanged.

## Required Qualification

- Frozen non-editable dependency sync, installed-source parity, lint, typing,
  source and dependency scans, full local tests, and reproducible distributions.
- Source-bound M8 and M9 evidence regeneration with every blocked metric
  retained as blocked.
- Exact protected-main CI on Linux, macOS, and native Windows, including public
  browser qualification and attestation.
- Build, wheel-from-sdist, metadata, SBOM, provenance, checksum, and immutable
  release-asset verification.
- Clean public installation of `0.19.2` and an isolated real update from public
  `0.19.1.post1`, preserving configuration, credentials, memory, workspace
  settings, private files, and SQLite integrity.

## Publication Receipt

Not yet published. Record the exact source and publisher revisions, CI and
release workflow runs, artifact sizes and SHA-256 digests, GitHub immutable
release state, PyPI index state, and public install/upgrade results here only
after each result is observed.
