# Contributing to Algo CLI

Thanks for helping improve Algo CLI.

## Set up

```bash
git clone https://github.com/Seabass-up/algo-cli.git
cd algo-cli
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Verify changes

```bash
ruff check algo_cli tests
mypy algo_cli/main.py algo_cli/nathan_runtime.py algo_cli/tools.py algo_cli/harness.py --ignore-missing-imports --no-strict-optional --follow-imports=skip
python -m compileall -q algo_cli ollama_cli
pytest tests
python scripts/check_public_release.py
python scripts/oliver_smoke_wheel_install.py dist  # after python -m build
```

Run `cargo test --manifest-path harness-indexer/Cargo.toml --locked` or `go test ./...` from `harness-gateway/` when changing a native helper.

## Public-data rule

Never commit user profiles, memories, credentials, real customer or business data, machine-specific absolute paths, generated indexes, local agent stores, or copied private documents. Use neutral fixtures such as `example`, `PROJECT-001`, and `example.test`.

Keep changes focused, add tests for behavior changes, and describe user-visible and privacy implications in the pull request.
