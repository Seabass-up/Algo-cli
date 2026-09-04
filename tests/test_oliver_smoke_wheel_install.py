from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oliver_smoke_wheel_install.py"


def _load_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("oliver_smoke_wheel_install", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wheel_directory_selects_the_source_version_among_stale_artifacts(tmp_path: Path) -> None:
    smoke = _load_smoke()
    stale = tmp_path / "algo_cli_runtime-0.15.1-py3-none-any.whl"
    current = tmp_path / f"algo_cli_runtime-{smoke._source_version()}-py3-none-any.whl"
    stale.touch()
    current.touch()
    assert smoke._wheel_from(str(tmp_path)) == current


def test_wheel_directory_rejects_ambiguous_current_artifacts(tmp_path: Path) -> None:
    smoke = _load_smoke()
    version = smoke._source_version()
    (tmp_path / f"algo_cli_runtime-{version}-py3-none-any.whl").touch()
    (tmp_path / f"algo_cli_runtime-{version}-cp312-cp312-any.whl").touch()
    with pytest.raises(SystemExit, match="found 2 matching wheel"):
        smoke._wheel_from(str(tmp_path))


def test_isolated_environment_uses_uv_when_ensurepip_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load_smoke()
    real_create = smoke.venv.EnvBuilder.create
    real_run = smoke.subprocess.run
    calls = 0

    def fail_once(builder: object, env_dir: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.CalledProcessError(1, ["ensurepip"])
        real_create(builder, env_dir)

    monkeypatch.setattr(smoke.venv.EnvBuilder, "create", fail_once)
    monkeypatch.setattr(smoke.shutil, "which", lambda name: "/usr/local/bin/uv" if name == "uv" else None)

    def create_uv_venv(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["/usr/local/bin/uv", "venv"]:
            real_create(smoke.venv.EnvBuilder(with_pip=False), Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(smoke.subprocess, "run", create_uv_venv)
    python, install = smoke._create_isolated_environment(tmp_path / "venv")
    assert python.is_file()
    assert install == [str(python), "-m", "pip", "install", "--disable-pip-version-check"]


def test_isolated_environment_fails_closed_without_pip_or_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = _load_smoke()

    def always_fail(builder: object, env_dir: Path) -> None:
        raise subprocess.CalledProcessError(1, ["ensurepip"])

    monkeypatch.setattr(smoke.venv.EnvBuilder, "create", always_fail)
    monkeypatch.setattr(smoke.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="uv is unavailable"):
        smoke._create_isolated_environment(tmp_path / "venv")
