#!/usr/bin/env python3
"""Prepare a public-only candidate for Austin lifecycle receipt authorities."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Mapping, NoReturn

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE_NAME = "AdaAustinLifecycleAuthorities.json"
EXPECTED_REPOSITORY_ID = 1_297_752_684
EXPECTED_REF = "refs/heads/main"
EXPECTED_WORKFLOW = ".github/workflows/henry-austin-signing-qualification.yml"
EXPECTED_WORKFLOW_NAME = "Austin signed-package qualification"
EXPECTED_JOB_NAME = "Developer ID, notarization, and Gatekeeper"
EXPECTED_RUNNER_GROUP = "algo-cli-signing"
EXPECTED_RUNNER_LABELS = ("ARM64", "algo-cli-signing-ephemeral", "macOS", "self-hosted")
MAX_PUBLIC_KEY_BYTES = 4 * 1024
MAX_MANIFEST_BYTES = 16 * 1024

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")


class HenryAustinAuthorityPreflightRejected(RuntimeError):
    """An authority-candidate preparation invariant failed closed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "austin_authority_preflight_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise HenryAustinAuthorityPreflightRejected(reason_code)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        payload = (
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            .encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        _reject("austin_authority_preflight_manifest")
    if not 1 <= len(payload) <= MAX_MANIFEST_BYTES:
        _reject("austin_authority_preflight_manifest")
    return payload


def _outside_repository(path: Path) -> bool:
    repository = ROOT.resolve()
    return path != repository and repository not in path.parents


def _open_descriptor_relative(path: Path, *, reason: str) -> int:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not path.name
        or ".." in path.parts
        or not _outside_repository(path)
    ):
        _reject("austin_authority_preflight_path")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(path.anchor, directory_flags)
    except OSError:
        _reject("austin_authority_preflight_path")
    try:
        for component in path.parts[1:-1]:
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError:
                _reject("austin_authority_preflight_path")
            information = os.fstat(child_descriptor)
            if not stat.S_ISDIR(information.st_mode):
                os.close(child_descriptor)
                _reject("austin_authority_preflight_path")
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor
        try:
            return os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            _reject(reason)
        except OSError:
            _reject("austin_authority_preflight_path")
    finally:
        os.close(directory_descriptor)


def _open_directory_descriptor_relative(path: Path) -> int:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or not path.name
        or ".." in path.parts
        or not _outside_repository(path)
    ):
        _reject("austin_authority_preflight_output")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(path.anchor, flags)
    except OSError:
        _reject("austin_authority_preflight_output")
    successful = False
    try:
        for component in path.parts[1:]:
            try:
                child_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=directory_descriptor,
                )
            except OSError:
                _reject("austin_authority_preflight_output")
            information = os.fstat(child_descriptor)
            if not stat.S_ISDIR(information.st_mode):
                os.close(child_descriptor)
                _reject("austin_authority_preflight_output")
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor
        successful = True
        return directory_descriptor
    finally:
        if not successful:
            os.close(directory_descriptor)


def _read_public_key(
    path: Path,
    *,
    expected_digest: object,
    expected_owner_uid: int,
) -> bytes:
    if (
        type(expected_digest) is not str
        or _DIGEST_RE.fullmatch(expected_digest) is None
        or type(expected_owner_uid) is not int
        or expected_owner_uid < 0
    ):
        _reject("austin_authority_preflight_key_digest")
    descriptor = _open_descriptor_relative(
        path,
        reason="austin_authority_preflight_key_missing",
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_owner_uid
            or before.st_mode & 0o022
            or not 1 <= before.st_size <= MAX_PUBLIC_KEY_BYTES
        ):
            _reject("austin_authority_preflight_key_security")
        payload = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                _reject("austin_authority_preflight_key_read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_authority_preflight_key_read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _reject("austin_authority_preflight_key_changed")
    finally:
        os.close(descriptor)

    public_payload = bytes(payload)
    if b"PRIVATE KEY" in public_payload:
        _reject("austin_authority_preflight_private_material")
    try:
        loaded = serialization.load_pem_public_key(public_payload)
    except (TypeError, ValueError):
        _reject("austin_authority_preflight_key_encoding")
    if not isinstance(loaded, Ed25519PublicKey):
        _reject("austin_authority_preflight_key_type")
    canonical = loaded.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not hmac.compare_digest(canonical, public_payload):
        _reject("austin_authority_preflight_key_encoding")
    raw = loaded.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if not hmac.compare_digest(_digest(raw), expected_digest):
        _reject("austin_authority_preflight_key_digest")
    return raw


def _authority_record(*, key_id: object, public_key: bytes) -> dict[str, str]:
    if type(key_id) is not str or _KEY_ID_RE.fullmatch(key_id) is None:
        _reject("austin_authority_preflight_key_id")
    encoded = base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("=")
    return {
        "key_id": key_id,
        "public_key_base64url": encoded,
        "public_key_sha256": _digest(public_key),
    }


def _manifest_payload(
    *,
    repository: object,
    github_controller_key_id: object,
    github_controller_public_key: bytes,
    log_sink_key_id: object,
    log_sink_public_key: bytes,
    host_provider_key_id: object,
    host_provider_public_key: bytes,
) -> bytes:
    if type(repository) is not str or _REPOSITORY_RE.fullmatch(repository) is None:
        _reject("austin_authority_preflight_repository")
    records = {
        "github_controller": _authority_record(
            key_id=github_controller_key_id,
            public_key=github_controller_public_key,
        ),
        "host_provider": _authority_record(
            key_id=host_provider_key_id,
            public_key=host_provider_public_key,
        ),
        "log_sink": _authority_record(
            key_id=log_sink_key_id,
            public_key=log_sink_public_key,
        ),
    }
    key_ids = {record["key_id"] for record in records.values()}
    public_keys = {
        record["public_key_base64url"]
        for record in records.values()
    }
    if len(key_ids) != 3 or len(public_keys) != 3:
        _reject("austin_authority_preflight_independence")
    return _canonical_json(
        {
            "authorities": records,
            "job_name": EXPECTED_JOB_NAME,
            "ref": EXPECTED_REF,
            "repository": repository,
            "repository_id": EXPECTED_REPOSITORY_ID,
            "runner_group": EXPECTED_RUNNER_GROUP,
            "runner_labels": list(EXPECTED_RUNNER_LABELS),
            "schema_version": 2,
            "status": "configured",
            "workflow": EXPECTED_WORKFLOW,
            "workflow_name": EXPECTED_WORKFLOW_NAME,
        }
    )


def _assert_offline_operator(
    *,
    environment: Mapping[str, str],
    effective_uid: int,
    expected_owner_uid: int,
) -> None:
    if type(environment) is not dict or any(
        type(key) is not str or type(value) is not str
        for key, value in environment.items()
    ):
        _reject("austin_authority_preflight_environment")
    if "GITHUB_ACTIONS" in environment:
        _reject("austin_authority_preflight_online")
    if (
        type(effective_uid) is not int
        or type(expected_owner_uid) is not int
        or effective_uid < 0
        or expected_owner_uid < 0
        or effective_uid != expected_owner_uid
    ):
        _reject("austin_authority_preflight_owner")


def _open_secure_output_parent(*, output: Path, expected_owner_uid: int) -> int:
    if (
        not isinstance(output, Path)
        or not output.is_absolute()
        or output.name != OUTPUT_FILE_NAME
        or ".." in output.parts
        or not _outside_repository(output)
    ):
        _reject("austin_authority_preflight_output")
    parent = output.parent
    descriptor = _open_directory_descriptor_relative(parent)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != expected_owner_uid
        or opened.st_mode & 0o022
    ):
        os.close(descriptor)
        _reject("austin_authority_preflight_output_security")
    return descriptor


def _publish_no_overwrite(
    *,
    directory_descriptor: int,
    payload: bytes,
    expected_owner_uid: int,
) -> None:
    temporary_name = f".{OUTPUT_FILE_NAME}.{secrets.token_hex(16)}.tmp"
    if re.fullmatch(
        r"\.AdaAustinLifecycleAuthorities\.json\.[0-9a-f]{32}\.tmp",
        temporary_name,
    ) is None:
        _reject("austin_authority_preflight_temporary")
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
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                _reject("austin_authority_preflight_write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        written_information = os.fstat(descriptor)
        identity = (written_information.st_dev, written_information.st_ino)
        if (
            not stat.S_ISREG(written_information.st_mode)
            or written_information.st_nlink != 1
            or written_information.st_uid != expected_owner_uid
            or stat.S_IMODE(written_information.st_mode) != 0o600
            or written_information.st_size != len(payload)
        ):
            _reject("austin_authority_preflight_write")
        try:
            os.link(
                temporary_name,
                OUTPUT_FILE_NAME,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            _reject("austin_authority_preflight_output_exists")
        except OSError:
            _reject("austin_authority_preflight_publish")
        final_created = True
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_created = False
        os.fsync(directory_descriptor)
        final_information = os.stat(
            OUTPUT_FILE_NAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        after_link = os.fstat(descriptor)
        if (
            identity != (final_information.st_dev, final_information.st_ino)
            or identity != (after_link.st_dev, after_link.st_ino)
            or not stat.S_ISREG(final_information.st_mode)
            or final_information.st_nlink != 1
            or final_information.st_uid != expected_owner_uid
            or stat.S_IMODE(final_information.st_mode) != 0o600
            or final_information.st_size != len(payload)
        ):
            _reject("austin_authority_preflight_publish")
        committed = True
    except HenryAustinAuthorityPreflightRejected:
        raise
    except OSError:
        _reject(
            "austin_authority_preflight_publish"
            if final_created
            else "austin_authority_preflight_write"
        )
    finally:
        if not committed and final_created and identity is not None:
            try:
                candidate = os.stat(
                    OUTPUT_FILE_NAME,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if identity == (candidate.st_dev, candidate.st_ino):
                    os.unlink(OUTPUT_FILE_NAME, dir_fd=directory_descriptor)
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


def _prepare_authority_candidate(
    *,
    output: Path,
    repository: object,
    github_controller_key_id: object,
    github_controller_public_key_path: Path,
    github_controller_public_key_sha256: object,
    log_sink_key_id: object,
    log_sink_public_key_path: Path,
    log_sink_public_key_sha256: object,
    host_provider_key_id: object,
    host_provider_public_key_path: Path,
    host_provider_public_key_sha256: object,
    environment: Mapping[str, str],
    effective_uid: int,
    expected_owner_uid: int,
) -> dict[str, Any]:
    _assert_offline_operator(
        environment=environment,
        effective_uid=effective_uid,
        expected_owner_uid=expected_owner_uid,
    )
    github_controller_key = _read_public_key(
        github_controller_public_key_path,
        expected_digest=github_controller_public_key_sha256,
        expected_owner_uid=expected_owner_uid,
    )
    log_sink_key = _read_public_key(
        log_sink_public_key_path,
        expected_digest=log_sink_public_key_sha256,
        expected_owner_uid=expected_owner_uid,
    )
    host_provider_key = _read_public_key(
        host_provider_public_key_path,
        expected_digest=host_provider_public_key_sha256,
        expected_owner_uid=expected_owner_uid,
    )
    payload = _manifest_payload(
        repository=repository,
        github_controller_key_id=github_controller_key_id,
        github_controller_public_key=github_controller_key,
        log_sink_key_id=log_sink_key_id,
        log_sink_public_key=log_sink_key,
        host_provider_key_id=host_provider_key_id,
        host_provider_public_key=host_provider_key,
    )
    directory_descriptor = _open_secure_output_parent(
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
    return {
        "activation_eligible": False,
        "authorities_digest": _digest(payload),
        "authority_count": 3,
        "limitations": [
            "This offline preflight accepts canonical Ed25519 public-key PEM files only and never creates, reads, or stores private keys.",
            "The candidate remains outside the repository and is not production authority until independently reviewed, installed, and digest-pinned by the protected external controller.",
        ],
        "public_claim_eligible": False,
        "schema_version": 1,
        "status": "passed",
    }


def prepare_authority_candidate(
    *,
    output: Path,
    repository: str,
    github_controller_key_id: str,
    github_controller_public_key_path: Path,
    github_controller_public_key_sha256: str,
    log_sink_key_id: str,
    log_sink_public_key_path: Path,
    log_sink_public_key_sha256: str,
    host_provider_key_id: str,
    host_provider_public_key_path: Path,
    host_provider_public_key_sha256: str,
) -> dict[str, Any]:
    """Prepare one immutable candidate under the current offline operator."""
    effective_uid = os.geteuid()
    return _prepare_authority_candidate(
        output=output,
        repository=repository,
        github_controller_key_id=github_controller_key_id,
        github_controller_public_key_path=github_controller_public_key_path,
        github_controller_public_key_sha256=github_controller_public_key_sha256,
        log_sink_key_id=log_sink_key_id,
        log_sink_public_key_path=log_sink_public_key_path,
        log_sink_public_key_sha256=log_sink_public_key_sha256,
        host_provider_key_id=host_provider_key_id,
        host_provider_public_key_path=host_provider_public_key_path,
        host_provider_public_key_sha256=host_provider_public_key_sha256,
        environment=dict(os.environ),
        effective_uid=effective_uid,
        expected_owner_uid=effective_uid,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    for authority in ("github-controller", "log-sink", "host-provider"):
        parser.add_argument(f"--{authority}-key-id", required=True)
        parser.add_argument(f"--{authority}-public-key", required=True, type=Path)
        parser.add_argument(f"--{authority}-public-key-sha256", required=True)
    arguments = parser.parse_args(argv)
    try:
        report = prepare_authority_candidate(
            output=arguments.output,
            repository=arguments.repository,
            github_controller_key_id=arguments.github_controller_key_id,
            github_controller_public_key_path=arguments.github_controller_public_key,
            github_controller_public_key_sha256=arguments.github_controller_public_key_sha256,
            log_sink_key_id=arguments.log_sink_key_id,
            log_sink_public_key_path=arguments.log_sink_public_key,
            log_sink_public_key_sha256=arguments.log_sink_public_key_sha256,
            host_provider_key_id=arguments.host_provider_key_id,
            host_provider_public_key_path=arguments.host_provider_public_key,
            host_provider_public_key_sha256=arguments.host_provider_public_key_sha256,
        )
    except HenryAustinAuthorityPreflightRejected as error:
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
