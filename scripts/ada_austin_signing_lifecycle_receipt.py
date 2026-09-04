#!/usr/bin/env python3
"""Verify independent post-job receipts for the ephemeral Austin signer."""

from __future__ import annotations

import argparse
import base64
import calendar
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Any, Mapping, NoReturn
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUTHORITIES = ROOT / "hardening" / "ada-signing-lifecycle-authorities.json"
AUTHORITIES_DIGEST_ENV = "ADA_AUSTIN_LIFECYCLE_AUTHORITIES_SHA256"
BINDING_DIGEST_ENV = "ADA_AUSTIN_LIFECYCLE_BINDING_SHA256"
EXPECTED_REPOSITORY_ID = 1_297_752_684
EXPECTED_REF = "refs/heads/main"
EXPECTED_WORKFLOW = ".github/workflows/henry-austin-signing-qualification.yml"
EXPECTED_WORKFLOW_NAME = "Austin signed-package qualification"
EXPECTED_JOB_NAME = "Developer ID, notarization, and Gatekeeper"
EXPECTED_RUNNER_GROUP = "algo-cli-signing"
EXPECTED_RUNNER_LABELS = ("ARM64", "algo-cli-signing-ephemeral", "macOS", "self-hosted")
EXPECTED_API_VERSION = "2026-03-10"

MAX_AUTHORITIES_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 32 * 1024
MAX_JOB_SECONDS = 95 * 60
MAX_LIFECYCLE_SECONDS = 6 * 60 * 60
MAX_RECEIPT_AGE_SECONDS = 24 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60
MIN_LOG_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_LOG_RETENTION_SECONDS = 365 * 24 * 60 * 60
MAX_COUNTER = 1_000_000
MAX_IDENTIFIER = (1 << 63) - 1

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RUNNER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
_KEY_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_DOMAINS = {
    "github_job": b"algo-cli:austin-lifecycle:github-job:v2\0",
    "external_log": b"algo-cli:austin-lifecycle:external-log:v2\0",
    "host_destroyed": b"algo-cli:austin-lifecycle:host-destroyed:v2\0",
}
_AUTHORITY_FOR_KIND = {
    "github_job": "github_controller",
    "external_log": "log_sink",
    "host_destroyed": "host_provider",
}


class AdaAustinLifecycleRejected(RuntimeError):
    """A post-job lifecycle evidence invariant failed closed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "austin_lifecycle_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise AdaAustinLifecycleRejected(reason_code)


def _duplicate_rejecting_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _reject("austin_lifecycle_json")
        result[key] = value
    return result


def _reject_json_number(_value: str) -> NoReturn:
    _reject("austin_lifecycle_json")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            .encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        _reject("austin_lifecycle_json")


def _parse_canonical_json(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_pairs,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except AdaAustinLifecycleRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _reject("austin_lifecycle_json")
    if type(value) is not dict or payload != _canonical_json(value):
        _reject("austin_lifecycle_canonical")
    return value


def _open_descriptor_relative(path: Path, *, missing_reason: str) -> int:
    if not path.is_absolute() or ".." in path.parts or not path.name:
        _reject("austin_lifecycle_path")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_descriptor = os.open(path.anchor, directory_flags)
    except OSError:
        _reject("austin_lifecycle_path")
    try:
        for component in path.parts[1:-1]:
            try:
                child_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
            except FileNotFoundError:
                _reject(missing_reason)
            except OSError:
                _reject("austin_lifecycle_path")
            information = os.fstat(child_descriptor)
            if not stat.S_ISDIR(information.st_mode):
                os.close(child_descriptor)
                _reject("austin_lifecycle_path")
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor
        leaf_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(path.name, leaf_flags, dir_fd=directory_descriptor)
        except FileNotFoundError:
            _reject(missing_reason)
        except OSError:
            _reject("austin_lifecycle_path")
    finally:
        os.close(directory_descriptor)


def _read_secure_regular(
    path: Path,
    *,
    maximum_bytes: int,
    expected_owner_uid: int,
    missing_reason: str,
) -> bytes:
    if (
        not isinstance(path, Path)
        or type(maximum_bytes) is not int
        or maximum_bytes < 1
        or type(expected_owner_uid) is not int
        or expected_owner_uid < 0
    ):
        _reject("austin_lifecycle_path")
    descriptor = _open_descriptor_relative(path, missing_reason=missing_reason)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_owner_uid
            or before.st_mode & 0o022
            or not 1 <= before.st_size <= maximum_bytes
        ):
            _reject("austin_lifecycle_file_security")
        remaining = before.st_size
        payload = bytearray()
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                _reject("austin_lifecycle_file_read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_lifecycle_file_read")
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
            _reject("austin_lifecycle_file_changed")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _decode_base64url(value: object, *, size: int, reason: str) -> bytes:
    if (
        type(value) is not str
        or not value
        or _BASE64URL_RE.fullmatch(value) is None
        or "=" in value
    ):
        _reject(reason)
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        _reject(reason)
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != size or not hmac.compare_digest(canonical, value):
        _reject(reason)
    return decoded


def _timestamp(value: object, *, reason: str = "austin_lifecycle_time") -> int:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        _reject(reason)
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (OverflowError, ValueError):
        _reject(reason)


def _generated_at(now_seconds: int) -> str:
    try:
        return datetime.fromtimestamp(now_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        _reject("austin_lifecycle_time")


def _canonical_uuid(value: object, *, version_four: bool, reason: str) -> str:
    if type(value) is not str or _UUID_RE.fullmatch(value) is None:
        _reject(reason)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        _reject(reason)
    if str(parsed) != value or (version_four and parsed.version != 4):
        _reject(reason)
    return value


def _positive_identifier(value: object, *, reason: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_IDENTIFIER:
        _reject(reason)
    return value


def _authority_config(value: Mapping[str, Any]) -> dict[str, Any]:
    status = value.get("status")
    if status == "unconfigured":
        if set(value) != {"reason_code", "schema_version", "status"} or (
            value.get("schema_version") != 1
            or value.get("reason_code") != "external_lifecycle_authorities_not_provisioned"
        ):
            _reject("austin_lifecycle_authorities_schema")
        return dict(value)

    expected_keys = {
        "authorities",
        "job_name",
        "ref",
        "repository",
        "repository_id",
        "runner_group",
        "runner_labels",
        "schema_version",
        "status",
        "workflow",
        "workflow_name",
    }
    if set(value) != expected_keys or status != "configured" or value.get("schema_version") != 2:
        _reject("austin_lifecycle_authorities_schema")
    if (
        type(value.get("repository")) is not str
        or _REPOSITORY_RE.fullmatch(value["repository"]) is None
        or value.get("repository_id") != EXPECTED_REPOSITORY_ID
        or value.get("ref") != EXPECTED_REF
        or value.get("workflow") != EXPECTED_WORKFLOW
        or value.get("workflow_name") != EXPECTED_WORKFLOW_NAME
        or value.get("job_name") != EXPECTED_JOB_NAME
        or value.get("runner_group") != EXPECTED_RUNNER_GROUP
        or value.get("runner_labels") != list(EXPECTED_RUNNER_LABELS)
    ):
        _reject("austin_lifecycle_authorities_scope")
    authorities = value.get("authorities")
    expected_authorities = {"github_controller", "host_provider", "log_sink"}
    if type(authorities) is not dict or set(authorities) != expected_authorities:
        _reject("austin_lifecycle_authorities_schema")

    seen_key_ids: set[str] = set()
    seen_keys: set[bytes] = set()
    for authority_name in sorted(expected_authorities):
        authority = authorities.get(authority_name)
        if type(authority) is not dict or set(authority) != {
            "key_id",
            "public_key_base64url",
            "public_key_sha256",
        }:
            _reject("austin_lifecycle_authorities_schema")
        key_id = authority.get("key_id")
        if type(key_id) is not str or _KEY_ID_RE.fullmatch(key_id) is None:
            _reject("austin_lifecycle_authority_key_id")
        public_key = _decode_base64url(
            authority.get("public_key_base64url"),
            size=32,
            reason="austin_lifecycle_authority_key",
        )
        public_key_digest = authority.get("public_key_sha256")
        if (
            type(public_key_digest) is not str
            or _DIGEST_RE.fullmatch(public_key_digest) is None
            or not hmac.compare_digest(_digest(public_key), public_key_digest)
        ):
            _reject("austin_lifecycle_authority_digest")
        if key_id in seen_key_ids or public_key in seen_keys:
            _reject("austin_lifecycle_authority_independence")
        seen_key_ids.add(key_id)
        seen_keys.add(public_key)
    return dict(value)


def _binding(value: object, *, config: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "boot_session_uuid",
        "image_digest",
        "job_id",
        "receipt_id",
        "ref",
        "repository",
        "repository_id",
        "run_attempt",
        "run_id",
        "runner_attestation_digest",
        "runner_group",
        "runner_id",
        "runner_labels",
        "runner_name",
        "source_commit",
        "workflow",
    }
    if type(value) is not dict or set(value) != expected_keys:
        _reject("austin_lifecycle_binding_schema")
    if (
        value.get("repository") != config.get("repository")
        or value.get("repository_id") != config.get("repository_id")
        or value.get("ref") != config.get("ref")
        or value.get("workflow") != config.get("workflow")
        or value.get("runner_group") != config.get("runner_group")
        or value.get("runner_labels") != config.get("runner_labels")
    ):
        _reject("austin_lifecycle_binding_scope")
    _canonical_uuid(
        value.get("receipt_id"),
        version_four=True,
        reason="austin_lifecycle_receipt_id",
    )
    _canonical_uuid(
        value.get("boot_session_uuid"),
        version_four=False,
        reason="austin_lifecycle_boot_session",
    )
    if type(value.get("source_commit")) is not str or _SHA_RE.fullmatch(value["source_commit"]) is None:
        _reject("austin_lifecycle_source_commit")
    for digest_name in ("image_digest", "runner_attestation_digest"):
        digest_value = value.get(digest_name)
        if type(digest_value) is not str or _DIGEST_RE.fullmatch(digest_value) is None:
            _reject("austin_lifecycle_binding_digest")
    _positive_identifier(value.get("run_id"), reason="austin_lifecycle_run_id")
    attempt = _positive_identifier(value.get("run_attempt"), reason="austin_lifecycle_run_attempt")
    if attempt > 1_000:
        _reject("austin_lifecycle_run_attempt")
    _positive_identifier(value.get("job_id"), reason="austin_lifecycle_job_id")
    _positive_identifier(value.get("runner_id"), reason="austin_lifecycle_runner_id")
    runner_name = value.get("runner_name")
    if type(runner_name) is not str or _RUNNER_NAME_RE.fullmatch(runner_name) is None:
        _reject("austin_lifecycle_runner_name")
    return dict(value)


def _receipt_shape(
    value: Mapping[str, Any],
    *,
    kind: str,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    if set(value) != {
        "authority_key_id",
        "binding",
        "kind",
        "observation",
        "schema_version",
        "signature",
    } or value.get("schema_version") != 2 or value.get("kind") != kind:
        _reject("austin_lifecycle_receipt_schema")
    binding = _binding(value.get("binding"), config=config)
    observation = value.get("observation")
    if type(observation) is not dict:
        _reject("austin_lifecycle_receipt_schema")
    return binding, observation


def _verify_signature(
    value: Mapping[str, Any],
    *,
    kind: str,
    config: Mapping[str, Any],
) -> None:
    authority_name = _AUTHORITY_FOR_KIND[kind]
    authorities = config.get("authorities")
    if type(authorities) is not dict:
        _reject("austin_lifecycle_authorities_schema")
    authority = authorities.get(authority_name)
    if type(authority) is not dict:
        _reject("austin_lifecycle_authorities_schema")
    key_id = value.get("authority_key_id")
    if type(key_id) is not str or not hmac.compare_digest(key_id, str(authority.get("key_id", ""))):
        _reject("austin_lifecycle_authority_key_id")
    public_key = _decode_base64url(
        authority.get("public_key_base64url"),
        size=32,
        reason="austin_lifecycle_authority_key",
    )
    signature = _decode_base64url(
        value.get("signature"),
        size=64,
        reason="austin_lifecycle_signature_encoding",
    )
    unsigned = dict(value)
    unsigned.pop("signature", None)
    message = _DOMAINS[kind] + _canonical_json(unsigned)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        _reject("austin_lifecycle_signature")


def _github_observation(
    value: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, int]:
    expected_keys = {
        "api_version",
        "artifact_attestation_digest",
        "conclusion",
        "head_branch",
        "head_sha",
        "job_completed_at",
        "job_name",
        "job_started_at",
        "observed_at",
        "package_digest",
        "release_evidence_digest",
        "runner_registration_state",
        "status",
        "workflow_job_action",
        "workflow_job_delivery_id",
        "workflow_name",
    }
    if set(value) != expected_keys:
        _reject("austin_lifecycle_github_schema")
    if (
        value.get("api_version") != EXPECTED_API_VERSION
        or value.get("workflow_name") != config.get("workflow_name")
        or value.get("job_name") != config.get("job_name")
        or value.get("status") != "completed"
        or value.get("conclusion") != "success"
        or value.get("workflow_job_action") != "completed"
        or value.get("runner_registration_state") != "absent"
        or value.get("head_branch") != "main"
        or value.get("head_sha") != binding.get("source_commit")
    ):
        _reject("austin_lifecycle_github_state")
    _canonical_uuid(
        value.get("workflow_job_delivery_id"),
        version_four=False,
        reason="austin_lifecycle_delivery_id",
    )
    for name in (
        "artifact_attestation_digest",
        "package_digest",
        "release_evidence_digest",
    ):
        digest_value = value.get(name)
        if type(digest_value) is not str or _DIGEST_RE.fullmatch(digest_value) is None:
            _reject("austin_lifecycle_artifact_digest")
    return {
        "completed": _timestamp(value.get("job_completed_at")),
        "observed": _timestamp(value.get("observed_at")),
        "started": _timestamp(value.get("job_started_at")),
    }


def _log_observation(value: Mapping[str, Any]) -> dict[str, int]:
    expected_keys = {
        "archive_digest",
        "first_event_at",
        "last_event_at",
        "receipt_issued_at",
        "received_at",
        "retention_until",
        "runner_log_count",
        "worker_log_count",
    }
    if set(value) != expected_keys:
        _reject("austin_lifecycle_log_schema")
    archive_digest = value.get("archive_digest")
    if type(archive_digest) is not str or _DIGEST_RE.fullmatch(archive_digest) is None:
        _reject("austin_lifecycle_log_digest")
    for name in ("runner_log_count", "worker_log_count"):
        count = value.get(name)
        if type(count) is not int or not 1 <= count <= MAX_COUNTER:
            _reject("austin_lifecycle_log_count")
    return {
        "first": _timestamp(value.get("first_event_at")),
        "issued": _timestamp(value.get("receipt_issued_at")),
        "last": _timestamp(value.get("last_event_at")),
        "received": _timestamp(value.get("received_at")),
        "retention": _timestamp(value.get("retention_until")),
    }


def _destroy_observation(value: Mapping[str, Any]) -> dict[str, int]:
    expected_keys = {
        "destroy_operation_digest",
        "destroyed_at",
        "host_state",
        "network_identity_state",
        "provider_instance_digest",
        "receipt_issued_at",
        "storage_state",
    }
    if set(value) != expected_keys:
        _reject("austin_lifecycle_destroy_schema")
    if (
        value.get("host_state") != "destroyed"
        or value.get("storage_state") != "destroyed"
        or value.get("network_identity_state") != "released"
    ):
        _reject("austin_lifecycle_destroy_state")
    for name in ("destroy_operation_digest", "provider_instance_digest"):
        digest_value = value.get(name)
        if type(digest_value) is not str or _DIGEST_RE.fullmatch(digest_value) is None:
            _reject("austin_lifecycle_destroy_digest")
    return {
        "destroyed": _timestamp(value.get("destroyed_at")),
        "issued": _timestamp(value.get("receipt_issued_at")),
    }


def _verify_temporal_contract(
    *,
    github: Mapping[str, int],
    logs: Mapping[str, int],
    destruction: Mapping[str, int],
    now_seconds: int,
) -> None:
    if type(now_seconds) is not int or now_seconds < 0:
        _reject("austin_lifecycle_time")
    started = github["started"]
    completed = github["completed"]
    observed = github["observed"]
    destroyed = destruction["destroyed"]
    destroy_issued = destruction["issued"]
    first_log = logs["first"]
    last_log = logs["last"]
    log_received = logs["received"]
    log_issued = logs["issued"]
    retention_until = logs["retention"]

    if not started < completed or completed - started > MAX_JOB_SECONDS:
        _reject("austin_lifecycle_job_time")
    if (
        not first_log <= last_log
        or first_log > started + MAX_CLOCK_SKEW_SECONDS
        or first_log < started - MAX_RECEIPT_AGE_SECONDS
    ):
        _reject("austin_lifecycle_log_time")
    if last_log < completed - MAX_CLOCK_SKEW_SECONDS:
        _reject("austin_lifecycle_log_incomplete")
    if log_received < max(completed, last_log) - MAX_CLOCK_SKEW_SECONDS or log_issued < log_received:
        _reject("austin_lifecycle_log_time")
    retention = retention_until - log_received
    if not MIN_LOG_RETENTION_SECONDS <= retention <= MAX_LOG_RETENTION_SECONDS:
        _reject("austin_lifecycle_log_retention")
    if destroyed < completed - MAX_CLOCK_SKEW_SECONDS or destroy_issued < destroyed:
        _reject("austin_lifecycle_destroy_time")
    if observed < max(completed, destroyed) - MAX_CLOCK_SKEW_SECONDS:
        _reject("austin_lifecycle_github_observation_time")

    issued_times = (observed, log_issued, destroy_issued)
    if any(value > now_seconds + MAX_CLOCK_SKEW_SECONDS for value in issued_times):
        _reject("austin_lifecycle_future_receipt")
    if any(now_seconds - value > MAX_RECEIPT_AGE_SECONDS for value in issued_times):
        _reject("austin_lifecycle_replay")
    if max(*issued_times, log_received, destroyed) - started > MAX_LIFECYCLE_SECONDS:
        _reject("austin_lifecycle_duration")


def _read_receipt(
    path: Path,
    *,
    kind: str,
    config: Mapping[str, Any],
    expected_owner_uid: int,
) -> tuple[dict[str, Any], bytes, dict[str, Any], Mapping[str, Any]]:
    payload = _read_secure_regular(
        path,
        maximum_bytes=MAX_RECEIPT_BYTES,
        expected_owner_uid=expected_owner_uid,
        missing_reason="austin_lifecycle_receipt_missing",
    )
    value = _parse_canonical_json(payload)
    binding, observation = _receipt_shape(value, kind=kind, config=config)
    _verify_signature(value, kind=kind, config=config)
    return value, payload, binding, observation


def verify_lifecycle_receipts(
    *,
    authorities_path: Path,
    expected_authorities_digest: str,
    expected_binding_digest: str,
    github_receipt_path: Path,
    log_receipt_path: Path,
    destroy_receipt_path: Path,
    expected_owner_uid: int,
    now_seconds: int,
) -> dict[str, Any]:
    authorities_payload = _read_secure_regular(
        authorities_path,
        maximum_bytes=MAX_AUTHORITIES_BYTES,
        expected_owner_uid=expected_owner_uid,
        missing_reason="austin_lifecycle_authorities_missing",
    )
    authorities_digest = _digest(authorities_payload)
    if (
        type(expected_authorities_digest) is not str
        or _DIGEST_RE.fullmatch(expected_authorities_digest) is None
        or not hmac.compare_digest(authorities_digest, expected_authorities_digest)
    ):
        _reject("austin_lifecycle_authorities_digest")
    config = _authority_config(_parse_canonical_json(authorities_payload))
    if config.get("status") != "configured":
        _reject("austin_lifecycle_authorities_unconfigured")

    _, github_payload, github_binding, github_observation = _read_receipt(
        github_receipt_path,
        kind="github_job",
        config=config,
        expected_owner_uid=expected_owner_uid,
    )
    _, log_payload, log_binding, log_observation = _read_receipt(
        log_receipt_path,
        kind="external_log",
        config=config,
        expected_owner_uid=expected_owner_uid,
    )
    _, destroy_payload, destroy_binding, destroy_observation = _read_receipt(
        destroy_receipt_path,
        kind="host_destroyed",
        config=config,
        expected_owner_uid=expected_owner_uid,
    )
    if github_binding != log_binding or github_binding != destroy_binding:
        _reject("austin_lifecycle_mixed_binding")
    binding_digest = _digest(_canonical_json(github_binding))
    if (
        type(expected_binding_digest) is not str
        or _DIGEST_RE.fullmatch(expected_binding_digest) is None
        or not hmac.compare_digest(binding_digest, expected_binding_digest)
    ):
        _reject("austin_lifecycle_binding_digest")

    github_times = _github_observation(
        github_observation,
        binding=github_binding,
        config=config,
    )
    log_times = _log_observation(log_observation)
    destroy_times = _destroy_observation(destroy_observation)
    _verify_temporal_contract(
        github=github_times,
        logs=log_times,
        destruction=destroy_times,
        now_seconds=now_seconds,
    )

    payload_digests = (
        hashlib.sha256(github_payload).digest(),
        hashlib.sha256(log_payload).digest(),
        hashlib.sha256(destroy_payload).digest(),
    )
    receipt_set_digest = "sha256:" + hashlib.sha256(
        b"algo-cli:austin-lifecycle:receipt-set:v1\0" + b"".join(payload_digests)
    ).hexdigest()
    return {
        "authorities_digest": authorities_digest,
        "binding_digest": binding_digest,
        "destruction_observation_digest": _digest(destroy_payload),
        "external_lifecycle_eligible": True,
        "github_observation_digest": _digest(github_payload),
        "limitations": [
            "The verifier proves only this signed post-job receipt set; it does not provision a runner, inspect a provider account, or publish a release.",
            "Each authority must remain outside the signing host and the authorities digest must be pinned by the protected external controller.",
        ],
        "log_observation_digest": _digest(log_payload),
        "public_claim_eligible": False,
        "reason_code": "",
        "receipt_set_digest": receipt_set_digest,
        "schema_version": 1,
        "status": "passed",
    }


def _status_report(
    *,
    status: str,
    reason_code: str,
    generated_at: str,
    authorities_digest: str = "",
) -> dict[str, Any]:
    if status == "blocked":
        limitations = [
            "No lifecycle claim is made until three externally produced receipts and a protected authorities digest are present.",
            "GitHub runner absence is not host-destruction evidence; provider destruction and external log delivery remain separate requirements.",
        ]
    else:
        limitations = [
            "Lifecycle evidence was rejected; no signing, notarization, teardown, or release claim is eligible.",
        ]
    return {
        "authorities_digest": authorities_digest,
        "binding_digest": "",
        "external_lifecycle_eligible": False,
        "generated_at": generated_at,
        "limitations": limitations,
        "public_claim_eligible": False,
        "reason_code": reason_code,
        "schema_version": 1,
        "status": status,
    }


def evaluate_lifecycle(
    *,
    authorities_path: Path,
    expected_authorities_digest: str | None,
    expected_binding_digest: str | None,
    github_receipt_path: Path | None,
    log_receipt_path: Path | None,
    destroy_receipt_path: Path | None,
    expected_owner_uid: int,
    now_seconds: int,
) -> dict[str, Any]:
    generated_at = _generated_at(now_seconds)
    authorities_digest = ""
    try:
        authorities_payload = _read_secure_regular(
            authorities_path,
            maximum_bytes=MAX_AUTHORITIES_BYTES,
            expected_owner_uid=expected_owner_uid,
            missing_reason="austin_lifecycle_authorities_missing",
        )
        authorities_digest = _digest(authorities_payload)
        config = _authority_config(_parse_canonical_json(authorities_payload))
        if config.get("status") == "unconfigured":
            return _status_report(
                status="blocked",
                reason_code="austin_lifecycle_authorities_unconfigured",
                generated_at=generated_at,
                authorities_digest=authorities_digest,
            )
        if expected_authorities_digest is None or not expected_authorities_digest:
            return _status_report(
                status="blocked",
                reason_code="austin_lifecycle_authorities_digest_unpinned",
                generated_at=generated_at,
                authorities_digest=authorities_digest,
            )
        if github_receipt_path is None or log_receipt_path is None or destroy_receipt_path is None:
            return _status_report(
                status="blocked",
                reason_code="austin_lifecycle_receipts_missing",
                generated_at=generated_at,
                authorities_digest=authorities_digest,
            )
        if expected_binding_digest is None or not expected_binding_digest:
            return _status_report(
                status="blocked",
                reason_code="austin_lifecycle_binding_digest_unpinned",
                generated_at=generated_at,
                authorities_digest=authorities_digest,
            )
        result = verify_lifecycle_receipts(
            authorities_path=authorities_path,
            expected_authorities_digest=expected_authorities_digest,
            expected_binding_digest=expected_binding_digest,
            github_receipt_path=github_receipt_path,
            log_receipt_path=log_receipt_path,
            destroy_receipt_path=destroy_receipt_path,
            expected_owner_uid=expected_owner_uid,
            now_seconds=now_seconds,
        )
    except AdaAustinLifecycleRejected as error:
        return _status_report(
            status=(
                "blocked"
                if error.reason_code == "austin_lifecycle_receipt_missing"
                else "failed"
            ),
            reason_code=error.reason_code,
            generated_at=generated_at,
            authorities_digest=authorities_digest,
        )
    result["generated_at"] = generated_at
    return result


def _bounded_output(path: Path) -> Path:
    candidate = (path if path.is_absolute() else ROOT / path).resolve(strict=False)
    evidence_root = (ROOT / "hardening").resolve()
    if (
        not candidate.is_relative_to(evidence_root)
        or candidate.name != "ada-signing-lifecycle-evidence.json"
    ):
        _reject("austin_lifecycle_output_scope")
    return candidate


def _absolute_without_symlink_resolution(path: Path) -> Path:
    if not isinstance(path, Path):
        _reject("austin_lifecycle_path")
    return Path(os.path.abspath(os.fspath(path)))


def _write_no_overwrite(path: Path, payload: bytes) -> None:
    parent = path.parent.resolve()
    if path.exists() or path.is_symlink():
        _reject("austin_lifecycle_output_exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            _reject("austin_lifecycle_output_exists")
        directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorities", type=Path, default=DEFAULT_AUTHORITIES)
    parser.add_argument("--github-receipt", type=Path)
    parser.add_argument("--log-receipt", type=Path)
    parser.add_argument("--destroy-receipt", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)

    report = evaluate_lifecycle(
        authorities_path=_absolute_without_symlink_resolution(arguments.authorities),
        expected_authorities_digest=os.environ.get(AUTHORITIES_DIGEST_ENV),
        expected_binding_digest=os.environ.get(BINDING_DIGEST_ENV),
        github_receipt_path=(
            _absolute_without_symlink_resolution(arguments.github_receipt)
            if arguments.github_receipt is not None
            else None
        ),
        log_receipt_path=(
            _absolute_without_symlink_resolution(arguments.log_receipt)
            if arguments.log_receipt is not None
            else None
        ),
        destroy_receipt_path=(
            _absolute_without_symlink_resolution(arguments.destroy_receipt)
            if arguments.destroy_receipt is not None
            else None
        ),
        expected_owner_uid=os.getuid(),
        now_seconds=int(time.time()),
    )
    payload = (json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    if arguments.output is not None:
        _write_no_overwrite(_bounded_output(arguments.output), payload)
    sys.stdout.buffer.write(payload)
    if report["status"] == "passed":
        return 0
    return 2 if report["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
