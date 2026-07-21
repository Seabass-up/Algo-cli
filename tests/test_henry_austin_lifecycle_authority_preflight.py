from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
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
    "henry_austin_lifecycle_authority_preflight_script",
    "henry_austin_lifecycle_authority_preflight.py",
)
VERIFIER = _load_script(
    "ada_austin_lifecycle_authority_preflight_compatibility_script",
    "ada_austin_signing_lifecycle_receipt.py",
)

REPOSITORY = "Algo-CLI-Org/Algo-cli"
KEY_IDS = {
    "github_controller": "github-controller-v1",
    "host_provider": "host-provider-v1",
    "log_sink": "external-log-sink-v1",
}

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Austin authority preflight verifies POSIX ownership and descriptor walks",
)


def _private_key(label: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode("ascii")).digest())


def _raw_public_key(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _public_pem(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _write_key(directory: Path, name: str, key: Ed25519PrivateKey) -> tuple[Path, str, bytes]:
    path = (directory / name).resolve()
    path.write_bytes(_public_pem(key))
    path.chmod(0o600)
    raw = _raw_public_key(key)
    return path, "sha256:" + hashlib.sha256(raw).hexdigest(), raw


def _fixture(tmp_path: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    directory = (tmp_path / "external-authorities").resolve()
    directory.mkdir(mode=0o700, parents=True)
    directory.chmod(0o700)
    keys = {
        "github_controller": _private_key("github-controller"),
        "host_provider": _private_key("host-provider"),
        "log_sink": _private_key("log-sink"),
    }
    written = {
        name: _write_key(directory, f"{name}.pem", key)
        for name, key in keys.items()
    }
    arguments: dict[str, Any] = {
        "effective_uid": os.geteuid(),
        "environment": {},
        "expected_owner_uid": os.geteuid(),
        "github_controller_key_id": KEY_IDS["github_controller"],
        "github_controller_public_key_path": written["github_controller"][0],
        "github_controller_public_key_sha256": written["github_controller"][1],
        "host_provider_key_id": KEY_IDS["host_provider"],
        "host_provider_public_key_path": written["host_provider"][0],
        "host_provider_public_key_sha256": written["host_provider"][1],
        "log_sink_key_id": KEY_IDS["log_sink"],
        "log_sink_public_key_path": written["log_sink"][0],
        "log_sink_public_key_sha256": written["log_sink"][1],
        "output": directory / SCRIPT.OUTPUT_FILE_NAME,
        "repository": REPOSITORY,
    }
    return arguments, {name: value[2] for name, value in written.items()}


def _prepare(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    arguments, keys = _fixture(tmp_path)
    report = SCRIPT._prepare_authority_candidate(**arguments)
    return report, arguments, keys


def test_candidate_is_canonical_public_only_private_and_verifier_compatible(
    tmp_path: Path,
) -> None:
    production_sentinel = (ROOT / "hardening" / "ada-signing-lifecycle-authorities.json").read_bytes()

    report, arguments, keys = _prepare(tmp_path)

    output = arguments["output"]
    payload = output.read_bytes()
    value = json.loads(payload)
    assert payload == SCRIPT._canonical_json(value)
    assert VERIFIER._authority_config(value) == value
    assert value["repository"] == REPOSITORY
    assert value["repository_id"] == SCRIPT.EXPECTED_REPOSITORY_ID
    assert value["schema_version"] == 2
    assert value["status"] == "configured"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.stat().st_nlink == 1
    assert report == {
        "activation_eligible": False,
        "authorities_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "authority_count": 3,
        "limitations": report["limitations"],
        "public_claim_eligible": False,
        "schema_version": 1,
        "status": "passed",
    }
    encoded_report = json.dumps(report, sort_keys=True)
    for key_id in KEY_IDS.values():
        assert key_id not in encoded_report
    for raw in keys.values():
        assert base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") not in encoded_report
    assert str(output) not in encoded_report
    assert (ROOT / "hardening" / "ada-signing-lifecycle-authorities.json").read_bytes() == production_sentinel


def test_preflight_and_verifier_contract_constants_are_exactly_aligned() -> None:
    for name in (
        "EXPECTED_JOB_NAME",
        "EXPECTED_REF",
        "EXPECTED_REPOSITORY_ID",
        "EXPECTED_RUNNER_GROUP",
        "EXPECTED_RUNNER_LABELS",
        "EXPECTED_WORKFLOW",
        "EXPECTED_WORKFLOW_NAME",
        "MAX_MANIFEST_BYTES",
    ):
        verifier_name = "MAX_AUTHORITIES_BYTES" if name == "MAX_MANIFEST_BYTES" else name
        assert getattr(SCRIPT, name) == getattr(VERIFIER, verifier_name)
    assert SCRIPT.OUTPUT_FILE_NAME.startswith("Ada")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"repository": "owner/../repo"}, "austin_authority_preflight_repository"),
        ({"repository": None}, "austin_authority_preflight_repository"),
        ({"github_controller_key_id": "INVALID"}, "austin_authority_preflight_key_id"),
        ({"github_controller_key_id": True}, "austin_authority_preflight_key_id"),
        ({"environment": {"GITHUB_ACTIONS": "false"}}, "austin_authority_preflight_online"),
        ({"effective_uid": -1}, "austin_authority_preflight_owner"),
        ({"effective_uid": True}, "austin_authority_preflight_owner"),
    ],
)
def test_invalid_scope_identity_and_execution_context_fail_closed(
    tmp_path: Path,
    changes: dict[str, object],
    reason: str,
) -> None:
    arguments, _keys = _fixture(tmp_path)
    arguments.update(changes)

    with pytest.raises(SCRIPT.HenryAustinAuthorityPreflightRejected, match=reason):
        SCRIPT._prepare_authority_candidate(**arguments)


@pytest.mark.parametrize("digest", ["sha256:" + "0" * 64, "SHA256:" + "0" * 64, True, None])
def test_unpinned_or_noncanonical_public_key_digest_rejects(
    tmp_path: Path,
    digest: object,
) -> None:
    arguments, _keys = _fixture(tmp_path)
    arguments["github_controller_public_key_sha256"] = digest

    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_key_digest",
    ):
        SCRIPT._prepare_authority_candidate(**arguments)


def test_private_key_pem_is_explicitly_rejected(tmp_path: Path) -> None:
    arguments, _keys = _fixture(tmp_path)
    path = arguments["github_controller_public_key_path"]
    key = _private_key("private-material")
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_private_material",
    ):
        SCRIPT._prepare_authority_candidate(**arguments)


def test_wrong_key_type_and_noncanonical_pem_reject(tmp_path: Path) -> None:
    wrong_arguments, _keys = _fixture(tmp_path / "wrong")
    wrong_path = wrong_arguments["github_controller_public_key_path"]
    x25519 = X25519PrivateKey.from_private_bytes(hashlib.sha256(b"x25519").digest())
    wrong_path.write_bytes(
        x25519.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_key_type",
    ):
        SCRIPT._prepare_authority_candidate(**wrong_arguments)

    noncanonical_arguments, _keys = _fixture(tmp_path / "noncanonical")
    noncanonical_path = noncanonical_arguments["github_controller_public_key_path"]
    noncanonical_path.write_bytes(noncanonical_path.read_bytes() + b"\n")
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_key_encoding",
    ):
        SCRIPT._prepare_authority_candidate(**noncanonical_arguments)


def test_duplicate_authority_key_or_identifier_rejects(tmp_path: Path) -> None:
    duplicate_key_arguments, _keys = _fixture(tmp_path / "key")
    duplicate_key_arguments["log_sink_public_key_path"] = duplicate_key_arguments[
        "github_controller_public_key_path"
    ]
    duplicate_key_arguments["log_sink_public_key_sha256"] = duplicate_key_arguments[
        "github_controller_public_key_sha256"
    ]
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_independence",
    ):
        SCRIPT._prepare_authority_candidate(**duplicate_key_arguments)

    duplicate_id_arguments, _keys = _fixture(tmp_path / "id")
    duplicate_id_arguments["log_sink_key_id"] = duplicate_id_arguments[
        "github_controller_key_id"
    ]
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_independence",
    ):
        SCRIPT._prepare_authority_candidate(**duplicate_id_arguments)


def test_key_symlink_hardlink_and_writable_file_reject(tmp_path: Path) -> None:
    symlink_arguments, _keys = _fixture(tmp_path / "symlink")
    original = symlink_arguments["github_controller_public_key_path"]
    link = original.with_name("github-controller-link.pem")
    link.symlink_to(original)
    symlink_arguments["github_controller_public_key_path"] = link
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_path",
    ):
        SCRIPT._prepare_authority_candidate(**symlink_arguments)

    hardlink_arguments, _keys = _fixture(tmp_path / "hardlink")
    hardlink_source = hardlink_arguments["github_controller_public_key_path"]
    hardlink = hardlink_source.with_name("github-controller-hardlink.pem")
    os.link(hardlink_source, hardlink)
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_key_security",
    ):
        SCRIPT._prepare_authority_candidate(**hardlink_arguments)

    writable_arguments, _keys = _fixture(tmp_path / "writable")
    writable = writable_arguments["github_controller_public_key_path"]
    writable.chmod(0o622)
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_key_security",
    ):
        SCRIPT._prepare_authority_candidate(**writable_arguments)


def test_output_boundary_rejects_relative_wrong_repository_and_symlinked_parent(
    tmp_path: Path,
) -> None:
    relative_arguments, _keys = _fixture(tmp_path / "relative")
    relative_arguments["output"] = Path(SCRIPT.OUTPUT_FILE_NAME)
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_output",
    ):
        SCRIPT._prepare_authority_candidate(**relative_arguments)

    wrong_name_arguments, _keys = _fixture(tmp_path / "wrong-name")
    wrong_name_arguments["output"] = wrong_name_arguments["output"].with_name("authorities.json")
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_output",
    ):
        SCRIPT._prepare_authority_candidate(**wrong_name_arguments)

    repository_arguments, _keys = _fixture(tmp_path / "repository")
    repository_arguments["output"] = ROOT / SCRIPT.OUTPUT_FILE_NAME
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_output",
    ):
        SCRIPT._prepare_authority_candidate(**repository_arguments)

    symlink_arguments, _keys = _fixture(tmp_path / "parent")
    real_parent = (tmp_path / "real-output").resolve()
    real_parent.mkdir(mode=0o700)
    link_parent = (tmp_path / "linked-output").resolve(strict=False)
    link_parent.symlink_to(real_parent, target_is_directory=True)
    symlink_arguments["output"] = link_parent / SCRIPT.OUTPUT_FILE_NAME
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_output",
    ):
        SCRIPT._prepare_authority_candidate(**symlink_arguments)

    ancestor_arguments, _keys = _fixture(tmp_path / "ancestor")
    real_tree = (tmp_path / "real-tree").resolve()
    real_leaf = real_tree / "leaf"
    real_leaf.mkdir(mode=0o700, parents=True)
    linked_tree = (tmp_path / "linked-tree").resolve(strict=False)
    linked_tree.symlink_to(real_tree, target_is_directory=True)
    ancestor_arguments["output"] = linked_tree / "leaf" / SCRIPT.OUTPUT_FILE_NAME
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_output",
    ):
        SCRIPT._prepare_authority_candidate(**ancestor_arguments)


def test_existing_output_is_preserved_and_never_replaced(tmp_path: Path) -> None:
    arguments, _keys = _fixture(tmp_path)
    output = arguments["output"]
    output.write_bytes(b"existing\n")
    output.chmod(0o600)

    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_output_exists",
    ):
        SCRIPT._prepare_authority_candidate(**arguments)

    assert output.read_bytes() == b"existing\n"
    assert list(output.parent.glob(f".{SCRIPT.OUTPUT_FILE_NAME}.*.tmp")) == []


def test_interrupted_write_removes_only_the_private_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments, _keys = _fixture(tmp_path)
    output = arguments["output"]

    def fail_write(_descriptor: int, _payload: object) -> int:
        raise OSError("injected")

    monkeypatch.setattr(SCRIPT.os, "write", fail_write)
    with pytest.raises(
        SCRIPT.HenryAustinAuthorityPreflightRejected,
        match="austin_authority_preflight_write",
    ):
        SCRIPT._prepare_authority_candidate(**arguments)

    assert not output.exists()
    assert list(output.parent.glob(f".{SCRIPT.OUTPUT_FILE_NAME}.*.tmp")) == []


def test_cli_emits_content_free_report_and_never_echoes_keys_or_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    arguments, keys = _fixture(tmp_path)
    argv = [
        "--output",
        str(arguments["output"]),
        "--repository",
        REPOSITORY,
        "--github-controller-key-id",
        KEY_IDS["github_controller"],
        "--github-controller-public-key",
        str(arguments["github_controller_public_key_path"]),
        "--github-controller-public-key-sha256",
        str(arguments["github_controller_public_key_sha256"]),
        "--log-sink-key-id",
        KEY_IDS["log_sink"],
        "--log-sink-public-key",
        str(arguments["log_sink_public_key_path"]),
        "--log-sink-public-key-sha256",
        str(arguments["log_sink_public_key_sha256"]),
        "--host-provider-key-id",
        KEY_IDS["host_provider"],
        "--host-provider-public-key",
        str(arguments["host_provider_public_key_path"]),
        "--host-provider-public-key-sha256",
        str(arguments["host_provider_public_key_sha256"]),
    ]

    assert SCRIPT.main(argv) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["status"] == "passed"
    assert report["activation_eligible"] is False
    for raw in keys.values():
        assert base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") not in output
    for value in arguments.values():
        if isinstance(value, Path):
            assert str(value) not in output
