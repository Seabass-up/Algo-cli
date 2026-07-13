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
SUPPORTED_MANAGERS = frozenset({"auto", "pipx", "uv", "pip"})


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


def infer_install_manager(*, executable: str | None = None, prefix: str | None = None) -> str:
    """Infer the manager that owns the running Algo CLI environment."""
    normalized = _normalized_install_path(
        executable=executable or sys.executable,
        prefix=prefix or sys.prefix,
    )
    if "/pipx/venvs/" in normalized or "/pipx/venv/" in normalized:
        return "pipx"
    if "/uv/tools/" in normalized or "/uv/tool/" in normalized:
        return "uv"
    return "pip"


def build_update_plan(
    *,
    manager: str = "auto",
    executable: str | None = None,
    prefix: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> UpdatePlan:
    """Build a fixed-argument update command for the owning package manager."""
    requested = manager.strip().casefold()
    if requested not in SUPPORTED_MANAGERS:
        choices = ", ".join(sorted(SUPPORTED_MANAGERS))
        raise ValueError(f"Unsupported update manager {manager!r}; choose one of: {choices}.")
    selected = (
        infer_install_manager(executable=executable, prefix=prefix)
        if requested == "auto"
        else requested
    )
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
    else:
        shown = after if after != "unknown" else before
        suffix = f" at v{shown}" if shown != "unknown" else ""
        message = f"Algo CLI is up to date{suffix}."
    return UpdateResult(
        returncode=0,
        manager=plan.manager,
        before_version=before,
        after_version=after,
        message=message,
    )
