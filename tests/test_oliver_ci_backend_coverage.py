from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import conftest
import pytest

from algo_cli.evals import nathan_agent_runtime_hardening as benchmark


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("job_name", ["quality", "test"])
def test_ci_requires_locked_native_search_backend(job_name: str) -> None:
    workflow = (ROOT / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^  {job_name}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)", workflow)
    assert match is not None
    job = match.group(1)
    assert 'ALGO_TEST_REQUIRE_RIPGREP: "1"' in job
    assert 'cargo install ripgrep --version 15.2.0 --locked --root "$RUNNER_TEMP/algo-ci-ripgrep"' in job
    assert '"$RUNNER_TEMP/algo-ci-ripgrep/bin"' in job
    assert '"$GITHUB_PATH"' in job
    assert "continue-on-error" not in job


def _git(root: Path, *arguments: str, input: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.autocrlf=true", "-C", str(root), *arguments],
        input=input,
        env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull),
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout


def test_windows_checkout_preserves_exact_runtime_lock_bytes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    (repository / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    locked = (ROOT / "uv.lock").read_bytes()
    (repository / "uv.lock").write_bytes(locked)
    _git(repository, "add", ".gitattributes", "uv.lock")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(repository, "checkout-index", f"--prefix={checkout.as_posix()}/", "--", "uv.lock")

    assert (checkout / "uv.lock").read_bytes() == locked
    attributes = _git(
        repository, "check-attr", "-z", "--stdin", "eol",
        input=b"\0".join(path.encode("ascii") for path in benchmark.SOURCE_PATHS) + b"\0",
    ).split(b"\0")
    assert set(attributes[2::3]) == {b"lf"}


@pytest.mark.parametrize("version", [b"", b"ripgrep 14.1.0\n", b"ripgrep 15.2.0-forged\n"])
def test_ci_rejects_wrong_search_backend(monkeypatch, request, version: bytes) -> None:
    monkeypatch.setenv("ALGO_TEST_REQUIRE_RIPGREP", "1")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "synthetic-rg")
    monkeypatch.setattr(conftest.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=version))

    with pytest.raises(pytest.UsageError, match="pinned ripgrep"):
        conftest.pytest_sessionstart(request.session)


def test_ci_accepts_exact_search_backend(monkeypatch, request) -> None:
    monkeypatch.setenv("ALGO_TEST_REQUIRE_RIPGREP", "1")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "synthetic-rg")
    calls = []

    def version(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=b"ripgrep 15.2.0\n\nfeatures:+pcre2\n")

    monkeypatch.setattr(conftest.subprocess, "run", version)
    conftest.pytest_sessionstart(request.session)
    assert calls == [(["synthetic-rg", "--version"], {"capture_output": True, "check": True, "timeout": 5})]


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("rg", 5)])
def test_ci_backend_preflight_errors_fail_closed(monkeypatch, request, error: Exception) -> None:
    monkeypatch.setenv("ALGO_TEST_REQUIRE_RIPGREP", "1")
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: "synthetic-rg")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(conftest.subprocess, "run", fail)
    with pytest.raises(pytest.UsageError, match="preflight failed"):
        conftest.pytest_sessionstart(request.session)


def test_local_fallback_only_install_remains_allowed(monkeypatch, request) -> None:
    monkeypatch.delenv("ALGO_TEST_REQUIRE_RIPGREP", raising=False)
    monkeypatch.setattr(conftest.shutil, "which", lambda _name: None)
    conftest.pytest_sessionstart(request.session)


def test_missing_ci_backend_stops_collection() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_search_execution.py"],
        cwd=ROOT,
        env=dict(os.environ, ALGO_TEST_REQUIRE_RIPGREP="1", PATH="", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"),
        capture_output=True, timeout=30,
    )
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert b"missing backend coverage is not a pass" in completed.stderr


def test_windows_profile_cannot_override_the_failed_job() -> None:
    workflow = (ROOT / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")
    diagnostic = workflow.split("      - name: Diagnose failed Windows runtime timing\n", 1)[1]
    diagnostic = diagnostic.split("\n  native:", 1)[0]
    condition = "if: ${{ failure() && runner.os == 'Windows' && steps.full-suite.outcome == 'failure' }}"
    assert diagnostic.count(condition) == 2
    assert "        timeout-minutes: 3\n" in diagnostic
    assert "python scripts/nathan_agent_runtime_profile.py" in diagnostic
    assert "if-no-files-found: error" in diagnostic
    assert "continue-on-error" not in diagnostic
