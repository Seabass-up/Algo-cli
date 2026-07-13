"""Tests for the safe package-manager-aware update command."""

from __future__ import annotations

import subprocess

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
    assert result.message == "Algo CLI is up to date at v0.15.0."


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
