#!/usr/bin/env python3
"""Verify the dedicated Austin signing runner before protected build work."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, Mapping, NoReturn, Protocol


ROOT = Path(__file__).resolve().parents[1]
ATTESTATION_PATH = Path("/Library/Application Support/AlgoCLI/AdaAustinSigningRunner.json")
ATTESTATION_DIGEST_ENV = "AUSTIN_RUNNER_ATTESTATION_SHA256"
EXPECTED_REPOSITORY_ID = 1_297_752_684
EXPECTED_REF = "refs/heads/main"
EXPECTED_WORKFLOW = ".github/workflows/henry-austin-signing-qualification.yml"
EXPECTED_WORKFLOW_NAME = "Austin signed-package qualification"
EXPECTED_JOB = "signed-package"
EXPECTED_LABELS = ("ARM64", "algo-cli-signing-ephemeral", "macOS", "self-hosted")
LOG_FORWARDER_LABEL = "com.algo-cli.austin.runner-log-forwarder"
MAX_ATTESTATION_BYTES = 16 * 1024
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_ATTESTATION_LIFETIME_SECONDS = 24 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RUNNER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class HenryAustinRunnerRejected(RuntimeError):
    """A signing-runner trust invariant failed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "austin_runner_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise HenryAustinRunnerRejected(reason_code)


def _duplicate_rejecting_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _reject("austin_runner_attestation_json")
        result[key] = value
    return result


def _reject_json_number(_value: str) -> NoReturn:
    _reject("austin_runner_attestation_json")


def _assert_no_symlink_ancestors(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        _reject("austin_runner_attestation_path")
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current = current / component
        try:
            information = current.lstat()
        except OSError:
            _reject("austin_runner_attestation_path")
        if stat.S_ISLNK(information.st_mode) or not stat.S_ISDIR(information.st_mode):
            _reject("austin_runner_attestation_path")


def _read_attestation(path: Path, *, expected_owner_uid: int) -> bytes:
    if type(expected_owner_uid) is not int or expected_owner_uid < 0:
        _reject("austin_runner_attestation_owner")
    _assert_no_symlink_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _reject("austin_runner_attestation_missing")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_owner_uid
            or before.st_mode & 0o133
            or not 1 <= before.st_size <= MAX_ATTESTATION_BYTES
        ):
            _reject("austin_runner_attestation_owner")
        remaining = before.st_size
        payload = bytearray()
        while remaining:
            chunk = os.read(descriptor, min(4096, remaining))
            if not chunk:
                _reject("austin_runner_attestation_read")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_runner_attestation_read")
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
            _reject("austin_runner_attestation_changed")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _attestation_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_pairs,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except HenryAustinRunnerRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _reject("austin_runner_attestation_json")
    if type(value) is not dict:
        _reject("austin_runner_attestation_json")
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if payload != canonical.encode("ascii") + b"\n":
        _reject("austin_runner_attestation_canonical")
    return value


def _timestamp(value: object) -> int:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        _reject("austin_runner_attestation_time")
    try:
        parsed = time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _reject("austin_runner_attestation_time")
    return int(calendar_timegm(parsed))


def calendar_timegm(value: time.struct_time) -> int:
    """Small indirection kept injectable in deterministic tests."""
    import calendar

    return calendar.timegm(value)


def _validate_attestation(
    value: Mapping[str, Any],
    *,
    environment: Mapping[str, str],
    now_seconds: int,
) -> dict[str, str]:
    expected_keys = {
        "allowed_ref",
        "allowed_repository_id",
        "allowed_workflow",
        "boot_session_uuid",
        "expires_at",
        "image_digest",
        "log_forwarder_label",
        "log_retention_days",
        "provisioned_at",
        "runner_labels",
        "runner_mode",
        "runner_name",
        "schema_version",
    }
    if set(value) != expected_keys:
        _reject("austin_runner_attestation_schema")
    if (
        value.get("schema_version") != 2
        or value.get("allowed_repository_id") != EXPECTED_REPOSITORY_ID
        or value.get("allowed_ref") != EXPECTED_REF
        or value.get("allowed_workflow") != EXPECTED_WORKFLOW
        or value.get("runner_mode") != "ephemeral"
        or value.get("log_forwarder_label") != LOG_FORWARDER_LABEL
        or type(value.get("log_retention_days")) is not int
        or not 30 <= value["log_retention_days"] <= 365
        or value.get("runner_labels") != list(EXPECTED_LABELS)
    ):
        _reject("austin_runner_attestation_schema")
    image_digest = value.get("image_digest")
    boot_session_uuid = value.get("boot_session_uuid")
    runner_name = value.get("runner_name")
    if type(image_digest) is not str or _DIGEST_RE.fullmatch(image_digest) is None:
        _reject("austin_runner_attestation_image")
    if type(boot_session_uuid) is not str or _UUID_RE.fullmatch(boot_session_uuid) is None:
        _reject("austin_runner_attestation_boot")
    if type(runner_name) is not str or _RUNNER_NAME_RE.fullmatch(runner_name) is None:
        _reject("austin_runner_attestation_runner")
    provisioned_at = _timestamp(value.get("provisioned_at"))
    expires_at = _timestamp(value.get("expires_at"))
    if (
        type(now_seconds) is not int
        or provisioned_at > now_seconds + MAX_CLOCK_SKEW_SECONDS
        or expires_at <= now_seconds
        or expires_at <= provisioned_at
        or expires_at - provisioned_at > MAX_ATTESTATION_LIFETIME_SECONDS
    ):
        _reject("austin_runner_attestation_time")
    expected_environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_JOB": EXPECTED_JOB,
        "GITHUB_REF": EXPECTED_REF,
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_REPOSITORY_ID": str(EXPECTED_REPOSITORY_ID),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_WORKFLOW": EXPECTED_WORKFLOW_NAME,
        "RUNNER_ARCH": "ARM64",
        "RUNNER_ENVIRONMENT": "self-hosted",
        "RUNNER_NAME": runner_name,
        "RUNNER_OS": "macOS",
    }
    if any(environment.get(key) != expected for key, expected in expected_environment.items()):
        _reject("austin_runner_environment")
    repository = environment.get("GITHUB_REPOSITORY")
    if type(repository) is not str or _REPOSITORY_RE.fullmatch(repository) is None:
        _reject("austin_runner_environment")
    if environment.get("GITHUB_WORKFLOW_REF") != f"{repository}/{EXPECTED_WORKFLOW}@{EXPECTED_REF}":
        _reject("austin_runner_environment")
    source_commit = environment.get("GITHUB_SHA", "")
    if _SHA_RE.fullmatch(source_commit) is None or environment.get("GITHUB_WORKFLOW_SHA") != source_commit:
        _reject("austin_runner_environment")
    return {
        "boot_session_uuid": boot_session_uuid,
        "image_digest": image_digest,
        "source_commit": source_commit,
    }


class HenryAustinCommandRunner(Protocol):
    def run(self, command: tuple[str, ...], *, timeout_seconds: float) -> bytes: ...


class HenryAustinSubprocessRunner:
    """Run fixed absolute host probes without a shell or inherited credentials."""

    def run(self, command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        if (
            type(command) is not tuple
            or not command
            or any(type(part) is not str or "\x00" in part for part in command)
            or not 0 < timeout_seconds <= 30
        ):
            _reject("austin_runner_command")
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env={
                    "HOME": str(Path.home()),
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
            _reject("austin_runner_command")
        if (
            completed.returncode != 0
            or len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES
            or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES
        ):
            _reject("austin_runner_command")
        return completed.stdout


def verify_runner(
    *,
    attestation_path: Path,
    expected_digest: str,
    environment: Mapping[str, str],
    expected_workspace: Path,
    expected_owner_uid: int,
    runner: HenryAustinCommandRunner,
    now_seconds: int,
) -> dict[str, Any]:
    if type(expected_digest) is not str or _DIGEST_RE.fullmatch(expected_digest) is None:
        _reject("austin_runner_attestation_digest")
    payload = _read_attestation(
        attestation_path,
        expected_owner_uid=expected_owner_uid,
    )
    observed_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(observed_digest, expected_digest):
        _reject("austin_runner_attestation_digest")
    bound = _validate_attestation(
        _attestation_object(payload),
        environment=environment,
        now_seconds=now_seconds,
    )
    workspace_value = environment.get("GITHUB_WORKSPACE", "")
    try:
        workspace = Path(workspace_value)
        _assert_no_symlink_ancestors(workspace / ".HenryAustinWorkspaceBoundary")
        if (
            not workspace.is_absolute()
            or workspace.resolve(strict=True) != expected_workspace.resolve(strict=True)
            or workspace.is_symlink()
        ):
            _reject("austin_runner_workspace")
    except OSError:
        _reject("austin_runner_workspace")
    try:
        head = (
            runner.run(
                ("/usr/bin/git", "rev-parse", "--verify", "HEAD"),
                timeout_seconds=10,
            )
            .decode("ascii", errors="strict")
            .strip()
        )
    except UnicodeDecodeError:
        _reject("austin_runner_command")
    if head != bound["source_commit"]:
        _reject("austin_runner_source")
    if runner.run(
        ("/usr/bin/git", "status", "--porcelain=v1", "--untracked-files=all"),
        timeout_seconds=10,
    ):
        _reject("austin_runner_source_dirty")
    if runner.run(
        ("/usr/bin/git", "submodule", "status", "--recursive"),
        timeout_seconds=10,
    ):
        _reject("austin_runner_submodule")
    try:
        boot_session = (
            runner.run(
                ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"),
                timeout_seconds=10,
            )
            .decode("ascii", errors="strict")
            .strip()
            .casefold()
        )
    except UnicodeDecodeError:
        _reject("austin_runner_command")
    if boot_session != bound["boot_session_uuid"]:
        _reject("austin_runner_attestation_boot")
    log_forwarder = runner.run(
        ("/bin/launchctl", "print", f"system/{LOG_FORWARDER_LABEL}"),
        timeout_seconds=10,
    )
    if (
        re.search(rb"(?m)^\s*state = running\s*$", log_forwarder) is None
        or re.search(rb"(?m)^\s*pid = [1-9][0-9]*\s*$", log_forwarder) is None
    ):
        _reject("austin_runner_log_forwarder")
    return {
        "attestation_digest": observed_digest,
        "image_digest": bound["image_digest"],
        "limitations": (
            "This preflight validates a root-owned, digest-pinned runner manifest, "
            "current boot binding, clean exact checkout, and a running system log "
            "forwarder. It does not prove certificate custody, notarization, artifact "
            "correctness, runner destruction, or external log receipt."
        ),
        "public_claim_eligible": False,
        "schema_version": 1,
        "source_commit": bound["source_commit"],
        "status": "passed",
    }


def main() -> int:
    try:
        report = verify_runner(
            attestation_path=ATTESTATION_PATH,
            expected_digest=os.environ.get(ATTESTATION_DIGEST_ENV, ""),
            environment=os.environ,
            expected_workspace=ROOT,
            expected_owner_uid=0,
            runner=HenryAustinSubprocessRunner(),
            now_seconds=int(time.time()),
        )
    except HenryAustinRunnerRejected as error:
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
