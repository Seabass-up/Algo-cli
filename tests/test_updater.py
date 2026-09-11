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
        installer="",
    ) == "pip"


def test_infer_install_manager_uses_distribution_installer_for_uv_pip_environment():
    assert updater.infer_install_manager(
        executable="/opt/algo/venv/bin/python",
        prefix="/opt/algo/venv",
        installer="uv",
    ) == "uv-pip"


def test_infer_install_manager_recognizes_custom_uv_tool_directory():
    assert updater.infer_install_manager(
        executable="/opt/company/apps/algo-cli-runtime/bin/python",
        prefix="/opt/company/apps/algo-cli-runtime",
        installer="uv",
        uv_tool_dir="/opt/company/apps",
    ) == "uv"


def test_custom_uv_tool_directory_requires_a_path_boundary():
    assert updater.infer_install_manager(
        executable="/opt/company/apps-other/algo-cli-runtime/bin/python",
        prefix="/opt/company/apps-other/algo-cli-runtime",
        installer="uv",
        uv_tool_dir="/opt/company/apps",
    ) == "uv-pip"


@pytest.mark.parametrize(
    ("executable", "prefix", "expected"),
    [
        (
            "/home/user/.local/pipx/venvs/algo-cli-runtime/bin/python",
            "/home/user/.local/pipx/venvs/algo-cli-runtime",
            "pipx",
        ),
        (
            "/home/user/.local/share/uv/tools/algo-cli-runtime/bin/python",
            "/home/user/.local/share/uv/tools/algo-cli-runtime",
            "uv",
        ),
    ],
)
def test_path_owned_managers_take_precedence_over_uv_installer_metadata(executable, prefix, expected):
    assert updater.infer_install_manager(
        executable=executable,
        prefix=prefix,
        installer="uv",
    ) == expected


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
        manager="uv-pip",
        executable="/venv/bin/python",
        which=which,
    ).command == (
        "/bin/uv",
        "pip",
        "install",
        "--python",
        "/venv/bin/python",
        "--upgrade",
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


def test_auto_manager_uses_uv_pip_when_distribution_installer_is_uv():
    plan = updater.build_update_plan(
        executable="/opt/algo/venv/bin/python",
        prefix="/opt/algo/venv",
        installer="uv",
        which=lambda name: f"/bin/{name}",
    )

    assert plan.manager == "uv-pip"
    assert plan.command[:5] == ("/bin/uv", "pip", "install", "--python", "/opt/algo/venv/bin/python")


def test_auto_manager_uses_uv_tool_for_custom_directory_from_environment():
    plan = updater.build_update_plan(
        executable="/opt/company/apps/algo-cli-runtime/bin/python",
        prefix="/opt/company/apps/algo-cli-runtime",
        installer="uv",
        env={"UV_TOOL_DIR": "/opt/company/apps"},
        which=lambda name: f"/bin/{name}",
    )

    assert plan.manager == "uv"
    assert plan.command == ("/bin/uv", "tool", "upgrade", "--no-sources", "algo-cli-runtime")


def test_auto_manager_queries_uv_for_configured_custom_tool_directory():
    probes = []

    def probe_runner(command, **kwargs):
        probes.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="/srv/uv-tools\n", stderr="")

    plan = updater.build_update_plan(
        executable="/srv/uv-tools/algo-cli-runtime/bin/python",
        prefix="/srv/uv-tools/algo-cli-runtime",
        installer="uv",
        env={},
        which=lambda name: f"/bin/{name}",
        probe_runner=probe_runner,
    )

    assert plan.manager == "uv"
    assert probes[0][0] == ["/bin/uv", "tool", "dir"]
    assert "shell" not in probes[0][1]


def test_failed_uv_tool_directory_probe_keeps_standalone_uv_pip_detection():
    plan = updater.build_update_plan(
        executable="/opt/algo/venv/bin/python",
        prefix="/opt/algo/venv",
        installer="uv",
        env={},
        which=lambda name: f"/bin/{name}",
        probe_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="unavailable"
        ),
    )

    assert plan.manager == "uv-pip"


def test_uv_pip_owner_without_uv_does_not_fall_back_to_missing_pip():
    with pytest.raises(RuntimeError, match="uv pip owns this installation"):
        updater.build_update_plan(
            executable="/opt/algo/venv/bin/python",
            prefix="/opt/algo/venv",
            installer="uv",
            which=lambda _name: None,
        )


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
@pytest.mark.parametrize("manager", ["pip", "pipx", "uv", "uv-pip"])
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
    elif manager == "uv-pip":
        assert r"C:\Tools\uv.exe" in result.details
        assert "'pip' 'install' '--python'" in result.details
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
