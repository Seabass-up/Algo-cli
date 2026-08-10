from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from algo_cli import grace_memory_receipts as receipts
from algo_cli.grace_memory_receipts import (
    ElsieReceiptAuthority,
    ElsieReceiptError,
    LegacyArtifactClass,
    ReceiptNamespace,
    advance_elsie_store_anchor,
    elsie_staging_path,
    inventory_legacy_tree,
    legacy_config_selects_echo,
    publish_elsie_staged_file,
    read_pinned_legacy_artifact,
    require_elsie_store_anchor,
    sanitized_legacy_config,
)
from algo_cli.grace_key_store import KeyMaterial, KeyStoreError, StaticKeyStore
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL


def _authority(byte: bytes = b"a") -> ElsieReceiptAuthority:
    return ElsieReceiptAuthority.from_key_store(store=StaticKeyStore({PRIVACY_KEY_LABEL: byte * 32}))


def test_receipts_resist_yes_no_dictionary_and_are_canonical() -> None:
    authority = _authority()
    yes_receipt = authority.receipt(ReceiptNamespace.MEMORY_CANDIDATE, "yes")
    no_receipt = authority.receipt(ReceiptNamespace.MEMORY_CANDIDATE, "no")
    offline_dictionary = {hashlib.sha256(candidate.encode()).hexdigest(): candidate for candidate in ("yes", "no")}

    assert yes_receipt != no_receipt
    assert yes_receipt.removeprefix("hmac-sha256:") not in offline_dictionary
    assert authority.verify(ReceiptNamespace.MEMORY_CANDIDATE, "yes", yes_receipt)
    assert not authority.verify(ReceiptNamespace.MEMORY_CANDIDATE, "no", yes_receipt)
    assert authority.receipt(ReceiptNamespace.GOAL_HISTORY, {"b": 2, "a": 1}) == authority.receipt(
        ReceiptNamespace.GOAL_HISTORY, {"a": 1, "b": 2}
    )
    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(ElsieReceiptError, match="canonical JSON"):
        authority.receipt(ReceiptNamespace.SKILL_GOAL, recursive)


def test_receipt_namespaces_are_separate_and_binding_rejects_wrong_key() -> None:
    first = _authority(b"a")
    second = _authority(b"b")
    value = {"value": "same"}

    assert first.receipt(ReceiptNamespace.SKILL_GOAL, value) != first.receipt(ReceiptNamespace.GOAL_HISTORY, value)
    with pytest.raises(ElsieReceiptError, match="binding mismatch"):
        second.require_binding(first.binding.as_dict())


def test_store_receipt_and_external_head_are_monotonic_and_content_free() -> None:
    store = StaticKeyStore({PRIVACY_KEY_LABEL: b"a" * 32})
    authority = ElsieReceiptAuthority.from_key_store(store=store)
    subject = "/private/example/task-ledger.json"
    first = authority.store_receipt(
        ReceiptNamespace.GOAL_STORE,
        {"status": "running", "goal": "SECRET_GOAL_CANARY"},
    )
    second = authority.store_receipt(
        ReceiptNamespace.GOAL_STORE,
        {"status": "complete", "goal": "SECRET_GOAL_CANARY"},
    )

    advance_elsie_store_anchor(
        authority,
        ReceiptNamespace.GOAL_STORE,
        subject=subject,
        sequence=1,
        previous_store_receipt="",
        store_receipt=first,
    )
    advance_elsie_store_anchor(
        authority,
        ReceiptNamespace.GOAL_STORE,
        subject=subject,
        sequence=2,
        previous_store_receipt=first,
        store_receipt=second,
    )
    require_elsie_store_anchor(
        authority,
        ReceiptNamespace.GOAL_STORE,
        subject=subject,
        sequence=2,
        store_receipt=second,
    )
    with pytest.raises(ElsieReceiptError, match="rollback or rewrite"):
        require_elsie_store_anchor(
            authority,
            ReceiptNamespace.GOAL_STORE,
            subject=subject,
            sequence=1,
            store_receipt=first,
        )
    serialized = json.dumps({key: value.decode("utf-8") for key, value in store._anchors.items()})
    assert "SECRET_GOAL_CANARY" not in serialized
    assert subject not in serialized


def test_store_anchor_missing_read_is_nonmutating() -> None:
    store = StaticKeyStore({PRIVACY_KEY_LABEL: b"a" * 32})
    authority = ElsieReceiptAuthority.from_existing_key_store(store=store)
    before_keys = dict(store._keys)
    before_anchors = dict(store._anchors)

    with pytest.raises(ElsieReceiptError, match="rollback or rewrite"):
        require_elsie_store_anchor(
            authority,
            ReceiptNamespace.MEMORY_CANDIDATE_STORE,
            subject="/private/example/candidates.json",
            sequence=1,
            store_receipt=authority.store_receipt(
                ReceiptNamespace.MEMORY_CANDIDATE_STORE,
                {"accepted": []},
            ),
        )

    assert store._keys == before_keys
    assert store._anchors == before_anchors


def test_skill_history_store_has_distinct_receipt_and_anchor_domain() -> None:
    store = StaticKeyStore({PRIVACY_KEY_LABEL: b"a" * 32})
    authority = ElsieReceiptAuthority.from_key_store(store=store)
    value = {"runs": [], "sequence": 1}
    goal_receipt = authority.store_receipt(ReceiptNamespace.GOAL_STORE, value)
    skill_receipt = authority.store_receipt(
        ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
        value,
    )

    assert skill_receipt != goal_receipt
    advance_elsie_store_anchor(
        authority,
        ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
        subject="/private/example/protected_run_history.jsonl",
        sequence=1,
        previous_store_receipt="",
        store_receipt=skill_receipt,
    )
    require_elsie_store_anchor(
        authority,
        ReceiptNamespace.SKILL_RUN_HISTORY_STORE,
        subject="/private/example/protected_run_history.jsonl",
        sequence=1,
        store_receipt=skill_receipt,
    )
    encoded_anchors = b"\n".join(store._anchors.values())
    assert b"elsie-skill-run-history-store-v1" in encoded_anchors


def test_receipt_is_stable_across_processes(tmp_path: Path) -> None:
    expected = _authority().receipt(
        ReceiptNamespace.AGENT_THREAD_OUTPUT,
        {"answer": 42},
    )
    script = """
from algo_cli.grace_memory_receipts import ElsieReceiptAuthority, ReceiptNamespace
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL
authority = ElsieReceiptAuthority.from_key_store(
    store=StaticKeyStore({PRIVACY_KEY_LABEL: b'a' * 32})
)
print(authority.receipt(ReceiptNamespace.AGENT_THREAD_OUTPUT, {'answer': 42}))
"""
    observed = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        text=True,
    ).strip()

    assert observed == expected


def test_volatile_key_material_is_refused() -> None:
    class VolatileStore:
        def get_or_create(self, _label: str, *, length: int = 32) -> KeyMaterial:
            return KeyMaterial(b"v" * length, persistent=False, backend="volatile_process")

    with pytest.raises(ElsieReceiptError, match="persistent"):
        ElsieReceiptAuthority.from_key_store(store=VolatileStore())


def test_existing_only_lookup_never_calls_key_creation_when_missing() -> None:
    class MissingStore:
        def __init__(self) -> None:
            self.create_calls = 0

        def get_existing(self, _label: str, *, length: int = 32) -> KeyMaterial:
            raise KeyStoreError("missing")

        def get_or_create(self, _label: str, *, length: int = 32) -> KeyMaterial:
            self.create_calls += 1
            return KeyMaterial(b"x" * length, persistent=True, backend="static")

    store = MissingStore()
    with pytest.raises(ElsieReceiptError, match="unavailable"):
        ElsieReceiptAuthority.from_existing_key_store(store=store)
    assert store.create_calls == 0


def test_optional_existing_lookup_distinguishes_absence_from_backend_failure() -> None:
    absent = StaticKeyStore()
    assert ElsieReceiptAuthority.from_optional_existing_key_store(store=absent) is None
    assert absent._keys == {}

    class BrokenStore:
        def get_existing(self, _label: str, *, length: int = 32) -> KeyMaterial:
            raise KeyStoreError("OS keyring operation failed: unavailable")

    with pytest.raises(ElsieReceiptError, match="unavailable"):
        ElsieReceiptAuthority.from_optional_existing_key_store(store=BrokenStore())


def test_echo_legacy_inventory_never_approves_raw_copy_and_sanitizes_config(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    (root / "skill_quarantine").mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "echo_veil_enabled": True,
                "echo_veil_protection": "required",
                "model": "test-model",
                "theme": "nord",
                "session_summary": "SECRET_SUMMARY_CANARY",
                "context_state": {"secret": "SECRET_CONTEXT_CANARY"},
                "attempt_ledger": [{"args": "SECRET_ARGS_CANARY"}],
                "cwd": "local-project-root",
                "system": "SECRET_SYSTEM_CANARY",
                "skill_crystallize_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    (root / "memory.json").write_text('["SECRET_MEMORY_CANARY"]', encoding="utf-8")
    (root / "run_history.jsonl").write_text("SECRET_RUN_CANARY\n", encoding="utf-8")
    (root / "last-block-plan.md").write_text("SECRET_BLOCK_CANARY", encoding="utf-8")
    (root / "memory_candidate_state.json").write_text("{}", encoding="utf-8")
    (root / "skill_quarantine" / "candidate.json").write_text("SECRET_SKILL_CANARY", encoding="utf-8")

    assert legacy_config_selects_echo(root) is True
    inventory = inventory_legacy_tree(root)
    classifications = {item.relative_path: item.classification for item in inventory.artifacts}
    assert inventory.safe_automatic_copy_paths == ()
    assert classifications["memory.json"] == LegacyArtifactClass.MEMORY
    assert classifications["run_history.jsonl"] == LegacyArtifactClass.RUN_HISTORY
    assert classifications["last-block-plan.md"] == LegacyArtifactClass.LAST_BLOCK
    assert classifications["memory_candidate_state.json"] == LegacyArtifactClass.CANDIDATE_STATE
    assert classifications["skill_quarantine/candidate.json"] == LegacyArtifactClass.DERIVED_SKILL

    sanitized = sanitized_legacy_config(root)
    serialized = json.dumps(sanitized, sort_keys=True)
    assert sanitized["echo_veil_protection"] == "required"
    assert sanitized["skill_crystallize_enabled"] is False
    assert sanitized["model"] == "test-model"
    for canary in (
        "SECRET_SUMMARY_CANARY",
        "SECRET_CONTEXT_CANARY",
        "SECRET_ARGS_CANARY",
        "SECRET_SYSTEM_CANARY",
        "local-project-root",
    ):
        assert canary not in serialized


def test_unprotected_inventory_requires_pinned_approved_reads(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir()
    source = b'{"echo_veil_enabled": false}'
    (root / "config.json").write_bytes(source)
    (root / "unknown.bin").write_bytes(b"no")
    inventory = inventory_legacy_tree(root, echo_selected=False)
    config_artifact = next(item for item in inventory.artifacts if item.relative_path == "config.json")
    unknown_artifact = next(item for item in inventory.artifacts if item.relative_path == "unknown.bin")

    assert read_pinned_legacy_artifact(root, config_artifact) == source
    with pytest.raises(ElsieReceiptError, match="not approved"):
        read_pinned_legacy_artifact(root, unknown_artifact)


def test_symlink_legacy_root_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "config.json").write_text("{}", encoding="utf-8")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ElsieReceiptError, match="real directory"):
        inventory_legacy_tree(linked)


def test_fifo_legacy_config_is_rejected_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO unavailable")
    root = tmp_path / "legacy"
    root.mkdir()
    os.mkfifo(root / "config.json")
    script = """
from pathlib import Path
from algo_cli.grace_memory_receipts import legacy_config_selects_echo
print('protected' if legacy_config_selects_echo(Path(__import__('sys').argv[1])) else 'unprotected')
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )

    assert completed.stdout.strip() == "protected"


def test_staged_publication_binds_exact_payload_and_inode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "state.json"
    stage = elsie_staging_path(target)
    expected = b'{"sequence":2,"receipt":"expected"}'
    replay = b'{"sequence":1,"receipt":"replayed"}'
    stage.write_bytes(expected)
    original_replace = os.replace

    def swap_before_publish(source, destination):
        if Path(source) == stage and Path(destination) == target:
            replacement = tmp_path / "replacement.json"
            replacement.write_bytes(replay)
            original_replace(replacement, stage)
        original_replace(source, destination)

    monkeypatch.setattr(receipts.os, "replace", swap_before_publish)

    with pytest.raises(ElsieReceiptError, match="changed identity"):
        publish_elsie_staged_file(
            stage,
            target,
            expected_payload=expected,
        )

    assert target.read_bytes() == replay


def test_staged_publication_rejects_wrong_expected_bytes_before_replace(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    stage = elsie_staging_path(target)
    stage.write_bytes(b"actual")

    with pytest.raises(ElsieReceiptError, match="payload changed"):
        publish_elsie_staged_file(
            stage,
            target,
            expected_payload=b"alterd",
        )

    assert stage.read_bytes() == b"actual"
    assert not target.exists()
