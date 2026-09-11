#!/usr/bin/env python3
"""Exercise the published updater against a candidate wheel with synthetic state."""

from __future__ import annotations

import argparse
from contextlib import closing
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
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
WHEEL_PACKAGES = frozenset({"algo_cli", "ollama_cli"})
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


def offline_update_environment(env: dict[str, str], wheelhouse: Path, cache: Path) -> dict[str, str]:
    # uv ignores pip's settings and has no UV_NO_INDEX environment variable.
    # A fresh cache plus offline mode keeps every resolver on the local wheels.
    location = wheelhouse.resolve().as_uri()
    return dict(
        env,
        PIP_NO_INDEX="1",
        PIP_FIND_LINKS=location,
        UV_OFFLINE="true",
        UV_FIND_LINKS=location,
        UV_CACHE_DIR=str(cache),
        UV_NO_CONFIG="true",
        UV_PYTHON_DOWNLOADS="never",
    )


def verify_installed_wheel(wheel: Path, site: Path) -> int:
    """Compare payload bytes, not just versions, without trusting installed RECORD."""
    with zipfile.ZipFile(wheel) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or len(names) != len(set(names)):
            raise ValueError("installed wheel has ambiguous archive metadata or paths")
        metadata_root = metadata[0].split("/", 1)[0]
        expected = set()
        for name in names:
            relative = PurePosixPath(name)
            if (
                relative.is_absolute() or relative.as_posix() != name
                or any(part in {".", ".."} for part in name.split("/"))
                or any(char in name for char in ("\\", ":", "\0"))
                or relative.parts[0] not in WHEEL_PACKAGES | {metadata_root}
            ):
                raise ValueError(f"installed wheel has an unsupported archive path: {name[:200]}")
            if name == f"{metadata_root}/RECORD":
                continue
            path = site.joinpath(*relative.parts)
            if (
                path.is_symlink() or not path.resolve().is_relative_to(site.resolve())
                or not path.is_file() or path.read_bytes() != archive.read(name)
            ):
                raise ValueError(f"installed wheel payload mismatch: {name}")
            expected.add(name)
    for name in sorted(WHEEL_PACKAGES):
        package = site / name
        if not package.is_dir() or package.is_symlink() or f"{name}/__init__.py" not in expected:
            raise ValueError("installed wheel package is missing or linked")
        for path in package.rglob("*"):
            if path.is_symlink():
                raise ValueError("installed wheel contains an unexpected link")
            if path.is_dir() or (path.suffix == ".pyc" and "__pycache__" in path.relative_to(package).parts):
                continue
            if path.relative_to(site).as_posix() not in expected:
                raise ValueError(f"installed wheel contains an unexpected payload: {path.relative_to(site)}")
    return len(expected)


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
    with closing(sqlite3.connect(config / "memory-fixture.sqlite3")) as db, db:
        db.execute("CREATE TABLE preserved (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO preserved VALUES (?, ?)", (1, "synthetic database preservation"))
    (config / "memory-fixture.sqlite3").chmod(0o600)
    legacy = home / ".ollama_cli"
    legacy.mkdir(mode=0o700)
    (legacy / "config.json").write_bytes(b'{"fixture":"legacy state must not migrate during update"}\n')
    (workspace / "kept.txt").write_bytes(b"uncommitted synthetic work\r\n")


def state_manifest(home: Path, workspace: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
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
    python: Path, env_dir: Path, expected: str, env: dict[str, str], work: Path, manager: str = "pip",
    *, wheel: Path,
) -> int:
    output = run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import json, algo_cli; from importlib.metadata import distribution; "
                "from algo_cli.updater import infer_install_manager; "
                "dist = distribution('algo-cli-runtime'); "
                "print(json.dumps({'metadata': dist.version, 'site': str(dist.locate_file('')), "
                "'runtime': algo_cli.__version__, 'file': algo_cli.__file__, 'manager': infer_install_manager()}))"
            ),
        ],
        env=env,
        cwd=work,
    )
    identity = json.loads(output)
    if identity["metadata"] != expected or identity["runtime"] != expected:
        raise ValueError(
            f"fresh-process installed metadata/runtime version mismatch: expected {expected}, "
            f"metadata {str(identity['metadata'])[:100]}, runtime {str(identity['runtime'])[:100]}"
        )
    if identity["manager"] != manager:
        raise ValueError("installed updater did not identify its actual owning manager")
    site = Path(identity["site"])
    if (
        not site.resolve().is_relative_to(env_dir.resolve())
        or Path(identity["file"]).resolve() != (site / "algo_cli/__init__.py").resolve()
    ):
        raise ValueError("upgrade smoke imported outside its isolated installation")
    return verify_installed_wheel(wheel, site)


def upgrade_command(python: Path, cli: Path, env: dict[str, str], work: Path) -> list[str]:
    if sys.platform != "win32":
        return [str(cli), "update"]
    # Published 0.18.0 cannot uninstall its own active Windows .exe wrapper.
    output = run(
        [
            str(python), "-I", "-c",
            "import json; from algo_cli.updater import build_update_plan; "
            "print(json.dumps(list(build_update_plan().command)))",
        ],
        env=env,
        cwd=work,
    )
    command = json.loads(output)
    if type(command) is not list or not command or any(type(argument) is not str or not argument for argument in command):
        raise ValueError("published updater returned an invalid owning-manager command")
    return command


def verify_windows_launcher_guard(cli: Path, env: dict[str, str], work: Path) -> None:
    result = subprocess.run(
        [str(cli), "update"], env=env, cwd=work, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    if result.returncode != 64 or "running Windows launcher" not in result.stdout or "PowerShell" not in result.stdout:
        diagnostic = json.dumps({
            "returncode": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }, ensure_ascii=True)
        raise ValueError(f"candidate Windows launcher did not refuse unsafe self-replacement: {diagnostic}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel")
    parser.add_argument("--manager", choices=("pip", "pipx", "uv", "uv-pip"), default="pip")
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
        "update_entrypoint": "owning-manager" if sys.platform == "win32" else "published-cli",
        "published_updater_exercised": False,
        "candidate_updater_exercised": False,
        "legacy_missing_pip_verified": False,
        "one_time_bootstrap_required": args.manager == "uv-pip",
        "windows_launcher_guard_verified": False,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="algo-cli-upgrade-smoke-") as raw:
            root = Path(raw)
            home, work, workspace = root / "home", root / "work", root / "workspace"
            for path in (home, work, workspace):
                path.mkdir(mode=0o700)
            controller_dir = root / ("venv" if args.manager == "pip" else "controller")
            controller, install = _create_isolated_environment(controller_dir)
            env_dir = controller_dir
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
            update_env = offline_update_environment(env, wheelhouse, root / "uv-cache")
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
                binary_name = "pipx" if args.manager == "pipx" else "uv"
                binary = controller.parent / (binary_name + (".exe" if os.name == "nt" else ""))
                if args.manager == "uv-pip":
                    env_dir = root / "uv-pip"
                    run(
                        [str(controller), "-m", "venv", "--without-pip", str(env_dir)],
                        env=env,
                        cwd=work,
                    )
                    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                    cli = python.parent / ("algo-cli.exe" if os.name == "nt" else "algo-cli")
                    run(
                        [str(binary), "pip", "install", "--python", str(python), "algo-cli-runtime"],
                        env=update_env,
                        cwd=work,
                    )
                else:
                    app_bin = root / "apps"
                    app_bin.mkdir()
                    update_env["PATH"] = str(app_bin) + os.pathsep + update_env["PATH"]
                if args.manager == "pipx":
                    update_env.update(
                        PIPX_HOME=str(root / "pipx"),
                        PIPX_BIN_DIR=str(app_bin),
                        PIPX_DEFAULT_PYTHON=str(controller),
                        PIPX_DEFAULT_BACKEND=args.pipx_backend,
                    )
                    env_dir = root / "pipx" / "venvs" / "algo-cli-runtime"
                    run([str(binary), "install", "algo-cli-runtime"], env=update_env, cwd=work)
                elif args.manager == "uv":
                    update_env.update(
                        UV_TOOL_DIR=str(root / "uv" / "tools"),
                        UV_TOOL_BIN_DIR=str(app_bin),
                    )
                    env_dir = root / "uv" / "tools" / "algo-cli-runtime"
                    run(
                        [str(binary), "tool", "install", "--python", str(controller), "algo-cli-runtime"],
                        env=update_env,
                        cwd=work,
                    )
                if args.manager != "uv-pip":
                    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                    cli = app_bin / ("algo-cli.exe" if os.name == "nt" else "algo-cli")
            baseline_manager = "pip" if args.manager == "uv-pip" else args.manager
            report["baseline_installed_wheel_files"] = installed_identity(
                python, env_dir, BASELINE_VERSION, update_env, work, baseline_manager, wheel=baseline,
            )
            if args.manager == "uv-pip":
                pip_probe = subprocess.run(
                    [str(python), "-I", "-m", "pip", "--version"],
                    env=update_env,
                    cwd=work,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                if pip_probe.returncode == 0:
                    raise ValueError("uv pip baseline unexpectedly contains pip")
                report["legacy_missing_pip_verified"] = True
                report["update_entrypoint"] = "owning-manager-bootstrap"
                command = [
                    str(binary), "pip", "install", "--python", str(python),
                    "--upgrade", "--no-sources", "algo-cli-runtime",
                ]
            else:
                command = upgrade_command(python, cli, update_env, work)
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
            run(command, env=update_env, cwd=work)
            report["published_updater_exercised"] = sys.platform != "win32" and args.manager != "uv-pip"
            report["candidate_installed_wheel_files"] = installed_identity(
                python, env_dir, expected, update_env, work, args.manager, wheel=wheel,
            )
            if state_manifest(home, workspace) != before:
                raise ValueError("update changed synthetic configuration, credentials, memory, or workspace")
            repeat_command = upgrade_command(python, cli, update_env, work)
            if sys.platform == "win32":
                verify_windows_launcher_guard(cli, update_env, work)
                report["windows_launcher_guard_verified"] = True
            run(repeat_command, env=update_env, cwd=work)
            report["candidate_updater_exercised"] = sys.platform != "win32"
            report["repeat_installed_wheel_files"] = installed_identity(
                python, env_dir, expected, update_env, work, args.manager, wheel=wheel,
            )
            if state_manifest(home, workspace) != before:
                raise ValueError("repeat update changed synthetic user state")
            with closing(sqlite3.connect(home / ".algo_cli" / "memory-fixture.sqlite3")) as db:
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
