# 0.20.0 Release Scope

State: published and verified on 2026-09-22. Publication requires exact-source local and
hosted qualification, an immutable `v0.20.0` tag, protected release-authority
approval, exact artifact verification, public registry verification, and clean
install and predecessor-upgrade checks.

## Included Scope

- Explicit user-only, session-only YOLO activation with owner, process,
  workspace, child-agent, and persistence isolation.
- In-scope YOLO action preapproval, curated owner-bound read-only sibling
  workspace observation, typed-program compatibility, and honest admission
  diagnostics under the existing safety and verification boundaries.
- Unbounded interactive work and verification-recovery rounds only while YOLO
  is active; ordinary modes retain their configured ceilings.
- Explicit fail-closed Continuum selection and direct native routing, scoped
  context and revision-bound writes. Retired memory backends, dependencies,
  tools and active qualification branches are removed. Historical state remains
  unchanged; retired configuration cannot implicitly select Continuum.
- Cloud-model context-cap correction and `/reload` preservation of live,
  nonpersistent YOLO activation.
- Persistent generation status on supported TTYs, including resize, JSON,
  runtime-cap, VT-capability, and POSIX signal cleanup.
- Slash-command inspection and output-capture correction, model-profile type
  narrowing, native crash-identity compatibility, and the fixed dependency
  lock inherited from the merged sticky-footer work.
- Personal-library separation: `docs/ALGO.md` ships as an empty template;
  each user's catalog lives at `~/.algo_cli/ALGO.md` and personal kernels in
  `~/.algo_cli/kernels/`. The Acrobat, finance, and construction kernels are
  removed from the package, and the public-release scan rejects populated
  catalogs and personal kernel paths.
- Optional advisory Jev kernel through the separately installed
  `jev-workflows` companion, with local lint and approval-gated inference.
- Compact `/` command menu, grouped `/help`, consistent prompt and in-progress
  footer rendering, and bounded Continuum context-size retry at startup.

## Excluded Scope

- The unfinished contained test runner is not stable release functionality.
- M8 external-browser and native authority work that remains blocked is not
  represented as complete.
- No retired memory backend is a supported option or a Continuum compatibility
  layer. Historical records and fail-closed retired-input recognition are not
  active backend support.
- Historical tags and published artifacts remain immutable and unchanged.
- No claim is made that local token estimates prove live provider quality,
  cross-harness superiority, or measured YOLO effectiveness.

## Required Qualification

- Frozen non-editable dependency sync, installed-source parity, lint, typing,
  compilation, source and dependency scans, full local tests, and reproducible
  distributions.
- Source-bound Alice, Nathan, M8, and M9 evidence regeneration with every
  blocked metric retained as blocked.
- Exact protected-main CI on Linux, macOS, and native Windows, including public
  browser contracts and every required release-policy gate.
- Build, wheel-from-sdist, metadata, SBOM, provenance, checksum, and immutable
  release-asset verification.
- Clean public installation of `0.20.0` and an isolated real update from public
  `0.19.2`, preserving configuration, credentials, Continuum and historical memory state,
  workspace settings, private files, file modes, and SQLite integrity.

## Publication Receipt

Observed on 2026-09-22 after the protected publish workflow completed.

- Source revision and publisher revision: `55a7ec5328892a07129c48e370d175eff8e0eff6`
  (`main`, merge of PR #66 after PR #65). Tag `v0.20.0` → `55a7ec5`.
- Qualifying CI on `main`: run 35718750599 (all jobs, including Boron
  public-browser boundary and attestation on Chrome `153.0.8010.52`).
- Publish workflow: run 35756316039, conclusion success; `release-authority`
  approvals given by the owner at policy capture, pre-PyPI, and pre-publish.
- GitHub release: https://github.com/Seabass-up/Algo-cli/releases/tag/v0.20.0,
  non-draft, non-prerelease, immutable, 17 assets.
- Artifacts (identical on GitHub and PyPI, not yanked):
  - `algo_cli_runtime-0.20.0-py3-none-any.whl` 1304126 bytes
    sha256 `dfd8f45d944bc0ea0e0645121f24be34d3ace54ff7c13497ee304815cc6fb3f1`
  - `algo_cli_runtime-0.20.0.tar.gz` 1173353 bytes
    sha256 `4bad73b8afb0692011237fc08c4af073f27e5cae632dde267afc5f96f2e06a57`
- PyPI: https://pypi.org/project/algo-cli-runtime/0.20.0/ ; project latest
  reported `0.20.0` after a brief post-upload lag during which the project
  endpoint still returned `0.19.2` while the version endpoint was already exact.
- Clean public install (isolated home, uv venv, Python 3.11): `0.20.0`;
  packaged `ALGO.md` is the empty template; `algo-cli config status` runs.
- Real upgrade: public `0.19.2` → `algo-cli update` → `0.20.0`. Seeded
  config, env, identity, private file, and SQLite state were byte-identical
  with unchanged modes afterwards; SQLite integrity ok; a second update
  reported no newer version and changed nothing.
- Not claimed: external-browser completion, native signing, live model
  quality, or M8/M9 lift; those remain blocked as recorded in the ledger.
