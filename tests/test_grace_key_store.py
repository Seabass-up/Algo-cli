from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest

from algo_cli.ada_control_journal import EMPTY_EVIDENCE_DIGEST, EMPTY_RECEIPT_HEAD_DIGEST
from algo_cli.ada_credential_registry import (
    AdaCredentialFingerprint,
    AdaNativeCredentialEnumeration,
)
from algo_cli.david_control_kernel import ControlSigner, canonical_json_bytes
from algo_cli.grace_key_store import (
    ADA_CREDENTIAL_REGISTRY_LABEL,
    ALGO_FIXED_CREDENTIAL_LABELS,
    BROWSER_PAIRING_KEY_LABEL,
    CONTROL_SIGNING_KEY_LABEL,
    MAX_RECEIPT_ANCHOR_BYTES,
    RECEIPT_ANCHOR_LABEL_PREFIX,
    GraceReceiptAnchorStore,
    KeyStoreError,
    KeyringKeyStore,
    ReceiptAnchorStoreError,
    StaticKeyStore,
    get_browser_pairing_key,
    get_control_signer,
    get_key_material,
)
from algo_cli.henry_effect_control import TargetLeaseManager


NONCE = "a" * 64
TEAM_ID = "ABCDE12345"
CODE_IDENTIFIER = "com.algo-cli.austin.credential-migrator"
REQUIREMENT_DIGEST = "sha256:" + "b" * 64
NOW_MS = 1_800_000_000_000


class FakeBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


def _store(tmp_path, backend: FakeBackend) -> KeyringKeyStore:
    return KeyringKeyStore(
        backend,
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
    )


def _registered_store(tmp_path, backend: FakeBackend) -> KeyringKeyStore:
    store = KeyringKeyStore(
        backend,
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
        clock_ms=lambda: 1_800_000_000_000,
    )
    store.initialize_fresh_credential_registry()
    return store


def _anchor_value(sequence: int) -> bytes:
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    unsigned = {
        "schema_version": 2,
        "receipt_id": f"00000000-0000-4000-8000-{sequence:012d}",
        "journal_id": "sha256:" + "a" * 64,
        "receipt_sequence": sequence,
        "previous_receipt_digest": EMPTY_RECEIPT_HEAD_DIGEST,
        "effect_id": f"00000000-0000-4000-8000-{100 + sequence:012d}",
        "grant_id": "00000000-0000-4000-8000-000000000201",
        "permit_id": f"00000000-0000-4000-8000-{300 + sequence:012d}",
        "request_digest": "sha256:" + "b" * 64,
        "target_id": "hmac-sha256:" + "c" * 64,
        "target_epoch": 1,
        "target_revision": "document-1",
        "fencing_token": 1,
        "sequence": sequence,
        "operation": "activate",
        "route": "connector",
        "state": "failed",
        "reason_code": "recovered_before_dispatch",
        "evidence_digest": EMPTY_EVIDENCE_DIGEST,
        "transition_version": 2,
        "completed_at_ms": 1_800_000_000_000 + sequence,
        "authority_key_id": signer.key_id,
    }
    return canonical_json_bytes(
        {
            **unsigned,
            "signature": signer.sign("control_receipt", unsigned),
        }
    )


def _native_enumeration(
    backend: FakeBackend,
    *,
    generated_at_ms: int = NOW_MS,
) -> AdaNativeCredentialEnumeration:
    records = tuple(
        AdaCredentialFingerprint(
            label=label,
            value_digest="sha256:"
            + hashlib.sha256(value.encode("utf-8")).hexdigest(),
        )
        for (service, label), value in sorted(backend.values.items())
        if service == "algo-cli-runtime"
        and label != ADA_CREDENTIAL_REGISTRY_LABEL
    )
    return AdaNativeCredentialEnumeration(
        service="algo-cli-runtime",
        nonce=NONCE,
        generated_at_ms=generated_at_ms,
        code_identifier=CODE_IDENTIFIER,
        team_id=TEAM_ID,
        designated_requirement_digest=REQUIREMENT_DIGEST,
        registry_present=False,
        unexpected_label_count=0,
        records=records,
    )


def _migrate(
    store: KeyringKeyStore,
    evidence: AdaNativeCredentialEnumeration,
):
    return store.initialize_from_native_credential_enumeration(
        evidence,
        expected_nonce=NONCE,
        expected_code_identifier=CODE_IDENTIFIER,
        expected_team_id=TEAM_ID,
        expected_designated_requirement_digest=REQUIREMENT_DIGEST,
    )


def test_keyring_key_is_persistent_and_not_rendered(tmp_path) -> None:
    backend = FakeBackend()
    store = _store(tmp_path, backend)

    first = store.get_or_create("artifact-master-v1")
    second = store.get_or_create("artifact-master-v1")

    assert first.key == second.key
    assert first.persistent is True
    assert first.backend == "os_keyring"
    assert first.key.hex() not in repr(first)
    assert len(backend.values) == 1


def test_malformed_existing_key_fails_closed(tmp_path) -> None:
    backend = FakeBackend()
    backend.values[("algo-cli-runtime", "artifact-master-v1")] = "not base64!!"

    with pytest.raises(KeyStoreError, match="malformed"):
        _store(tmp_path, backend).get_or_create("artifact-master-v1")


def test_wrong_length_existing_key_is_not_silently_replaced(tmp_path) -> None:
    backend = FakeBackend()
    backend.values[("algo-cli-runtime", "artifact-master-v1")] = base64.urlsafe_b64encode(
        b"short"
    ).decode("ascii")

    with pytest.raises(KeyStoreError, match="invalid"):
        _store(tmp_path, backend).get_or_create("artifact-master-v1")


def test_delete_revokes_keyring_item(tmp_path) -> None:
    backend = FakeBackend()
    store = _store(tmp_path, backend)
    store.get_or_create("artifact-master-v1")

    store.delete("artifact-master-v1")

    assert backend.values == {}


def test_existing_load_never_creates_missing_key_material(tmp_path) -> None:
    backend = FakeBackend()
    store = _store(tmp_path, backend)

    with pytest.raises(KeyStoreError, match="absent"):
        store.get_existing("artifact-master-v1")

    assert backend.values == {}


def test_content_free_fingerprint_and_compare_delete_are_cas_bound(tmp_path) -> None:
    backend = FakeBackend()
    store = _store(tmp_path, backend)
    material = store.get_or_create("artifact-master-v1")
    fingerprint = store.fingerprint("artifact-master-v1")

    assert fingerprint is not None and fingerprint.startswith("sha256:")
    assert material.key.hex() not in fingerprint
    assert store.compare_and_delete(
        "artifact-master-v1", expected_digest="sha256:" + "0" * 64
    ) is False
    assert store.fingerprint("artifact-master-v1") == fingerprint
    assert store.compare_and_delete(
        "artifact-master-v1", expected_digest=fingerprint
    ) is True
    assert store.fingerprint("artifact-master-v1") is None


def test_receipt_anchor_compare_and_set_is_external_and_monotonic(tmp_path) -> None:
    backend = FakeBackend()
    store = _registered_store(tmp_path, backend)
    anchors = GraceReceiptAnchorStore(store)
    journal_id = "sha256:" + "a" * 64
    first = _anchor_value(1)
    second = _anchor_value(2)

    assert anchors.load(journal_id) is None
    assert anchors.compare_and_set(journal_id, expected_digest=None, value=first) is True
    assert anchors.load(journal_id) == first
    assert anchors.compare_and_set(journal_id, expected_digest=None, value=second) is False
    assert anchors.load(journal_id) == first

    expected = "sha256:" + hashlib.sha256(first).hexdigest()
    assert anchors.compare_and_set(journal_id, expected_digest=expected, value=second) is True
    assert anchors.load(journal_id) == second
    label = RECEIPT_ANCHOR_LABEL_PREFIX + "a" * 64
    assert ("algo-cli-runtime", label) in backend.values
    assert first.decode("ascii") not in backend.values[("algo-cli-runtime", label)]
    assert label in (store.complete_inventory_labels() or ())


def test_receipt_anchor_rejects_malformed_values_and_inputs(tmp_path) -> None:
    backend = FakeBackend()
    anchors = GraceReceiptAnchorStore(_registered_store(tmp_path, backend))
    journal_id = "sha256:" + "b" * 64
    label = RECEIPT_ANCHOR_LABEL_PREFIX + "b" * 64
    backend.values[("algo-cli-runtime", label)] = "not-an-anchor"

    with pytest.raises(ReceiptAnchorStoreError, match="anchor_encoding"):
        anchors.load(journal_id)
    with pytest.raises(ReceiptAnchorStoreError, match="anchor_journal_id"):
        anchors.load("private journal")
    with pytest.raises(ReceiptAnchorStoreError, match="anchor_expected_digest"):
        anchors.compare_and_set(journal_id, expected_digest="wrong", value=b"safe")
    with pytest.raises(ReceiptAnchorStoreError, match="anchor_value"):
        anchors.compare_and_set(journal_id, expected_digest=None, value=b'{"private":true}')
    with pytest.raises(ReceiptAnchorStoreError, match="anchor_value"):
        anchors.compare_and_set(
            journal_id,
            expected_digest=None,
            value=b"x" * (MAX_RECEIPT_ANCHOR_BYTES + 1),
        )


def test_receipt_anchor_compare_and_set_has_exactly_one_concurrent_winner(tmp_path) -> None:
    backend = FakeBackend()
    anchors = GraceReceiptAnchorStore(_registered_store(tmp_path, backend))
    journal_id = "sha256:" + "a" * 64
    values = tuple(_anchor_value(sequence) for sequence in range(1, 17))

    with ThreadPoolExecutor(max_workers=len(values)) as executor:
        results = tuple(
            executor.map(
                lambda value: anchors.compare_and_set(
                    journal_id,
                    expected_digest=None,
                    value=value,
                ),
                values,
            )
        )

    assert sum(results) == 1
    assert anchors.load(journal_id) in values


def test_fresh_registry_is_signed_complete_and_idempotent(tmp_path) -> None:
    backend = FakeBackend()
    store = _registered_store(tmp_path, backend)

    first = store.initialize_fresh_credential_registry()
    second = store.initialize_fresh_credential_registry()
    snapshot = store.complete_inventory_snapshot()

    assert first == second
    assert first.labels == tuple(sorted(ALGO_FIXED_CREDENTIAL_LABELS))
    assert first.migration_kind == "fresh_namespace"
    assert snapshot is not None
    assert tuple(label for label, _digest in snapshot) == first.labels
    assert dict(snapshot)[ADA_CREDENTIAL_REGISTRY_LABEL] is not None
    assert dict(snapshot)[CONTROL_SIGNING_KEY_LABEL] is not None
    assert first.to_bytes().decode("utf-8") in backend.values.values()


def test_production_fresh_initialization_requires_native_enumeration() -> None:
    with pytest.raises(KeyStoreError, match="credential_registry_native_enumeration_required"):
        KeyringKeyStore().initialize_fresh_credential_registry()


def test_native_empty_census_creates_signed_complete_registry(tmp_path) -> None:
    backend = FakeBackend()
    store = KeyringKeyStore(
        backend,
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
        clock_ms=lambda: NOW_MS,
    )

    registry = _migrate(store, _native_enumeration(backend))
    snapshot = dict(store.complete_inventory_snapshot() or ())

    assert registry.migration_kind == "native_enumeration"
    assert registry.labels == tuple(sorted(ALGO_FIXED_CREDENTIAL_LABELS))
    assert snapshot[CONTROL_SIGNING_KEY_LABEL] is not None
    assert snapshot[ADA_CREDENTIAL_REGISTRY_LABEL] is not None
    assert all(
        snapshot[label] is None
        for label in ALGO_FIXED_CREDENTIAL_LABELS
        if label not in {CONTROL_SIGNING_KEY_LABEL, ADA_CREDENTIAL_REGISTRY_LABEL}
    )


def test_native_migration_preserves_legacy_keys_and_stranded_anchor(tmp_path) -> None:
    backend = FakeBackend()
    control = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    anchor = RECEIPT_ANCHOR_LABEL_PREFIX + "f" * 64
    backend.values[("algo-cli-runtime", CONTROL_SIGNING_KEY_LABEL)] = control
    backend.values[("algo-cli-runtime", BROWSER_PAIRING_KEY_LABEL)] = "legacy-pairing"
    backend.values[("algo-cli-runtime", anchor)] = "stranded-anchor"
    before = dict(backend.values)
    store = KeyringKeyStore(
        backend,
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
        clock_ms=lambda: NOW_MS,
    )

    registry = _migrate(store, _native_enumeration(backend))

    assert registry.migration_kind == "native_enumeration"
    assert anchor in registry.labels
    assert registry.migration_evidence_digest.startswith("sha256:")
    assert all(backend.values[key] == value for key, value in before.items())
    assert dict(store.complete_inventory_snapshot() or ())[anchor] is not None


def test_native_migration_rejects_changed_unknown_and_replayed_census(tmp_path) -> None:
    backend = FakeBackend()
    backend.values[("algo-cli-runtime", BROWSER_PAIRING_KEY_LABEL)] = "legacy"
    store = KeyringKeyStore(
        backend,
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
        clock_ms=lambda: NOW_MS,
    )
    changed = _native_enumeration(backend)
    backend.values[("algo-cli-runtime", BROWSER_PAIRING_KEY_LABEL)] = "changed"
    with pytest.raises(KeyStoreError, match="credential_enumeration_changed"):
        _migrate(store, changed)
    assert ("algo-cli-runtime", ADA_CREDENTIAL_REGISTRY_LABEL) not in backend.values

    backend.values[("algo-cli-runtime", "unexpected-item")] = "opaque"
    with pytest.raises(KeyStoreError, match="credential_enumeration_scope"):
        _migrate(store, _native_enumeration(backend))

    backend.values.pop(("algo-cli-runtime", "unexpected-item"))
    replayed = _native_enumeration(backend, generated_at_ms=NOW_MS - 60_001)
    with pytest.raises(KeyStoreError, match="credential_enumeration_freshness"):
        _migrate(store, replayed)


def test_existing_namespace_requires_authenticated_migration(tmp_path) -> None:
    backend = FakeBackend()
    backend.values[("algo-cli-runtime", BROWSER_PAIRING_KEY_LABEL)] = "legacy"
    store = KeyringKeyStore(
        backend,
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
    )

    with pytest.raises(KeyStoreError, match="credential_registry_migration_required"):
        store.initialize_fresh_credential_registry()

    assert ("algo-cli-runtime", CONTROL_SIGNING_KEY_LABEL) not in backend.values
    assert ("algo-cli-runtime", ADA_CREDENTIAL_REGISTRY_LABEL) not in backend.values


def test_registry_tampering_blocks_complete_inventory_and_anchor_write(tmp_path) -> None:
    backend = FakeBackend()
    store = _registered_store(tmp_path, backend)
    raw = backend.values[("algo-cli-runtime", ADA_CREDENTIAL_REGISTRY_LABEL)]
    backend.values[("algo-cli-runtime", ADA_CREDENTIAL_REGISTRY_LABEL)] = raw.replace(
        '"revision":1', '"revision":2'
    )

    with pytest.raises(KeyStoreError, match="credential_registry_signature"):
        store.complete_inventory_snapshot()
    with pytest.raises(ReceiptAnchorStoreError, match="credential_registry_signature"):
        GraceReceiptAnchorStore(store).compare_and_set(
            "sha256:" + "d" * 64,
            expected_digest=None,
            value=_anchor_value(1),
        )


def test_anchor_failure_leaves_a_safe_absent_registry_tombstone(tmp_path) -> None:
    class FailingAnchorBackend(FakeBackend):
        fail_label: str | None = None

        def set_password(self, service_name: str, username: str, password: str) -> None:
            if username == self.fail_label:
                raise RuntimeError("simulated")
            super().set_password(service_name, username, password)

    backend = FailingAnchorBackend()
    store = _registered_store(tmp_path, backend)
    journal_id = "sha256:" + "e" * 64
    label = RECEIPT_ANCHOR_LABEL_PREFIX + "e" * 64
    backend.fail_label = label

    with pytest.raises(ReceiptAnchorStoreError, match="anchor_write_runtimeerror"):
        GraceReceiptAnchorStore(store).compare_and_set(
            journal_id,
            expected_digest=None,
            value=_anchor_value(1),
        )

    snapshot = dict(store.complete_inventory_snapshot() or ())
    assert label in snapshot
    assert snapshot[label] is None


def test_concurrent_anchor_registration_has_no_lost_labels(tmp_path) -> None:
    backend = FakeBackend()
    store = _registered_store(tmp_path, backend)
    anchors = GraceReceiptAnchorStore(store)
    journal_ids = tuple("sha256:" + f"{index:064x}" for index in range(1, 17))

    with ThreadPoolExecutor(max_workers=len(journal_ids)) as executor:
        results = tuple(
            executor.map(
                lambda journal_id: anchors.compare_and_set(
                    journal_id,
                    expected_digest=None,
                    value=_anchor_value(1),
                ),
                journal_ids,
            )
        )

    assert all(results)
    labels = set(store.complete_inventory_labels() or ())
    assert {
        RECEIPT_ANCHOR_LABEL_PREFIX + journal_id.removeprefix("sha256:")
        for journal_id in journal_ids
    } <= labels


def test_production_backend_requires_a_recognized_os_credential_store() -> None:
    class SecureBackend(FakeBackend):
        pass

    SecureBackend.__module__ = "keyring.backends.macOS"
    secure = SecureBackend()

    assert KeyringKeyStore._validate_system_backend(secure) is secure


def test_null_chained_and_third_party_plaintext_backends_fail_closed() -> None:
    for module_name in (
        "keyring.backends.null",
        "keyring.backends.fail",
        "keyring.backends.chainer",
        "keyrings.alt.file",
    ):
        backend_type = type("Backend", (FakeBackend,), {"__module__": module_name})
        with pytest.raises(KeyStoreError, match="recognized OS credential"):
            KeyringKeyStore._validate_system_backend(backend_type())


def test_recognized_backend_must_implement_the_complete_password_contract() -> None:
    backend_type = type(
        "IncompleteBackend",
        (),
        {
            "__module__": "keyring.backends.Windows",
            "get_password": lambda *_args: None,
        },
    )

    with pytest.raises(KeyStoreError, match="contract is incomplete"):
        KeyringKeyStore._validate_system_backend(backend_type())


def test_persistent_requirement_never_falls_back_to_volatile() -> None:
    class BrokenStore:
        def get_or_create(self, *_args, **_kwargs):
            raise RuntimeError("unavailable")

    with pytest.raises(KeyStoreError, match="persistent key source failed"):
        get_key_material(
            "artifact-master-v1",
            require_persistent=True,
            store=BrokenStore(),
        )


def test_privacy_key_can_use_bounded_process_fallback() -> None:
    class BrokenStore:
        def get_or_create(self, *_args, **_kwargs):
            raise RuntimeError("unavailable")

    first = get_key_material(
        "privacy-hmac-v1",
        require_persistent=False,
        store=BrokenStore(),
    )
    second = get_key_material(
        "privacy-hmac-v1",
        require_persistent=False,
        store=BrokenStore(),
    )

    assert first.key == second.key
    assert first.persistent is False
    assert first.backend == "volatile_process"


def test_static_store_rejects_wrong_key_length() -> None:
    store = StaticKeyStore({"privacy-hmac-v1": b"too-short"})

    with pytest.raises(KeyStoreError, match="wrong length"):
        store.get_or_create("privacy-hmac-v1")


def test_control_signer_is_stable_and_requires_persistent_storage() -> None:
    store = StaticKeyStore({CONTROL_SIGNING_KEY_LABEL: bytes(range(32))})

    first = get_control_signer(store=store)
    second = get_control_signer(store=store)

    assert first.key_id == second.key_id
    signature = first.sign("control_key_probe", {"value": 1})
    second.verifier.verify("control_key_probe", {"value": 1}, signature)
    assert first.private_bytes.hex() not in repr(first)


def test_browser_pairing_key_is_stable_and_persistent() -> None:
    store = StaticKeyStore({BROWSER_PAIRING_KEY_LABEL: b"p" * 32})

    first = get_browser_pairing_key(store=store)
    second = get_browser_pairing_key(store=store)

    assert first == second
    assert first.persistent is True
    assert first.key.hex() not in repr(first)


@pytest.mark.parametrize("label", ["", "../escape", "contains space", "x" * 97])
def test_labels_are_bounded_non_sensitive_identifiers(label: str) -> None:
    with pytest.raises(ValueError, match="key label"):
        StaticKeyStore().get_or_create(label)
