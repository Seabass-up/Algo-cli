from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import conftest
import pytest

from algo_cli import model_info
from algo_cli import theodore_runtime_services as runtime_services
from algo_cli.action_registry import build_doctor_report
from algo_cli.config import Config
from algo_cli.oliver_slash_dispatch import handle_command


def test_guard_hides_and_refuses_the_ollama_cli(tmp_path: Path) -> None:
    for name in ("ollama", "OLLAMA", "ollama.exe", "/usr/local/bin/ollama", r"C:\Ollama\ollama.exe"):
        assert shutil.which(name) is None, name
    for command in (["ollama", "show", "qwen3"], ["/usr/local/bin/ollama", "serve"], "ollama signin"):
        with pytest.raises(FileNotFoundError, match="never runs a real Ollama CLI"):
            subprocess.run(command, capture_output=True, timeout=5)
    with pytest.raises(FileNotFoundError):
        subprocess.run(["anything"], executable="ollama", capture_output=True, timeout=5)

    # The guard is narrow: other programs are still found and run.
    interpreter = Path(sys.executable)
    assert shutil.which(interpreter.name, path=str(interpreter.parent)) is not None
    completed = subprocess.run([sys.executable, "-c", "print('ran')"], capture_output=True, text=True, timeout=30)
    assert completed.stdout.strip() == "ran"
    with subprocess.Popen([sys.executable, "-c", "pass"]) as process:
        assert isinstance(process, conftest._REAL_POPEN)
        assert process.wait(timeout=30) == 0


def test_doctor_reports_ollama_missing_as_on_a_ci_runner() -> None:
    findings = {finding.area: finding for finding in build_doctor_report(Config()).findings}
    assert findings["ollama"].status == "degraded"


def _tripwire(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "ollama-calls.txt"
    tripwire = bin_dir / "ollama"
    # Shell builtins only: PATH may hold nothing but this directory.
    tripwire.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 0\n', encoding="utf-8")
    tripwire.chmod(0o755)
    return bin_dir, calls


def _recorded(calls: Path) -> list[str]:
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def _start_local_server(monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(runtime_services, "ollama_server_ready", lambda _host: False)
    monkeypatch.setattr(runtime_services, "LOCAL_STARTUP_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(runtime_services, "_LOCAL_SERVE_PROCESSES", {})
    monkeypatch.setattr(runtime_services, "_LOCAL_START_FAILED_AT", {})
    return runtime_services.start_local_ollama_host("http://127.0.0.1:11434")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell tripwire")
def test_suite_code_paths_never_execute_an_ollama_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir, calls = _tripwire(tmp_path)

    # The paths that ran the developer's real CLI: model metadata lookup and local server start.
    monkeypatch.setenv("PATH", str(bin_dir))
    assert model_info.fetch_model_info_from_cli("qwen3") is None
    assert _start_local_server(monkeypatch) is False

    # The slash-command route most tests took to reach the lookup.
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr(Config, "save", lambda self: None)
    handled, _ = handle_command("/mode yolo", cfg, None, user_initiated=True)
    assert handled

    assert _recorded(calls) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell tripwire")
def test_tripwire_detects_an_unguarded_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: with the guard lifted the same paths reach the tripwire, never a real Ollama."""
    bin_dir, calls = _tripwire(tmp_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(shutil, "which", conftest._REAL_WHICH)
    monkeypatch.setattr(subprocess, "Popen", conftest._REAL_POPEN)

    model_info.fetch_model_info_from_cli("qwen3")
    _start_local_server(monkeypatch)
    for process in runtime_services._LOCAL_SERVE_PROCESSES.values():
        process.wait(timeout=10)

    assert "show qwen3" in _recorded(calls)
    assert "serve" in _recorded(calls)
