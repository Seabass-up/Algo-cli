# Public release checklist

Use this checklist before publishing a tag or changing repository visibility.

## Source and history

- Run `python scripts/check_public_release.py`.
- Run `python scripts/check_public_history.py` from a full clone.
- The default history scan enforces privacy-preserving commit and tag emails for
  the one-time sanitized publication audit. Routine public CI uses
  `--allow-contributor-identities` for commit and tag metadata while continuing
  to reject private paths, secret-bearing reachable blobs, machine paths, and
  private ref names.
- Run a history-aware secret and privacy scanner over every branch and tag.
- Publish from a reviewed squashed/orphan history or a new public repository when the development history contains removed private blobs or author metadata.
- Do not copy old branches or tags into the sanitized public repository.
- Use repository-hosted privacy-preserving author metadata for the public root commit.
- Build and review the exact candidate on a private branch, then require green
  hosted CI for that commit before changing visibility.
- Require the pinned native-amd64 Boron CI cell to fetch current official Linux
  stable evidence, attest zero-or-bounded update lag, and complete one isolated
  browser/broker navigation. A local emulated run is not substitute evidence.

## Package

```bash
VERSION="$(python -c 'import algo_cli; print(algo_cli.__version__)')"
python scripts/check_release_version.py --tag "v${VERSION}"
uv sync --frozen --no-install-project --extra release
uv run --frozen --no-install-project --extra release python -m build --no-isolation
python -m twine check dist/*
python scripts/check_public_release.py --artifacts-only \
  --artifact "dist/algo_cli_runtime-${VERSION}-py3-none-any.whl" \
  --artifact "dist/algo_cli_runtime-${VERSION}.tar.gz"
python scripts/oliver_smoke_wheel_install.py dist
```

Confirm the sdist contains no `website/`, `node_modules/`, virtual environment,
cache, or generated build paths. The website is deployed independently and is
not part of the Python source distribution.

Install the wheel in an empty virtual environment and empty home directory. Verify `algo-cli --help`, `algo-cli --version`, `algo-cli doctor`, `python -m algo_cli --help`, and `scripts/smoke_installed_release.py`.

## Hosting

- Create or rename the public repository to match the URLs in `pyproject.toml` and `README.md`.
- Enable private vulnerability reporting and branch protection.
- Enable GitHub immutable releases.
- Create exactly one active repository ruleset targeting tags with the exact
  include pattern `refs/tags/v*`, no exclusions, no bypass actors, and both
  update and deletion prevention. Creation remains allowed.
- Create a protected GitHub environment named `release-authority`. Require
  protected branches, at least one external required reviewer, self-review
  prevention, and disabled administrator bypass. Set the repository Actions
  variable `ALGO_RELEASE_AUTHORITY_READY` to the exact value `true` only after
  those controls have been independently reviewed.
- Install a repository-scoped policy-audit GitHub App for `Seabass-up/Algo-cli`.
  The App needs Administration write capability because GitHub otherwise omits
  `bypass_actors` from detailed ruleset responses, but the release workflow is
  fixed to GET-only policy calls. Store its client ID as the environment
  variable `ALGO_RELEASE_POLICY_APP_CLIENT_ID` and its private key as the
  environment secret `ALGO_RELEASE_POLICY_APP_PRIVATE_KEY`. The two fixed,
  no-checkout mutation jobs mint this token only after environment approval,
  repeat exact GET-only policy checks, require explicit successful revocation,
  and unset it before the PyPI action or release PATCH. Never expose it to
  checkout, package code, uploaded payloads, the PyPI action, or the PATCH
  command.
- Configure PyPI Trusted Publishing for `.github/workflows/oliver-release.yml`
  and the same protected `release-authority` environment.
- Before readiness, inventory every PyPI project and organization Owner,
  Maintainer, API token, and Trusted Publisher that can affect
  `algo-cli-runtime`. Remove every unintended principal, token, or publisher and
  record the deliberately retained human authorities. The only intended CI
  publisher is the exact `Seabass-up/Algo-cli` +
  `.github/workflows/oliver-release.yml` + `release-authority` tuple; reject any
  additional repository, workflow, environment, or pending publisher entry.
- Confirm the tag is exactly `v` plus `algo_cli.__version__` and points at the
  reviewed protected-main source revision. Create an exact non-prerelease
  **draft** release whose target is that 40-character source SHA; do not publish
  the GitHub release manually.
- From the protected `main` branch, manually dispatch `Publish release` with
  that tag. The workflow validates dispatch and environment authority, captures
  immutable-release and no-bypass tag policy, binds the tag to the protected
  source and same-SHA hosted Boron run, builds twice from independent extracts
  of one verified Git archive, runs verification, and attaches byte-verified
  evidence to the draft. It then rechecks policy, publishes or repairs only an
  absent/partial-exact PyPI file set, rechecks policy again, and publishes the
  immutable GitHub release last.
- If an upstream Boron job must be retried, use **Re-run all jobs** so its
  attempt-scoped report, attestation, and artifact name remain bound together.
  Release-workflow artifacts use producer artifact IDs and attempt-unique names;
  they are never overwritten. If PyPI partially succeeds, start a fresh manual
  dispatch: the `draft-exact` path downloads, cryptographically verifies, and
  reuses the attached package and Sigstore bytes before fresh policy and PyPI
  gates. A final post-PATCH failure may be retried through the workflow's
  read-only `published-exact` reconciliation path. A fresh dispatch with a
  partial draft asset set fails closed; recover that interrupted attachment with
  the original run's **Re-run failed jobs** path so its original attestation
  artifacts remain authoritative. That retry deliberately stops at the
  `GITHUB_RUN_ATTEMPT == 1` mutation guard; after the attachment is complete,
  start a fresh manual dispatch so the `draft-exact` recovery path performs new
  policy gates before any PyPI or release mutation.

The release remains intentionally blocked until immutable releases, the exact
no-bypass tag ruleset, the protected `release-authority` environment, its
readiness variable, and the policy-audit App credentials all exist and pass the
hosted checks. Do not weaken a missing or red authority gate and do not create
these external controls from the release workflow.

Before changing visibility, set the repository homepage to
`https://algo-cli.com`, enable Dependabot alerts where the private plan allows
it, and keep the website release manifest in release-candidate state.

Immediately after changing visibility:

- Enable private vulnerability reporting, secret scanning, push protection,
  Dependabot alerts, and a `main` protection ruleset.
- Verify anonymous clone, issue links, the security-reporting flow, and every
  README and website source link.
- Update the website release manifest to mark source availability and pin the
  reviewed public revision.

Only after PyPI Trusted Publishing succeeds should the website and README move
from source-install/release-candidate language to stable index-install language.

## Disabled native control boundary

Do not ship or market browser/computer control from the Austin/Neon foundation
until the official Developer ID team is pinned in the distributed finalizer and
the Austin release packager completes both required notarization rounds. The app
must pass nested Developer ID signing, hardened runtime, exact entitlements and
requirements, accepted zero-issue notary logs, stapling, Gatekeeper, and the
native package audit. The signed flat package must independently pass Developer
ID Installer signature verification, notarization, stapling, and Gatekeeper.
Never place raw notary credentials in a command or script; use only a named
`notarytool` Keychain profile.

Install the package into a disposable macOS user and run the explicit non-root
`algo-cli-control-install` finalizer. Verify that it creates only the inert
LaunchAgent definition, stable-Chrome native-host manifest, and signed Ada
inventory; it must not bootstrap the agent, request TCC, pair a browser, or
enable the protocol. Then pass installed doctor, extension pairing, XPC/TCC,
permission denial/revocation/regrant, move, upgrade, downgrade rejection,
reinstall, bounded runtime-only uninstall, private-state preservation, and
crash/power-loss gates. The current ad-hoc staged bundle, simulated packager,
and protocol-disabled native host are negative/local evidence only.

Private-state purge may be enabled only for a valid signed finite credential
registry with an atomic complete snapshot. Fresh empty namespaces have that
foundation only after the exact signed `austin-credential-migrator` produces a
fresh nonce-bound, all-service census and the finalizer verifies the app identity
before and after execution. Before enabling purge, run the production-signed
flow in a disposable user against empty, legacy fixed-label, dynamic receipt-
anchor, unexpected-label, nonce-replay, and concurrent-change cases; confirm
that only complete unchanged namespaces produce a signed registry. Generic
keyring access and the local ad-hoc rejection probe do not prove complete
enumeration. Runtime-only uninstall may be enabled only from a valid signed Ada
inventory and must preserve private state by default.

Run the signed Ada uninstall recovery fault matrix in a disposable user. Verify
that the write-ahead record exists before the first mutation, dry-run creates no
state, registry deletion is second-to-last, signer deletion is last, every
runtime boundary resumes, the pre-signed `commit_ready` record is durable before
signer deletion, every purge boundary resumes without loading the deleted private
key, and a tampered, concurrent, reappeared, or digest-changed surface fails
closed. Do not call private purge production-ready until the same matrix passes
against a Developer-ID-signed disposable installation with real Keychain and
power-loss injection; local fixture durability is not production evidence.

## Website

```bash
cd website
npm ci
npm test
npm run lint
npm audit
```

Keep aggregate benchmark copy explicit about unpublished raw evidence and
independent reproducibility. Do not advertise `pipx install algo-cli-runtime` or
`uv tool install algo-cli-runtime` as active until the package index confirms the
release exists.

The release workflow packages from a commit-bound Git archive with locked
`build==1.5.0` and `hatchling==1.31.0`, uses `--no-isolation`, and requires two
byte-identical build trials before tests can authorize the artifacts. Retained
Sigstore bundle verification is bundle-local; the pinned GitHub CLI may still
retrieve trust roots, so it is not described as offline verification. PyPI uses
short-lived OIDC and no long-lived PyPI API token. Repository-policy App tokens
are short-lived, repository-scoped, protected by `release-authority`, and used
only for fixed GET requests. Each irreversible job repeats those checks after
its environment wait and explicitly revokes the App token before mutation.
GitHub's release PATCH has no compare-and-swap precondition, so every principal
or workflow retaining repository `contents:write` and every retained PyPI
Owner, Maintainer, API token, or Trusted Publisher remains trusted during the
final check-to-mutation critical section. A concurrent draft asset change or
PyPI yank/deletion is checked again after the PATCH, and a separate no-write
protected-environment job compares repository policy with the original ID-bound
snapshot. Those postchecks can make the workflow red, but an already immutable
publication cannot be rolled back. This is an explicit external-authority race boundary, not an atomicity claim or an atomic release.
