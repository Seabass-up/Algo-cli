#!/usr/bin/env python3
"""Exercise the published updater against a candidate wheel with synthetic state."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.oliver_smoke_wheel_install import (  # noqa: E402
    _create_isolated_environment,
    _isolated_environment as isolated_environment,
    _source_version,
    _wheel_from,
)


BASELINE_VERSION = "0.18.0"
BASELINE_URL = (
    "https://files.pythonhosted.org/packages/52/f8/8cc7543d1e327921aadeca59567a7a5a519261138e91e20f1f7f6c1b8326/"
    "algo_cli_runtime-0.18.0-py3-none-any.whl"
)
BASELINE_SHA256 = "027903f9e383635fa09dde9e6b4bc991758d798b5375e72d8a35e0c2fc934ac0"
BASELINE_SIZE = 1_034_630
EXCLUDED_MODULES = frozenset(
    {
        "algo_cli/irene_verifier_snapshot.py",
        "algo_cli/oliver_verifier_isolation.py",
    }
)


def run(command: list[str], *, env: dict[str, str], cwd: Path) -> str:
    result = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError(f"upgrade subprocess exited {result.returncode}: {(result.stderr or result.stdout)[-8000:]}")
    return result.stdout


def validate_candidate(wheel: Path, expected: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or EXCLUDED_MODULES.intersection(names):
            raise ValueError("candidate wheel contains ambiguous metadata or excluded verifier modules")
        info = BytesParser().parsebytes(archive.read(metadata[0]))
    if info["Name"] != "algo-cli-runtime" or info["Version"] != expected or expected == BASELINE_VERSION:
        raise ValueError("candidate wheel does not match the new source version")


def download_baseline(path: Path) -> None:
    with urlopen(BASELINE_URL, timeout=30) as response:
        payload = response.read(BASELINE_SIZE + 1)
    if len(payload) != BASELINE_SIZE or hashlib.sha256(payload).hexdigest() != BASELINE_SHA256:
        raise ValueError("published 0.18.0 wheel does not match its pinned PyPI digest")
    path.write_bytes(payload)


def seed_state(home: Path, workspace: Path) -> None:
    config = home / ".algo_cli"
    config.mkdir(mode=0o700)
    files = {
        "config.json": json.dumps(
            {
                "cwd": str(workspace),
                "model": "release-smoke-local",
                "theme": "tokyo-night",
                "echo_veil_enabled": False,
                "echo_veil_protection": "optional",
                "memory_auto_capture_enabled": False,
                "code_rag_enabled": False,
            }
        ).encode(),
        "env": b"ALGO_RELEASE_SENTINEL=synthetic-only\n",
        "chatgpt_auth.json": b'{"fixture":"synthetic credential preservation, no usable token"}\n',
        "memory.json": b'[{"id":"release-fixture","text":"synthetic retained memory"}]\n',
        "USER.md": b"Synthetic upgrade fixture, not a real user profile.\n",
        "SOUL.md": b"Synthetic identity fixture.\n",
        "saves/retained.json": b'[{"role":"user","content":"synthetic saved conversation"}]\n',
        "private/opaque-state.bin": bytes(range(256)),
    }
    for name, payload in files.items():
        path = config / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(0o600)
    with sqlite3.connect(config / "memory-fixture.sqlite3") as db:
        db.execute("CREATE TABLE preserved (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO preserved VALUES (?, ?)", (1, "synthetic database preservation"))
    (config / "memory-fixture.sqlite3").chmod(0o600)
    legacy = home / ".ollama_cli"
    legacy.mkdir(mode=0o700)
    (legacy / "config.json").write_bytes(b'{"fixture":"legacy state must not migrate during update"}\n')
    (workspace / "kept.txt").write_bytes(b"uncommitted synthetic work\r\n")


def state_manifest(home: Path, workspace: Path) -> dict[str, dict[str, object]]:
    result = {}
    for label, root in (("config", home / ".algo_cli"), ("legacy", home / ".ollama_cli"), ("workspace", workspace)):
        for path in (root, *sorted(root.rglob("*"))):
            if path.is_symlink():
                raise ValueError("unexpected link in synthetic upgrade state")
            info = path.stat()
            result[f"{label}/{path.relative_to(root).as_posix()}"] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
                "kind": "file" if path.is_file() else "directory",
                "mode": stat.S_IMODE(info.st_mode),
                "mtime_ns": info.st_mtime_ns,
            }
    return result


def installed_identity(
    python: Path, env_dir: Path, expected: str, env: dict[str, str], work: Path, manager: str = "pip"
) -> None:
    output = run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import json, algo_cli; from importlib.metadata import version; "
                "from algo_cli.updater import infer_install_manager; "
                "print(json.dumps({'metadata': version('algo-cli-runtime'), "
                "'runtime': algo_cli.__version__, 'file': algo_cli.__file__, 'manager': infer_install_manager()}))"
            ),
        ],
        env=env,
        cwd=work,
    )
    identity = json.loads(output)
    if identity["metadata"] != expected or identity["runtime"] != expected:
        raise ValueError("fresh-process installed metadata/runtime version mismatch")
    if identity["manager"] != manager:
        raise ValueError("installed updater did not identify its actual owning manager")
    if not Path(identity["file"]).resolve().is_relative_to(env_dir.resolve()):
        raise ValueError("upgrade smoke imported outside its isolated installation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel")
    parser.add_argument("--manager", choices=("pip", "pipx", "uv"), default="pip")
    parser.add_argument("--pipx-backend", choices=("pip", "uv"), default="pip")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    wheel = _wheel_from(args.wheel)
    expected = _source_version()
    validate_candidate(wheel, expected)
    report = {
        "schema": "algo-upgrade-smoke-v1",
        "status": "failed",
        "baseline_version": BASELINE_VERSION,
        "candidate_version": expected,
        "candidate_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "baseline_sha256": BASELINE_SHA256,
        "platform": sys.platform,
        "manager": args.manager,
        "pipx_backend": args.pipx_backend if args.manager == "pipx" else None,
        "real_credentials_used": False,
        "os_keychain_qualified": False,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="algo-cli-upgrade-smoke-") as raw:
            root = Path(raw)
            home, work, workspace = root / "home", root / "work", root / "workspace"
            for path in (home, work, workspace):
                path.mkdir(mode=0o700)
            env_dir = root / ("venv" if args.manager == "pip" else "controller")
            controller, install = _create_isolated_environment(env_dir)
            env = isolated_environment(home, controller.parent)
            baseline = root / "algo_cli_runtime-0.18.0-py3-none-any.whl"
            download_baseline(baseline)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            run(
                [
                    str(controller),
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheelhouse),
                    str(baseline),
                ],
                env=env,
                cwd=work,
            )
            update_env = dict(env, PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheelhouse))
            if args.manager == "pipx" and args.pipx_backend == "pip":
                # pipx bootstraps its shared pip even when the app install is offline.
                run(
                    [str(controller), "-m", "pip", "download", "--dest", str(wheelhouse), "pip>=26.1"],
                    env=env,
                    cwd=work,
                )
            if args.manager == "pip":
                python = controller
                cli = python.parent / ("algo-cli.exe" if os.name == "nt" else "algo-cli")
                run([*install, "algo-cli-runtime"], env=update_env, cwd=work)
            else:
                tooling = "pipx==1.17.2" if args.manager == "pipx" else "uv==0.11.26"
                run([*install, tooling], env=env, cwd=work)
                if args.manager == "pipx" and args.pipx_backend == "uv":
                    run([*install, "uv==0.11.26"], env=env, cwd=work)
                app_bin = root / "apps"
                app_bin.mkdir()
                update_env["PATH"] = str(app_bin) + os.pathsep + update_env["PATH"]
                binary = controller.parent / (args.manager + (".exe" if os.name == "nt" else ""))
                if args.manager == "pipx":
                    update_env.update(
                        PIPX_HOME=str(root / "pipx"),
                        PIPX_BIN_DIR=str(app_bin),
                        PIPX_DEFAULT_PYTHON=str(controller),
                        PIPX_DEFAULT_BACKEND=args.pipx_backend,
                        UV_CACHE_DIR=str(root / "uv-cache"),
                        UV_NO_CONFIG="true",
                        UV_NO_INDEX="true",
                        UV_FIND_LINKS=str(wheelhouse),
                        UV_PYTHON_DOWNLOADS="never",
                    )
                    env_dir = root / "pipx" / "venvs" / "algo-cli-runtime"
                    run([str(binary), "install", "algo-cli-runtime"], env=update_env, cwd=work)
                else:
                    update_env.update(
                        UV_TOOL_DIR=str(root / "uv" / "tools"),
                        UV_TOOL_BIN_DIR=str(app_bin),
                        UV_CACHE_DIR=str(root / "uv-cache"),
                        UV_NO_CONFIG="true",
                        UV_NO_INDEX="true",
                        UV_FIND_LINKS=str(wheelhouse),
                        UV_PYTHON_DOWNLOADS="never",
                    )
                    env_dir = root / "uv" / "tools" / "algo-cli-runtime"
                    run(
                        [str(binary), "tool", "install", "--python", str(controller), "algo-cli-runtime"],
                        env=update_env,
                        cwd=work,
                    )
                python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                cli = app_bin / ("algo-cli.exe" if os.name == "nt" else "algo-cli")
            installed_identity(python, env_dir, BASELINE_VERSION, update_env, work, args.manager)
            if args.manager == "pipx":
                metadata = json.loads((env_dir / "pipx_metadata.json").read_text(encoding="utf-8"))
                if metadata["backend"] != args.pipx_backend:
                    raise ValueError("pipx did not install with the selected backend")
            seed_state(home, workspace)
            before = state_manifest(home, workspace)
            run(
                [
                    str(controller),
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--dest",
                    str(wheelhouse),
                    str(wheel),
                ],
                env=env,
                cwd=work,
            )
            if not (wheelhouse / wheel.name).exists():
                shutil.copy2(wheel, wheelhouse / wheel.name)
            # This invokes the real updater shipped in the pinned public wheel.
            run([str(cli), "update"], env=update_env, cwd=work)
            installed_identity(python, env_dir, expected, update_env, work, args.manager)
            if state_manifest(home, workspace) != before:
                raise ValueError("update changed synthetic configuration, credentials, memory, or workspace")
            run([str(cli), "update"], env=update_env, cwd=work)
            installed_identity(python, env_dir, expected, update_env, work, args.manager)
            if state_manifest(home, workspace) != before:
                raise ValueError("repeat update changed synthetic user state")
            with sqlite3.connect(home / ".algo_cli" / "memory-fixture.sqlite3") as db:
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("retained SQLite state failed integrity check")
            report.update(
                status="passed",
                upgrade_verified=True,
                repeat_update_verified=True,
                retained_files=sum(row["kind"] == "file" for row in before.values()),
                state_preserved=True,
            )
    finally:
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
