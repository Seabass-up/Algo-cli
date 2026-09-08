from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json

import pytest

from algo_cli.ada_credential_registry import ADA_CREDENTIAL_REGISTRY_LABEL
from algo_cli.elsie_keyring_anchors import ElsieKeyringAnchorStore
from algo_cli.grace_key_store import (
    CONTROL_SIGNING_KEY_LABEL,
    ELSIE_MEMORY_ANCHORS_LABEL,
    ContentFreeReceiptHead,
    GraceReceiptAnchorStore,
    KeyStoreError,
    KeyringKeyStore,
    _anchor_label,
    _decode_anchor,
    _encode_anchor,
)
from algo_cli.grace_memory_receipts import ElsieReceiptAuthority
from algo_cli.henry_effect_control import TargetLeaseManager
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL


class Backend:
    def __init__(self):
        self.values = {}

    def get_password(self, service, label):
        return self.values.get((service, label))

    def set_password(self, service, label, value):
        self.values[service, label] = value

    def delete_password(self, service, label):
        self.values.pop((service, label), None)


@pytest.fixture
def setup(tmp_path):
    backend = Backend()
    keys = KeyringKeyStore(backend, lease_manager=TargetLeaseManager(tmp_path / "leases"))
    keys.get_or_create(PRIVACY_KEY_LABEL, length=32)
    return backend, keys, ElsieKeyringAnchorStore(keys)


def head(sequence=1, *, name="a", namespace="elsie-goal-store-v1"):
    return ContentFreeReceiptHead(
        namespace=namespace,
        journal_id="sha256:" + name * 64,
        subject_digest="b" * 64,
        sequence=sequence,
        head_digest=str(sequence) * 64,
        authentication="c" * 64,
    )


def write(anchors, value, previous=None):
    return anchors.compare_and_set(
        value.journal_id,
        expected_digest=None if previous is None else "sha256:" + hashlib.sha256(previous.to_bytes()).hexdigest(),
        value=value.to_bytes(),
    )


def test_requires_explicit_provision_and_preserves_key(setup):
    backend, keys, anchors = setup
    before = dict(backend.values)
    with pytest.raises(KeyStoreError, match="memory_anchor_provisioning_required"):
        write(anchors, head())
    assert backend.values == before
    assert anchors.provision() is True
    assert anchors.provision() is False
    assert backend.values[keys.service, PRIVACY_KEY_LABEL] == before[keys.service, PRIVACY_KEY_LABEL]
    assert keys.complete_inventory_snapshot() is None
    assert keys.fingerprint(CONTROL_SIGNING_KEY_LABEL) is None
    assert keys.fingerprint(ADA_CREDENTIAL_REGISTRY_LABEL) is None
    assert write(anchors, head())
    assert anchors.load(head().journal_id) == head().to_bytes()
    assert len(backend.values) == 2


def test_default_receipt_authorities_use_memory_only_store(setup):
    _backend, keys, _anchors = setup
    for factory in (
        ElsieReceiptAuthority.from_key_store,
        ElsieReceiptAuthority.from_existing_key_store,
        ElsieReceiptAuthority.from_optional_existing_key_store,
    ):
        assert isinstance(factory(store=keys).anchor_store(), ElsieKeyringAnchorStore)


def test_cas_and_restart_keep_all_heads(setup):
    _backend, keys, anchors = setup
    anchors.provision()
    first, second = head(), head(name="d")
    assert write(anchors, first)
    assert write(anchors, second)
    assert not write(anchors, head(2))
    assert write(anchors, head(2), first)
    reopened = ElsieKeyringAnchorStore(keys)
    assert reopened.load(first.journal_id) == head(2).to_bytes()
    assert reopened.load(second.journal_id) == second.to_bytes()


def test_competing_cas_has_one_winner(setup):
    _backend, _keys, anchors = setup
    anchors.provision()
    assert write(anchors, head())
    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = list(pool.map(lambda _: write(anchors, head(2), head()), range(4)))
    assert winners.count(True) == 1


@pytest.mark.parametrize("namespace", ["native-control", "authority-rotation", "elsie-goal-store-v2"])
def test_rejects_non_memory_domains_without_writes(setup, namespace):
    backend, _keys, anchors = setup
    anchors.provision()
    before = dict(backend.values)
    with pytest.raises(KeyStoreError, match="memory_anchor_scope"):
        write(anchors, head(namespace=namespace))
    assert backend.values == before


@pytest.mark.parametrize(
    "changes",
    [{"sequence": 1}, {"sequence": 3}, {"subject_digest": "d" * 64}, {"namespace": "elsie-agent-thread-store-v1"}],
)
def test_rejects_rollback_skip_or_rebinding(setup, changes):
    _backend, _keys, anchors = setup
    anchors.provision()
    write(anchors, head())
    with pytest.raises(KeyStoreError, match="memory_anchor_sequence"):
        write(anchors, replace(head(2), **changes), head())
    assert anchors.load(head().journal_id) == head().to_bytes()


@pytest.mark.parametrize("replacement", ["{}", "not-json", "x" * 65536, '{"schema_version":true}'])
def test_never_overwrites_invalid_bundle(setup, replacement):
    backend, keys, anchors = setup
    backend.values[keys.service, ELSIE_MEMORY_ANCHORS_LABEL] = replacement
    before = dict(backend.values)
    for operation in (anchors.provision, lambda: anchors.load(head().journal_id), lambda: write(anchors, head())):
        with pytest.raises(KeyStoreError, match="memory_anchor_invalid"):
            operation()
        assert backend.values == before


def test_authentication_tamper_and_missing_key_fail_closed(setup):
    backend, keys, anchors = setup
    anchors.provision()
    write(anchors, head())
    original = backend.values[keys.service, ELSIE_MEMORY_ANCHORS_LABEL]
    changed = json.loads(original)
    changed["heads"] = {}
    backend.values[keys.service, ELSIE_MEMORY_ANCHORS_LABEL] = json.dumps(changed)
    with pytest.raises(KeyStoreError, match="memory_anchor_invalid"):
        anchors.provision()
    backend.values[keys.service, ELSIE_MEMORY_ANCHORS_LABEL] = original
    backend.delete_password(keys.service, PRIVACY_KEY_LABEL)
    with pytest.raises(KeyStoreError, match="memory_anchor_invalid"):
        anchors.provision()


def test_existing_native_heads_are_not_migrated_or_bypassed(setup):
    backend, keys, anchors = setup
    backend.values[keys.service, _anchor_label(head().journal_id)] = _encode_anchor(head().to_bytes())
    anchors.provision()
    assert anchors.load(head().journal_id) == head().to_bytes()
    with pytest.raises(KeyStoreError, match="credential_registry_unavailable"):
        write(anchors, head(2), head())
    assert anchors.status()["memory_heads"] == 0


def test_native_registry_path_remains_compatible(setup):
    backend, keys, anchors = setup
    # The test-only registry initializer requires a genuinely fresh namespace.
    backend.values.clear()
    keys.initialize_fresh_credential_registry()
    keys.get_or_create(PRIVACY_KEY_LABEL, length=32)
    assert write(anchors, head())
    anchors.provision()
    assert write(anchors, head(2), head())
    legacy = backend.values[keys.service, _anchor_label(head().journal_id)]
    assert _decode_anchor(legacy) == head(2).to_bytes()
    assert anchors.status()["memory_heads"] == 0
    snapshot = dict(keys.complete_inventory_snapshot())
    assert snapshot[ELSIE_MEMORY_ANCHORS_LABEL] is not None


def test_provision_cannot_authorize_native_anchor_write(setup):
    _backend, keys, anchors = setup
    anchors.provision()
    with pytest.raises(KeyStoreError, match="credential_registry_unavailable"):
        write(GraceReceiptAnchorStore(keys), head())


def test_failed_keyring_readback_stays_failed(setup, monkeypatch):
    backend, _keys, anchors = setup
    monkeypatch.setattr(backend, "set_password", lambda *_: None)
    with pytest.raises(KeyStoreError, match="anchor_write_lost"):
        anchors.provision()


def test_real_protected_startup_and_restart_without_native_registry(setup, config_dir):
    from algo_cli import ada_task_ledger as task_ledger
    from algo_cli.config import Config
    from algo_cli.elsie_echo_preflight import prepare_echo_auxiliary_state

    _backend, keys, anchors = setup
    anchors.provision()
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    task_ledger.save_goal(task_ledger.GoalRecord(goal="test migration"))
    assert prepare_echo_auxiliary_state(cfg, receipt_key_store=keys)["protected"] is True
    assert anchors.status()["memory_heads"] >= 1
    assert prepare_echo_auxiliary_state(cfg, receipt_key_store=keys)["protected"] is True
    assert keys.complete_inventory_snapshot() is None


def test_deleted_anchor_bundle_does_not_reset_committed_store(setup, config_dir):
    from algo_cli import ada_task_ledger as task_ledger
    from algo_cli.config import Config
    from algo_cli.elsie_echo_preflight import EchoAuxiliaryPreflightError, prepare_echo_auxiliary_state

    backend, keys, anchors = setup
    anchors.provision()
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    task_ledger.save_goal(task_ledger.GoalRecord(goal="test migration"))
    prepare_echo_auxiliary_state(cfg, receipt_key_store=keys)
    backend.delete_password(keys.service, ELSIE_MEMORY_ANCHORS_LABEL)
    anchors.provision()
    with pytest.raises(EchoAuxiliaryPreflightError):
        prepare_echo_auxiliary_state(cfg, receipt_key_store=keys)


def test_memory_config_command_preserves_secrets_and_never_calls_native_initializer(setup, monkeypatch):
    from algo_cli import cli_config, grace_key_store

    backend, keys, anchors = setup
    # Patch only construction at the command boundary, retaining its exact type
    # for the production anchor-store check.
    original_init = grace_key_store.KeyringKeyStore.__init__

    def initialize(instance, *args, **kwargs):
        original_init(instance, backend, lease_manager=keys._leases)

    monkeypatch.setattr(grace_key_store.KeyringKeyStore, "__init__", initialize)
    monkeypatch.setattr(
        grace_key_store.KeyringKeyStore,
        "initialize_fresh_credential_registry",
        lambda *_: pytest.fail("native initialization"),
    )
    before = backend.values[keys.service, PRIVACY_KEY_LABEL]
    with cli_config.console.capture() as captured:
        assert cli_config.run(["memory", "status"]) == 1
        assert cli_config.run(["memory", "provision"]) == 0
        assert cli_config.run(["memory", "status"]) == 0
    assert before not in captured.get()
    assert before == backend.values[keys.service, PRIVACY_KEY_LABEL]
    assert anchors.status()["provisioned"] is True
