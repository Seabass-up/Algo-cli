from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading

import pytest

from algo_cli.ada_uninstall_recovery import (
    AdaUninstallRecoveryError,
    AdaUninstallRecoveryRecord,
    AdaUninstallRecoveryStore,
)
from algo_cli.david_control_kernel import ControlSigner


NOW_MS = 1_800_000_000_000
INSTALL_ID = "00000000-0000-4000-8000-000000000701"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _signer() -> ControlSigner:
    return ControlSigner.from_private_bytes(bytes(range(32)))


def _record(
    signer: ControlSigner,
    *,
    plan_digest: str = DIGEST_B,
) -> AdaUninstallRecoveryRecord:
    return AdaUninstallRecoveryRecord.authorize(
        install_id=INSTALL_ID,
        inventory_digest=DIGEST_A,
        plan_digest=plan_digest,
        mode="purge_private_state",
        present_entry_ids=(DIGEST_A, DIGEST_B),
        present_credential_ids=(DIGEST_C,),
        launch_agent_state="loaded",
        created_at_ms=NOW_MS,
        signer=signer,
    )


def _store(tmp_path: Path) -> AdaUninstallRecoveryStore:
    parent = tmp_path / "AdaState"
    parent.mkdir(mode=0o700, parents=True)
    parent.chmod(0o700)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return AdaUninstallRecoveryStore(parent / "AdaUninstallRecovery.json", uid=uid)


def test_authorization_is_canonical_signed_content_free_and_context_bound() -> None:
    signer = _signer()
    record = _record(signer)

    record.verify(signer.verifier)
    record.verify_context(
        install_id=INSTALL_ID,
        inventory_digest=DIGEST_A,
        mode="purge_private_state",
    )
    assert AdaUninstallRecoveryRecord.from_bytes(record.to_bytes()) == record
    assert record.phase == "authorized"
    assert record.revision == 1
    assert record.terminal_receipt == {}
    assert bytes(range(32)).hex() not in record.to_bytes().decode("utf-8")

    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_inventory"):
        record.verify_context(
            install_id=INSTALL_ID,
            inventory_digest=DIGEST_C,
            mode="purge_private_state",
        )


def test_tamper_noncanonical_schema_order_and_wrong_authority_fail_closed() -> None:
    signer = _signer()
    record = _record(signer)
    value = record.to_dict()
    value["plan_digest"] = DIGEST_C
    tampered = AdaUninstallRecoveryRecord.from_dict(value)
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_signature"):
        tampered.verify(signer.verifier)

    noncanonical = json.dumps(record.to_dict(), indent=2).encode("utf-8")
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_noncanonical"):
        AdaUninstallRecoveryRecord.from_bytes(noncanonical)

    value = record.to_dict()
    value["present_entry_ids"] = [DIGEST_B, DIGEST_A]
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_entries"):
        AdaUninstallRecoveryRecord.from_dict(value)

    value = record.to_dict()
    value["extra"] = True
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_schema"):
        AdaUninstallRecoveryRecord.from_dict(value)

    value = record.to_dict()
    value["mode"] = []
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_mode"):
        AdaUninstallRecoveryRecord.from_dict(value)

    value = record.to_dict()
    value["schema_version"] = True
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_version"):
        AdaUninstallRecoveryRecord.from_dict(value)

    other = ControlSigner.from_private_bytes(bytes(reversed(range(32))))
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_authority"):
        record.verify(other.verifier)


def test_terminal_transition_is_monotonic_and_independently_signed() -> None:
    signer = _signer()
    authorized = _record(signer)
    receipt = {
        "authority_key_id": signer.key_id,
        "outcome": "completed",
        "signature": "A" * 86,
    }
    terminal = authorized.complete(
        terminal_receipt=receipt,
        updated_at_ms=NOW_MS + 1,
        signer=signer,
    )

    terminal.verify(signer.verifier)
    assert terminal.phase == "terminal"
    assert terminal.revision == 2
    assert terminal.terminal_receipt == receipt
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_transition"):
        terminal.complete(
            terminal_receipt=receipt,
            updated_at_ms=NOW_MS + 2,
            signer=signer,
        )

    commit_ready = authorized.prepare_commit(
        terminal_receipt=receipt,
        updated_at_ms=NOW_MS + 1,
        signer=signer,
    )
    commit_ready.verify(signer.verifier)
    assert commit_ready.phase == "commit_ready"
    assert commit_ready.revision == 2
    assert commit_ready.terminal_receipt == receipt
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_transition"):
        commit_ready.prepare_commit(
            terminal_receipt=receipt,
            updated_at_ms=NOW_MS + 2,
            signer=signer,
        )

    runtime = AdaUninstallRecoveryRecord.authorize(
        install_id=INSTALL_ID,
        inventory_digest=DIGEST_A,
        plan_digest=DIGEST_B,
        mode="runtime_only",
        present_entry_ids=(DIGEST_A,),
        present_credential_ids=(),
        launch_agent_state="absent",
        created_at_ms=NOW_MS,
        signer=signer,
    )
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_transition"):
        runtime.prepare_commit(
            terminal_receipt=receipt,
            updated_at_ms=NOW_MS + 1,
            signer=signer,
        )


def test_store_is_owner_only_atomic_idempotent_and_transition_bounded(tmp_path: Path) -> None:
    signer = _signer()
    store = _store(tmp_path)
    authorized = _record(signer)

    assert store.load() is None
    assert not (store.path.parent / ".AdaUninstallRecovery.lock").exists()
    assert store.publish(authorized, verifier=signer.verifier) == authorized
    assert store.publish(authorized, verifier=signer.verifier) == authorized
    assert store.load() == authorized
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (store.path.parent / ".AdaUninstallRecovery.lock").stat().st_mode
    ) == 0o600

    terminal = authorized.complete(
        terminal_receipt={
            "authority_key_id": signer.key_id,
            "outcome": "completed",
            "signature": "A" * 86,
        },
        updated_at_ms=NOW_MS + 1,
        signer=signer,
    )
    assert store.publish(terminal, verifier=signer.verifier) == terminal
    assert store.load() == terminal

    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_conflict"):
        store.publish(_record(signer, plan_digest=DIGEST_C), verifier=signer.verifier)

    empty_store = _store(tmp_path / "terminal-first")
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_conflict"):
        empty_store.publish(terminal, verifier=signer.verifier)

    commit_store = _store(tmp_path / "commit-ready")
    assert commit_store.publish(authorized, verifier=signer.verifier) == authorized
    commit_ready = authorized.prepare_commit(
        terminal_receipt={
            "authority_key_id": signer.key_id,
            "outcome": "completed",
            "signature": "A" * 86,
        },
        updated_at_ms=NOW_MS + 1,
        signer=signer,
    )
    assert (
        commit_store.publish(commit_ready, verifier=signer.verifier)
        == commit_ready
    )
    assert commit_store.load() == commit_ready


def test_store_rejects_symlink_hardlink_permissions_and_corruption(tmp_path: Path) -> None:
    signer = _signer()
    store = _store(tmp_path)
    store.publish(_record(signer), verifier=signer.verifier)

    real = store.path
    linked = store.path.with_name("AdaLinked.json")
    os.link(real, linked)
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_file"):
        store.load()
    linked.unlink()
    real.unlink()

    target = store.path.with_name("AdaTarget.json")
    target.write_bytes(b"{}")
    target.chmod(0o600)
    store.path.symlink_to(target)
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_read"):
        store.load()
    store.path.unlink()

    store.path.write_bytes(b"{}")
    store.path.chmod(0o644)
    with pytest.raises(AdaUninstallRecoveryError, match="uninstall_recovery_file"):
        store.load()


def test_concurrent_first_publication_has_one_winner_without_overwrite(
    tmp_path: Path,
) -> None:
    signer = _signer()
    store = _store(tmp_path)
    records = (_record(signer), _record(signer, plan_digest=DIGEST_C))
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def publish(record: AdaUninstallRecoveryRecord) -> None:
        barrier.wait()
        try:
            store.publish(record, verifier=signer.verifier)
        except AdaUninstallRecoveryError as exc:
            outcomes.append(exc.reason_code)
        else:
            outcomes.append("published")

    threads = [threading.Thread(target=publish, args=(record,)) for record in records]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["published", "uninstall_recovery_conflict"]
    assert store.load() in records
