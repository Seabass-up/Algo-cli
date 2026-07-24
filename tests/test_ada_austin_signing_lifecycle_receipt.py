from __future__ import annotations

import base64
import calendar
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Austin lifecycle receipts verify POSIX ownership and descriptor identity",
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "ada_austin_signing_lifecycle_receipt.py"
SPEC = importlib.util.spec_from_file_location(
    "ada_austin_signing_lifecycle_receipt_script",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCRIPT
SPEC.loader.exec_module(SCRIPT)

NOW = calendar.timegm(time.strptime("2026-07-20T10:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
SOURCE_COMMIT = "a" * 40
RECEIPT_ID = "11111111-1111-4111-8111-111111111111"
DELIVERY_ID = "22222222-2222-4222-8222-222222222222"
BOOT_SESSION = "33333333-3333-4333-8333-333333333333"
REPOSITORY = "Algo-CLI-Org/Algo-cli"


def _private_key(label: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode("ascii")).digest())


KEYS = {
    "github_job": _private_key("github-controller"),
    "external_log": _private_key("external-log-sink"),
    "host_destroyed": _private_key("host-provider"),
}
KEY_IDS = {
    "github_job": "github-controller-v1",
    "external_log": "external-log-sink-v1",
    "host_destroyed": "host-provider-v1",
}
AUTHORITY_NAMES = {
    "github_job": "github_controller",
    "external_log": "log_sink",
    "host_destroyed": "host_provider",
}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _config() -> dict[str, Any]:
    authorities: dict[str, Any] = {}
    for kind, authority_name in AUTHORITY_NAMES.items():
        public_key = _public(KEYS[kind])
        authorities[authority_name] = {
            "key_id": KEY_IDS[kind],
            "public_key_base64url": _b64(public_key),
            "public_key_sha256": "sha256:" + hashlib.sha256(public_key).hexdigest(),
        }
    return {
        "authorities": authorities,
        "job_name": SCRIPT.EXPECTED_JOB_NAME,
        "ref": SCRIPT.EXPECTED_REF,
        "repository": REPOSITORY,
        "repository_id": SCRIPT.EXPECTED_REPOSITORY_ID,
        "runner_group": SCRIPT.EXPECTED_RUNNER_GROUP,
        "runner_labels": list(SCRIPT.EXPECTED_RUNNER_LABELS),
        "schema_version": 2,
        "status": "configured",
        "workflow": SCRIPT.EXPECTED_WORKFLOW,
        "workflow_name": SCRIPT.EXPECTED_WORKFLOW_NAME,
    }


def _binding() -> dict[str, Any]:
    return {
        "boot_session_uuid": BOOT_SESSION,
        "image_digest": "sha256:" + "b" * 64,
        "job_id": 303,
        "receipt_id": RECEIPT_ID,
        "ref": SCRIPT.EXPECTED_REF,
        "repository": REPOSITORY,
        "repository_id": SCRIPT.EXPECTED_REPOSITORY_ID,
        "run_attempt": 1,
        "run_id": 101,
        "runner_attestation_digest": "sha256:" + "c" * 64,
        "runner_group": SCRIPT.EXPECTED_RUNNER_GROUP,
        "runner_id": 404,
        "runner_labels": list(SCRIPT.EXPECTED_RUNNER_LABELS),
        "runner_name": "Austin-Ephemeral-1",
        "source_commit": SOURCE_COMMIT,
        "workflow": SCRIPT.EXPECTED_WORKFLOW,
    }


def _observations() -> dict[str, dict[str, Any]]:
    return {
        "github_job": {
            "api_version": SCRIPT.EXPECTED_API_VERSION,
            "artifact_attestation_digest": "sha256:" + "d" * 64,
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": SOURCE_COMMIT,
            "job_completed_at": "2026-07-20T08:30:00Z",
            "job_name": SCRIPT.EXPECTED_JOB_NAME,
            "job_started_at": "2026-07-20T08:00:00Z",
            "observed_at": "2026-07-20T08:45:00Z",
            "package_digest": "sha256:" + "e" * 64,
            "release_evidence_digest": "sha256:" + "f" * 64,
            "runner_registration_state": "absent",
            "status": "completed",
            "workflow_job_action": "completed",
            "workflow_job_delivery_id": DELIVERY_ID,
            "workflow_name": SCRIPT.EXPECTED_WORKFLOW_NAME,
        },
        "external_log": {
            "archive_digest": "sha256:" + "1" * 64,
            "first_event_at": "2026-07-20T07:58:00Z",
            "last_event_at": "2026-07-20T08:31:00Z",
            "receipt_issued_at": "2026-07-20T08:42:00Z",
            "received_at": "2026-07-20T08:36:00Z",
            "retention_until": "2026-08-19T08:36:00Z",
            "runner_log_count": 1,
            "worker_log_count": 1,
        },
        "host_destroyed": {
            "destroy_operation_digest": "sha256:" + "2" * 64,
            "destroyed_at": "2026-07-20T08:35:00Z",
            "host_state": "destroyed",
            "network_identity_state": "released",
            "provider_instance_digest": "sha256:" + "3" * 64,
            "receipt_issued_at": "2026-07-20T08:40:00Z",
            "storage_state": "destroyed",
        },
    }


def _receipt(kind: str, *, binding: dict[str, Any] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "authority_key_id": KEY_IDS[kind],
        "binding": binding or _binding(),
        "kind": kind,
        "observation": _observations()[kind],
        "schema_version": 2,
    }
    return _sign(value, kind=kind)


def _sign(value: dict[str, Any], *, kind: str) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("signature", None)
    message = SCRIPT._DOMAINS[kind] + SCRIPT._canonical_json(unsigned)
    signed = dict(unsigned)
    signed["signature"] = _b64(KEYS[kind].sign(message))
    return signed


def _write(path: Path, value: dict[str, Any], *, mode: int = 0o644) -> tuple[Path, str]:
    payload = SCRIPT._canonical_json(value)
    path.write_bytes(payload)
    path.chmod(mode)
    return path.resolve(), "sha256:" + hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, Any]:
    authorities, authorities_digest = _write(tmp_path / "authorities.json", _config())
    github, _ = _write(tmp_path / "github.json", _receipt("github_job"))
    logs, _ = _write(tmp_path / "logs.json", _receipt("external_log"))
    destroy, _ = _write(tmp_path / "destroy.json", _receipt("host_destroyed"))
    return {
        "authorities_path": authorities,
        "expected_authorities_digest": authorities_digest,
        "expected_binding_digest": SCRIPT._digest(SCRIPT._canonical_json(_binding())),
        "github_receipt_path": github,
        "log_receipt_path": logs,
        "destroy_receipt_path": destroy,
        "expected_owner_uid": os.getuid(),
        "now_seconds": NOW,
    }


def _strict(tmp_path: Path) -> dict[str, Any]:
    return SCRIPT.verify_lifecycle_receipts(**_fixture(tmp_path))


def test_three_independent_receipts_pass_without_identifier_disclosure(tmp_path: Path) -> None:
    report = _strict(tmp_path)

    assert report["status"] == "passed"
    assert report["external_lifecycle_eligible"] is True
    assert report["public_claim_eligible"] is False
    encoded = json.dumps(report, sort_keys=True)
    assert "Austin-Ephemeral-1" not in encoded
    assert RECEIPT_ID not in encoded
    assert DELIVERY_ID not in encoded
    assert _b64(_public(KEYS["github_job"])) not in encoded


def test_unconfigured_production_authorities_block_without_touching_receipts(tmp_path: Path) -> None:
    authorities, _ = _write(
        tmp_path / "authorities.json",
        {
            "reason_code": "external_lifecycle_authorities_not_provisioned",
            "schema_version": 1,
            "status": "unconfigured",
        },
    )
    report = SCRIPT.evaluate_lifecycle(
        authorities_path=authorities,
        expected_authorities_digest=None,
        expected_binding_digest=None,
        github_receipt_path=(tmp_path / "missing-github.json").resolve(),
        log_receipt_path=(tmp_path / "missing-log.json").resolve(),
        destroy_receipt_path=(tmp_path / "missing-destroy.json").resolve(),
        expected_owner_uid=os.getuid(),
        now_seconds=NOW,
    )

    assert report["status"] == "blocked"
    assert report["reason_code"] == "austin_lifecycle_authorities_unconfigured"


def test_configured_authorities_require_separately_pinned_digest(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    report = SCRIPT.evaluate_lifecycle(
        authorities_path=fixture["authorities_path"],
        expected_authorities_digest=None,
        expected_binding_digest=fixture["expected_binding_digest"],
        github_receipt_path=fixture["github_receipt_path"],
        log_receipt_path=fixture["log_receipt_path"],
        destroy_receipt_path=fixture["destroy_receipt_path"],
        expected_owner_uid=os.getuid(),
        now_seconds=NOW,
    )

    assert report["status"] == "blocked"
    assert report["reason_code"] == "austin_lifecycle_authorities_digest_unpinned"


def test_missing_receipt_set_is_blocked_not_passed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    report = SCRIPT.evaluate_lifecycle(
        authorities_path=fixture["authorities_path"],
        expected_authorities_digest=fixture["expected_authorities_digest"],
        expected_binding_digest=fixture["expected_binding_digest"],
        github_receipt_path=None,
        log_receipt_path=None,
        destroy_receipt_path=None,
        expected_owner_uid=os.getuid(),
        now_seconds=NOW,
    )

    assert report["status"] == "blocked"
    assert report["reason_code"] == "austin_lifecycle_receipts_missing"


def test_current_dispatch_binding_must_be_separately_pinned(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    report = SCRIPT.evaluate_lifecycle(
        authorities_path=fixture["authorities_path"],
        expected_authorities_digest=fixture["expected_authorities_digest"],
        expected_binding_digest=None,
        github_receipt_path=fixture["github_receipt_path"],
        log_receipt_path=fixture["log_receipt_path"],
        destroy_receipt_path=fixture["destroy_receipt_path"],
        expected_owner_uid=os.getuid(),
        now_seconds=NOW,
    )

    assert report["status"] == "blocked"
    assert report["reason_code"] == "austin_lifecycle_binding_digest_unpinned"


def test_named_but_absent_external_receipt_remains_blocked(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["destroy_receipt_path"].unlink()
    report = SCRIPT.evaluate_lifecycle(**fixture)

    assert report["status"] == "blocked"
    assert report["reason_code"] == "austin_lifecycle_receipt_missing"


def test_recent_valid_receipts_cannot_replay_for_another_pinned_dispatch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["expected_binding_digest"] = "sha256:" + "9" * 64

    with pytest.raises(
        SCRIPT.AdaAustinLifecycleRejected,
        match="austin_lifecycle_binding_digest",
    ):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_key_substitution_cannot_replace_pinned_authorities(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original_digest = fixture["expected_authorities_digest"]
    config = _config()
    replacement = _private_key("attacker")
    public_key = _public(replacement)
    config["authorities"]["github_controller"] = {
        "key_id": "attacker-v1",
        "public_key_base64url": _b64(public_key),
        "public_key_sha256": "sha256:" + hashlib.sha256(public_key).hexdigest(),
    }
    _write(fixture["authorities_path"], config)

    with pytest.raises(
        SCRIPT.AdaAustinLifecycleRejected,
        match="austin_lifecycle_authorities_digest",
    ):
        SCRIPT.verify_lifecycle_receipts(
            **{**fixture, "expected_authorities_digest": original_digest}
        )


def test_signature_swapping_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    github = _receipt("github_job")
    github["signature"] = _receipt("external_log")["signature"]
    _write(fixture["github_receipt_path"], github)

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_signature"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_mixed_run_receipts_are_rejected_after_valid_signatures(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    binding = _binding()
    binding["run_id"] = 999
    _write(fixture["log_receipt_path"], _receipt("external_log", binding=binding))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_mixed_binding"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("repository", "other/repo", "austin_lifecycle_binding_scope"),
        ("ref", "refs/heads/feature", "austin_lifecycle_binding_scope"),
        ("workflow", ".github/workflows/other.yml", "austin_lifecycle_binding_scope"),
        ("runner_group", "default", "austin_lifecycle_binding_scope"),
        ("source_commit", "not-a-sha", "austin_lifecycle_source_commit"),
        ("run_id", True, "austin_lifecycle_run_id"),
        ("run_attempt", 1001, "austin_lifecycle_run_attempt"),
        ("receipt_id", "not-a-uuid", "austin_lifecycle_receipt_id"),
    ],
)
def test_binding_scope_and_types_fail_closed(
    tmp_path: Path,
    field: str,
    value: object,
    reason: str,
) -> None:
    fixture = _fixture(tmp_path)
    binding = _binding()
    binding[field] = value
    _write(fixture["github_receipt_path"], _receipt("github_job", binding=binding))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match=reason):
        SCRIPT.verify_lifecycle_receipts(**fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("workflow_job_action", "queued"),
        ("runner_registration_state", "offline"),
        ("head_sha", "b" * 40),
        ("head_branch", "feature"),
    ],
)
def test_github_job_must_be_successful_completed_and_absent(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    fixture = _fixture(tmp_path)
    receipt = _receipt("github_job")
    receipt["observation"][field] = value
    _write(fixture["github_receipt_path"], _sign(receipt, kind="github_job"))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_github_state"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


@pytest.mark.parametrize("field", ["runner_log_count", "worker_log_count"])
def test_external_archive_requires_runner_and_worker_logs(tmp_path: Path, field: str) -> None:
    fixture = _fixture(tmp_path)
    receipt = _receipt("external_log")
    receipt["observation"][field] = 0
    _write(fixture["log_receipt_path"], _sign(receipt, kind="external_log"))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_log_count"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_state", "stopped"),
        ("storage_state", "retained"),
        ("network_identity_state", "attached"),
    ],
)
def test_provider_receipt_requires_actual_host_storage_and_network_destruction(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    fixture = _fixture(tmp_path)
    receipt = _receipt("host_destroyed")
    receipt["observation"][field] = value
    _write(fixture["destroy_receipt_path"], _sign(receipt, kind="host_destroyed"))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_destroy_state"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


@pytest.mark.parametrize(
    ("kind", "field", "value", "reason"),
    [
        ("github_job", "job_completed_at", "2026-07-20T07:59:00Z", "austin_lifecycle_job_time"),
        ("external_log", "last_event_at", "2026-07-20T08:20:00Z", "austin_lifecycle_log_incomplete"),
        ("external_log", "first_event_at", "2026-07-19T07:59:59Z", "austin_lifecycle_log_time"),
        ("external_log", "received_at", "2026-07-20T08:20:00Z", "austin_lifecycle_log_time"),
        ("host_destroyed", "destroyed_at", "2026-07-20T08:00:00Z", "austin_lifecycle_destroy_time"),
        ("github_job", "observed_at", "2026-07-20T08:00:00Z", "austin_lifecycle_github_observation_time"),
    ],
)
def test_cross_receipt_time_order_is_enforced(
    tmp_path: Path,
    kind: str,
    field: str,
    value: str,
    reason: str,
) -> None:
    fixture = _fixture(tmp_path)
    receipt = _receipt(kind)
    receipt["observation"][field] = value
    path_name = {
        "github_job": "github_receipt_path",
        "external_log": "log_receipt_path",
        "host_destroyed": "destroy_receipt_path",
    }[kind]
    _write(fixture[path_name], _sign(receipt, kind=kind))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match=reason):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_stale_signed_receipts_are_rejected_as_replay(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["now_seconds"] = NOW + SCRIPT.MAX_RECEIPT_AGE_SECONDS + 1

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_replay"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


@pytest.mark.parametrize(
    "retention_until",
    ["2026-08-18T08:36:00Z", "2027-07-21T08:36:01Z"],
)
def test_log_retention_must_be_bounded(retention_until: str, tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = _receipt("external_log")
    receipt["observation"]["retention_until"] = retention_until
    _write(fixture["log_receipt_path"], _sign(receipt, kind="external_log"))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_log_retention"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_noncanonical_base64url_signature_spelling_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = _receipt("github_job")
    signature = receipt["signature"]
    decoded = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    replacement = next(
        candidate
        for candidate in alphabet
        if candidate != signature[-1]
        and base64.urlsafe_b64decode(signature[:-1] + candidate + "==") == decoded
    )
    receipt["signature"] = signature[:-1] + replacement
    _write(fixture["github_receipt_path"], receipt)

    with pytest.raises(
        SCRIPT.AdaAustinLifecycleRejected,
        match="austin_lifecycle_signature_encoding",
    ):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_duplicate_keys_and_noncanonical_json_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["github_receipt_path"].write_text(
        '{"kind":"github_job","kind":"github_job"}\n',
        encoding="ascii",
    )

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_json"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_group_writable_and_hardlinked_receipts_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["github_receipt_path"].chmod(0o664)
    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_file_security"):
        SCRIPT.verify_lifecycle_receipts(**fixture)

    fixture["github_receipt_path"].chmod(0o644)
    os.link(fixture["github_receipt_path"], tmp_path / "receipt-link.json")
    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_file_security"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_leaf_and_ancestor_symlinks_are_rejected_descriptor_relatively(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original_github = fixture["github_receipt_path"]
    leaf = tmp_path / "github-symlink.json"
    leaf.symlink_to(original_github)
    fixture["github_receipt_path"] = leaf.absolute()
    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_path"):
        SCRIPT.verify_lifecycle_receipts(**fixture)
    fixture["github_receipt_path"] = original_github

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    authorities, authorities_digest = _write(real_parent / "authorities.json", _config())
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    fixture["authorities_path"] = linked_parent.absolute() / authorities.name
    fixture["expected_authorities_digest"] = authorities_digest
    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_path"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_authority_keys_must_be_independent(tmp_path: Path) -> None:
    config = _config()
    config["authorities"]["host_provider"] = dict(config["authorities"]["log_sink"])
    authorities, digest = _write(tmp_path / "authorities.json", config)

    with pytest.raises(
        SCRIPT.AdaAustinLifecycleRejected,
        match="austin_lifecycle_authority_independence",
    ):
        SCRIPT._authority_config(SCRIPT._parse_canonical_json(authorities.read_bytes()))
    assert digest.startswith("sha256:")


@pytest.mark.parametrize("repository_id", [1, True, "1297752684"])
def test_configured_authorities_require_the_pinned_repository_id(
    repository_id: object,
) -> None:
    config = _config()
    config["repository_id"] = repository_id

    with pytest.raises(
        SCRIPT.AdaAustinLifecycleRejected,
        match="austin_lifecycle_authorities_scope",
    ):
        SCRIPT._authority_config(config)


def test_receipt_binding_cannot_substitute_the_pinned_repository_id(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    binding = _binding()
    binding["repository_id"] = 1
    github, _ = _write(tmp_path / "substituted-github.json", _receipt("github_job", binding=binding))
    fixture["github_receipt_path"] = github

    with pytest.raises(
        SCRIPT.AdaAustinLifecycleRejected,
        match="austin_lifecycle_binding_scope",
    ):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_artifact_digests_are_closed_and_signed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt = _receipt("github_job")
    receipt["observation"]["package_digest"] = "sha256:short"
    _write(fixture["github_receipt_path"], _sign(receipt, kind="github_job"))

    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_artifact_digest"):
        SCRIPT.verify_lifecycle_receipts(**fixture)


def test_evidence_output_is_private_durable_and_no_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    payload = b'{"schema_version":1}\n'
    SCRIPT._write_no_overwrite(output, payload)

    assert output.read_bytes() == payload
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SCRIPT.AdaAustinLifecycleRejected, match="austin_lifecycle_output_exists"):
        SCRIPT._write_no_overwrite(output, payload)
