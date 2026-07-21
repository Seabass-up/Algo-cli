from __future__ import annotations

import calendar
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(module_name: str, file_name: str):
    path = ROOT / "scripts" / file_name
    specification = importlib.util.spec_from_file_location(module_name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


SCRIPT = _load_script(
    "henry_austin_signing_provisioner_script",
    "henry_austin_signing_provisioner.py",
)
RUNNER_SCRIPT = _load_script(
    "henry_austin_signing_runner_compatibility_script",
    "henry_austin_signing_runner.py",
)
LIFECYCLE_SCRIPT = _load_script(
    "ada_austin_signing_lifecycle_compatibility_script",
    "ada_austin_signing_lifecycle_receipt.py",
)
READINESS_SCRIPT = _load_script(
    "henry_github_hardening_readiness_compatibility_script",
    "henry_github_hardening_readiness.py",
)

NOW = calendar.timegm(time.strptime("2026-07-20T10:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
BOOT_SESSION = "00000000-1111-4222-8333-444444444444"
IMAGE_DIGEST = "sha256:" + "b" * 64
SOURCE_COMMIT = "a" * 40
REPOSITORY = "Algo-CLI-Org/Algo-cli"


class FakeRunner:
    def __init__(self) -> None:
        self.outputs = {
            ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"): (BOOT_SESSION.upper().encode("ascii") + b"\n"),
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


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    parent = (tmp_path / "Application Support").resolve()
    parent.mkdir(mode=0o755, parents=True)
    parent.chmod(0o755)
    output = parent / SCRIPT.ATTESTATION_DIRECTORY_NAME / SCRIPT.ATTESTATION_FILE_NAME
    return parent, output


def _arguments(tmp_path: Path) -> dict[str, Any]:
    parent, output = _paths(tmp_path)
    return {
        "effective_uid": os.geteuid(),
        "environment": {},
        "expected_owner_uid": os.geteuid(),
        "image_digest": IMAGE_DIGEST,
        "lifetime_seconds": 60 * 60,
        "log_retention_days": 30,
        "machine_name": "arm64",
        "now_seconds": NOW,
        "output": output,
        "parent": parent,
        "platform_name": "darwin",
        "runner": FakeRunner(),
        "runner_name": "Austin-Ephemeral-1",
    }


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_API_URL": "https://api.github.com",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_JOB": RUNNER_SCRIPT.EXPECTED_JOB,
        "GITHUB_REF": SCRIPT.EXPECTED_REF,
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REPOSITORY_ID": str(SCRIPT.EXPECTED_REPOSITORY_ID),
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_SHA": SOURCE_COMMIT,
        "GITHUB_WORKFLOW": RUNNER_SCRIPT.EXPECTED_WORKFLOW_NAME,
        "GITHUB_WORKFLOW_REF": (f"{REPOSITORY}/{SCRIPT.EXPECTED_WORKFLOW}@{SCRIPT.EXPECTED_REF}"),
        "GITHUB_WORKFLOW_SHA": SOURCE_COMMIT,
        "RUNNER_ARCH": "ARM64",
        "RUNNER_ENVIRONMENT": "self-hosted",
        "RUNNER_NAME": "Austin-Ephemeral-1",
        "RUNNER_OS": "macOS",
    }


def test_provisioned_manifest_is_canonical_content_free_and_runner_compatible(
    tmp_path: Path,
) -> None:
    arguments = _arguments(tmp_path)
    output = arguments["output"]

    report = SCRIPT._provision_runner_manifest(**arguments)

    payload = output.read_bytes()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert report == {
        "attestation_digest": digest,
        "limitations": report["limitations"],
        "public_claim_eligible": False,
        "schema_version": 1,
        "status": "passed",
    }
    encoded_report = json.dumps(report, sort_keys=True)
    assert "Austin-Ephemeral-1" not in encoded_report
    assert IMAGE_DIGEST not in encoded_report
    assert BOOT_SESSION not in encoded_report
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert output.stat().st_nlink == 1
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o755
    value = RUNNER_SCRIPT._attestation_object(payload)
    workspace = tmp_path.resolve()
    bound = RUNNER_SCRIPT._validate_attestation(
        value,
        environment=_environment(workspace),
        now_seconds=NOW,
    )
    assert bound == {
        "boot_session_uuid": BOOT_SESSION,
        "image_digest": IMAGE_DIGEST,
        "source_commit": SOURCE_COMMIT,
    }


def test_provisioner_and_runner_contract_constants_are_exactly_aligned() -> None:
    for name in (
        "ATTESTATION_PATH",
        "EXPECTED_LABELS",
        "EXPECTED_REF",
        "EXPECTED_REPOSITORY_ID",
        "EXPECTED_WORKFLOW",
        "LOG_FORWARDER_LABEL",
        "MAX_ATTESTATION_BYTES",
        "MAX_ATTESTATION_LIFETIME_SECONDS",
    ):
        assert getattr(SCRIPT, name) == getattr(RUNNER_SCRIPT, name)
    assert {
        SCRIPT.EXPECTED_REPOSITORY_ID,
        RUNNER_SCRIPT.EXPECTED_REPOSITORY_ID,
        LIFECYCLE_SCRIPT.EXPECTED_REPOSITORY_ID,
        READINESS_SCRIPT.EXPECTED_REPOSITORY_ID,
    } == {1_297_752_684}


def test_production_wrapper_cannot_redirect_or_relax_root_boundary(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    expected_report = {"status": "passed"}

    def capture(**arguments):
        captured.update(arguments)
        return expected_report

    monkeypatch.setattr(SCRIPT, "_provision_runner_manifest", capture)
    monkeypatch.setattr(SCRIPT.os, "geteuid", lambda: 501)
    monkeypatch.setattr(SCRIPT.sys, "platform", "darwin")
    monkeypatch.setattr(SCRIPT.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(SCRIPT.time, "time", lambda: NOW)
    monkeypatch.setattr(SCRIPT.os, "environ", {"SAFE": "1"})

    report = SCRIPT.provision_runner_manifest(
        runner_name="Austin-Ephemeral-1",
        image_digest=IMAGE_DIGEST,
        log_retention_days=30,
        lifetime_seconds=3_600,
    )

    assert report == expected_report
    assert captured["output"] == SCRIPT.ATTESTATION_PATH
    assert captured["parent"] == SCRIPT.ATTESTATION_PARENT
    assert captured["effective_uid"] == 501
    assert captured["expected_owner_uid"] == 0
    assert captured["platform_name"] == "darwin"
    assert captured["machine_name"] == "arm64"
    assert captured["environment"] == {"SAFE": "1"}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"environment": {"GITHUB_ACTIONS": "false"}}, "austin_provision_online"),
        ({"effective_uid": -1}, "austin_provision_administrator"),
        ({"effective_uid": True}, "austin_provision_administrator"),
        ({"platform_name": "linux"}, "austin_provision_platform"),
        ({"machine_name": "x86_64"}, "austin_provision_platform"),
        ({"machine_name": None}, "austin_provision_platform"),
    ],
)
def test_only_offline_root_equivalent_apple_silicon_admin_can_provision(
    tmp_path: Path,
    changes: dict[str, object],
    reason: str,
) -> None:
    arguments = _arguments(tmp_path)
    arguments.update(changes)

    with pytest.raises(SCRIPT.HenryAustinProvisioningRejected, match=reason):
        SCRIPT._provision_runner_manifest(**arguments)

    assert not arguments["output"].exists()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("runner_name", "bad runner", "austin_provision_runner"),
        ("image_digest", "sha256:short", "austin_provision_image"),
        ("log_retention_days", 29, "austin_provision_retention"),
        ("log_retention_days", 366, "austin_provision_retention"),
        ("log_retention_days", True, "austin_provision_retention"),
        ("lifetime_seconds", 0, "austin_provision_lifetime"),
        ("lifetime_seconds", 86_401, "austin_provision_lifetime"),
        ("lifetime_seconds", True, "austin_provision_lifetime"),
        ("now_seconds", -1, "austin_provision_time"),
        ("now_seconds", True, "austin_provision_time"),
    ],
)
def test_operator_inputs_fail_closed_before_file_creation(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    arguments = _arguments(tmp_path)
    arguments[field] = value

    with pytest.raises(SCRIPT.HenryAustinProvisioningRejected, match=reason):
        SCRIPT._provision_runner_manifest(**arguments)

    assert not arguments["output"].exists()


@pytest.mark.parametrize(
    ("command", "output", "reason"),
    [
        (
            ("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"),
            b"not-a-uuid\n",
            "austin_provision_boot",
        ),
        (
            (
                "/bin/launchctl",
                "print",
                f"system/{SCRIPT.LOG_FORWARDER_LABEL}",
            ),
            b"state = exited\npid = 123\n",
            "austin_provision_log_forwarder",
        ),
        (
            (
                "/bin/launchctl",
                "print",
                f"system/{SCRIPT.LOG_FORWARDER_LABEL}",
            ),
            b"state = running\n",
            "austin_provision_log_forwarder",
        ),
    ],
)
def test_boot_and_system_log_forwarder_faults_block_without_artifact(
    tmp_path: Path,
    command: tuple[str, ...],
    output: bytes,
    reason: str,
) -> None:
    arguments = _arguments(tmp_path)
    runner = FakeRunner()
    runner.outputs[command] = output
    arguments["runner"] = runner

    with pytest.raises(SCRIPT.HenryAustinProvisioningRejected, match=reason):
        SCRIPT._provision_runner_manifest(**arguments)

    assert not arguments["output"].exists()


def test_insecure_or_symlinked_parent_and_leaf_are_rejected(tmp_path: Path) -> None:
    insecure_arguments = _arguments(tmp_path / "insecure")
    insecure_arguments["parent"].chmod(0o777)
    with pytest.raises(
        SCRIPT.HenryAustinProvisioningRejected,
        match="austin_provision_parent",
    ):
        SCRIPT._provision_runner_manifest(**insecure_arguments)

    real_parent = (tmp_path / "real" / "Application Support").resolve()
    real_parent.mkdir(parents=True, mode=0o755)
    symlink_parent = tmp_path / "linked-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    symlink_arguments = _arguments(tmp_path / "unused")
    symlink_arguments["parent"] = symlink_parent
    symlink_arguments["output"] = (
        symlink_parent / SCRIPT.ATTESTATION_DIRECTORY_NAME / SCRIPT.ATTESTATION_FILE_NAME
    )
    with pytest.raises(
        SCRIPT.HenryAustinProvisioningRejected,
        match="austin_provision_path",
    ):
        SCRIPT._provision_runner_manifest(**symlink_arguments)

    leaf_arguments = _arguments(tmp_path / "leaf")
    leaf = leaf_arguments["output"].parent
    leaf.mkdir(mode=0o777)
    leaf.chmod(0o777)
    with pytest.raises(
        SCRIPT.HenryAustinProvisioningRejected,
        match="austin_provision_directory",
    ):
        SCRIPT._provision_runner_manifest(**leaf_arguments)


def test_existing_attestation_is_preserved_without_temporary_files(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path)
    output = arguments["output"]
    output.parent.mkdir(mode=0o755)
    output.write_bytes(b"preserve\n")
    output.chmod(0o644)

    with pytest.raises(
        SCRIPT.HenryAustinProvisioningRejected,
        match="austin_provision_attestation_exists",
    ):
        SCRIPT._provision_runner_manifest(**arguments)

    assert output.read_bytes() == b"preserve\n"
    assert list(output.parent.glob(".*.tmp")) == []


def test_partial_write_is_removed_without_publishing(tmp_path: Path, monkeypatch) -> None:
    arguments = _arguments(tmp_path)
    real_write = SCRIPT.os.write
    calls = 0

    def interrupted_write(descriptor: int, payload) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600
            return real_write(descriptor, payload[:8])
        raise OSError("interrupted")

    monkeypatch.setattr(SCRIPT.os, "write", interrupted_write)
    with pytest.raises(
        SCRIPT.HenryAustinProvisioningRejected,
        match="austin_provision_write",
    ):
        SCRIPT._provision_runner_manifest(**arguments)

    assert not arguments["output"].exists()
    assert list(arguments["output"].parent.glob(".*.tmp")) == []


def test_link_failure_is_removed_without_publishing(tmp_path: Path, monkeypatch) -> None:
    arguments = _arguments(tmp_path)

    def failed_link(source: str, _destination: str, **kwargs) -> None:
        source_information = os.stat(
            source,
            dir_fd=kwargs["src_dir_fd"],
            follow_symlinks=False,
        )
        assert stat.S_IMODE(source_information.st_mode) == 0o644
        raise OSError("link failed")

    monkeypatch.setattr(SCRIPT.os, "link", failed_link)
    with pytest.raises(
        SCRIPT.HenryAustinProvisioningRejected,
        match="austin_provision_publish",
    ):
        SCRIPT._provision_runner_manifest(**arguments)

    assert not arguments["output"].exists()
    assert list(arguments["output"].parent.glob(".*.tmp")) == []


def test_cli_report_does_not_echo_operator_inputs(monkeypatch, capsys) -> None:
    report = {
        "attestation_digest": "sha256:" + "c" * 64,
        "limitations": "bounded",
        "public_claim_eligible": False,
        "schema_version": 1,
        "status": "passed",
    }
    monkeypatch.setattr(SCRIPT, "provision_runner_manifest", lambda **_kwargs: report)

    status = SCRIPT.main(
        [
            "--runner-name",
            "Private-Runner-Name",
            "--image-digest",
            IMAGE_DIGEST,
            "--log-retention-days",
            "30",
            "--lifetime-hours",
            "1",
        ]
    )

    output = capsys.readouterr().out
    assert status == 0
    assert json.loads(output) == report
    assert "Private-Runner-Name" not in output
    assert IMAGE_DIGEST not in output
