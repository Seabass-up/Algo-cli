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

## Package

```bash
python scripts/check_release_version.py --tag v0.14.0
python -m build
python -m twine check dist/*
python scripts/check_public_release.py --artifacts-only \
  --artifact dist/algo_cli-0.14.0-py3-none-any.whl \
  --artifact dist/algo_cli-0.14.0.tar.gz
python scripts/smoke_wheel_install.py dist
```

Confirm the sdist contains no `website/`, `node_modules/`, virtual environment,
cache, or generated build paths. The website is deployed independently and is
not part of the Python source distribution.

Install the wheel in an empty virtual environment and empty home directory. Verify `algo-cli --help`, `algo-cli --version`, `algo-cli doctor`, `python -m algo_cli --help`, and `scripts/smoke_installed_release.py`.

## Hosting

- Create or rename the public repository to match the URLs in `pyproject.toml` and `README.md`.
- Enable private vulnerability reporting and branch protection.
- Create a protected GitHub environment named `pypi` with required approval.
- Configure PyPI Trusted Publishing for `.github/workflows/release.yml` and the `pypi` environment.
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

## Website

```bash
cd website
npm ci
npm test
npm run lint
npm audit
```

Keep aggregate benchmark copy explicit about unpublished raw evidence and
independent reproducibility. Do not advertise `pipx install algo-cli` or
`uv tool install algo-cli` as active until the package index confirms the
release exists.

The release workflow builds, tests, scans, and publishes distributions with short-lived OIDC credentials. It does not use a long-lived PyPI API token.
