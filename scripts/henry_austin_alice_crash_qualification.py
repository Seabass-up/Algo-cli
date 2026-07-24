#!/usr/bin/env python3
"""Qualify native Alice artifact recovery across a killed Swift publisher."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUSTIN = ROOT / "native" / "austin"
HARDENING = ROOT / "hardening"
PUBLISH_TEST = "aliceProcessCrashProbePublishesThenWaitsForKill"
RECOVER_TEST = "aliceProcessCrashProbeRecoversKilledPublisher"
MODE_KEY = "ALGO_AUSTIN_ALICE_CRASH_MODE"
DIRECTORY_KEY = "ALGO_AUSTIN_ALICE_CRASH_DIRECTORY"
MARKER_KEY = "ALGO_AUSTIN_ALICE_CRASH_MARKER"
TOKEN_KEY = "ALGO_AUSTIN_ALICE_CRASH_TOKEN"
MAX_CAPTURE_BYTES = 64 * 1_024
MAX_MARKER_BYTES = 256
LIMITATIONS = (
    "DEBUG Swift test-process evidence only; this does not prove a production-signed "
    "installed XPC, TCC, Keychain, ScreenCaptureKit, sudden-power-loss, or secure-erasure path."
)
SOURCE_PATHS = (
    "native/austin/Sources/AustinTCCAdapter/AustinAliceCaptureArtifact.swift",
    "native/austin/Tests/AustinCoreTests/AustinAliceCaptureArtifactTests.swift",
    "scripts/henry_austin_alice_crash_qualification.py",
    "tests/test_henry_austin_alice_crash_qualification.py",
)


class AliceCrashQualificationError(RuntimeError):
    """A content-free Alice process-kill qualification invariant failed."""


def _failure(reason_code: str) -> AliceCrashQualificationError:
    return AliceCrashQualificationError(reason_code)


def _clean_environment(*, mode: str, directory: Path, marker: Path, token: str) -> dict[str, str]:
    if mode not in {"publish-and-wait", "recover"} or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise _failure("alice_crash_fixture")
    environment = os.environ.copy()
    for key in (MODE_KEY, DIRECTORY_KEY, MARKER_KEY, TOKEN_KEY):
        environment.pop(key, None)
    environment.update(
        {
            MODE_KEY: mode,
            DIRECTORY_KEY: str(directory),
            MARKER_KEY: str(marker),
            TOKEN_KEY: token,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _swift_test_command(test_name: str, *, skip_build: bool) -> tuple[str, ...]:
    command = ["swift", "test", "--package-path", str(AUSTIN)]
    if skip_build:
        command.append("--skip-build")
    command.extend(("--filter", test_name))
    return tuple(command)


def _bounded_process_output(stdout: bytes, stderr: bytes) -> None:
    if len(stdout) > MAX_CAPTURE_BYTES or len(stderr) > MAX_CAPTURE_BYTES:
        raise _failure("alice_crash_output")


def _read_marker(marker: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(marker, flags)
    except OSError as exc:
        raise _failure("alice_crash_marker") from exc
    try:
        information = os.fstat(descriptor)
        if (
            not stat.S_ISREG(information.st_mode)
            or information.st_nlink != 1
            or information.st_uid != os.geteuid()
            or information.st_mode & 0o777 != 0o600
            or information.st_size <= 0
            or information.st_size > MAX_MARKER_BYTES
        ):
            raise _failure("alice_crash_marker")
        chunks: list[bytes] = []
        remaining = information.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise _failure("alice_crash_marker")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != information.st_size
            or len(payload) > MAX_MARKER_BYTES
            or (
                information.st_dev,
                information.st_ino,
                information.st_mode,
                information.st_size,
                information.st_mtime_ns,
                information.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise _failure("alice_crash_marker")
        return payload.decode("ascii", errors="strict")
    except AliceCrashQualificationError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _failure("alice_crash_marker") from exc
    finally:
        os.close(descriptor)


def _wait_for_marker(process: subprocess.Popen[bytes], marker: Path) -> str:
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if marker.exists():
            return _read_marker(marker)
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1.0)
            _bounded_process_output(stdout, stderr)
            raise _failure("alice_crash_publisher_early_exit")
        time.sleep(0.02)
    raise _failure("alice_crash_publisher_timeout")


def _process_parent_and_command(process_id: int) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ("ps", "-o", "ppid=,command=", "-p", str(process_id)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _failure("alice_crash_process_identity") from exc
    _bounded_process_output(completed.stdout, completed.stderr)
    try:
        fields = completed.stdout.decode("utf-8", errors="strict").strip().split(maxsplit=1)
        parent = int(fields[0])
        command = fields[1]
    except (UnicodeDecodeError, ValueError, IndexError) as exc:
        raise _failure("alice_crash_process_identity") from exc
    if completed.returncode != 0 or parent < 0 or not command:
        raise _failure("alice_crash_process_identity")
    return parent, command


def _assert_owned_test_process(publisher_id: int, test_process_id: int) -> None:
    if publisher_id <= 1 or test_process_id <= 1 or publisher_id == test_process_id:
        raise _failure("alice_crash_process_identity")
    current = test_process_id
    for depth in range(16):
        parent, command = _process_parent_and_command(current)
        if depth == 0 and ("AustinNativeControlPackageTests.xctest" not in command or "xctest" not in command):
            raise _failure("alice_crash_process_identity")
        if parent == publisher_id:
            return
        if parent <= 1 or parent == current:
            break
        current = parent
    raise _failure("alice_crash_process_identity")


def _kill_owned_test_process(process: subprocess.Popen[bytes], test_process_id: int) -> tuple[bytes, bytes]:
    _assert_owned_test_process(process.pid, test_process_id)
    try:
        os.kill(test_process_id, signal.SIGKILL)
    except ProcessLookupError as exc:
        raise _failure("alice_crash_kill") from exc
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            _, command = _process_parent_and_command(test_process_id)
        except AliceCrashQualificationError:
            break
        if command.startswith("(xctest)") or "<defunct>" in command:
            break
        time.sleep(0.01)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        stdout, stderr = process.communicate(timeout=10.0)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5.0)
        raise _failure("alice_crash_kill") from exc
    _bounded_process_output(stdout, stderr)
    if process.returncode == 0:
        raise _failure("alice_crash_kill")
    return stdout, stderr


def _assert_private_artifact_safe(directory: Path, expected_name: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}\.alice", expected_name):
        raise _failure("alice_crash_artifact_name")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise _failure("alice_crash_artifact") from exc
    try:
        entries = os.listdir(descriptor)
        if entries != [expected_name]:
            raise _failure("alice_crash_artifact_count")
        information = os.stat(expected_name, dir_fd=descriptor, follow_symlinks=False)
    except AliceCrashQualificationError:
        raise
    except OSError as exc:
        raise _failure("alice_crash_artifact") from exc
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(information.st_mode)
        or stat.S_ISLNK(information.st_mode)
        or information.st_nlink != 1
        or information.st_uid != os.geteuid()
        or information.st_mode & 0o777 != 0o600
        or information.st_size <= 0
        or information.st_size > 64 * 1024 * 1024
    ):
        raise _failure("alice_crash_artifact")


def _run_recovery(directory: Path, marker: Path, token: str) -> None:
    try:
        completed = subprocess.run(
            _swift_test_command(RECOVER_TEST, skip_build=True),
            cwd=ROOT,
            env=_clean_environment(mode="recover", directory=directory, marker=marker, token=token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _failure("alice_crash_recovery_process") from exc
    _bounded_process_output(completed.stdout, completed.stderr)
    if completed.returncode != 0 or _read_marker(marker) != token:
        raise _failure("alice_crash_recovery")
    try:
        if os.listdir(directory):
            raise _failure("alice_crash_recovery")
    except OSError as exc:
        raise _failure("alice_crash_recovery") from exc


def _run_trial(trial: int, *, skip_build: bool) -> None:
    token = hashlib.sha256(f"alice-crash-v1:{trial}".encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="HenryAliceCrash-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        directory = root / "AliceArtifacts"
        directory.mkdir(mode=0o700)
        ready = root / "publisher.ready"
        recovered = root / "recovered.ready"
        try:
            publisher = subprocess.Popen(
                _swift_test_command(PUBLISH_TEST, skip_build=skip_build),
                cwd=ROOT,
                env=_clean_environment(
                    mode="publish-and-wait",
                    directory=directory,
                    marker=ready,
                    token=token,
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise _failure("alice_crash_publisher_process") from exc
        try:
            marker = _wait_for_marker(publisher, ready)
            fields = marker.split(":")
            if len(fields) != 3 or fields[0] != token or not fields[1].isdigit():
                raise _failure("alice_crash_marker")
            test_process_id = int(fields[1])
            artifact_name = fields[2]
            _assert_private_artifact_safe(directory, artifact_name)
            _kill_owned_test_process(publisher, test_process_id)
            _assert_private_artifact_safe(directory, artifact_name)
        finally:
            if publisher.poll() is None:
                try:
                    os.killpg(publisher.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                publisher.wait(timeout=5.0)
        _run_recovery(directory, recovered, token)


def _digest_paths(paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(paths):
        path = ROOT / relative
        try:
            information = path.lstat()
            if (
                not stat.S_ISREG(information.st_mode)
                or stat.S_ISLNK(information.st_mode)
                or information.st_nlink != 1
                or information.st_size <= 0
                or information.st_size > 2 * 1024 * 1024
            ):
                raise _failure("alice_crash_source")
            payload = path.read_bytes()
        except OSError as exc:
            raise _failure("alice_crash_source") from exc
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _validate_report(report: dict[str, Any]) -> None:
    expected_keys = {
        "fixture_digest",
        "generated_at",
        "limitations",
        "orphans_recovered_after_restart",
        "process_kills",
        "published_before_kill",
        "schema_version",
        "source_digest",
        "status",
        "trials",
    }
    if set(report) != expected_keys:
        raise _failure("alice_crash_report")
    trials = report.get("trials")
    if (
        report.get("schema_version") != 1
        or report.get("status") != "passed"
        or type(trials) is not int
        or not 1 <= trials <= 20
        or report.get("process_kills") != trials
        or report.get("published_before_kill") != trials
        or report.get("orphans_recovered_after_restart") != trials
        or report.get("limitations") != LIMITATIONS
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(report.get("fixture_digest")))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(report.get("source_digest")))
        or not isinstance(report.get("generated_at"), str)
    ):
        raise _failure("alice_crash_report")


def qualify(*, trials: int) -> dict[str, Any]:
    if sys.platform != "darwin":
        raise _failure("alice_crash_platform")
    if type(trials) is not int or not 1 <= trials <= 20:
        raise _failure("alice_crash_trials")
    for trial in range(trials):
        _run_trial(trial, skip_build=trial > 0)
    fixture = json.dumps(
        {"publish_test": PUBLISH_TEST, "recover_test": RECOVER_TEST, "trials": trials},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    report: dict[str, Any] = {
        "fixture_digest": "sha256:" + hashlib.sha256(fixture).hexdigest(),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "limitations": LIMITATIONS,
        "orphans_recovered_after_restart": trials,
        "process_kills": trials,
        "published_before_kill": trials,
        "schema_version": 1,
        "source_digest": _digest_paths(SOURCE_PATHS),
        "status": "passed",
        "trials": trials,
    }
    _validate_report(report)
    return report


def _bounded_output(path: Path) -> Path:
    candidate = ROOT / path if not path.is_absolute() else path
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise _failure("alice_crash_output_scope") from exc
    if parent != HARDENING.resolve() or not candidate.name.startswith("alice-") or candidate.suffix != ".json":
        raise _failure("alice_crash_output_scope")
    return parent / candidate.name


def _atomic_private_write(path: Path, payload: bytes) -> None:
    path = _bounded_output(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(path.parent, flags)
    except OSError as exc:
        raise _failure("alice_crash_output_identity") from exc
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
            # A source-controlled, content-free report is normally restored as
            # 0644.  Permit read bits on an existing report, but never accept
            # an executable or group/other-writable replacement target.
            or existing.st_mode & 0o133
        ):
            raise _failure("alice_crash_output_identity")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise _failure("alice_crash_output_write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary_name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except AliceCrashQualificationError:
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
        raise _failure("alice_crash_output_write") from exc
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
    except AliceCrashQualificationError as exc:
        print(
            json.dumps(
                {"reason_code": str(exc), "schema_version": 1, "status": "failed"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
