# Public release checklist

Use this checklist before publishing a tag or changing repository visibility.

## Source and history

- Run `python scripts/check_public_release.py`.
- Run `python scripts/check_public_history.py` from a full clone.
- Run a history-aware secret and privacy scanner over every branch and tag.
- Publish from a reviewed squashed/orphan history or a new public repository when the development history contains removed private blobs or author metadata.
- Do not copy old branches or tags into the sanitized public repository.
- Use repository-hosted privacy-preserving author metadata for the public root commit.

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

Install the wheel in an empty virtual environment and empty home directory. Verify `algo-cli --help`, `algo-cli --version`, `algo-cli doctor`, `python -m algo_cli --help`, and `scripts/smoke_installed_release.py`.

## Hosting

- Create or rename the public repository to match the URLs in `pyproject.toml` and `README.md`.
- Enable private vulnerability reporting and branch protection.
- Create a protected GitHub environment named `pypi` with required approval.
- Configure PyPI Trusted Publishing for `.github/workflows/release.yml` and the `pypi` environment.
- Confirm the tag is exactly `v` plus `algo_cli.__version__`, then publish a GitHub release.

The release workflow builds, tests, scans, and publishes distributions with short-lived OIDC credentials. It does not use a long-lived PyPI API token.
