# Python Dependency Audits

Algo CLI and its regular `dev` extra support Python 3.10+. The repository's
`supply-chain` tools require Python 3.11+ because patched filelock does. This
group is opt-in, is not a published runtime extra, and does not raise the CLI's
minimum interpreter. `--extra supply-chain` is retired; use `--group supply-chain`.
Selecting this group with Python 3.10 fails explicitly rather than installing an
older filelock or silently omitting the audit tool.

## Modern Audit Environment

Run from the checkout with the pinned uv version in `pyproject.toml`:

```bash
uv sync --python 3.11 --frozen --no-editable --extra dev --extra release --group supply-chain --link-mode copy
uv export --quiet --frozen --extra dev --extra release --group supply-chain --no-emit-project --output-file /tmp/algo-audit-requirements.txt
.venv/bin/pip-audit --requirement /tmp/algo-audit-requirements.txt --require-hashes --disable-pip --strict --progress-spinner off
```

No advisory ignores, dependency pruning or runtime minimum changes are needed.
The lock retains pip-audit 2.10.1 and resolves filelock 4.2.4; the group declares
filelock `>=4.2.1,<5` so future resolution cannot restore the vulnerable branch.

## Python 3.10 Target

Do not sync the audit group into the target environment. On macOS/Linux:

```bash
UV_PROJECT_ENVIRONMENT=.venv310 uv sync --python 3.10 --frozen --no-editable --extra dev --extra release --link-mode copy
.venv310/bin/python -I scripts/oliver_installed_source_parity.py
ALGO_TEST_REQUIRE_RIPGREP=1 .venv310/bin/python -I -m pytest tests
purelib=$(.venv310/bin/python -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
platlib=$(.venv310/bin/python -I -c 'import sysconfig; print(sysconfig.get_path("platlib"))')
.venv/bin/pip-audit --path "$purelib" --path "$platlib" --strict --progress-spinner off --format=json
```

On Windows use the target environment's `Scripts/python.exe` and the equivalent
PowerShell environment assignment. Audit both installation paths if distinct.
Compare the returned normalized package-name/version inventory with the target
interpreter's `importlib.metadata.distributions()` inventory, and reject missing
or skipped packages. Preserve the frozen lock, target install log and audit JSON.
The auditor runs on the modern interpreter but reads every installed target
distribution, including Python-3.10-only dependencies. Merely auditing a universal
requirements export under modern Python would evaluate its markers for the
auditor, not prove coverage of the target environment.

## Evidence And Limits

Keep failing pre-repair audits separate from successful reruns. A lockfile check
or empty advisory list does not prove application compatibility: run the actual
CacheControl cache and process-lock regressions plus the CLI suites on supported
interpreters. Local macOS verification is not native Windows/Linux evidence,
external browser/signing qualification or publication approval.

Upstream sources: [filelock advisory](https://github.com/tox-dev/filelock/security/advisories/GHSA-v4x8-3wr5-3wq8),
[filelock 4.2.4 metadata](https://pypi.org/project/filelock/4.2.4/), and
[uv group interpreter requirements](https://docs.astral.sh/uv/concepts/projects/dependencies/#group-requires-python).
