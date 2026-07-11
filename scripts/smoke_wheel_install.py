#!/usr/bin/env python3
"""Install a built wheel into a temporary venv/home and exercise public entry points."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
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
    if len(candidates) != 1:
        raise SystemExit(f"expected exactly one algo-cli-runtime wheel under {path}; found {len(candidates)}")
    return candidates[0]


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
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        cli = bin_dir / ("algo-cli.exe" if os.name == "nt" else "algo-cli")
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
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)],
            env=run_env,
            cwd=work,
        )
        _run([str(cli), "--version"], env=run_env, cwd=work)
        _run([str(cli), "--help"], env=run_env, cwd=work)
        _run([str(python), "-m", "algo_cli", "--help"], env=run_env, cwd=work)
        _run([str(cli), "doctor"], env=run_env, cwd=work)
        if config_dir.exists():
            raise SystemExit("version/help/doctor mutated the isolated config directory")
        _run(
            [str(python), str(ROOT / "scripts" / "smoke_installed_release.py")],
            env=run_env,
            cwd=work,
        )

    print(f"Isolated wheel smoke passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
