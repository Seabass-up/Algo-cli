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
python -m build
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
- Create a protected GitHub environment named `pypi` with required approval.
- Configure PyPI Trusted Publishing for `.github/workflows/oliver-release.yml` and the `pypi` environment.
- Confirm the tag is exactly `v` plus `algo_cli.__version__`, then publish a
  final (not draft or prerelease) GitHub release. Prerelease events do not run
  the stable PyPI workflow.

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

The release workflow builds, tests, scans, and publishes distributions with short-lived OIDC credentials. It does not use a long-lived PyPI API token.
