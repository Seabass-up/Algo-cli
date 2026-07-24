from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from algo_cli.ada_control_journal import (
    CONTROL_RECEIPT_SCHEMA,
    EMPTY_EVIDENCE_DIGEST,
    EMPTY_RECEIPT_HEAD_DIGEST,
    ControlEffectState,
    ControlJournal,
    ControlJournalCorrupt,
    ControlJournalError,
    ControlJournalRejected,
    RevocationKind,
    verify_control_receipt,
)
from algo_cli.david_control_kernel import (
    AuthorityRejected,
    ControlDataClass,
    ControlEnvelope,
    ControlRoute,
    ControlSigner,
    ControlVerifier,
    Operation,
    PermitRejected,
    SnapshotRef,
    TargetKind,
    canonical_json_bytes,
    content_digest,
    default_control_policy,
    issue_grant,
    issue_permit,
)


NOW_MS = 1_800_000_000_000


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthorityFixture:
    signer: ControlSigner
    grant: Any
    target_id: str
    policy: Any
    maximum_action_count: int

    def envelope(
        self,
        sequence: int,
        *,
        serial: int | None = None,
        session_serial: int = 1,
        target_epoch: int = 7,
        target_revision: str = "document-4",
        fencing_token: int = 11,
        operation: Operation = Operation.ACTIVATE,
        text: str = "bounded input",
        permit_lifetime_ms: int = 2_000,
    ) -> ControlEnvelope:
        number = serial if serial is not None else sequence
        issued_at = NOW_MS + sequence * 10
        if operation is Operation.INPUT_TEXT:
            arguments: dict[str, Any] = {
                "element_id": _opaque("field"),
                "replace": True,
                "text": text,
            }
            data_class = ControlDataClass.PRIVATE
        else:
            arguments = {"element_id": _opaque("button")}
            data_class = ControlDataClass.STRUCTURAL
        request = {
            "schema_version": 1,
            "request_id": _uuid(1_000 + number),
            "session_id": _uuid(2_000 + session_serial),
            "subject_id": "runtime.operator",
            "sequence": sequence,
            "issued_at_ms": issued_at - 5,
            "deadline_ms": issued_at + 5_000,
            "target": {
                "kind": TargetKind.BROWSER_DOCUMENT.value,
                "target_id": self.target_id,
                "epoch": target_epoch,
                "revision": target_revision,
                "fencing_token": fencing_token,
            },
            "snapshot": {
                "snapshot_id": _uuid(3_000 + number),
                "target_id": self.target_id,
                "epoch": target_epoch,
                "revision": target_revision,
                "fencing_token": fencing_token,
                "observed_at_ms": issued_at - 2,
                "sequence": sequence,
            },
            "operation": operation.value,
            "data_class": data_class.value,
            "arguments": arguments,
            "requested_routes": [ControlRoute.CONNECTOR.value],
            "max_output_bytes": 4096,
        }
        from algo_cli.david_control_kernel import ControlRequest

        parsed = ControlRequest.from_dict(request)
        permit = issue_permit(
            self.signer,
            self.signer.verifier,
            self.policy,
            self.grant,
            parsed,
            permit_id=_uuid(4_000 + number),
            issued_at_ms=issued_at,
            expires_at_ms=issued_at + permit_lifetime_ms,
        )
        return ControlEnvelope(parsed, self.grant, permit)


def _authority(maximum_action_count: int = 8) -> AuthorityFixture:
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    policy = default_control_policy()
    target_id = _opaque("journal-target")
    grant = issue_grant(
        signer,
        policy,
        grant_id=_uuid(10),
        subject_id="runtime.operator",
        target_ids=(target_id,),
        target_kinds=(TargetKind.BROWSER_DOCUMENT,),
        operations=(Operation.ACTIVATE, Operation.INPUT_TEXT),
        data_classes=(ControlDataClass.STRUCTURAL, ControlDataClass.PRIVATE),
        routes=(ControlRoute.CONNECTOR,),
        issued_at_ms=NOW_MS - 1_000,
        expires_at_ms=NOW_MS + 100_000,
        maximum_action_count=maximum_action_count,
        max_input_bytes=policy.max_input_bytes,
        max_output_bytes=policy.max_output_bytes,
        max_transmit_bytes=0,
    )
    return AuthorityFixture(signer, grant, target_id, policy, maximum_action_count)


def _journal(tmp_path: Path) -> ControlJournal:
    return ControlJournal(tmp_path / "private" / "ada-control.sqlite3")


class MemoryAnchorStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.fail_next_write = False
        self._lock = threading.Lock()

    def load(self, journal_id: str) -> bytes | None:
        with self._lock:
            value = self.values.get(journal_id)
            return None if value is None else bytes(value)

    def compare_and_set(
        self,
        journal_id: str,
        *,
        expected_digest: str | None,
        value: bytes,
    ) -> bool:
        with self._lock:
            if self.fail_next_write:
                self.fail_next_write = False
                raise RuntimeError("simulated unavailable anchor")
            current = self.values.get(journal_id)
            actual = None if current is None else "sha256:" + hashlib.sha256(current).hexdigest()
            if actual != expected_digest:
                return False
            self.values[journal_id] = bytes(value)
            return True


class AdvancingAnchorStore(MemoryAnchorStore):
    def __init__(self) -> None:
        super().__init__()
        self.on_next_load: Any = None

    def load(self, journal_id: str) -> bytes | None:
        callback = self.on_next_load
        self.on_next_load = None
        if callback is not None:
            callback()
        return super().load(journal_id)


def _claim(
    journal: ControlJournal,
    fixture: AuthorityFixture,
    envelope: ControlEnvelope,
    *,
    now_ms: int | None = None,
):
    return journal.claim(
        envelope,
        ControlRoute.CONNECTOR,
        verifier=fixture.signer.verifier,
        policy=fixture.policy,
        live_snapshot=envelope.request.snapshot,
        now_ms=now_ms if now_ms is not None else envelope.permit.issued_at_ms + 1,
    )


def _parallel_claim(
    path: str,
    envelope_value: dict[str, Any],
    public_key: bytes,
    results: Any,
) -> None:
    journal = ControlJournal(path)
    envelope = ControlEnvelope.from_dict(envelope_value)
    try:
        record = journal.claim(
            envelope,
            ControlRoute.CONNECTOR,
            verifier=ControlVerifier.from_public_bytes(public_key),
            policy=default_control_policy(),
            live_snapshot=envelope.request.snapshot,
            now_ms=envelope.permit.issued_at_ms + 1,
        )
    except ControlJournalRejected as exc:
        results.put(exc.reason_code)
    else:
        results.put(record.state.value)


def test_private_sqlite_configuration_and_closed_schema(tmp_path) -> None:
    journal = _journal(tmp_path)
    path = journal.path
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA application_id").fetchone()[0] == 0x414C474F
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    finally:
        connection.close()
    assert tables == {
        "ada_identity",
        "ada_grants",
        "ada_permits",
        "ada_revocations",
        "ada_sessions",
        "ada_targets",
        "ada_effects",
        "ada_receipts",
    }


def test_path_symlink_hardlink_permission_and_relative_path_reject(tmp_path) -> None:
    with pytest.raises(ControlJournalError, match="journal_path"):
        ControlJournal(Path("relative.sqlite3"))

    if os.name != "posix":
        return

    real = tmp_path / "real.sqlite3"
    real.write_bytes(b"")
    real.chmod(0o600)
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(real)
    with pytest.raises(ControlJournalError, match="journal_symlink"):
        ControlJournal(link)

    hard = tmp_path / "hard.sqlite3"
    os.link(real, hard)
    with pytest.raises(ControlJournalError, match="journal_hardlink"):
        ControlJournal(hard)

    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(ControlJournalError, match="journal_directory_mode"):
        ControlJournal(public / "ada.sqlite3")


def test_unlinked_sqlite_sidecar_race_is_not_misclassified_as_hardlink(
    tmp_path,
    monkeypatch,
) -> None:
    journal = _journal(tmp_path)
    original_lstat = Path.lstat
    durable = journal.path.lstat()

    def zero_link_sidecar(path: Path):
        if str(path).endswith("-wal"):
            return SimpleNamespace(
                st_mode=durable.st_mode,
                st_nlink=0,
                st_uid=durable.st_uid,
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", zero_link_sidecar)
    journal._validate_sidecars()


def test_hardlinked_sqlite_sidecar_still_fails_closed(tmp_path, monkeypatch) -> None:
    journal = _journal(tmp_path)
    original_lstat = Path.lstat
    durable = journal.path.lstat()

    def hardlinked_sidecar(path: Path):
        if str(path).endswith("-wal"):
            return SimpleNamespace(
                st_mode=durable.st_mode,
                st_nlink=2,
                st_uid=durable.st_uid,
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", hardlinked_sidecar)
    with pytest.raises(ControlJournalError, match="journal_sidecar_hardlink"):
        journal._validate_sidecars()


def test_claim_atomically_consumes_permit_usage_sequence_and_target(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    envelope = fixture.envelope(1)
    record = _claim(journal, fixture, envelope)

    assert record.state is ControlEffectState.PREPARED
    assert record.request_digest == envelope.request.digest
    assert record.target_id == fixture.target_id
    assert journal.grant_usage(fixture.grant.grant_id) == (1, 8)
    assert journal.by_permit(envelope.permit.permit_id) == record

    with pytest.raises(ControlJournalRejected, match="permit_replayed"):
        _claim(journal, fixture, envelope)
    assert journal.grant_usage(fixture.grant.grant_id) == (1, 8)


def test_same_permit_race_has_exactly_one_winner(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    envelope = fixture.envelope(1)
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_parallel_claim,
            args=(
                str(journal.path),
                envelope.to_dict(),
                fixture.signer.verifier.public_bytes,
                results,
            ),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        assert process.exitcode == 0
    assert sorted(results.get(timeout=2) for _ in processes) == [
        "permit_replayed",
        "prepared",
    ]
    assert journal.grant_usage(fixture.grant.grant_id) == (1, 8)


def test_grant_action_limit_is_durable(tmp_path) -> None:
    fixture = _authority(maximum_action_count=1)
    journal = _journal(tmp_path)
    _claim(journal, fixture, fixture.envelope(1, serial=1, session_serial=1))

    with pytest.raises(ControlJournalRejected, match="grant_exhausted"):
        _claim(journal, fixture, fixture.envelope(1, serial=2, session_serial=2))
    assert journal.grant_usage(fixture.grant.grant_id) == (1, 1)


@pytest.mark.parametrize("kind", tuple(RevocationKind))
def test_preclaim_revocation_rejects_without_consumption(tmp_path, kind) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    envelope = fixture.envelope(1)
    object_id = fixture.grant.grant_id if kind is RevocationKind.GRANT else envelope.permit.permit_id
    journal.revoke(kind, object_id, revoked_at_ms=NOW_MS)

    with pytest.raises(ControlJournalRejected, match=f"{kind.value}_revoked"):
        _claim(journal, fixture, envelope)
    assert journal.grant_usage(fixture.grant.grant_id) == (0, 0)
    assert journal.is_revoked(kind, object_id) is True


def test_revocation_after_claim_blocks_started_transition(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    envelope = fixture.envelope(1)
    record = _claim(journal, fixture, envelope)
    journal.revoke(
        RevocationKind.PERMIT,
        envelope.permit.permit_id,
        revoked_at_ms=record.updated_at_ms + 1,
    )

    with pytest.raises(ControlJournalRejected, match="permit_revoked"):
        journal.transition(
            record.effect_id,
            ControlEffectState.STARTED,
            now_ms=record.updated_at_ms + 2,
            reason_code="none",
        )
    failed = journal.transition(
        record.effect_id,
        ControlEffectState.FAILED,
        now_ms=record.updated_at_ms + 3,
        reason_code="permit_revoked",
    )
    assert failed.state is ControlEffectState.FAILED


def test_session_sequence_and_target_failures_roll_back_all_counters(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    first = fixture.envelope(
        1,
        serial=1,
        target_epoch=8,
        target_revision="document-5",
        fencing_token=12,
    )
    _claim(journal, fixture, first)

    stale = fixture.envelope(
        2,
        serial=2,
        target_epoch=7,
        target_revision="document-4",
        fencing_token=11,
    )
    with pytest.raises(ControlJournalRejected, match="target_epoch_stale"):
        _claim(journal, fixture, stale)
    assert journal.grant_usage(fixture.grant.grant_id) == (1, 8)
    assert journal.by_permit(stale.permit.permit_id) is None

    valid = fixture.envelope(
        2,
        serial=3,
        target_epoch=9,
        target_revision="document-6",
        fencing_token=13,
    )
    _claim(journal, fixture, valid)
    assert journal.grant_usage(fixture.grant.grant_id) == (2, 8)


@pytest.mark.parametrize(
    ("epoch", "revision", "fence", "reason"),
    [
        (7, "document-other", 11, "target_revision_changed"),
        (8, "document-5", 11, "target_fence_not_advanced"),
        (7, "document-4", 10, "target_fence_stale"),
    ],
)
def test_target_generation_and_fence_rules_reject_aba(tmp_path, epoch, revision, fence, reason) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    _claim(journal, fixture, fixture.envelope(1, serial=1))
    candidate = fixture.envelope(
        1,
        serial=2,
        session_serial=2,
        target_epoch=epoch,
        target_revision=revision,
        fencing_token=fence,
    )
    with pytest.raises(ControlJournalRejected, match=reason):
        _claim(journal, fixture, candidate)


def test_session_must_start_at_one_and_advance_exactly(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    with pytest.raises(ControlJournalRejected, match="session_sequence_start"):
        _claim(journal, fixture, fixture.envelope(2, serial=1))
    _claim(journal, fixture, fixture.envelope(1, serial=2))
    with pytest.raises(ControlJournalRejected, match="session_sequence"):
        _claim(journal, fixture, fixture.envelope(3, serial=3))


def test_signed_authority_policy_snapshot_and_route_are_revalidated(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    envelope = fixture.envelope(1)
    attacker = ControlSigner.generate()
    unsigned = envelope.permit.unsigned_dict()
    forged_permit = type(envelope.permit).from_dict(
        {**unsigned, "signature": attacker.sign("control_permit", unsigned)}
    )
    with pytest.raises(AuthorityRejected):
        journal.claim(
            ControlEnvelope(envelope.request, envelope.grant, forged_permit),
            ControlRoute.CONNECTOR,
            verifier=fixture.signer.verifier,
            policy=fixture.policy,
            live_snapshot=envelope.request.snapshot,
            now_ms=envelope.permit.issued_at_ms + 1,
        )

    changed_snapshot = envelope.request.snapshot.to_dict()
    changed_snapshot["fencing_token"] += 1
    with pytest.raises(PermitRejected, match="snapshot_changed"):
        journal.claim(
            envelope,
            ControlRoute.CONNECTOR,
            verifier=fixture.signer.verifier,
            policy=fixture.policy,
            live_snapshot=SnapshotRef.from_dict(changed_snapshot),
            now_ms=envelope.permit.issued_at_ms + 1,
        )
    assert journal.grant_usage(fixture.grant.grant_id) == (0, 0)


def test_expired_permit_rejects_before_durable_claim(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    envelope = fixture.envelope(1, permit_lifetime_ms=1)
    with pytest.raises(PermitRejected, match="permit_expired"):
        _claim(journal, fixture, envelope, now_ms=envelope.permit.expires_at_ms)
    assert journal.by_permit(envelope.permit.permit_id) is None


def test_transition_graph_and_terminal_states_are_fail_closed(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    prepared = _claim(journal, fixture, fixture.envelope(1))
    with pytest.raises(ControlJournalRejected, match="effect_transition"):
        journal.transition(
            prepared.effect_id,
            ControlEffectState.VERIFIED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="verified",
        )
    started = journal.transition(
        prepared.effect_id,
        ControlEffectState.STARTED,
        now_ms=prepared.updated_at_ms + 1,
        reason_code="none",
    )
    applied = journal.transition(
        started.effect_id,
        ControlEffectState.APPLIED,
        now_ms=started.updated_at_ms + 1,
        reason_code="none",
    )
    verified = journal.transition(
        applied.effect_id,
        ControlEffectState.VERIFIED,
        now_ms=applied.updated_at_ms + 1,
        reason_code="postcondition_verified",
        evidence_digest=content_digest({"verified": True}),
    )
    assert verified.transition_version == 4
    with pytest.raises(ControlJournalRejected, match="effect_transition"):
        journal.transition(
            verified.effect_id,
            ControlEffectState.FAILED,
            now_ms=verified.updated_at_ms + 1,
            reason_code="late_failure",
        )


def test_started_can_become_unknown_then_explicitly_reconciled(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    prepared = _claim(journal, fixture, fixture.envelope(1))
    started = journal.transition(
        prepared.effect_id,
        ControlEffectState.STARTED,
        now_ms=prepared.updated_at_ms + 1,
        reason_code="none",
    )
    unknown = journal.transition(
        started.effect_id,
        ControlEffectState.UNKNOWN,
        now_ms=started.updated_at_ms + 1,
        reason_code="adapter_disconnected",
    )
    first_receipt = journal.finalize_receipt(
        unknown.effect_id,
        fixture.signer,
        completed_at_ms=unknown.updated_at_ms + 1,
    )
    verified = journal.transition(
        unknown.effect_id,
        ControlEffectState.VERIFIED,
        now_ms=unknown.updated_at_ms + 2,
        reason_code="reconciled_applied",
        evidence_digest=content_digest({"effect": unknown.effect_id, "applied": True}),
    )
    second_receipt = journal.finalize_receipt(
        verified.effect_id,
        fixture.signer,
        completed_at_ms=verified.updated_at_ms + 1,
    )

    assert first_receipt.state is ControlEffectState.UNKNOWN
    assert second_receipt.state is ControlEffectState.VERIFIED
    assert first_receipt.receipt_id != second_receipt.receipt_id
    assert journal.receipts(verified.effect_id, fixture.signer.verifier) == (
        first_receipt,
        second_receipt,
    )


def test_receipt_is_idempotent_signed_and_bound_to_exact_transition(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    prepared = _claim(journal, fixture, fixture.envelope(1))
    failed = journal.transition(
        prepared.effect_id,
        ControlEffectState.FAILED,
        now_ms=prepared.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    first = journal.finalize_receipt(
        failed.effect_id,
        fixture.signer,
        completed_at_ms=failed.updated_at_ms + 1,
    )
    second = journal.finalize_receipt(
        failed.effect_id,
        fixture.signer,
        completed_at_ms=failed.updated_at_ms + 100,
    )

    assert first == second
    assert first.effect_id == failed.effect_id
    assert first.transition_version == failed.transition_version
    verify_control_receipt(first, fixture.signer.verifier)
    with pytest.raises(AuthorityRejected):
        verify_control_receipt(first, ControlSigner.generate().verifier)


def test_receipts_form_one_signed_journal_bound_sequence(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    receipts = []
    for sequence in (1, 2):
        prepared = _claim(journal, fixture, fixture.envelope(sequence))
        failed = journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="recovered_before_dispatch",
        )
        receipts.append(
            journal.finalize_receipt(
                failed.effect_id,
                fixture.signer,
                completed_at_ms=failed.updated_at_ms + 1,
            )
        )

    assert receipts[0].receipt_sequence == 1
    assert receipts[0].previous_receipt_digest == EMPTY_RECEIPT_HEAD_DIGEST
    assert receipts[1].receipt_sequence == 2
    assert receipts[1].journal_id == receipts[0].journal_id
    assert receipts[1].previous_receipt_digest == content_digest(receipts[0].to_dict())
    assert journal.receipt_sequence(fixture.signer.verifier) == tuple(receipts)
    assert journal.receipt_head(fixture.signer.verifier) == receipts[-1]


def test_receipt_sequence_detects_interior_deletion(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    receipts = []
    for sequence in (1, 2, 3):
        prepared = _claim(journal, fixture, fixture.envelope(sequence))
        failed = journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="recovered_before_dispatch",
        )
        receipts.append(
            journal.finalize_receipt(
                failed.effect_id,
                fixture.signer,
                completed_at_ms=failed.updated_at_ms + 1,
            )
        )

    direct = sqlite3.connect(journal.path)
    try:
        direct.execute(
            "DELETE FROM ada_receipts WHERE receipt_sequence = ?",
            (receipts[1].receipt_sequence,),
        )
        direct.commit()
    finally:
        direct.close()
    with pytest.raises(ControlJournalCorrupt, match="receipt_sequence"):
        journal.receipt_sequence(fixture.signer.verifier)


def test_externally_retained_signed_head_detects_tail_rollback(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    receipts = []
    for sequence in (1, 2):
        prepared = _claim(journal, fixture, fixture.envelope(sequence))
        failed = journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="recovered_before_dispatch",
        )
        receipts.append(
            journal.finalize_receipt(
                failed.effect_id,
                fixture.signer,
                completed_at_ms=failed.updated_at_ms + 1,
            )
        )

    direct = sqlite3.connect(journal.path)
    try:
        direct.execute("DELETE FROM ada_receipts WHERE receipt_sequence = 2")
        direct.commit()
    finally:
        direct.close()

    assert journal.receipt_sequence(fixture.signer.verifier) == (receipts[0],)
    with pytest.raises(ControlJournalCorrupt, match="receipt_head_rollback"):
        journal.receipt_sequence(
            fixture.signer.verifier,
            expected_head=receipts[-1],
        )


def test_external_anchor_is_created_advanced_and_verified_automatically(tmp_path) -> None:
    fixture = _authority()
    anchors = MemoryAnchorStore()
    path = tmp_path / "private" / "ada-anchored.sqlite3"
    journal = ControlJournal(path, receipt_anchor_store=anchors)
    receipts = []
    for sequence in (1, 2):
        prepared = _claim(journal, fixture, fixture.envelope(sequence))
        failed = journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="recovered_before_dispatch",
        )
        receipts.append(
            journal.finalize_receipt(
                failed.effect_id,
                fixture.signer,
                completed_at_ms=failed.updated_at_ms + 1,
            )
        )

    assert journal.receipt_anchor_configured is True
    assert len(anchors.values) == 1
    assert next(iter(anchors.values.values())) == canonical_json_bytes(receipts[-1].to_dict())
    reopened = ControlJournal(path, receipt_anchor_store=anchors)
    assert reopened.receipt_sequence(fixture.signer.verifier) == tuple(receipts)


def test_external_anchor_detects_local_tail_rollback_without_caller_head(tmp_path) -> None:
    fixture = _authority()
    anchors = MemoryAnchorStore()
    path = tmp_path / "private" / "ada-anchored.sqlite3"
    journal = ControlJournal(path, receipt_anchor_store=anchors)
    for sequence in (1, 2):
        prepared = _claim(journal, fixture, fixture.envelope(sequence))
        failed = journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="recovered_before_dispatch",
        )
        journal.finalize_receipt(
            failed.effect_id,
            fixture.signer,
            completed_at_ms=failed.updated_at_ms + 1,
        )

    direct = sqlite3.connect(path)
    try:
        direct.execute("DELETE FROM ada_receipts WHERE receipt_sequence = 2")
        direct.commit()
    finally:
        direct.close()
    with pytest.raises(ControlJournalCorrupt, match="receipt_head_rollback"):
        journal.receipt_sequence(fixture.signer.verifier)


def test_missing_external_anchor_with_existing_receipts_fails_closed(tmp_path) -> None:
    fixture = _authority()
    anchors = MemoryAnchorStore()
    path = tmp_path / "private" / "ada-anchored.sqlite3"
    journal = ControlJournal(path, receipt_anchor_store=anchors)
    prepared = _claim(journal, fixture, fixture.envelope(1))
    failed = journal.transition(
        prepared.effect_id,
        ControlEffectState.FAILED,
        now_ms=prepared.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    journal.finalize_receipt(
        failed.effect_id,
        fixture.signer,
        completed_at_ms=failed.updated_at_ms + 1,
    )
    anchors.values.clear()

    reopened = ControlJournal(path, receipt_anchor_store=anchors)
    with pytest.raises(ControlJournalCorrupt, match="receipt_anchor_missing"):
        reopened.synchronize_receipt_anchor(fixture.signer.verifier)


def test_anchor_write_failure_commits_receipt_but_safe_prefix_can_catch_up(tmp_path) -> None:
    fixture = _authority()
    anchors = MemoryAnchorStore()
    path = tmp_path / "private" / "ada-anchored.sqlite3"
    journal = ControlJournal(path, receipt_anchor_store=anchors)
    first = _claim(journal, fixture, fixture.envelope(1))
    first_failed = journal.transition(
        first.effect_id,
        ControlEffectState.FAILED,
        now_ms=first.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    journal.finalize_receipt(
        first_failed.effect_id,
        fixture.signer,
        completed_at_ms=first_failed.updated_at_ms + 1,
    )

    second = _claim(journal, fixture, fixture.envelope(2))
    second_failed = journal.transition(
        second.effect_id,
        ControlEffectState.FAILED,
        now_ms=second.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    anchors.fail_next_write = True
    with pytest.raises(ControlJournalError, match="receipt_anchor_unavailable"):
        journal.finalize_receipt(
            second_failed.effect_id,
            fixture.signer,
            completed_at_ms=second_failed.updated_at_ms + 1,
        )

    unanchored = ControlJournal(path)
    receipts = unanchored.receipt_sequence(fixture.signer.verifier)
    assert len(receipts) == 2
    reopened = ControlJournal(path, receipt_anchor_store=anchors)
    assert reopened.synchronize_receipt_anchor(fixture.signer.verifier) == receipts[-1]
    assert next(iter(anchors.values.values())) == canonical_json_bytes(receipts[-1].to_dict())


def test_anchor_ahead_of_stale_reader_refreshes_before_declaring_rollback(tmp_path) -> None:
    fixture = _authority()
    anchors = AdvancingAnchorStore()
    path = tmp_path / "private" / "ada-anchored.sqlite3"
    first_journal = ControlJournal(path, receipt_anchor_store=anchors)
    first = _claim(first_journal, fixture, fixture.envelope(1))
    first_failed = first_journal.transition(
        first.effect_id,
        ControlEffectState.FAILED,
        now_ms=first.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    first_journal.finalize_receipt(
        first_failed.effect_id,
        fixture.signer,
        completed_at_ms=first_failed.updated_at_ms + 1,
    )
    second_journal = ControlJournal(path)
    second_receipt = None

    def advance_other_process() -> None:
        nonlocal second_receipt
        second = _claim(second_journal, fixture, fixture.envelope(2))
        second_failed = second_journal.transition(
            second.effect_id,
            ControlEffectState.FAILED,
            now_ms=second.updated_at_ms + 1,
            reason_code="recovered_before_dispatch",
        )
        second_receipt = second_journal._finalize_receipt_unanchored(
            second_failed.effect_id,
            fixture.signer,
            completed_at_ms=second_failed.updated_at_ms + 1,
        )
        anchors.values[second_receipt.journal_id] = canonical_json_bytes(
            second_receipt.to_dict()
        )

    anchors.on_next_load = advance_other_process
    stale_snapshot = first_journal.receipt_sequence(fixture.signer.verifier)
    current = first_journal.receipt_sequence(fixture.signer.verifier)

    assert second_receipt is not None
    assert stale_snapshot[-1].receipt_sequence == 1
    assert current[-1] == second_receipt


def test_tampered_or_foreign_external_anchor_fails_closed(tmp_path) -> None:
    fixture = _authority()
    anchors = MemoryAnchorStore()
    path = tmp_path / "private" / "ada-anchored.sqlite3"
    journal = ControlJournal(path, receipt_anchor_store=anchors)
    prepared = _claim(journal, fixture, fixture.envelope(1))
    failed = journal.transition(
        prepared.effect_id,
        ControlEffectState.FAILED,
        now_ms=prepared.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    receipt = journal.finalize_receipt(
        failed.effect_id,
        fixture.signer,
        completed_at_ms=failed.updated_at_ms + 1,
    )
    journal_id = receipt.journal_id
    tampered = receipt.to_dict()
    tampered["reason_code"] = "tampered_reason"
    anchors.values[journal_id] = canonical_json_bytes(tampered)
    with pytest.raises(ControlJournalCorrupt, match="receipt_anchor_value"):
        journal.synchronize_receipt_anchor(fixture.signer.verifier)

    foreign = dict(receipt.to_dict())
    foreign["journal_id"] = "sha256:" + "f" * 64
    foreign["signature"] = fixture.signer.sign(
        "control_receipt",
        {key: value for key, value in foreign.items() if key != "signature"},
    )
    anchors.values[journal_id] = canonical_json_bytes(foreign)
    with pytest.raises(ControlJournalCorrupt, match="receipt_anchor_journal"):
        journal.synchronize_receipt_anchor(fixture.signer.verifier)


def test_signed_head_from_another_journal_cannot_anchor_this_journal(tmp_path) -> None:
    fixture = _authority()
    first = ControlJournal(tmp_path / "first" / "ada.sqlite3")
    second = ControlJournal(tmp_path / "second" / "ada.sqlite3")
    heads = []
    for journal in (first, second):
        prepared = _claim(journal, fixture, fixture.envelope(1))
        failed = journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="recovered_before_dispatch",
        )
        heads.append(
            journal.finalize_receipt(
                failed.effect_id,
                fixture.signer,
                completed_at_ms=failed.updated_at_ms + 1,
            )
        )

    assert heads[0].journal_id != heads[1].journal_id
    with pytest.raises(ControlJournalCorrupt, match="receipt_head_rollback"):
        first.receipt_sequence(fixture.signer.verifier, expected_head=heads[1])


def test_receipt_blob_tampering_is_detected(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    prepared = _claim(journal, fixture, fixture.envelope(1))
    failed = journal.transition(
        prepared.effect_id,
        ControlEffectState.FAILED,
        now_ms=prepared.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    receipt = journal.finalize_receipt(
        failed.effect_id,
        fixture.signer,
        completed_at_ms=failed.updated_at_ms + 1,
    )
    value = receipt.to_dict()
    value["reason_code"] = "tampered_reason"
    blob = canonical_json_bytes(value)
    direct = sqlite3.connect(journal.path)
    try:
        direct.execute(
            "UPDATE ada_receipts SET receipt_blob = ?, receipt_digest = ? WHERE effect_id = ?",
            (blob, content_digest(value), failed.effect_id),
        )
        direct.commit()
    finally:
        direct.close()
    with pytest.raises(AuthorityRejected, match="signature_invalid"):
        journal.receipts(failed.effect_id, fixture.signer.verifier)


def test_recovery_candidates_exclude_final_effects(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    first = _claim(journal, fixture, fixture.envelope(1, serial=1))
    second = _claim(journal, fixture, fixture.envelope(2, serial=2))
    journal.transition(
        second.effect_id,
        ControlEffectState.FAILED,
        now_ms=second.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    assert journal.recovery_candidates() == (first,)


def test_clock_regression_and_invalid_transition_metadata_reject(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    prepared = _claim(journal, fixture, fixture.envelope(1))
    with pytest.raises(ControlJournalRejected, match="clock_regression"):
        journal.transition(
            prepared.effect_id,
            ControlEffectState.STARTED,
            now_ms=prepared.updated_at_ms - 1,
            reason_code="none",
        )
    with pytest.raises(ControlJournalRejected, match="transition_reason"):
        journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="none",
        )
    with pytest.raises(ControlJournalRejected, match="invalid_evidence"):
        journal.transition(
            prepared.effect_id,
            ControlEffectState.FAILED,
            now_ms=prepared.updated_at_ms + 1,
            reason_code="safe_failure",
            evidence_digest="private evidence",
        )


def test_journal_never_persists_text_urls_selectors_or_paths(tmp_path) -> None:
    secret = "https://private.example/path?token=never-persist"
    fixture = _authority()
    journal = _journal(tmp_path)
    envelope = fixture.envelope(1, operation=Operation.INPUT_TEXT, text=secret)
    record = _claim(journal, fixture, envelope)
    journal.transition(
        record.effect_id,
        ControlEffectState.FAILED,
        now_ms=record.updated_at_ms + 1,
        reason_code="recovered_before_dispatch",
    )
    journal.checkpoint()

    persisted = b""
    for path in (
        journal.path,
        Path(str(journal.path) + "-wal"),
        Path(str(journal.path) + "-shm"),
    ):
        if path.exists():
            persisted += path.read_bytes()
    assert secret.encode("utf-8") not in persisted
    assert b"private.example" not in persisted
    assert b"element_id" not in persisted


def test_schema_version_extra_table_and_corrupt_row_fail_closed(tmp_path) -> None:
    fixture = _authority()
    journal = _journal(tmp_path)
    record = _claim(journal, fixture, fixture.envelope(1))
    direct = sqlite3.connect(journal.path)
    try:
        direct.execute(
            "UPDATE ada_effects SET reason_code = ? WHERE effect_id = ?",
            ("not a safe reason", record.effect_id),
        )
        direct.commit()
    finally:
        direct.close()
    with pytest.raises(ControlJournalCorrupt, match="reason_code"):
        journal.get(record.effect_id)

    other_path = tmp_path / "other" / "ada.sqlite3"
    other = ControlJournal(other_path)
    direct = sqlite3.connect(other.path)
    try:
        direct.execute("CREATE TABLE injected (value TEXT)")
        direct.commit()
    finally:
        direct.close()
    with pytest.raises(ControlJournalCorrupt, match="journal_schema"):
        ControlJournal(other.path)

    version_path = tmp_path / "version" / "ada.sqlite3"
    versioned = ControlJournal(version_path)
    direct = sqlite3.connect(versioned.path)
    try:
        direct.execute("PRAGMA user_version = 99")
    finally:
        direct.close()
    with pytest.raises(ControlJournalCorrupt, match="journal_version"):
        ControlJournal(versioned.path)


def test_receipt_schema_is_closed_and_json_serializable() -> None:
    assert CONTROL_RECEIPT_SCHEMA["additionalProperties"] is False
    assert set(CONTROL_RECEIPT_SCHEMA["required"]) == set(CONTROL_RECEIPT_SCHEMA["properties"])
    uuid_pattern = CONTROL_RECEIPT_SCHEMA["properties"]["receipt_id"]["pattern"]
    assert uuid_pattern.startswith("^") and uuid_pattern.endswith(r"(?![\s\S])")
    assert "{12}" in uuid_pattern
    json.dumps(CONTROL_RECEIPT_SCHEMA, allow_nan=False)
    assert EMPTY_EVIDENCE_DIGEST.startswith("sha256:")
    assert EMPTY_RECEIPT_HEAD_DIGEST.startswith("sha256:")
