# Repository Guidelines

## Project Structure & Module Organization

This repository builds a local-first agentic terminal assistant. The main Python package lives in `algo_cli/`: `main.py` contains the CLI loop, `tools.py` defines model-callable tools, `config.py` handles runtime state, and `harness.py` indexes local agent assets. Optional helper components are split by language: `harness-indexer/` is a Rust indexer and `harness-gateway/` is a Go localhost gateway. Root launchers `algo-cli.ps1` and `algo-cli.cmd` run the CLI on Windows. Documentation belongs in `README.md`, `CHANGELOG.md`, and focused root-level guides.

## Build, Test, and Development Commands

Use PowerShell from the repository root unless noted:

```powershell
python -m pip install -e .
algo-cli
.\algo-cli.ps1
```

`pip install -e .` installs the package and console script for local development. `algo-cli` starts the installed CLI; the `.ps1` launcher is useful when the script is not on `PATH`.

Optional helper checks:

```powershell
cd harness-indexer; cargo build --release
cd harness-gateway; go run . -addr 127.0.0.1:8765
```

## Local Development Environment Memory

As of 2026-07-09, the repository `.venv` is synchronized with the `dev` extra:

```bash
UV_CACHE_DIR=/tmp/algo-cli-uv-cache uv sync --extra dev
```

- `mypy` is already declared in `pyproject.toml` and `uv.lock`; do not add a duplicate dependency.
- The installed checker is available as `.venv/bin/mypy`.
- In restricted/sandboxed sessions, use `--cache-dir /tmp/algo-cli-mypy-cache` because the repository-local `.mypy_cache` may not be writable.
- Use `.venv/bin/pytest -q` for the full suite and `.venv/bin/ruff check ...` for scoped lint checks.
- A pytest cache-write warning is benign when the test command exits successfully; use the exit code as authority.

## Coding Style & Naming Conventions

Python code targets Python 3.10+ and follows standard PEP 8 conventions: 4-space indentation, `snake_case` functions and variables, `PascalCase` classes, and clear module-level constants in uppercase. Keep CLI behavior conservative on lower-resource Windows machines: cap broad scans, prefer `rg` for search paths, and avoid loading large files eagerly. Go and Rust helpers should stay minimal, formatted with `gofmt` and `cargo fmt`.

## Testing Guidelines

Run `pytest tests` for Python changes, plus a focused test for the affected command or tool path. For harness changes, verify `/harness refresh`, `/harness status`, `/hsearch <query>`, and `/hread <id>` where relevant. For Rust or Go helper edits, run `cargo test --manifest-path harness-indexer/Cargo.toml --locked` or `go test ./...` from `harness-gateway/`.

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects, for example `Add identity layer and harness RAG` and `Polish terminal display`. Follow that style: one focused change per commit, concise subject, and details in the body only when needed. Pull requests should describe the user-facing behavior changed, list manual verification commands, call out config or environment impacts, and include screenshots only for terminal display changes.

## Security & Configuration Tips

Do not commit API keys, local memory contents, generated credential files, or `%USERPROFILE%\.algo_cli` data (or legacy `\.ollama_cli`). Keep secrets in environment variables or an ignored runtime env file. Treat shell and file tools carefully because approval and safe-mode behavior are part of the product surface.
