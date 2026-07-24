from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

import pytest

from algo_cli.ada_control_journal import (
    ControlEffectState,
    ControlJournal,
    ControlJournalRejected,
    RevocationKind,
    verify_control_receipt,
)
from algo_cli.david_control_kernel import (
    ControlDataClass,
    ControlEnvelope,
    ControlRequest,
    ControlRoute,
    ControlSigner,
    Operation,
    SnapshotRef,
    TargetKind,
    canonical_json_bytes,
    default_control_policy,
    issue_grant,
    issue_permit,
)
from algo_cli.david_control_runtime import (
    AdapterDispatchResult,
    AdapterReconciliationResult,
    ControlCrashPoint,
    ControlRuntime,
    ControlRuntimeError,
    DispatchDisposition,
    ReconciliationDisposition,
    SimulatedControlCrash,
    create_anchored_control_runtime,
    fresh_postcondition_evidence,
    structural_evidence,
)


NOW_MS = 1_800_000_000_000


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RuntimeFixture:
    signer: ControlSigner
    policy: Any
    envelope: ControlEnvelope


def _fixture() -> RuntimeFixture:
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    policy = default_control_policy()
    target_id = _opaque("runtime-target")
    request = ControlRequest.from_dict(
        {
            "schema_version": 1,
            "request_id": _uuid(1),
            "session_id": _uuid(2),
            "subject_id": "runtime.operator",
            "sequence": 1,
            "issued_at_ms": NOW_MS - 5,
            "deadline_ms": NOW_MS + 20_000,
            "target": {
                "kind": TargetKind.BROWSER_DOCUMENT.value,
                "target_id": target_id,
                "epoch": 7,
                "revision": "document-4",
                "fencing_token": 11,
            },
            "snapshot": {
                "snapshot_id": _uuid(3),
                "target_id": target_id,
                "epoch": 7,
                "revision": "document-4",
                "fencing_token": 11,
                "observed_at_ms": NOW_MS - 2,
                "sequence": 1,
            },
            "operation": Operation.ACTIVATE.value,
            "data_class": ControlDataClass.STRUCTURAL.value,
            "arguments": {"element_id": _opaque("button")},
            "requested_routes": [
                ControlRoute.CONNECTOR.value,
                ControlRoute.DOM.value,
            ],
            "max_output_bytes": 4096,
        }
    )
    grant = issue_grant(
        signer,
        policy,
        grant_id=_uuid(4),
        subject_id=request.subject_id,
        target_ids=(target_id,),
        target_kinds=(TargetKind.BROWSER_DOCUMENT,),
        operations=(Operation.ACTIVATE,),
        data_classes=(ControlDataClass.STRUCTURAL,),
        routes=(ControlRoute.CONNECTOR, ControlRoute.DOM),
        issued_at_ms=NOW_MS - 1_000,
        expires_at_ms=NOW_MS + 30_000,
        maximum_action_count=4,
        max_input_bytes=policy.max_input_bytes,
        max_output_bytes=policy.max_output_bytes,
        max_transmit_bytes=0,
    )
    permit = issue_permit(
        signer,
        signer.verifier,
        policy,
        grant,
        request,
        permit_id=_uuid(5),
        issued_at_ms=NOW_MS,
        expires_at_ms=NOW_MS + 10_000,
    )
    return RuntimeFixture(signer, policy, ControlEnvelope(request, grant, permit))


class IncrementingClock:
    def __init__(self, start: int = NOW_MS + 1) -> None:
        self.value = start

    def __call__(self) -> int:
        current = self.value
        self.value += 1
        return current


class RuntimeAnchorStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def load(self, journal_id: str) -> bytes | None:
        return self.values.get(journal_id)

    def compare_and_set(
        self,
        journal_id: str,
        *,
        expected_digest: str | None,
        value: bytes,
    ) -> bool:
        current = self.values.get(journal_id)
        actual = None if current is None else "sha256:" + hashlib.sha256(current).hexdigest()
        if actual != expected_digest:
            return False
        self.values[journal_id] = bytes(value)
        return True


class SequenceClock:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)
        self.last = values[-1]

    def __call__(self) -> int:
        if self.values:
            self.last = self.values.pop(0)
        else:
            self.last += 1
        return self.last


class FiniteAdapter:
    def __init__(
        self,
        snapshot: SnapshotRef,
        *,
        dispatch_disposition: DispatchDisposition = DispatchDisposition.APPLIED,
        mutate_on_unknown: bool = False,
        raise_after_mutation: bool = False,
        invalid_dispatch: bool = False,
        reconciliation: ReconciliationDisposition | None = None,
        invalid_reconciliation: bool = False,
        raise_reconciliation: bool = False,
        route_sets: list[tuple[ControlRoute, ...]] | None = None,
        drift_after_first_snapshot: bool = False,
        revoke_on_second_snapshot: Any = None,
    ) -> None:
        self.snapshot = snapshot
        self.dispatch_disposition = dispatch_disposition
        self.mutate_on_unknown = mutate_on_unknown
        self.raise_after_mutation = raise_after_mutation
        self.invalid_dispatch = invalid_dispatch
        self.reconciliation = reconciliation
        self.invalid_reconciliation = invalid_reconciliation
        self.raise_reconciliation = raise_reconciliation
        self.route_sets = route_sets or [(ControlRoute.CONNECTOR, ControlRoute.DOM)]
        self.drift_after_first_snapshot = drift_after_first_snapshot
        self.revoke_on_second_snapshot = revoke_on_second_snapshot
        self.route_calls = 0
        self.snapshot_calls = 0
        self.dispatch_calls = 0
        self.reconcile_calls = 0
        self.mutation_count = 0
        self.effects: set[str] = set()
        self.postconditions: dict[str, SnapshotRef] = {}

    def available_routes(self, target):
        del target
        index = min(self.route_calls, len(self.route_sets) - 1)
        self.route_calls += 1
        return self.route_sets[index]

    def current_snapshot(self, target):
        del target
        self.snapshot_calls += 1
        if self.snapshot_calls == 2 and self.revoke_on_second_snapshot is not None:
            self.revoke_on_second_snapshot()
        if self.snapshot_calls > 1 and self.drift_after_first_snapshot:
            changed = self.snapshot.to_dict()
            changed["fencing_token"] += 1
            return SnapshotRef.from_dict(changed)
        return self.snapshot

    def dispatch(self, effect_id, request, route):
        self.dispatch_calls += 1
        if request.snapshot != self.snapshot:
            return AdapterDispatchResult(
                DispatchDisposition.REJECTED,
                "snapshot_changed",
                structural_evidence(effect_id, "snapshot_changed"),
            )
        assert route in {ControlRoute.CONNECTOR, ControlRoute.DOM}
        should_mutate = (
            self.dispatch_disposition is DispatchDisposition.APPLIED
            or (self.dispatch_disposition is DispatchDisposition.UNKNOWN and self.mutate_on_unknown)
            or self.raise_after_mutation
            or self.invalid_dispatch
        )
        if should_mutate and effect_id not in self.effects:
            self.effects.add(effect_id)
            self.mutation_count += 1
            changed = self.snapshot.to_dict()
            changed["snapshot_id"] = _uuid(900_000 + self.mutation_count)
            changed["observed_at_ms"] += self.mutation_count
            changed["sequence"] += self.mutation_count
            self.postconditions[effect_id] = SnapshotRef.from_dict(changed)
        if self.raise_after_mutation:
            raise TimeoutError("private adapter detail")
        if self.invalid_dispatch:
            return {"state": "applied"}
        if self.dispatch_disposition is DispatchDisposition.APPLIED:
            return AdapterDispatchResult(
                DispatchDisposition.APPLIED,
                "none",
                structural_evidence(effect_id, "dispatch_applied"),
            )
        if self.dispatch_disposition is DispatchDisposition.REJECTED:
            return AdapterDispatchResult(
                DispatchDisposition.REJECTED,
                "target_rejected",
                structural_evidence(effect_id, "target_rejected"),
            )
        return AdapterDispatchResult(
            DispatchDisposition.UNKNOWN,
            "adapter_uncertain",
            structural_evidence(effect_id, "adapter_uncertain"),
        )

    def reconcile(self, effect_id, request, route):
        del request, route
        self.reconcile_calls += 1
        if self.raise_reconciliation:
            raise TimeoutError("private reconciliation detail")
        if self.invalid_reconciliation:
            return {"state": "verified"}
        disposition = self.reconciliation
        if disposition is None:
            disposition = (
                ReconciliationDisposition.VERIFIED if effect_id in self.effects else ReconciliationDisposition.FAILED
            )
        reason = {
            ReconciliationDisposition.VERIFIED: "reconciled_applied",
            ReconciliationDisposition.FAILED: "reconciled_absent",
            ReconciliationDisposition.UNKNOWN: "reconciliation_uncertain",
        }[disposition]
        postcondition = (
            self.postconditions.get(effect_id)
            if disposition is ReconciliationDisposition.VERIFIED
            else None
        )
        evidence = (
            fresh_postcondition_evidence(effect_id, reason, postcondition)
            if postcondition is not None
            else structural_evidence(effect_id, reason)
        )
        return AdapterReconciliationResult(
            disposition,
            reason,
            evidence,
            postcondition,
        )


class NoTouchAdapter(FiniteAdapter):
    def available_routes(self, target):
        raise AssertionError("recovery consulted routes")

    def current_snapshot(self, target):
        raise AssertionError("recovery consulted snapshot")

    def dispatch(self, effect_id, request, route):
        raise AssertionError("recovery redispatched")

    def reconcile(self, effect_id, request, route):
        raise AssertionError("prepared recovery reconciled")


class UnprovenPostconditionAdapter(FiniteAdapter):
    def __init__(self, snapshot: SnapshotRef, mode: str) -> None:
        super().__init__(snapshot)
        self.mode = mode

    def reconcile(self, effect_id, request, route):
        del request, route
        self.reconcile_calls += 1
        reason = "reconciled_applied"
        fresh = self.postconditions[effect_id]
        if self.mode == "missing":
            return AdapterReconciliationResult(
                ReconciliationDisposition.VERIFIED,
                reason,
                structural_evidence(effect_id, reason),
            )
        if self.mode == "stale":
            postcondition = self.snapshot
        elif self.mode == "wrong_target":
            value = fresh.to_dict()
            value["target_id"] = _opaque("wrong-target")
            postcondition = SnapshotRef.from_dict(value)
        else:
            postcondition = fresh
        evidence = (
            structural_evidence(effect_id, reason)
            if self.mode == "wrong_evidence"
            else fresh_postcondition_evidence(effect_id, reason, postcondition)
        )
        return AdapterReconciliationResult(
            ReconciliationDisposition.VERIFIED,
            reason,
            evidence,
            postcondition,
        )


def _forged_dispatch_result() -> AdapterDispatchResult:
    result = object.__new__(AdapterDispatchResult)
    object.__setattr__(result, "disposition", DispatchDisposition.APPLIED)
    object.__setattr__(result, "reason_code", "forged_applied")
    object.__setattr__(result, "evidence_digest", structural_evidence(_uuid(91), "forged"))
    return result


def _forged_reconciliation_result() -> AdapterReconciliationResult:
    result = object.__new__(AdapterReconciliationResult)
    object.__setattr__(result, "disposition", ReconciliationDisposition.VERIFIED)
    object.__setattr__(result, "reason_code", "none")
    object.__setattr__(result, "evidence_digest", structural_evidence(_uuid(92), "forged"))
    return result


class ForgedDispatchAdapter(FiniteAdapter):
    def dispatch(self, effect_id, request, route):
        del request, route
        self.dispatch_calls += 1
        if effect_id not in self.effects:
            self.effects.add(effect_id)
            self.mutation_count += 1
        return _forged_dispatch_result()


class ForgedReconciliationAdapter(FiniteAdapter):
    def reconcile(self, effect_id, request, route):
        del effect_id, request, route
        self.reconcile_calls += 1
        return _forged_reconciliation_result()


def _runtime(tmp_path: Path, fixture: RuntimeFixture, clock=None):
    journal = ControlJournal(tmp_path / "private" / "ada-runtime.sqlite3")
    runtime = ControlRuntime(
        journal,
        fixture.signer.verifier,
        fixture.policy,
        fixture.signer,
        clock_ms=clock or IncrementingClock(),
    )
    return journal, runtime


def test_verified_execution_dispatches_exactly_once_and_signs_receipt(tmp_path) -> None:
    fixture = _fixture()
    journal, runtime = _runtime(tmp_path, fixture)
    adapter = FiniteAdapter(fixture.envelope.request.snapshot)
    receipt = runtime.execute(fixture.envelope, adapter)

    assert receipt.state is ControlEffectState.VERIFIED
    assert receipt.route is ControlRoute.CONNECTOR
    assert adapter.dispatch_calls == 1
    assert adapter.reconcile_calls == 1
    assert adapter.mutation_count == 1
    verify_control_receipt(receipt, fixture.signer.verifier)
    assert journal.get(receipt.effect_id).state is ControlEffectState.VERIFIED

    with pytest.raises(ControlJournalRejected, match="permit_replayed"):
        runtime.execute(fixture.envelope, adapter)
    assert adapter.dispatch_calls == 1
    assert adapter.mutation_count == 1


def test_production_runtime_requires_and_automatically_uses_external_anchor(tmp_path) -> None:
    fixture = _fixture()
    path = tmp_path / "private" / "ada-anchored-runtime.sqlite3"
    with pytest.raises(ControlRuntimeError, match="receipt_anchor_required"):
        ControlRuntime(
            ControlJournal(path),
            fixture.signer.verifier,
            fixture.policy,
            fixture.signer,
            require_external_anchor=True,
        )

    anchors = RuntimeAnchorStore()
    runtime = create_anchored_control_runtime(
        path,
        fixture.signer.verifier,
        fixture.policy,
        fixture.signer,
        clock_ms=IncrementingClock(),
        receipt_anchor_store=anchors,
    )
    receipt = runtime.execute(
        fixture.envelope,
        FiniteAdapter(fixture.envelope.request.snapshot),
    )

    assert runtime.journal.receipt_anchor_configured is True
    assert len(anchors.values) == 1
    assert next(iter(anchors.values.values())) == canonical_json_bytes(receipt.to_dict())


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("missing", "postcondition_missing"),
        ("stale", "postcondition_stale"),
        ("wrong_target", "postcondition_target"),
        ("wrong_evidence", "postcondition_evidence"),
    ],
)
def test_mutation_cannot_verify_without_a_fresh_bound_postcondition(
    tmp_path,
    mode,
    reason,
) -> None:
    fixture = _fixture()
    _, runtime = _runtime(tmp_path, fixture)
    adapter = UnprovenPostconditionAdapter(
        fixture.envelope.request.snapshot,
        mode,
    )

    receipt = runtime.execute(fixture.envelope, adapter)

    assert receipt.state is ControlEffectState.UNKNOWN
    assert receipt.reason_code == reason
    assert adapter.dispatch_calls == 1
    assert adapter.reconcile_calls == 1
    assert adapter.mutation_count == 1
    verify_control_receipt(receipt, fixture.signer.verifier)


@pytest.mark.parametrize(
    ("disposition", "expected_state", "expected_mutations"),
    [
        (DispatchDisposition.REJECTED, ControlEffectState.FAILED, 0),
        (DispatchDisposition.UNKNOWN, ControlEffectState.UNKNOWN, 0),
    ],
)
def test_adapter_rejection_and_uncertainty_are_typed_without_retry(
    tmp_path, disposition, expected_state, expected_mutations
) -> None:
    fixture = _fixture()
    _, runtime = _runtime(tmp_path, fixture)
    adapter = FiniteAdapter(
        fixture.envelope.request.snapshot,
        dispatch_disposition=disposition,
    )
    receipt = runtime.execute(fixture.envelope, adapter)
    assert receipt.state is expected_state
    assert adapter.dispatch_calls == 1
    assert adapter.reconcile_calls == 0
    assert adapter.mutation_count == expected_mutations


@pytest.mark.parametrize("invalid", ["exception", "protocol"])
def test_post_dispatch_exception_or_bad_result_becomes_unknown(tmp_path, invalid) -> None:
    fixture = _fixture()
    _, runtime = _runtime(tmp_path, fixture)
    adapter = FiniteAdapter(
        fixture.envelope.request.snapshot,
        raise_after_mutation=invalid == "exception",
        invalid_dispatch=invalid == "protocol",
    )
    receipt = runtime.execute(fixture.envelope, adapter)
    assert receipt.state is ControlEffectState.UNKNOWN
    assert adapter.dispatch_calls == 1
    assert adapter.mutation_count == 1
    assert "private" not in receipt.reason_code

    recovered = runtime.recover(receipt.effect_id, fixture.envelope, adapter)
    assert recovered.state is ControlEffectState.VERIFIED
    assert adapter.dispatch_calls == 1
    assert adapter.mutation_count == 1


@pytest.mark.parametrize("invalid", ["exception", "protocol", "unknown"])
def test_reconciliation_failure_stays_unknown_until_explicit_proof(tmp_path, invalid) -> None:
    fixture = _fixture()
    _, runtime = _runtime(tmp_path, fixture)
    adapter = FiniteAdapter(
        fixture.envelope.request.snapshot,
        raise_reconciliation=invalid == "exception",
        invalid_reconciliation=invalid == "protocol",
        reconciliation=(ReconciliationDisposition.UNKNOWN if invalid == "unknown" else None),
    )
    receipt = runtime.execute(fixture.envelope, adapter)
    assert receipt.state is ControlEffectState.UNKNOWN
    assert adapter.mutation_count == 1
    assert adapter.dispatch_calls == 1


@pytest.mark.parametrize(
    ("crash_point", "dispatch", "mutate_unknown", "expected_state", "mutations"),
    [
        (
            ControlCrashPoint.AFTER_PREPARED,
            DispatchDisposition.APPLIED,
            False,
            ControlEffectState.FAILED,
            0,
        ),
        (
            ControlCrashPoint.AFTER_STARTED,
            DispatchDisposition.APPLIED,
            False,
            ControlEffectState.FAILED,
            0,
        ),
        (
            ControlCrashPoint.AFTER_DISPATCH,
            DispatchDisposition.APPLIED,
            False,
            ControlEffectState.VERIFIED,
            1,
        ),
        (
            ControlCrashPoint.AFTER_APPLIED,
            DispatchDisposition.APPLIED,
            False,
            ControlEffectState.VERIFIED,
            1,
        ),
        (
            ControlCrashPoint.AFTER_VERIFIED,
            DispatchDisposition.APPLIED,
            False,
            ControlEffectState.VERIFIED,
            1,
        ),
        (
            ControlCrashPoint.AFTER_RECEIPT,
            DispatchDisposition.APPLIED,
            False,
            ControlEffectState.VERIFIED,
            1,
        ),
        (
            ControlCrashPoint.AFTER_FAILED,
            DispatchDisposition.REJECTED,
            False,
            ControlEffectState.FAILED,
            0,
        ),
        (
            ControlCrashPoint.AFTER_UNKNOWN,
            DispatchDisposition.UNKNOWN,
            True,
            ControlEffectState.VERIFIED,
            1,
        ),
    ],
)
def test_every_crash_checkpoint_recovers_without_duplicate_mutation(
    tmp_path,
    crash_point,
    dispatch,
    mutate_unknown,
    expected_state,
    mutations,
) -> None:
    fixture = _fixture()
    journal, runtime = _runtime(tmp_path, fixture)
    adapter = FiniteAdapter(
        fixture.envelope.request.snapshot,
        dispatch_disposition=dispatch,
        mutate_on_unknown=mutate_unknown,
    )
    with pytest.raises(SimulatedControlCrash) as captured:
        runtime.execute(fixture.envelope, adapter, crash_after=crash_point)
    assert captured.value.crash_point is crash_point

    candidates = journal.recovery_candidates()
    record = candidates[0] if candidates else journal.by_permit(fixture.envelope.permit.permit_id)
    assert record is not None
    recovered = runtime.recover(record.effect_id, fixture.envelope, adapter)
    assert recovered.state is expected_state
    assert adapter.mutation_count == mutations
    assert adapter.dispatch_calls <= 1


def test_prepared_recovery_never_touches_adapter(tmp_path) -> None:
    fixture = _fixture()
    journal, runtime = _runtime(tmp_path, fixture)
    adapter = FiniteAdapter(fixture.envelope.request.snapshot)
    with pytest.raises(SimulatedControlCrash):
        runtime.execute(
            fixture.envelope,
            adapter,
            crash_after=ControlCrashPoint.AFTER_PREPARED,
        )
    record = journal.by_permit(fixture.envelope.permit.permit_id)
    assert record is not None

    receipt = runtime.recover(
        record.effect_id,
        fixture.envelope,
        NoTouchAdapter(fixture.envelope.request.snapshot),
    )
    assert receipt.state is ControlEffectState.FAILED


def test_route_or_snapshot_change_after_claim_fails_before_dispatch(tmp_path) -> None:
    for index, adapter in enumerate(
        (
            FiniteAdapter(
                _fixture().envelope.request.snapshot,
                route_sets=[
                    (ControlRoute.CONNECTOR, ControlRoute.DOM),
                    (ControlRoute.DOM,),
                ],
            ),
            FiniteAdapter(
                _fixture().envelope.request.snapshot,
                drift_after_first_snapshot=True,
            ),
        )
    ):
        fixture = _fixture()
        child = tmp_path / str(index)
        child.mkdir(mode=0o700)
        _, runtime = _runtime(child, fixture)
        receipt = runtime.execute(fixture.envelope, adapter)
        assert receipt.state is ControlEffectState.FAILED
        assert adapter.dispatch_calls == 0
        assert adapter.mutation_count == 0


def test_revocation_race_between_claim_and_started_fails_safe(tmp_path) -> None:
    fixture = _fixture()
    journal, runtime = _runtime(tmp_path, fixture)

    def revoke() -> None:
        journal.revoke(
            RevocationKind.PERMIT,
            fixture.envelope.permit.permit_id,
            revoked_at_ms=NOW_MS + 2,
        )

    adapter = FiniteAdapter(
        fixture.envelope.request.snapshot,
        revoke_on_second_snapshot=revoke,
    )
    receipt = runtime.execute(fixture.envelope, adapter)
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "permit_revoked"
    assert adapter.dispatch_calls == 0


def test_expiry_between_claim_and_started_fails_safe(tmp_path) -> None:
    fixture = _fixture()
    expiry = fixture.envelope.permit.expires_at_ms
    clock = SequenceClock([NOW_MS + 1, expiry, expiry + 1, expiry + 2])
    _, runtime = _runtime(tmp_path, fixture, clock=clock)
    adapter = FiniteAdapter(fixture.envelope.request.snapshot)
    receipt = runtime.execute(fixture.envelope, adapter)
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "permit_expired"
    assert adapter.dispatch_calls == 0


def test_recovery_works_after_permit_expiry_and_rejects_wrong_binding(tmp_path) -> None:
    fixture = _fixture()
    journal, runtime = _runtime(tmp_path, fixture)
    adapter = FiniteAdapter(fixture.envelope.request.snapshot)
    with pytest.raises(SimulatedControlCrash):
        runtime.execute(
            fixture.envelope,
            adapter,
            crash_after=ControlCrashPoint.AFTER_DISPATCH,
        )
    record = journal.by_permit(fixture.envelope.permit.permit_id)
    assert record is not None
    runtime._clock_ms = IncrementingClock(fixture.envelope.permit.expires_at_ms + 100)
    receipt = runtime.recover(record.effect_id, fixture.envelope, adapter)
    assert receipt.state is ControlEffectState.VERIFIED

    request = fixture.envelope.request.to_dict()
    request["arguments"] = {"element_id": _opaque("other-button")}
    changed = ControlRequest.from_dict(request)
    with pytest.raises(ControlRuntimeError, match="recovery_binding"):
        runtime.recover(
            record.effect_id,
            ControlEnvelope(changed, fixture.envelope.grant, fixture.envelope.permit),
            adapter,
        )


def test_invalid_adapter_routes_snapshot_and_preclaim_exception_leave_no_effect(
    tmp_path,
) -> None:
    fixture = _fixture()
    journal, runtime = _runtime(tmp_path, fixture)
    adapters = (
        FiniteAdapter(
            fixture.envelope.request.snapshot,
            route_sets=[(ControlRoute.DOM, ControlRoute.CONNECTOR)],
        ),
        FiniteAdapter(fixture.envelope.request.snapshot),
    )
    changed = fixture.envelope.request.snapshot.to_dict()
    changed["epoch"] += 1
    adapters[1].snapshot = SnapshotRef.from_dict(changed)

    for adapter in adapters:
        with pytest.raises(ControlRuntimeError):
            runtime.execute(fixture.envelope, adapter)
        assert journal.by_permit(fixture.envelope.permit.permit_id) is None


def test_adapter_result_constructors_reject_ambiguous_metadata() -> None:
    with pytest.raises(ValueError, match="dispatch_reason"):
        AdapterDispatchResult(DispatchDisposition.APPLIED, "maybe")
    with pytest.raises(ValueError, match="dispatch_reason"):
        AdapterDispatchResult(DispatchDisposition.UNKNOWN, "none")
    with pytest.raises(ValueError, match="evidence_digest"):
        AdapterDispatchResult(
            DispatchDisposition.REJECTED,
            "safe_rejection",
            "private evidence",
        )
    with pytest.raises(ValueError, match="reconciliation_reason"):
        AdapterReconciliationResult(ReconciliationDisposition.VERIFIED, "none")
    with pytest.raises(ValueError, match="reconciliation_postcondition"):
        AdapterReconciliationResult(
            ReconciliationDisposition.FAILED,
            "effect_absent",
            structural_evidence(_uuid(9), "effect_absent"),
            _fixture().envelope.request.snapshot,
        )


def test_forged_exact_dispatch_result_cannot_bypass_constructor_validation(tmp_path) -> None:
    fixture = _fixture()
    _, runtime = _runtime(tmp_path, fixture)
    adapter = ForgedDispatchAdapter(fixture.envelope.request.snapshot)

    receipt = runtime.execute(fixture.envelope, adapter)

    assert receipt.state is ControlEffectState.UNKNOWN
    assert receipt.reason_code == "adapter_protocol"
    assert adapter.dispatch_calls == 1
    assert adapter.mutation_count == 1


def test_forged_exact_reconciliation_result_cannot_verify_unknown_effect(tmp_path) -> None:
    fixture = _fixture()
    _, runtime = _runtime(tmp_path, fixture)
    adapter = ForgedReconciliationAdapter(
        fixture.envelope.request.snapshot,
        dispatch_disposition=DispatchDisposition.UNKNOWN,
    )
    initial = runtime.execute(fixture.envelope, adapter)
    assert initial.state is ControlEffectState.UNKNOWN

    receipt = runtime.recover(initial.effect_id, fixture.envelope, adapter)

    assert receipt.state is ControlEffectState.UNKNOWN
    assert receipt.reason_code == "adapter_uncertain"
    assert adapter.reconcile_calls == 1
    assert adapter.mutation_count == 0


def test_structural_evidence_is_stable_and_rejects_content() -> None:
    effect_id = _uuid(9)
    assert structural_evidence(effect_id, "verified") == structural_evidence(effect_id, "verified")
    with pytest.raises(ValueError):
        structural_evidence("not-a-uuid", "verified")
    with pytest.raises(ValueError):
        structural_evidence(effect_id, "private state")


def test_runtime_source_has_no_dynamic_code_or_generic_program_execution() -> None:
    from algo_cli import david_control_runtime as module

    tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
    forbidden = {"eval", "exec", "compile", "__import__"}
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert calls.isdisjoint(forbidden)
