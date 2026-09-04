#!/usr/bin/env python3
"""Install a built wheel into a temporary venv/home and exercise public entry points."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, env: dict[str, str], cwd: Path) -> None:
    subprocess.run(command, check=True, cwd=cwd, env=env)


def _wheel_from(value: str) -> Path:
    path = Path(value).resolve()
    if path.is_file() and path.suffix == ".whl":
        return path
    candidates = sorted(path.glob("algo_cli_runtime-*.whl")) if path.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    expected = _source_version()
    matches = [candidate for candidate in candidates if candidate.name.startswith(f"algo_cli_runtime-{expected}-")]
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(
        f"expected one algo-cli-runtime {expected} wheel under {path}; "
        f"found {len(matches)} matching wheel(s) among {len(candidates)} candidate(s)"
    )


def _source_version() -> str:
    init_path = ROOT / "algo_cli" / "__init__.py"
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        init_path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if match is None:
        raise SystemExit(f"unable to read __version__ from {init_path}")
    return match.group(1).replace("-", "_")


def _create_isolated_environment(env_dir: Path) -> tuple[Path, list[str]]:
    bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        return python, [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    except subprocess.CalledProcessError as exc:
        uv = shutil.which("uv")
        if uv is None:
            raise SystemExit(
                "isolated venv could not bootstrap pip and uv is unavailable for the fail-safe fallback"
            ) from exc
        shutil.rmtree(env_dir, ignore_errors=True)
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        try:
            subprocess.run(
                [uv, "venv", "--seed", "--python", version, str(env_dir)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except subprocess.CalledProcessError as uv_exc:
            detail = (uv_exc.stderr or uv_exc.stdout or "").strip()
            raise SystemExit(f"uv could not create the isolated fallback venv: {detail}") from uv_exc
        if not python.is_file():
            raise SystemExit(f"fallback venv did not create {python}") from exc
        return python, [str(python), "-m", "pip", "install", "--disable-pip-version-check"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", help="Wheel file or directory containing exactly one wheel")
    args = parser.parse_args(argv)
    wheel = _wheel_from(args.wheel)

    with tempfile.TemporaryDirectory(prefix="algo-cli-wheel-smoke-") as raw_tmp:
        tmp = Path(raw_tmp)
        env_dir = tmp / "venv"
        home = tmp / "home"
        work = tmp / "work"
        home.mkdir()
        work.mkdir()
        bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
        python, install_command = _create_isolated_environment(env_dir)
        cli = bin_dir / ("algo-cli.exe" if os.name == "nt" else "algo-cli")
        control_uninstall = bin_dir / (
            "algo-cli-control-uninstall.exe"
            if os.name == "nt"
            else "algo-cli-control-uninstall"
        )
        control_install = bin_dir / (
            "algo-cli-control-install.exe"
            if os.name == "nt"
            else "algo-cli-control-install"
        )
        config_dir = home / ".algo_cli"
        run_env = os.environ.copy()
        run_env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "ALGO_CLI_CONFIG_DIR": str(config_dir),
                "PYTHONUTF8": "1",
            }
        )
        run_env.pop("OLLAMA_CLI_CONFIG_DIR", None)

        _run(
            [*install_command, str(wheel)],
            env=run_env,
            cwd=work,
        )
        _run([str(cli), "--version"], env=run_env, cwd=work)
        _run([str(cli), "--help"], env=run_env, cwd=work)
        _run([str(python), "-m", "algo_cli", "--help"], env=run_env, cwd=work)
        _run([str(cli), "doctor"], env=run_env, cwd=work)
        _run([str(control_install), "--help"], env=run_env, cwd=work)
        uninstall_probe = subprocess.run(
            [str(control_uninstall)],
            check=False,
            cwd=work,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            uninstall_payload = json.loads(uninstall_probe.stdout)
        except json.JSONDecodeError as exc:
            raise SystemExit("installed control-uninstall entry point returned invalid JSON") from exc
        if uninstall_probe.returncode != 1 or uninstall_payload != {
            "reason_code": "inventory_unavailable",
            "status": "blocked",
        }:
            raise SystemExit("installed control-uninstall entry point did not fail closed")
        if config_dir.exists():
            raise SystemExit("version/help/doctor/uninstall probe mutated the isolated config directory")
        _run(
            [str(python), str(ROOT / "scripts" / "smoke_installed_release.py")],
            env=run_env,
            cwd=work,
        )

    print(f"Isolated wheel smoke passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
