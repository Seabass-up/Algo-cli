"""Tests for the safe package-manager-aware update command."""

from __future__ import annotations

import subprocess

import pytest

from algo_cli import updater


def test_infer_install_manager_from_common_environment_paths():
    assert updater.infer_install_manager(
        executable="/home/user/.local/pipx/venvs/algo-cli-runtime/bin/python",
        prefix="/home/user/.local/pipx/venvs/algo-cli-runtime",
    ) == "pipx"
    assert updater.infer_install_manager(
        executable=r"C:\Users\example\AppData\Roaming\uv\tools\algo-cli-runtime\Scripts\python.exe",
        prefix=r"C:\Users\example\AppData\Roaming\uv\tools\algo-cli-runtime",
    ) == "uv"
    assert updater.infer_install_manager(
        executable="/opt/algo/venv/bin/python",
        prefix="/opt/algo/venv",
    ) == "pip"


def test_build_update_plan_uses_fixed_manager_commands():
    binaries = {"pipx": "/bin/pipx", "uv": "/bin/uv"}
    which = binaries.get

    assert updater.build_update_plan(manager="pipx", which=which).command == (
        "/bin/pipx",
        "upgrade",
        "algo-cli-runtime",
    )
    assert updater.build_update_plan(manager="uv", which=which).command == (
        "/bin/uv",
        "tool",
        "upgrade",
        "--no-sources",
        "algo-cli-runtime",
    )
    assert updater.build_update_plan(
        manager="pip",
        executable="/venv/bin/python",
        which=which,
    ).command == (
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
        "algo-cli-runtime",
    )


def test_auto_manager_falls_back_to_current_python_when_owner_binary_is_missing():
    plan = updater.build_update_plan(
        executable="/home/user/.local/pipx/venvs/algo-cli-runtime/bin/python",
        prefix="/home/user/.local/pipx/venvs/algo-cli-runtime",
        which=lambda _name: None,
    )

    assert plan.manager == "pip"
    assert plan.command[0] == "/home/user/.local/pipx/venvs/algo-cli-runtime/bin/python"


def test_update_reports_changed_version_and_passes_no_shell_arguments():
    versions = iter(["0.15.0", "0.16.0"])
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="upgraded", stderr="")

    result = updater.update_algo_cli(
        env={"ALGO_CLI_UPDATE_MANAGER": "pip"},
        executable="/venv/bin/python",
        runner=runner,
        version_getter=lambda: next(versions),
    )

    assert result.returncode == 0
    assert result.changed is True
    assert "0.15.0 → 0.16.0" in result.message
    assert calls[0][0][-1] == "algo-cli-runtime"
    assert "shell" not in calls[0][1]


def test_update_reports_already_current():
    result = updater.update_algo_cli(
        env={"ALGO_CLI_UPDATE_MANAGER": "pip"},
        executable="/venv/bin/python",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
        version_getter=lambda: "0.15.0",
    )

    assert result.returncode == 0
    assert result.changed is False
    assert "No newer compatible published package was installed" in result.message
    assert "v0.15.0" in result.message
    assert "GitHub" in result.message


def test_update_does_not_claim_latest_when_version_cannot_be_verified():
    result = updater.update_algo_cli(
        env={"ALGO_CLI_UPDATE_MANAGER": "pip"},
        runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
        version_getter=lambda: "unknown",
    )

    assert result.returncode == 0
    assert result.changed is False
    assert "could not be verified" in result.message
    assert "up to date" not in result.message


def test_update_surfaces_bounded_package_manager_failure():
    result = updater.update_algo_cli(
        env={"ALGO_CLI_UPDATE_MANAGER": "pip"},
        executable="/venv/bin/python",
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            9,
            stdout="",
            stderr="index unavailable",
        ),
        version_getter=lambda: "0.15.0",
    )

    assert result.returncode == 9
    assert "failed with pip" in result.message
    assert result.details == "index unavailable"


def test_update_rejects_invalid_manager_override_without_running_command():
    result = updater.update_algo_cli(
        env={"ALGO_CLI_UPDATE_MANAGER": "unknown"},
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
        version_getter=lambda: "0.15.0",
    )

    assert result.returncode == 64
    assert "Unsupported update manager" in result.message


@pytest.mark.parametrize("launcher", ["algo-cli", "ALGO-CLI.EXE", "ollama-cli", "ollama-cli.exe"])
@pytest.mark.parametrize("manager", ["pip", "pipx", "uv"])
def test_windows_launcher_refuses_update_before_starting_package_manager(monkeypatch, launcher, manager):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.sys, "argv", [rf"C:\Tools\{launcher}", "update"])
    result = updater.update_algo_cli(
        env={"ALGO_CLI_UPDATE_MANAGER": manager},
        executable=r"C:\Tools\Python's $env\python.exe",
        which=lambda name: rf"C:\Tools\{name}.exe",
        runner=lambda *_args, **_kwargs: pytest.fail("unsafe package-manager invocation"),
        version_getter=lambda: "0.19.0",
    )
    assert result.returncode == 64
    assert not result.changed
    assert result.after_version == result.before_version == "0.19.0"
    assert "running Windows launcher" in result.message
    assert "PowerShell" in result.message
    assert result.details.startswith("& '")
    if manager == "pip":
        assert "Python''s $env" in result.details
        assert "'-m' 'pip' 'install'" in result.details
    else:
        assert rf"C:\Tools\{manager}.exe" in result.details


@pytest.mark.parametrize(
    ("platform", "launcher"),
    [("linux", "/bin/algo-cli"), ("darwin", "/bin/algo-cli"), ("win32", r"C:\pkg\algo_cli\__main__.py")],
)
def test_non_launcher_updates_still_run(monkeypatch, platform, launcher):
    monkeypatch.setattr(updater.sys, "platform", platform)
    monkeypatch.setattr(updater.sys, "argv", [launcher, "update"])
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = updater.update_algo_cli(
        env={"ALGO_CLI_UPDATE_MANAGER": "pip"},
        runner=runner,
        version_getter=lambda: "0.19.0",
    )
    assert result.returncode == 0
    assert len(calls) == 1
