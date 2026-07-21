#!/usr/bin/env python3
"""Qualify Ada replay-store crash atomicity with a DEBUG-only Austin probe."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUSTIN = ROOT / "native" / "austin"
HARDENING = ROOT / "hardening"
PRODUCT = "austin-ada-crash-probe"
CRASH_ENVIRONMENT_KEY = "ALGO_AUSTIN_ADA_CRASH_CHECKPOINT"
CHECKPOINTS = (
    "after_begin",
    "after_maintenance",
    "after_insert",
    "after_state",
    "after_commit",
)
NAMESPACES = ("permit", "preparation")
PRECOMMIT_CHECKPOINTS = frozenset(CHECKPOINTS[:-1])
MAX_CAPTURE_BYTES = 8_192
MAX_BINARY_BYTES = 32 * 1_024 * 1_024


class CrashQualificationError(RuntimeError):
    """A content-free crash-qualification invariant failed."""


def _failure(reason_code: str) -> CrashQualificationError:
    return CrashQualificationError(reason_code)


def _run(
    command: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _failure("crash_probe_process") from exc
    if len(completed.stdout) > MAX_CAPTURE_BYTES or len(completed.stderr) > MAX_CAPTURE_BYTES:
        raise _failure("crash_probe_output")
    return completed


def _clean_environment(*, checkpoint: str | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop(CRASH_ENVIRONMENT_KEY, None)
    if checkpoint is not None:
        if checkpoint not in CHECKPOINTS:
            raise _failure("crash_probe_checkpoint")
        environment[CRASH_ENVIRONMENT_KEY] = checkpoint
    return environment


def _build_probe(configuration: str) -> Path:
    if configuration not in {"debug", "release"}:
        raise _failure("crash_probe_configuration")
    configuration_args = () if configuration == "debug" else ("--configuration", "release")
    built = _run(
        (
            "swift",
            "build",
            "--package-path",
            str(AUSTIN),
            *configuration_args,
            "--product",
            PRODUCT,
        ),
        environment=_clean_environment(),
    )
    if built.returncode != 0:
        raise _failure("crash_probe_build")
    located = _run(
        (
            "swift",
            "build",
            "--package-path",
            str(AUSTIN),
            *configuration_args,
            "--show-bin-path",
        ),
        environment=_clean_environment(),
    )
    if located.returncode != 0:
        raise _failure("crash_probe_build_path")
    try:
        directory = Path(located.stdout.decode("utf-8", errors="strict").strip()).resolve()
    except UnicodeDecodeError as exc:
        raise _failure("crash_probe_build_path") from exc
    binary = directory / PRODUCT
    try:
        information = binary.lstat()
        build_root = (AUSTIN / ".build").resolve()
        binary.relative_to(build_root)
    except (OSError, ValueError) as exc:
        raise _failure("crash_probe_binary") from exc
    if (
        not stat.S_ISREG(information.st_mode)
        or stat.S_ISLNK(information.st_mode)
        or information.st_nlink != 1
        or information.st_size <= 0
        or information.st_size > MAX_BINARY_BYTES
        or information.st_mode & 0o022
    ):
        raise _failure("crash_probe_binary")
    return binary


def _binary_digest(binary: Path) -> str:
    digest = hashlib.sha256()
    try:
        with binary.open("rb") as handle:
            while chunk := handle.read(1_048_576):
                digest.update(chunk)
    except OSError as exc:
        raise _failure("crash_probe_binary") from exc
    return "sha256:" + digest.hexdigest()


def _release_hook_is_absent(binary: Path) -> bool:
    try:
        payload = binary.read_bytes()
    except OSError as exc:
        raise _failure("crash_probe_binary") from exc
    if len(payload) > MAX_BINARY_BYTES:
        raise _failure("crash_probe_binary")
    return CRASH_ENVIRONMENT_KEY.encode("ascii") not in payload


def _permit_id(value: int) -> str:
    if not 0 <= value <= 0xFFFFFFFFFFFF:
        raise _failure("crash_probe_fixture")
    return f"00000000-0000-4000-8000-{value:012x}"


def _decode_report(completed: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    if completed.returncode != 0 or completed.stderr:
        raise _failure("crash_probe_invocation")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _failure("crash_probe_output") from exc
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if type(value) is not dict or canonical != completed.stdout:
        raise _failure("crash_probe_output")
    return value


def _claim(
    binary: Path,
    database: Path,
    permit_id: str,
    claimed_at: int,
    expires_at: int,
    *,
    namespace: str = "permit",
    checkpoint: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = {
        "permit": "claim",
        "preparation": "claim-preparation",
    }.get(namespace)
    if command is None:
        raise _failure("crash_probe_namespace")
    return _run(
        (
            str(binary),
            command,
            str(database),
            permit_id,
            str(claimed_at),
            str(expires_at),
        ),
        environment=_clean_environment(checkpoint=checkpoint),
        timeout=15.0,
    )


def _inspect(
    binary: Path,
    database: Path,
    claim_id: str,
    *,
    namespace: str = "permit",
) -> dict[str, Any]:
    command = {
        "permit": "inspect",
        "preparation": "inspect-preparation",
    }.get(namespace)
    if command is None:
        raise _failure("crash_probe_namespace")
    return _decode_report(
        _run(
            (str(binary), command, str(database), claim_id),
            environment=_clean_environment(),
            timeout=15.0,
        )
    )


def _assert_store_state(
    binary: Path,
    database: Path,
    *,
    retained_id: str,
    absent_id: str,
    high_water: int,
    namespace: str,
) -> None:
    retained = _inspect(binary, database, retained_id, namespace=namespace)
    absent = _inspect(binary, database, absent_id, namespace=namespace)
    permit_count = 1 if namespace == "permit" else 0
    preparation_count = 1 if namespace == "preparation" else 0
    expected_common = {
        "high_water_ms": high_water,
        "permit_claim_count": permit_count,
        "preparation_claim_count": preparation_count,
        "retention_floor_ms": max(0, high_water - 5_000),
        "status": "inspected",
    }
    if retained != {**expected_common, "contains": True}:
        raise _failure("crash_probe_retained_state")
    if absent != {**expected_common, "contains": False}:
        raise _failure("crash_probe_absent_state")


def _qualify_checkpoint(
    binary: Path,
    checkpoint: str,
    trial: int,
    namespace: str,
) -> None:
    checkpoint_index = CHECKPOINTS.index(checkpoint)
    try:
        namespace_index = NAMESPACES.index(namespace)
    except ValueError as exc:
        raise _failure("crash_probe_namespace") from exc
    fixture = 10_000 + namespace_index * 100_000 + checkpoint_index * 1_000 + trial * 10
    identifier_base = (
        0x2000 + namespace_index * 0x1000 + checkpoint_index * 0x100 + trial * 2
    )
    first_id = _permit_id(identifier_base)
    second_id = _permit_id(identifier_base + 1)
    first_now = fixture
    first_expires = first_now + 1
    second_now = first_now + 2
    second_expires = second_now + (300_000 if namespace == "permit" else 60_000)

    with tempfile.TemporaryDirectory(prefix="AustinAdaCrash-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        database = root / "private" / "AdaPermitClaims.sqlite3"
        baseline = _claim(
            binary,
            database,
            first_id,
            first_now,
            first_expires,
            namespace=namespace,
        )
        _decode_report(baseline)

        crashed = _claim(
            binary,
            database,
            second_id,
            second_now,
            second_expires,
            namespace=namespace,
            checkpoint=checkpoint,
        )
        if crashed.returncode != -signal.SIGKILL or crashed.stdout or crashed.stderr:
            raise _failure("crash_probe_kill")

        if checkpoint in PRECOMMIT_CHECKPOINTS:
            retained_id, absent_id, high_water = first_id, second_id, first_now
            replay_now, replay_expires = first_now, first_expires
        else:
            retained_id, absent_id, high_water = second_id, first_id, second_now
            replay_now, replay_expires = second_now, second_expires
        _assert_store_state(
            binary,
            database,
            retained_id=retained_id,
            absent_id=absent_id,
            high_water=high_water,
            namespace=namespace,
        )
        replay = _claim(
            binary,
            database,
            retained_id,
            replay_now,
            replay_expires,
            namespace=namespace,
        )
        replay_reason = b"permit_replay" if namespace == "permit" else b"preparation_replay"
        if replay.returncode != 78 or replay.stdout or replay_reason not in replay.stderr:
            raise _failure("crash_probe_replay")


def _qualify_release_hook(debug_binary: Path, release_binary: Path) -> None:
    if not _release_hook_is_absent(release_binary):
        raise _failure("crash_probe_release_hook")
    with tempfile.TemporaryDirectory(prefix="AustinAdaRelease-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        database = root / "private" / "AdaPermitClaims.sqlite3"
        permit_id = _permit_id(0x9000)
        completed = _claim(
            release_binary,
            database,
            permit_id,
            50_000,
            51_000,
            checkpoint="after_state",
        )
        _decode_report(completed)
        _assert_store_state(
            debug_binary,
            database,
            retained_id=permit_id,
            absent_id=_permit_id(0x9001),
            high_water=50_000,
            namespace="permit",
        )


def qualify(*, trials: int) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise _failure("crash_probe_platform")
    if type(trials) is not int or not 1 <= trials <= 50:
        raise _failure("crash_probe_trials")
    debug_binary = _build_probe("debug")
    release_binary = _build_probe("release")
    _qualify_release_hook(debug_binary, release_binary)
    for namespace in NAMESPACES:
        for checkpoint in CHECKPOINTS:
            for trial in range(trials):
                _qualify_checkpoint(debug_binary, checkpoint, trial, namespace)
    namespace_count = len(NAMESPACES)
    return {
        "checkpoints": list(CHECKPOINTS),
        "debug_binary_digest": _binary_digest(debug_binary),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "namespaces": list(NAMESPACES),
        "postcommit_durable": namespace_count * trials,
        "precommit_rollbacks": namespace_count * len(PRECOMMIT_CHECKPOINTS) * trials,
        "process_kills": namespace_count * len(CHECKPOINTS) * trials,
        "release_binary_digest": _binary_digest(release_binary),
        "release_hook_absent": True,
        "replay_rejections": namespace_count * len(CHECKPOINTS) * trials,
        "schema_version": 1,
        "status": "passed",
        "trials_per_checkpoint": trials,
    }


def _bounded_output(path: Path) -> Path:
    candidate = ROOT / path if not path.is_absolute() else path
    hardening = HARDENING.resolve()
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise _failure("crash_probe_output_scope") from exc
    if parent != hardening:
        raise _failure("crash_probe_output_scope")
    if not candidate.name.startswith("ada-") or candidate.suffix != ".json":
        raise _failure("crash_probe_output_scope")
    return parent / candidate.name


def _atomic_private_write(path: Path, payload: bytes) -> None:
    path = _bounded_output(path)
    if not path.parent.is_dir():
        raise _failure("crash_probe_output_scope")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(path.parent, directory_flags)
    except OSError as exc:
        raise _failure("crash_probe_output_identity") from exc
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
            or existing.st_uid != os.geteuid()
            or existing.st_mode & 0o077
        ):
            raise _failure("crash_probe_output_identity")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise _failure("crash_probe_output_write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except CrashQualificationError:
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except OSError:
            pass
        raise _failure("crash_probe_output_write") from exc
    finally:
        os.close(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = qualify(trials=arguments.trials)
        payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if arguments.output is not None:
            _atomic_private_write(arguments.output, payload)
        sys.stdout.buffer.write(payload)
        return 0
    except CrashQualificationError as exc:
        error_payload = {
            "reason_code": str(exc),
            "schema_version": 1,
            "status": "failed",
        }
        print(json.dumps(error_payload, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
