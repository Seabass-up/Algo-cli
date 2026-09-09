from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import subprocess
import zipfile

import pytest

from scripts import oliver_smoke_upgrade as smoke


def _wheel(path: Path, *, version: str = "0.19.0", extra: str = "") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"algo_cli_runtime-{version}.dist-info/METADATA", f"Name: algo-cli-runtime\nVersion: {version}\n"
        )
        if extra:
            archive.writestr(extra, "")
    return path


def test_candidate_version_and_experimental_scope_are_checked(tmp_path):
    path = _wheel(tmp_path / "candidate.whl")
    smoke.validate_candidate(path, "0.19.0")
    for version in ("0.18.0", "0.20.0"):
        with pytest.raises(ValueError, match="source version"):
            smoke.validate_candidate(path, version)
    for module in smoke.EXCLUDED_MODULES:
        _wheel(path, extra=module)
        with pytest.raises(ValueError, match="excluded verifier"):
            smoke.validate_candidate(path, "0.19.0")


def test_isolation_does_not_inherit_provider_credentials_or_installer_settings(tmp_path, monkeypatch):
    for key in ("OPENAI_API_KEY", "OLLAMA_API_KEY", "PYTHONPATH", "PIP_EXTRA_INDEX_URL", "UV_INDEX", "ALGO_CLI_CWD"):
        monkeypatch.setenv(key, "synthetic-inherited-value")
    monkeypatch.setenv("ALGO_CLI_CONFIG_DIR", str(tmp_path / "real-state"))
    env = smoke.isolated_environment(tmp_path / "home", tmp_path / "bin")
    assert env["ALGO_CLI_CONFIG_DIR"] == str(tmp_path / "home" / ".algo_cli")
    assert "synthetic-inherited-value" not in env.values()
    assert env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.null.Keyring"
    assert env["PIP_INDEX_URL"] == "https://pypi.org/simple"


@pytest.mark.parametrize("surface", ["config.json", "chatgpt_auth.json", "memory.json", "private/opaque-state.bin"])
def test_state_manifest_detects_changed_user_state(tmp_path, surface):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    smoke.seed_state(home, work)
    before = smoke.state_manifest(home, work)
    assert json.loads((home / ".algo_cli" / "config.json").read_text())["cwd"] == str(work)
    (home / ".algo_cli" / surface).write_bytes(b"changed")
    assert smoke.state_manifest(home, work) != before


def test_state_manifest_detects_workspace_changes_and_missing_files(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    smoke.seed_state(home, work)
    before = smoke.state_manifest(home, work)
    (work / "kept.txt").unlink()
    assert smoke.state_manifest(home, work) != before


def test_wrong_published_baseline_digest_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "urlopen", lambda *args, **kwargs: io.BytesIO(b"not the published wheel"))
    path = tmp_path / "baseline.whl"
    with pytest.raises(ValueError, match="pinned PyPI digest"):
        smoke.download_baseline(path)
    assert not path.exists()


def test_seed_state_closes_sqlite_connection_before_returning(tmp_path, monkeypatch):
    connections = []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(smoke.sqlite3, "connect", tracked_connect)
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    smoke.seed_state(home, work)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


def test_windows_upgrade_uses_external_owning_manager_from_public_install(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    expected = ["isolated-python", "-m", "pip", "install", "--upgrade", "algo-cli-runtime"]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return json.dumps(expected)

    monkeypatch.setattr(smoke, "run", run)
    assert smoke.upgrade_command(Path("isolated-python"), Path("algo-cli.exe"), {}, tmp_path) == expected
    assert calls[0][:3] == ["isolated-python", "-I", "-c"]
    assert "build_update_plan" in calls[0][3]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_upgrade_exercises_the_actual_published_cli(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(smoke.sys, "platform", platform)
    monkeypatch.setattr(smoke, "run", lambda *args, **kwargs: pytest.fail("must use actual CLI"))
    assert smoke.upgrade_command(Path("python"), Path("algo-cli"), {}, tmp_path) == ["algo-cli", "update"]


@pytest.mark.parametrize("payload", ["{}", "[]", '["python", null]', '[""]'])
def test_windows_upgrade_rejects_invalid_manager_command(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    monkeypatch.setattr(smoke, "run", lambda *args, **kwargs: payload)
    with pytest.raises(ValueError, match="owning-manager command"):
        smoke.upgrade_command(Path("python"), Path("algo-cli.exe"), {}, tmp_path)


@pytest.mark.parametrize("returncode,output,accepted", [
    (64, "cannot replace its running Windows launcher. PowerShell:", True),
    (0, "cannot replace its running Windows launcher. PowerShell:", False),
    (64, "different failure", False),
])
def test_windows_launcher_probe_requires_the_specific_guard(tmp_path, monkeypatch, returncode, output, accepted):
    monkeypatch.setattr(
        smoke.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, stdout=output, stderr=""),
    )
    if accepted:
        smoke.verify_windows_launcher_guard(Path("algo-cli.exe"), {}, tmp_path)
    else:
        with pytest.raises(ValueError, match="refuse unsafe self-replacement"):
            smoke.verify_windows_launcher_guard(Path("algo-cli.exe"), {}, tmp_path)


def test_windows_launcher_probe_retains_bounded_failure_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(
        command, 1, stdout="x" * 9000, stderr="migration blocked",
    ))
    with pytest.raises(ValueError) as exc:
        smoke.verify_windows_launcher_guard(Path("algo-cli.exe"), {}, tmp_path)
    assert '"returncode": 1' in str(exc.value)
    assert "migration blocked" in str(exc.value)
    assert len(str(exc.value)) < 4500


def test_ci_requires_upgrade_on_every_installed_platform():
    workflow = (smoke.ROOT / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")
    job = workflow.split("  package-smoke:\n", 1)[1]
    assert "os: [ubuntu-latest, windows-latest, macos-latest]" in job
    assert "python scripts/oliver_smoke_upgrade.py dist" in job
    for manager in ("pip", "pipx", "uv"):
        assert (
            f"run: python scripts/oliver_smoke_upgrade.py dist --manager {manager} "
            f"--report upgrade-smoke-{manager}.json"
        ) in job
    assert "if: always()" in job
    assert (
        "run: python scripts/oliver_smoke_upgrade.py dist --manager pipx "
        "--pipx-backend uv --report upgrade-smoke-pipx-uv.json"
    ) in job
