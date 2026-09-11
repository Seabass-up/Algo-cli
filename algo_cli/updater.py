"""Safe self-update planning for the published Algo CLI distribution."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable, Mapping


PACKAGE_NAME = "algo-cli-runtime"
UPDATE_TIMEOUT_SECONDS = 600
UV_TOOL_DIR_TIMEOUT_SECONDS = 5
SUPPORTED_MANAGERS = frozenset({"auto", "pipx", "uv", "uv-pip", "pip"})


@dataclass(frozen=True)
class UpdatePlan:
    manager: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class UpdateResult:
    returncode: int
    manager: str
    before_version: str
    after_version: str
    message: str
    details: str = ""

    @property
    def changed(self) -> bool:
        return bool(
            self.returncode == 0
            and self.before_version
            and self.after_version
            and self.before_version != self.after_version
        )


def installed_version() -> str:
    """Return the installed distribution version without importing the runtime."""
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        try:
            from . import __version__

            return __version__
        except Exception:
            return "unknown"


def _normalized_install_path(*, executable: str, prefix: str) -> str:
    combined = f"{Path(executable)}|{Path(prefix)}"
    return combined.replace("\\", "/").casefold()


def _distribution_installer() -> str:
    """Read the installer recorded for the installed Algo distribution."""
    try:
        value = metadata.distribution(PACKAGE_NAME).read_text("INSTALLER")
    except (metadata.PackageNotFoundError, OSError):
        return ""
    return (value or "").strip().casefold()


def _path_is_within(path: str, directory: str) -> bool:
    normalized_path = str(path).replace("\\", "/").rstrip("/").casefold()
    normalized_directory = str(directory).replace("\\", "/").rstrip("/").casefold()
    return bool(
        normalized_directory
        and (
            normalized_path == normalized_directory
            or normalized_path.startswith(f"{normalized_directory}/")
        )
    )


def _configured_uv_tool_dir(
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Return uv's configured tool directory without trusting shell parsing."""
    runtime_env = os.environ if env is None else env
    configured = runtime_env.get("UV_TOOL_DIR", "").strip()
    if configured:
        return configured
    binary = which("uv")
    if not binary:
        return ""
    try:
        completed = runner(
            [binary, "tool", "dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=UV_TOOL_DIR_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def infer_install_manager(
    *,
    executable: str | None = None,
    prefix: str | None = None,
    installer: str | None = None,
    uv_tool_dir: str | None = None,
) -> str:
    """Infer the manager that owns the running Algo CLI environment."""
    normalized = _normalized_install_path(
        executable=executable or sys.executable,
        prefix=prefix or sys.prefix,
    )
    if "/pipx/venvs/" in normalized or "/pipx/venv/" in normalized:
        return "pipx"
    if "/uv/tools/" in normalized or "/uv/tool/" in normalized:
        return "uv"
    recorded_installer = _distribution_installer() if installer is None else installer.strip().casefold()
    if recorded_installer == "uv":
        if uv_tool_dir and (
            _path_is_within(executable or sys.executable, uv_tool_dir)
            or _path_is_within(prefix or sys.prefix, uv_tool_dir)
        ):
            return "uv"
        return "uv-pip"
    return "pip"


def build_update_plan(
    *,
    manager: str = "auto",
    executable: str | None = None,
    prefix: str | None = None,
    installer: str | None = None,
    uv_tool_dir: str | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    probe_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> UpdatePlan:
    """Build a fixed-argument update command for the owning package manager."""
    requested = manager.strip().casefold()
    if requested not in SUPPORTED_MANAGERS:
        choices = ", ".join(sorted(SUPPORTED_MANAGERS))
        raise ValueError(f"Unsupported update manager {manager!r}; choose one of: {choices}.")
    if requested == "auto":
        recorded_installer = (
            _distribution_installer() if installer is None else installer.strip().casefold()
        )
        configured_uv_tool_dir = uv_tool_dir
        if recorded_installer == "uv" and configured_uv_tool_dir is None:
            configured_uv_tool_dir = _configured_uv_tool_dir(
                env=env,
                which=which,
                runner=probe_runner,
            )
        selected = infer_install_manager(
            executable=executable,
            prefix=prefix,
            installer=recorded_installer,
            uv_tool_dir=configured_uv_tool_dir,
        )
    else:
        selected = requested
    python = executable or sys.executable
    if selected == "pipx":
        binary = which("pipx")
        if binary:
            return UpdatePlan(manager="pipx", command=(binary, "upgrade", PACKAGE_NAME))
        if requested != "auto":
            raise RuntimeError("pipx owns this installation but the pipx command is not on PATH.")
        selected = "pip"
    if selected == "uv":
        binary = which("uv")
        if binary:
            return UpdatePlan(
                manager="uv",
                command=(binary, "tool", "upgrade", "--no-sources", PACKAGE_NAME),
            )
        if requested != "auto":
            raise RuntimeError("uv owns this installation but the uv command is not on PATH.")
        selected = "pip"
    if selected == "uv-pip":
        binary = which("uv")
        if not binary:
            raise RuntimeError("uv pip owns this installation but the uv command is not on PATH.")
        return UpdatePlan(
            manager="uv-pip",
            command=(
                binary,
                "pip",
                "install",
                "--python",
                python,
                "--upgrade",
                "--no-sources",
                PACKAGE_NAME,
            ),
        )
    return UpdatePlan(
        manager="pip",
        command=(
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            PACKAGE_NAME,
        ),
    )


def _bounded_details(stdout: str, stderr: str, *, limit: int = 4_000) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if len(combined) <= limit:
        return combined
    return "…" + combined[-(limit - 1) :]


def update_algo_cli(
    *,
    env: Mapping[str, str] | None = None,
    executable: str | None = None,
    prefix: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    version_getter: Callable[[], str] = installed_version,
) -> UpdateResult:
    """Upgrade Algo CLI from the package index and return a display-ready result."""
    runtime_env = os.environ if env is None else env
    requested_manager = runtime_env.get("ALGO_CLI_UPDATE_MANAGER", "auto")
    before = version_getter()
    try:
        plan = build_update_plan(
            manager=requested_manager,
            executable=executable,
            prefix=prefix,
            env=runtime_env,
            which=which,
        )
    except (RuntimeError, ValueError) as exc:
        return UpdateResult(
            returncode=64,
            manager=requested_manager,
            before_version=before,
            after_version=before,
            message=f"Algo CLI update could not start: {exc}",
        )

    # Windows entry-point wrappers may strip .exe from argv[0] before calling us.
    launcher = (sys.argv[0] if sys.argv else "").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if sys.platform == "win32" and launcher in {"algo-cli", "algo-cli.exe", "ollama-cli", "ollama-cli.exe"}:
        command = "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in plan.command)
        return UpdateResult(
            returncode=64,
            manager=plan.manager,
            before_version=before,
            after_version=before,
            message=(
                "Algo CLI update cannot replace its running Windows launcher. "
                "Close other Algo CLI sessions, then run this in PowerShell:"
            ),
            details=command,
        )

    try:
        completed = runner(
            list(plan.command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=UPDATE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return UpdateResult(
            returncode=124,
            manager=plan.manager,
            before_version=before,
            after_version=before,
            message=f"Algo CLI update timed out after {UPDATE_TIMEOUT_SECONDS} seconds.",
            details=_bounded_details(str(exc.stdout or ""), str(exc.stderr or "")),
        )
    except OSError as exc:
        return UpdateResult(
            returncode=1,
            manager=plan.manager,
            before_version=before,
            after_version=before,
            message=f"Algo CLI update could not run with {plan.manager}: {exc}",
        )

    details = _bounded_details(completed.stdout or "", completed.stderr or "")
    if completed.returncode != 0:
        return UpdateResult(
            returncode=completed.returncode,
            manager=plan.manager,
            before_version=before,
            after_version=before,
            message=f"Algo CLI update failed with {plan.manager} (exit {completed.returncode}).",
            details=details,
        )

    after = version_getter()
    if before != "unknown" and after != "unknown" and before != after:
        message = f"Updated Algo CLI {before} → {after}. Restart the command to use the new version."
    elif after == "unknown":
        message = "The package manager completed, but the installed Algo CLI version could not be verified."
    else:
        message = (
            f"No newer compatible published package was installed; current version is v{after}. "
            "This command does not install unpublished GitHub or local source changes."
        )
    return UpdateResult(
        returncode=0,
        manager=plan.manager,
        before_version=before,
        after_version=after,
        message=message,
    )
