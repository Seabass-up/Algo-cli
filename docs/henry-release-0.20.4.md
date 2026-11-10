# 0.20.4 Core CLI Release Scope

State: release candidate. Publication is conditional on the gates below; this
document does not claim that any of them passed. Existing immutable tags, normal
launchers and owner state stay unchanged.

## Included Scope

- Bounded runtime diagnostics: public/runtime metadata sections, exact read-only
  `/selfcheck` and `/context` forms, explicit output visibility and pagination,
  and capability/format/runner/version checks for native MLX decision models.
  Model-called diagnostics receive the live session configuration; a missing
  configuration is reported as unavailable, not as false or zero.
- Repairs from the 2026-11-09 whole-codebase review: byte-preserving edits,
  harness freshness that honours build exclusions, a local model-server start
  cooldown, no-test commands rejected as verification, goal completion only
  from accepted text, word-boundary task routing, and smaller fixes listed in
  `CHANGELOG.md`. Regression tests: `tests/test_bughunt_*_20261109.py`,
  `tests/test_protected_diagnostics.py` and `tests/test_runtime_diagnostics_config.py`.
- `/ship commit --files` anchors selected paths at the workspace root.
- Release capture and authority validators move from the immutable `v0.20.3`
  tag to `v0.20.4`; the old tags are rejected.

## Excluded Scope

The signed native-browser companion and unfinished native authority bridge remain
disabled and unqualified. Blocked M8/M9 external metrics and false public-claim
eligibility are retained. The managed Chrome pin is unchanged at
`155.0.8059.84`: the freshness gate admits it only while the newest stable
release (157.0.8103.50, served from 2026-11-09T20:47Z) is less than 72 hours old,
so a pin refresh is due before any later release. No owner catalog, custom
kernel, credential, memory data or private local adapter is included.

## Required Gates

1. Final-source frozen installed parity, complete local tests, quality gates,
   privacy scans over source, history, wheel and sdist, and refreshed receipts.
2. Exact-source hosted CI on `main` with clean wheel installation, sdist rebuild
   and the hosted managed-browser evidence.
3. Protected publisher/source identity, environment approvals, exact asset,
   SBOM, provenance and checksum checks, immutable tag and trusted publishing.
4. Public registry visibility, then a clean install of the predecessor and an
   `algo-cli update` to this version.

Record runs and install receipts only when those observations exist.
