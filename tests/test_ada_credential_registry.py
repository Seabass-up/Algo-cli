from __future__ import annotations

import json

import pytest

from algo_cli.ada_credential_registry import (
    ADA_CREDENTIAL_REGISTRY_LABEL,
    AdaCredentialFingerprint,
    AdaCredentialRegistry,
    AdaCredentialRegistryError,
    AdaNativeCredentialEnumeration,
)
from algo_cli.david_control_kernel import ControlSigner, content_digest


NOW_MS = 1_800_000_000_000
SERVICE = "algo-cli-runtime"
LABELS = (
    "ada-credential-labels-v1",
    "alice-artifact-master-v1",
    "browser-pairing-hmac-v1",
    "control-signing-ed25519-v1",
    "irene-privacy-hmac-v1",
)
NONCE = "a" * 64
TEAM_ID = "ABCDE12345"
CODE_IDENTIFIER = "com.algo-cli.austin.credential-migrator"
REQUIREMENT_DIGEST = "sha256:" + "b" * 64


def _registry() -> tuple[ControlSigner, AdaCredentialRegistry]:
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    return signer, AdaCredentialRegistry.create(
        revision=1,
        service=SERVICE,
        labels=LABELS,
        migration_kind="fresh_namespace",
        migration_evidence_digest=content_digest(
            {"kind": "fresh_namespace", "prior_known_label_count": 0}
        ),
        created_at_ms=NOW_MS,
        updated_at_ms=NOW_MS,
        signer=signer,
    )


def test_signed_registry_is_canonical_content_free_and_round_trips() -> None:
    signer, registry = _registry()
    payload = registry.to_bytes()

    decoded = AdaCredentialRegistry.from_bytes(payload)
    decoded.verify(signer.verifier)

    assert decoded == registry
    assert payload == json.dumps(
        registry.to_dict(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert ADA_CREDENTIAL_REGISTRY_LABEL in decoded.labels
    assert signer.private_bytes.hex() not in payload.decode("utf-8")


def test_registry_rejects_signature_schema_order_and_duplicate_tampering() -> None:
    signer, registry = _registry()

    tampered = registry.to_dict()
    tampered["revision"] = 2
    changed = AdaCredentialRegistry.from_dict(tampered)
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_signature"):
        changed.verify(signer.verifier)

    extra = registry.to_dict()
    extra["private_value"] = "forbidden"
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_schema"):
        AdaCredentialRegistry.from_dict(extra)

    reversed_labels = registry.to_dict()
    reversed_labels["labels"] = list(reversed(registry.labels))
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_label_order"):
        AdaCredentialRegistry.from_dict(reversed_labels)

    duplicate_labels = registry.to_dict()
    duplicate_labels["labels"] = [*registry.labels, registry.labels[-1]]
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_label_order"):
        AdaCredentialRegistry.from_dict(duplicate_labels)


def test_registry_advance_is_monotonic_idempotent_and_authority_bound() -> None:
    signer, registry = _registry()
    label = "receipt-head-v1-" + "a" * 64

    advanced = registry.advance(
        label=label,
        updated_at_ms=NOW_MS + 1,
        signer=signer,
    )
    unchanged = advanced.advance(
        label=label,
        updated_at_ms=NOW_MS + 2,
        signer=signer,
    )

    assert advanced.revision == 2
    assert label in advanced.labels
    assert unchanged == advanced
    advanced.verify(signer.verifier)

    other = ControlSigner.from_private_bytes(bytes(reversed(range(32))))
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_authority"):
        registry.advance(label=label, updated_at_ms=NOW_MS + 1, signer=other)


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        ("contains space", "credential_registry_label"),
        ("../escape", "credential_registry_label"),
        ("x" * 97, "credential_registry_label"),
    ],
)
def test_registry_rejects_unbounded_or_sensitive_label_shapes(
    label: str, reason: str
) -> None:
    signer, registry = _registry()

    with pytest.raises(AdaCredentialRegistryError, match=reason):
        registry.advance(label=label, updated_at_ms=NOW_MS + 1, signer=signer)


def test_registry_rejects_clock_regression_and_noncanonical_json() -> None:
    signer, registry = _registry()
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_integer"):
        registry.advance(
            label="receipt-head-v1-" + "b" * 64,
            updated_at_ms=NOW_MS - 1,
            signer=signer,
        )

    duplicate_key = registry.to_bytes().replace(
        b'{"authority_key_id":',
        b'{"revision":1,"authority_key_id":',
        1,
    )
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_encoding"):
        AdaCredentialRegistry.from_bytes(duplicate_key)


def _enumeration() -> AdaNativeCredentialEnumeration:
    return AdaNativeCredentialEnumeration(
        service=SERVICE,
        nonce=NONCE,
        generated_at_ms=NOW_MS,
        code_identifier=CODE_IDENTIFIER,
        team_id=TEAM_ID,
        designated_requirement_digest=REQUIREMENT_DIGEST,
        registry_present=False,
        unexpected_label_count=0,
        records=(
            AdaCredentialFingerprint(
                label="browser-pairing-hmac-v1",
                value_digest="sha256:" + "c" * 64,
            ),
            AdaCredentialFingerprint(
                label="control-signing-ed25519-v1",
                value_digest="sha256:" + "d" * 64,
            ),
        ),
    )


def test_native_enumeration_is_canonical_content_free_and_context_bound() -> None:
    evidence = _enumeration()
    payload = evidence.to_bytes()

    decoded = AdaNativeCredentialEnumeration.from_bytes(payload)
    decoded.verify_context(
        expected_service=SERVICE,
        expected_nonce=NONCE,
        expected_code_identifier=CODE_IDENTIFIER,
        expected_team_id=TEAM_ID,
        expected_designated_requirement_digest=REQUIREMENT_DIGEST,
        now_ms=NOW_MS + 1,
    )

    assert decoded == evidence
    assert b"private" not in payload
    assert b"credential_value" not in payload
    assert payload == json.dumps(
        evidence.to_dict(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def test_native_enumeration_rejects_replay_identity_scope_and_order() -> None:
    evidence = _enumeration()
    with pytest.raises(AdaCredentialRegistryError, match="credential_enumeration_nonce"):
        evidence.verify_context(
            expected_service=SERVICE,
            expected_nonce="e" * 64,
            expected_code_identifier=CODE_IDENTIFIER,
            expected_team_id=TEAM_ID,
            expected_designated_requirement_digest=REQUIREMENT_DIGEST,
            now_ms=NOW_MS,
        )
    with pytest.raises(AdaCredentialRegistryError, match="credential_enumeration_freshness"):
        evidence.verify_context(
            expected_service=SERVICE,
            expected_nonce=NONCE,
            expected_code_identifier=CODE_IDENTIFIER,
            expected_team_id=TEAM_ID,
            expected_designated_requirement_digest=REQUIREMENT_DIGEST,
            now_ms=NOW_MS + 60_001,
        )

    reversed_records = evidence.to_dict()
    reversed_records["records"] = list(reversed(reversed_records["records"]))
    with pytest.raises(AdaCredentialRegistryError, match="credential_enumeration_order"):
        AdaNativeCredentialEnumeration.from_dict(reversed_records)

    scoped = evidence.to_dict()
    scoped["unexpected_label_count"] = 1
    with pytest.raises(AdaCredentialRegistryError, match="credential_enumeration_scope"):
        AdaNativeCredentialEnumeration.from_dict(scoped)


def test_native_enumeration_rejects_noncanonical_and_extra_fields() -> None:
    evidence = _enumeration()
    payload = json.dumps(evidence.to_dict(), indent=2, sort_keys=True).encode("utf-8")
    with pytest.raises(AdaCredentialRegistryError, match="credential_enumeration_noncanonical"):
        AdaNativeCredentialEnumeration.from_bytes(payload)

    extra = evidence.to_dict()
    extra["credential_value"] = "forbidden"
    with pytest.raises(AdaCredentialRegistryError, match="credential_registry_schema"):
        AdaNativeCredentialEnumeration.from_dict(extra)
