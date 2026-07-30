#!/usr/bin/env python3
"""Provision the root-owned manifest for one ephemeral Austin signing runner."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import time
from typing import Any, Mapping, NoReturn, Protocol


ATTESTATION_PATH = Path("/Library/Application Support/AlgoCLI/AdaAustinSigningRunner.json")
ATTESTATION_PARENT = Path("/Library/Application Support")
ATTESTATION_DIRECTORY_NAME = "AlgoCLI"
ATTESTATION_FILE_NAME = "AdaAustinSigningRunner.json"
EXPECTED_REPOSITORY_ID = 1_297_752_684
EXPECTED_REF = "refs/heads/main"
EXPECTED_WORKFLOW = ".github/workflows/henry-austin-signing-qualification.yml"
EXPECTED_LABELS = ("ARM64", "algo-cli-signing-ephemeral", "macOS", "self-hosted")
LOG_FORWARDER_LABEL = "com.algo-cli.austin.runner-log-forwarder"
MAX_ATTESTATION_BYTES = 16 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_ATTESTATION_LIFETIME_SECONDS = 24 * 60 * 60
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RUNNER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class HenryAustinProvisioningRejected(RuntimeError):
    """An offline signing-runner provisioning invariant failed closed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "austin_provision_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise HenryAustinProvisioningRejected(reason_code)


class HenryAustinProvisioningCommandRunner(Protocol):
    def run(self, command: tuple[str, ...], *, timeout_seconds: float) -> bytes: ...


class HenryAustinProvisioningSubprocessRunner:
    """Run fixed absolute host probes without a shell or inherited credentials."""

    def run(self, command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        if (
            type(command) is not tuple
            or not command
            or any(type(part) is not str or "\x00" in part for part in command)
            or not 0 < timeout_seconds <= 30
        ):
            _reject("austin_provision_command")
        try:
            completed = subprocess.run(
                command,
                cwd="/",
                env={
                    "HOME": "/var/root",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            _reject("austin_provision_command")
        if (
            completed.returncode != 0
            or len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            _reject("austin_provision_command")
        return completed.stdout


def _assert_offline_administrator(
    *,
    environment: Mapping[str, str],
    effective_uid: int,
    expected_owner_uid: int,
    platform_name: str,
    machine_name: str,
) -> None:
    if type(environment) is not dict or any(
        type(key) is not str or type(value) is not str
        for key, value in environment.items()
    ):
        _reject("austin_provision_environment")
    if "GITHUB_ACTIONS" in environment:
        _reject("austin_provision_online")
    if (
        type(effective_uid) is not int
        or type(expected_owner_uid) is not int
        or effective_uid < 0
        or expected_owner_uid < 0
        or effective_uid != expected_owner_uid
    ):
        _reject("austin_provision_administrator")
    if (
        type(platform_name) is not str
        or type(machine_name) is not str
        or platform_name != "darwin"
        or machine_name.casefold() != "arm64"
    ):
        _reject("austin_provision_platform")


def _validated_inputs(
    *,
    runner_name: object,
    image_digest: object,
    log_retention_days: object,
    lifetime_seconds: object,
    now_seconds: object,
) -> tuple[str, str, int, int, int]:
    if type(runner_name) is not str or _RUNNER_NAME_RE.fullmatch(runner_name) is None:
        _reject("austin_provision_runner")
    if type(image_digest) is not str or _DIGEST_RE.fullmatch(image_digest) is None:
        _reject("austin_provision_image")
    if type(log_retention_days) is not int or not 30 <= log_retention_days <= 365:
        _reject("austin_provision_retention")
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= MAX_ATTESTATION_LIFETIME_SECONDS:
        _reject("austin_provision_lifetime")
    if type(now_seconds) is not int or now_seconds < 0:
        _reject("austin_provision_time")
    return runner_name, image_digest, log_retention_days, lifetime_seconds, now_seconds


def _host_state(runner: HenryAustinProvisioningCommandRunner) -> str:
    try:
        raw_boot_session = runner.run(
            ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"),
            timeout_seconds=10,
        )
        if type(raw_boot_session) is not bytes or len(raw_boot_session) > MAX_COMMAND_OUTPUT_BYTES:
            _reject("austin_provision_command")
        boot_session = raw_boot_session.decode("ascii", errors="strict").strip().casefold()
    except UnicodeDecodeError:
        _reject("austin_provision_command")
    if _UUID_RE.fullmatch(boot_session) is None:
        _reject("austin_provision_boot")
    forwarder = runner.run(
        ("/bin/launchctl", "print", f"system/{LOG_FORWARDER_LABEL}"),
        timeout_seconds=10,
    )
    if (
        type(forwarder) is not bytes
        or len(forwarder) > MAX_COMMAND_OUTPUT_BYTES
        or re.search(rb"(?m)^\s*state = running\s*$", forwarder) is None
        or re.search(rb"(?m)^\s*pid = [1-9][0-9]*\s*$", forwarder) is None
    ):
        _reject("austin_provision_log_forwarder")
    return boot_session


def _timestamp(seconds: int) -> str:
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        _reject("austin_provision_time")


def _manifest_payload(
    *,
    runner_name: str,
    image_digest: str,
    log_retention_days: int,
    lifetime_seconds: int,
    now_seconds: int,
    boot_session_uuid: str,
) -> bytes:
    value: dict[str, Any] = {
        "allowed_ref": EXPECTED_REF,
        "allowed_repository_id": EXPECTED_REPOSITORY_ID,
        "allowed_workflow": EXPECTED_WORKFLOW,
        "boot_session_uuid": boot_session_uuid,
        "expires_at": _timestamp(now_seconds + lifetime_seconds),
        "image_digest": image_digest,
        "log_forwarder_label": LOG_FORWARDER_LABEL,
        "log_retention_days": log_retention_days,
        "provisioned_at": _timestamp(now_seconds),
        "runner_labels": list(EXPECTED_LABELS),
        "runner_mode": "ephemeral",
        "runner_name": runner_name,
        "schema_version": 2,
    }
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if not 1 <= len(payload) <= MAX_ATTESTATION_BYTES:
        _reject("austin_provision_manifest")
    return payload


def _open_secure_parent(
    *,
    parent: Path,
    output: Path,
    expected_owner_uid: int,
) -> tuple[int, bool]:
    if (
        not isinstance(parent, Path)
        or not isinstance(output, Path)
        or not parent.is_absolute()
        or not output.is_absolute()
        or ".." in parent.parts
        or ".." in output.parts
        or output != parent / ATTESTATION_DIRECTORY_NAME / ATTESTATION_FILE_NAME
    ):
        _reject("austin_provision_path")
    try:
        parent_information = parent.lstat()
        if parent.resolve(strict=True) != parent:
            _reject("austin_provision_path")
    except OSError:
        _reject("austin_provision_path")
    if (
        not stat.S_ISDIR(parent_information.st_mode)
        or stat.S_ISLNK(parent_information.st_mode)
        or parent_information.st_uid != expected_owner_uid
        or parent_information.st_mode & 0o022
    ):
        _reject("austin_provision_parent")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent_descriptor = os.open(parent, flags)
    except OSError:
        _reject("austin_provision_parent")
    directory_created = False
    directory_descriptor: int | None = None
    successful = False
    try:
        opened_parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or opened_parent.st_uid != expected_owner_uid
            or opened_parent.st_mode & 0o022
            or (opened_parent.st_dev, opened_parent.st_ino)
            != (parent_information.st_dev, parent_information.st_ino)
        ):
            _reject("austin_provision_parent")
        try:
            os.mkdir(ATTESTATION_DIRECTORY_NAME, 0o755, dir_fd=parent_descriptor)
            directory_created = True
        except FileExistsError:
            pass
        directory_descriptor = os.open(
            ATTESTATION_DIRECTORY_NAME,
            flags,
            dir_fd=parent_descriptor,
        )
        directory_information = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_information.st_mode)
            or directory_information.st_uid != expected_owner_uid
            or directory_information.st_mode & 0o022
        ):
            _reject("austin_provision_directory")
        if directory_created:
            os.fchmod(directory_descriptor, 0o755)
            os.fsync(parent_descriptor)
        successful = True
        return directory_descriptor, directory_created
    except HenryAustinProvisioningRejected:
        raise
    except OSError:
        _reject("austin_provision_directory")
    finally:
        if not successful and directory_descriptor is not None:
            os.close(directory_descriptor)
        if not successful and directory_created:
            try:
                os.rmdir(ATTESTATION_DIRECTORY_NAME, dir_fd=parent_descriptor)
            except OSError:
                pass
        os.close(parent_descriptor)


def _publish_no_overwrite(
    *,
    directory_descriptor: int,
    payload: bytes,
    expected_owner_uid: int,
) -> None:
    temporary_name = f".{ATTESTATION_FILE_NAME}.{secrets.token_hex(16)}.tmp"
    if re.fullmatch(r"\.AdaAustinSigningRunner\.json\.[0-9a-f]{32}\.tmp", temporary_name) is None:
        _reject("austin_provision_temporary")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    temporary_created = False
    final_created = False
    committed = False
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _reject("austin_provision_write")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        written_information = os.fstat(descriptor)
        created_identity = (written_information.st_dev, written_information.st_ino)
        if (
            not stat.S_ISREG(written_information.st_mode)
            or written_information.st_nlink != 1
            or written_information.st_uid != expected_owner_uid
            or stat.S_IMODE(written_information.st_mode) != 0o644
            or written_information.st_size != len(payload)
        ):
            _reject("austin_provision_write")
        try:
            os.link(
                temporary_name,
                ATTESTATION_FILE_NAME,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            _reject("austin_provision_attestation_exists")
        except OSError:
            _reject("austin_provision_publish")
        final_created = True
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_created = False
        os.fsync(directory_descriptor)
        final_information = os.stat(
            ATTESTATION_FILE_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        after_link = os.fstat(descriptor)
        if (
            created_identity != (final_information.st_dev, final_information.st_ino)
            or created_identity != (after_link.st_dev, after_link.st_ino)
            or not stat.S_ISREG(final_information.st_mode)
            or final_information.st_nlink != 1
            or final_information.st_uid != expected_owner_uid
            or stat.S_IMODE(final_information.st_mode) != 0o644
            or final_information.st_size != len(payload)
        ):
            _reject("austin_provision_publish")
        committed = True
    except HenryAustinProvisioningRejected:
        raise
    except OSError:
        _reject("austin_provision_publish" if final_created else "austin_provision_write")
    finally:
        if not committed and final_created and created_identity is not None:
            try:
                candidate = os.stat(
                    ATTESTATION_FILE_NAME,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if created_identity == (candidate.st_dev, candidate.st_ino):
                    os.unlink(ATTESTATION_FILE_NAME, dir_fd=directory_descriptor)
            except OSError:
                pass
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
        if not committed:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
        if descriptor is not None:
            os.close(descriptor)


def _provision_runner_manifest(
    *,
    output: Path,
    parent: Path,
    runner_name: object,
    image_digest: object,
    log_retention_days: object,
    lifetime_seconds: object,
    environment: Mapping[str, str],
    effective_uid: int,
    expected_owner_uid: int,
    platform_name: str,
    machine_name: str,
    runner: HenryAustinProvisioningCommandRunner,
    now_seconds: object,
) -> dict[str, Any]:
    _assert_offline_administrator(
        environment=environment,
        effective_uid=effective_uid,
        expected_owner_uid=expected_owner_uid,
        platform_name=platform_name,
        machine_name=machine_name,
    )
    selected_name, selected_image, retention, lifetime, selected_now = _validated_inputs(
        runner_name=runner_name,
        image_digest=image_digest,
        log_retention_days=log_retention_days,
        lifetime_seconds=lifetime_seconds,
        now_seconds=now_seconds,
    )
    boot_session = _host_state(runner)
    payload = _manifest_payload(
        runner_name=selected_name,
        image_digest=selected_image,
        log_retention_days=retention,
        lifetime_seconds=lifetime,
        now_seconds=selected_now,
        boot_session_uuid=boot_session,
    )
    directory_descriptor, _created = _open_secure_parent(
        parent=parent,
        output=output,
        expected_owner_uid=expected_owner_uid,
    )
    try:
        _publish_no_overwrite(
            directory_descriptor=directory_descriptor,
            payload=payload,
            expected_owner_uid=expected_owner_uid,
        )
    finally:
        os.close(directory_descriptor)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    return {
        "attestation_digest": digest,
        "limitations": (
            "This offline tool writes one no-overwrite, root-owned, boot-bound manifest after confirming a running "
            "system log forwarder. It does not create image provenance, configure GitHub or its protected secret, "
            "prove certificate custody or log delivery, execute signing, or destroy the runner host."
        ),
        "public_claim_eligible": False,
        "schema_version": 1,
        "status": "passed",
    }


def provision_runner_manifest(
    *,
    runner_name: str,
    image_digest: str,
    log_retention_days: int,
    lifetime_seconds: int,
) -> dict[str, Any]:
    """Provision the fixed production path under a real offline root administrator."""
    return _provision_runner_manifest(
        output=ATTESTATION_PATH,
        parent=ATTESTATION_PARENT,
        runner_name=runner_name,
        image_digest=image_digest,
        log_retention_days=log_retention_days,
        lifetime_seconds=lifetime_seconds,
        environment=dict(os.environ),
        effective_uid=os.geteuid(),
        expected_owner_uid=0,
        platform_name=sys.platform,
        machine_name=platform.machine(),
        runner=HenryAustinProvisioningSubprocessRunner(),
        now_seconds=int(time.time()),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-name", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--log-retention-days", required=True, type=int)
    parser.add_argument("--lifetime-hours", required=True, type=int)
    arguments = parser.parse_args(argv)
    try:
        report = provision_runner_manifest(
            runner_name=arguments.runner_name,
            image_digest=arguments.image_digest,
            log_retention_days=arguments.log_retention_days,
            lifetime_seconds=arguments.lifetime_hours * 60 * 60,
        )
    except HenryAustinProvisioningRejected as error:
        print(
            json.dumps(
                {
                    "reason_code": error.reason_code,
                    "schema_version": 1,
                    "status": "blocked",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
