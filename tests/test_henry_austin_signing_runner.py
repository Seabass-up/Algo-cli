from __future__ import annotations

import calendar
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "henry_austin_signing_runner.py"
SPEC = importlib.util.spec_from_file_location(
    "henry_austin_signing_runner_script",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)

NOW = calendar.timegm(time.strptime("2026-07-20T09:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
SOURCE_COMMIT = "a" * 40
BOOT_SESSION = "00000000-1111-4222-8333-444444444444"
REPOSITORY = "Algo-CLI-Org/Algo-cli"


def _attestation() -> dict[str, object]:
    return {
        "allowed_ref": SCRIPT.EXPECTED_REF,
        "allowed_repository_id": SCRIPT.EXPECTED_REPOSITORY_ID,
        "allowed_workflow": SCRIPT.EXPECTED_WORKFLOW,
        "boot_session_uuid": BOOT_SESSION,
        "expires_at": "2026-07-20T10:00:00Z",
        "image_digest": "sha256:" + "b" * 64,
        "log_forwarder_label": SCRIPT.LOG_FORWARDER_LABEL,
        "log_retention_days": 30,
        "provisioned_at": "2026-07-20T08:30:00Z",
        "runner_labels": list(SCRIPT.EXPECTED_LABELS),
        "runner_mode": "ephemeral",
        "runner_name": "Austin-Ephemeral-1",
        "schema_version": 2,
    }


def _payload(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"


def _write(path: Path, value: dict[str, object]) -> str:
    payload = _payload(value)
    path.write_bytes(payload)
    path.chmod(0o644)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_JOB": SCRIPT.EXPECTED_JOB,
        "GITHUB_REF": SCRIPT.EXPECTED_REF,
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(SCRIPT.EXPECTED_REPOSITORY_ID),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_SHA": SOURCE_COMMIT,
        "GITHUB_WORKFLOW": SCRIPT.EXPECTED_WORKFLOW_NAME,
        "GITHUB_WORKFLOW_REF": (f"{REPOSITORY}/{SCRIPT.EXPECTED_WORKFLOW}@{SCRIPT.EXPECTED_REF}"),
        "GITHUB_WORKFLOW_SHA": SOURCE_COMMIT,
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_ARCH": "ARM64",
        "RUNNER_ENVIRONMENT": "self-hosted",
        "RUNNER_NAME": "Austin-Ephemeral-1",
        "RUNNER_OS": "macOS",
    }


class FakeRunner:
    def __init__(self) -> None:
        self.outputs = {
            ("/usr/bin/git", "rev-parse", "--verify", "HEAD"): (SOURCE_COMMIT.encode("ascii") + b"\n"),
            (
                "/usr/bin/git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ): b"",
            ("/usr/bin/git", "submodule", "status", "--recursive"): b"",
            ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"): (BOOT_SESSION.encode("ascii") + b"\n"),
            (
                "/bin/launchctl",
                "print",
                f"system/{SCRIPT.LOG_FORWARDER_LABEL}",
            ): b"state = running\npid = 123\n",
        }
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: tuple[str, ...], *, timeout_seconds: float) -> bytes:
        assert 0 < timeout_seconds <= 10
        self.commands.append(command)
        return self.outputs[command]


def _verify(
    tmp_path: Path,
    *,
    value: dict[str, object] | None = None,
    environment: dict[str, str] | None = None,
    runner: FakeRunner | None = None,
    now_seconds: int = NOW,
) -> dict[str, object]:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(exist_ok=True)
    attestation = (tmp_path / "AdaAustinSigningRunner.json").resolve()
    digest = _write(attestation, value or _attestation())
    return SCRIPT.verify_runner(
        attestation_path=attestation,
        expected_digest=digest,
        environment=environment or _environment(workspace),
        expected_workspace=workspace,
        expected_owner_uid=os.getuid(),
        runner=runner or FakeRunner(),
        now_seconds=now_seconds,
    )


def test_exact_root_equivalent_attestation_and_host_probes_pass(tmp_path: Path) -> None:
    report = _verify(tmp_path)

    assert report == {
        "attestation_digest": report["attestation_digest"],
        "image_digest": "sha256:" + "b" * 64,
        "limitations": report["limitations"],
        "public_claim_eligible": False,
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "status": "passed",
    }
    assert str(report["attestation_digest"]).startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("allowed_repository_id", 1, "austin_runner_attestation_schema"),
        ("schema_version", 1, "austin_runner_attestation_schema"),
        ("allowed_ref", "refs/heads/feature", "austin_runner_attestation_schema"),
        ("runner_mode", "persistent", "austin_runner_attestation_schema"),
        ("runner_labels", ["self-hosted"], "austin_runner_attestation_schema"),
        ("image_digest", "sha256:short", "austin_runner_attestation_image"),
        ("boot_session_uuid", "not-a-uuid", "austin_runner_attestation_boot"),
        ("log_retention_days", 7, "austin_runner_attestation_schema"),
        ("expires_at", "2026-07-22T10:00:00Z", "austin_runner_attestation_time"),
    ],
)
def test_attestation_authority_and_freshness_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    attestation = _attestation()
    attestation[field] = value

    with pytest.raises(SCRIPT.HenryAustinRunnerRejected, match=reason):
        _verify(tmp_path, value=attestation)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REF_PROTECTED", "false"),
        ("GITHUB_REPOSITORY_ID", "1"),
        ("GITHUB_REPOSITORY", "other/repo"),
        ("GITHUB_WORKFLOW_SHA", "b" * 40),
        ("RUNNER_ARCH", "X64"),
        ("RUNNER_ENVIRONMENT", "github-hosted"),
        ("RUNNER_NAME", "substitute"),
    ],
)
def test_job_environment_must_match_attested_ephemeral_runner(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    environment = _environment(workspace)
    environment[field] = value

    with pytest.raises(
        SCRIPT.HenryAustinRunnerRejected,
        match="austin_runner_environment",
    ):
        _verify(tmp_path, environment=environment)


def test_repository_transfer_name_is_allowed_only_with_the_pinned_repository_id(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    environment = _environment(workspace)
    environment["GITHUB_REPOSITORY"] = "New-Owner/New-Name"
    environment["GITHUB_WORKFLOW_REF"] = (
        f"New-Owner/New-Name/{SCRIPT.EXPECTED_WORKFLOW}@{SCRIPT.EXPECTED_REF}"
    )

    report = _verify(tmp_path, environment=environment)

    assert report["status"] == "passed"
    assert report["public_claim_eligible"] is False


@pytest.mark.parametrize(
    ("command", "output", "reason"),
    [
        (
            ("/usr/bin/git", "rev-parse", "--verify", "HEAD"),
            b"b" * 40 + b"\n",
            "austin_runner_source",
        ),
        (
            (
                "/usr/bin/git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            b" M source.py\n",
            "austin_runner_source_dirty",
        ),
        (
            ("/usr/bin/git", "submodule", "status", "--recursive"),
            b" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa dependency\n",
            "austin_runner_submodule",
        ),
        (
            ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"),
            b"ffffffff-ffff-4fff-8fff-ffffffffffff\n",
            "austin_runner_attestation_boot",
        ),
        (
            (
                "/bin/launchctl",
                "print",
                f"system/{SCRIPT.LOG_FORWARDER_LABEL}",
            ),
            b"state = exited\n",
            "austin_runner_log_forwarder",
        ),
    ],
)
def test_checkout_boot_and_log_forwarder_faults_block(
    tmp_path: Path,
    command: tuple[str, ...],
    output: bytes,
    reason: str,
) -> None:
    runner = FakeRunner()
    runner.outputs[command] = output

    with pytest.raises(SCRIPT.HenryAustinRunnerRejected, match=reason):
        _verify(tmp_path, runner=runner)


def test_attestation_digest_permissions_and_canonical_json_are_enforced(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    attestation = (tmp_path / "AdaAustinSigningRunner.json").resolve()
    digest = _write(attestation, _attestation())
    arguments = {
        "attestation_path": attestation,
        "environment": _environment(workspace),
        "expected_workspace": workspace,
        "expected_owner_uid": os.getuid(),
        "runner": FakeRunner(),
        "now_seconds": NOW,
    }

    with pytest.raises(
        SCRIPT.HenryAustinRunnerRejected,
        match="austin_runner_attestation_digest",
    ):
        SCRIPT.verify_runner(expected_digest="sha256:" + "0" * 64, **arguments)

    attestation.chmod(0o664)
    with pytest.raises(
        SCRIPT.HenryAustinRunnerRejected,
        match="austin_runner_attestation_owner",
    ):
        SCRIPT.verify_runner(expected_digest=digest, **arguments)

    malformed = b'{"schema_version":1,"schema_version":1}\n'
    attestation.write_bytes(malformed)
    attestation.chmod(0o644)
    with pytest.raises(
        SCRIPT.HenryAustinRunnerRejected,
        match="austin_runner_attestation_json",
    ):
        SCRIPT.verify_runner(
            expected_digest="sha256:" + hashlib.sha256(malformed).hexdigest(),
            **arguments,
        )


def test_attestation_symlink_is_never_followed(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    target = tmp_path / "target.json"
    digest = _write(target, _attestation())
    attestation = tmp_path / "AdaAustinSigningRunner.json"
    attestation.symlink_to(target)

    with pytest.raises(
        SCRIPT.HenryAustinRunnerRejected,
        match="austin_runner_attestation_missing",
    ):
        SCRIPT.verify_runner(
            attestation_path=attestation,
            expected_digest=digest,
            environment=_environment(workspace),
            expected_workspace=workspace,
            expected_owner_uid=os.getuid(),
            runner=FakeRunner(),
            now_seconds=NOW,
        )
