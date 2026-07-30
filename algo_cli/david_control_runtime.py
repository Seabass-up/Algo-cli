"""Bounded adapter orchestration for the disabled control foundation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import time
from typing import Callable, Protocol
import uuid

from .ada_control_journal import (
    EMPTY_EVIDENCE_DIGEST,
    ControlEffectRecord,
    ControlEffectState,
    ControlJournal,
    ControlJournalRejected,
    ControlReceipt,
    ReceiptHeadAnchorStore,
    verify_control_receipt,
)
from .david_control_kernel import (
    ROUTE_ORDER,
    AuthorityRejected,
    ControlEnvelope,
    ControlPolicy,
    ControlRequest,
    ControlRoute,
    ControlSigner,
    ControlVerifier,
    Operation,
    PermitRejected,
    SchemaRejected,
    SnapshotRef,
    TargetRef,
    content_digest,
    verify_envelope_authority,
)


_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROUTE_INDEX = {route: index for index, route in enumerate(ROUTE_ORDER)}


class ControlRuntimeError(RuntimeError):
    """A content-free runtime or adapter boundary failure."""


class SimulatedControlCrash(BaseException):
    """Test-only process-death injection after a durable checkpoint."""

    def __init__(self, crash_point: "ControlCrashPoint") -> None:
        self.crash_point = crash_point
        super().__init__(crash_point.value)


class ControlCrashPoint(str, Enum):
    AFTER_PREPARED = "after_prepared"
    AFTER_STARTED = "after_started"
    AFTER_DISPATCH = "after_dispatch"
    AFTER_APPLIED = "after_applied"
    AFTER_VERIFIED = "after_verified"
    AFTER_FAILED = "after_failed"
    AFTER_UNKNOWN = "after_unknown"
    AFTER_RECEIPT = "after_receipt"


class DispatchDisposition(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class ReconciliationDisposition(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _safe_reason(value: object, fallback: str) -> str:
    if type(value) is str and _SAFE_ID_RE.fullmatch(value):
        return value
    if _SAFE_ID_RE.fullmatch(fallback):
        return fallback
    return "runtime_error"


def _evidence(value: object) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        raise ValueError("evidence_digest")
    return value


@dataclass(frozen=True, slots=True)
class AdapterDispatchResult:
    disposition: DispatchDisposition
    reason_code: str
    evidence_digest: str = EMPTY_EVIDENCE_DIGEST

    def __post_init__(self) -> None:
        if type(self.disposition) is not DispatchDisposition:
            raise ValueError("dispatch_disposition")
        reason = _safe_reason(self.reason_code, "invalid_adapter_reason")
        if reason != self.reason_code:
            raise ValueError("dispatch_reason")
        _evidence(self.evidence_digest)
        if self.disposition is DispatchDisposition.APPLIED and reason != "none":
            raise ValueError("dispatch_reason")
        if self.disposition is not DispatchDisposition.APPLIED and reason == "none":
            raise ValueError("dispatch_reason")


@dataclass(frozen=True, slots=True)
class AdapterReconciliationResult:
    disposition: ReconciliationDisposition
    reason_code: str
    evidence_digest: str = EMPTY_EVIDENCE_DIGEST
    postcondition: SnapshotRef | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not ReconciliationDisposition:
            raise ValueError("reconciliation_disposition")
        reason = _safe_reason(self.reason_code, "invalid_adapter_reason")
        if reason != self.reason_code or reason == "none":
            raise ValueError("reconciliation_reason")
        _evidence(self.evidence_digest)
        if self.postcondition is not None:
            if type(self.postcondition) is not SnapshotRef:
                raise ValueError("reconciliation_postcondition")
            SnapshotRef.from_dict(self.postcondition.to_dict())
            if self.disposition is not ReconciliationDisposition.VERIFIED:
                raise ValueError("reconciliation_postcondition")


class ControlAdapter(Protocol):
    """Finite adapter contract; no generic program or decoded JSON crosses it."""

    def available_routes(self, target: TargetRef) -> tuple[ControlRoute, ...]: ...

    def current_snapshot(self, target: TargetRef) -> SnapshotRef: ...

    def dispatch(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterDispatchResult: ...

    def reconcile(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterReconciliationResult: ...


class ControlRuntime:
    """Verify, claim, dispatch once, and reconcile without blind retry."""

    def __init__(
        self,
        journal: ControlJournal,
        verifier: ControlVerifier,
        policy: ControlPolicy,
        receipt_signer: ControlSigner,
        *,
        clock_ms: Callable[[], int] | None = None,
        require_external_anchor: bool = False,
    ) -> None:
        if type(journal) is not ControlJournal:
            raise ValueError("journal")
        if type(verifier) is not ControlVerifier:
            raise ValueError("verifier")
        if type(policy) is not ControlPolicy:
            raise ValueError("policy")
        if type(receipt_signer) is not ControlSigner:
            raise ValueError("receipt_signer")
        if receipt_signer.key_id != verifier.key_id:
            raise AuthorityRejected("receipt_key")
        if clock_ms is not None and not callable(clock_ms):
            raise ValueError("clock")
        if type(require_external_anchor) is not bool:
            raise ValueError("require_external_anchor")
        if require_external_anchor and not journal.receipt_anchor_configured:
            raise ControlRuntimeError("receipt_anchor_required")
        if journal.receipt_anchor_configured:
            journal.synchronize_receipt_anchor(verifier)
        self.journal = journal
        self.verifier = verifier
        self.policy = policy
        self.receipt_signer = receipt_signer
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def _now(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= (1 << 53) - 1:
            raise ControlRuntimeError("clock_invalid")
        return value

    @staticmethod
    def _maybe_crash(
        configured: ControlCrashPoint | None,
        actual: ControlCrashPoint,
    ) -> None:
        if configured is not None and type(configured) is not ControlCrashPoint:
            raise ValueError("crash_point")
        if configured is actual:
            raise SimulatedControlCrash(actual)

    @staticmethod
    def _routes(adapter: ControlAdapter, target: TargetRef) -> tuple[ControlRoute, ...]:
        try:
            routes = adapter.available_routes(target)
        except Exception:
            raise ControlRuntimeError("adapter_routes_unavailable") from None
        if type(routes) is not tuple or not 1 <= len(routes) <= len(ROUTE_ORDER):
            raise ControlRuntimeError("adapter_routes_invalid")
        if not all(type(route) is ControlRoute for route in routes):
            raise ControlRuntimeError("adapter_routes_invalid")
        if len(set(routes)) != len(routes):
            raise ControlRuntimeError("adapter_routes_invalid")
        if tuple(sorted(routes, key=lambda route: _ROUTE_INDEX[route])) != routes:
            raise ControlRuntimeError("adapter_routes_invalid")
        return routes

    @staticmethod
    def _snapshot(adapter: ControlAdapter, target: TargetRef) -> SnapshotRef:
        try:
            snapshot = adapter.current_snapshot(target)
            if type(snapshot) is not SnapshotRef:
                raise ValueError("snapshot_type")
            snapshot = SnapshotRef.from_dict(snapshot.to_dict())
        except Exception:
            raise ControlRuntimeError("adapter_snapshot_unavailable") from None
        if not snapshot.matches_target(target):
            raise ControlRuntimeError("adapter_snapshot_invalid")
        return snapshot

    @staticmethod
    def _exception_reason(error: BaseException, fallback: str) -> str:
        return _safe_reason(getattr(error, "reason_code", str(error)), fallback)

    def _receipt(
        self,
        record: ControlEffectRecord,
        *,
        crash_after: ControlCrashPoint | None,
    ) -> ControlReceipt:
        receipt = self.journal.finalize_receipt(
            record.effect_id,
            self.receipt_signer,
            completed_at_ms=self._now(),
        )
        verify_control_receipt(receipt, self.verifier)
        self._maybe_crash(crash_after, ControlCrashPoint.AFTER_RECEIPT)
        return receipt

    def _transition(
        self,
        record: ControlEffectRecord,
        state: ControlEffectState,
        *,
        reason_code: str,
        evidence_digest: str = EMPTY_EVIDENCE_DIGEST,
        crash_after: ControlCrashPoint | None,
    ) -> ControlEffectRecord:
        changed = self.journal.transition(
            record.effect_id,
            state,
            now_ms=self._now(),
            reason_code=reason_code,
            evidence_digest=evidence_digest,
        )
        crash_for_state = {
            ControlEffectState.STARTED: ControlCrashPoint.AFTER_STARTED,
            ControlEffectState.APPLIED: ControlCrashPoint.AFTER_APPLIED,
            ControlEffectState.VERIFIED: ControlCrashPoint.AFTER_VERIFIED,
            ControlEffectState.FAILED: ControlCrashPoint.AFTER_FAILED,
            ControlEffectState.UNKNOWN: ControlCrashPoint.AFTER_UNKNOWN,
        }.get(state)
        if crash_for_state is not None:
            self._maybe_crash(crash_after, crash_for_state)
        return changed

    def _fail_prepared(
        self,
        record: ControlEffectRecord,
        reason_code: str,
        *,
        crash_after: ControlCrashPoint | None,
    ) -> ControlReceipt:
        failed = self._transition(
            record,
            ControlEffectState.FAILED,
            reason_code=_safe_reason(reason_code, "pre_dispatch_rejected"),
            crash_after=crash_after,
        )
        return self._receipt(failed, crash_after=crash_after)

    def execute(
        self,
        envelope: ControlEnvelope,
        adapter: ControlAdapter,
        *,
        crash_after: ControlCrashPoint | None = None,
    ) -> ControlReceipt:
        if type(envelope) is not ControlEnvelope:
            raise ControlRuntimeError("envelope_type")
        envelope = ControlEnvelope.from_dict(envelope.to_dict())
        initial_routes = self._routes(adapter, envelope.request.target)
        initial_snapshot = self._snapshot(adapter, envelope.request.target)
        now_ms = self._now()
        route = verify_envelope_authority(
            envelope,
            self.verifier,
            self.policy,
            now_ms=now_ms,
            live_routes=initial_routes,
            live_snapshot=initial_snapshot,
        )
        record = self.journal.claim(
            envelope,
            route,
            verifier=self.verifier,
            policy=self.policy,
            live_snapshot=initial_snapshot,
            now_ms=now_ms,
        )
        self._maybe_crash(crash_after, ControlCrashPoint.AFTER_PREPARED)

        try:
            current_routes = self._routes(adapter, envelope.request.target)
            current_snapshot = self._snapshot(adapter, envelope.request.target)
            current_route = verify_envelope_authority(
                envelope,
                self.verifier,
                self.policy,
                now_ms=self._now(),
                live_routes=current_routes,
                live_snapshot=current_snapshot,
            )
            if current_route is not record.route:
                return self._fail_prepared(
                    record,
                    "route_changed",
                    crash_after=crash_after,
                )
            record = self._transition(
                record,
                ControlEffectState.STARTED,
                reason_code="none",
                crash_after=crash_after,
            )
        except SimulatedControlCrash:
            raise
        except (AuthorityRejected, PermitRejected, SchemaRejected) as error:
            return self._fail_prepared(
                record,
                self._exception_reason(error, "authority_changed"),
                crash_after=crash_after,
            )
        except ControlJournalRejected as error:
            return self._fail_prepared(
                record,
                error.reason_code,
                crash_after=crash_after,
            )
        except ControlRuntimeError as error:
            return self._fail_prepared(
                record,
                self._exception_reason(error, "adapter_changed"),
                crash_after=crash_after,
            )

        try:
            dispatch_result = adapter.dispatch(
                record.effect_id,
                envelope.request,
                record.route,
            )
        except SimulatedControlCrash:
            raise
        except Exception:
            unknown = self._transition(
                record,
                ControlEffectState.UNKNOWN,
                reason_code="adapter_exception",
                crash_after=crash_after,
            )
            return self._receipt(unknown, crash_after=crash_after)
        self._maybe_crash(crash_after, ControlCrashPoint.AFTER_DISPATCH)

        try:
            if type(dispatch_result) is not AdapterDispatchResult:
                raise ValueError("dispatch_type")
            dispatch_result = AdapterDispatchResult(
                dispatch_result.disposition,
                dispatch_result.reason_code,
                dispatch_result.evidence_digest,
            )
        except (AttributeError, TypeError, ValueError):
            unknown = self._transition(
                record,
                ControlEffectState.UNKNOWN,
                reason_code="adapter_protocol",
                crash_after=crash_after,
            )
            return self._receipt(unknown, crash_after=crash_after)
        if dispatch_result.disposition is DispatchDisposition.REJECTED:
            failed = self._transition(
                record,
                ControlEffectState.FAILED,
                reason_code=dispatch_result.reason_code,
                evidence_digest=dispatch_result.evidence_digest,
                crash_after=crash_after,
            )
            return self._receipt(failed, crash_after=crash_after)
        if dispatch_result.disposition is DispatchDisposition.UNKNOWN:
            unknown = self._transition(
                record,
                ControlEffectState.UNKNOWN,
                reason_code=dispatch_result.reason_code,
                evidence_digest=dispatch_result.evidence_digest,
                crash_after=crash_after,
            )
            return self._receipt(unknown, crash_after=crash_after)

        applied = self._transition(
            record,
            ControlEffectState.APPLIED,
            reason_code="none",
            evidence_digest=dispatch_result.evidence_digest,
            crash_after=crash_after,
        )
        return self._reconcile(
            applied,
            envelope,
            adapter,
            crash_after=crash_after,
        )

    def _reconcile(
        self,
        record: ControlEffectRecord,
        envelope: ControlEnvelope,
        adapter: ControlAdapter,
        *,
        crash_after: ControlCrashPoint | None,
    ) -> ControlReceipt:
        try:
            result = adapter.reconcile(
                record.effect_id,
                envelope.request,
                record.route,
            )
        except SimulatedControlCrash:
            raise
        except Exception:
            result = AdapterReconciliationResult(
                ReconciliationDisposition.UNKNOWN,
                "reconciliation_exception",
            )
        try:
            if type(result) is not AdapterReconciliationResult:
                raise ValueError("reconciliation_type")
            result = AdapterReconciliationResult(
                result.disposition,
                result.reason_code,
                result.evidence_digest,
                result.postcondition,
            )
        except (AttributeError, TypeError, ValueError):
            result = AdapterReconciliationResult(
                ReconciliationDisposition.UNKNOWN,
                "reconciliation_protocol",
            )

        if result.disposition is ReconciliationDisposition.VERIFIED:
            mutation = envelope.request.operation not in {
                Operation.OBSERVE,
                Operation.HANDOFF,
            }
            if mutation:
                postcondition_error = self._postcondition_error(
                    record.effect_id,
                    envelope.request,
                    result,
                )
                if postcondition_error is not None:
                    result = AdapterReconciliationResult(
                        ReconciliationDisposition.UNKNOWN,
                        postcondition_error,
                    )
        if result.disposition is ReconciliationDisposition.VERIFIED:
            if record.state is ControlEffectState.STARTED:
                record = self._transition(
                    record,
                    ControlEffectState.APPLIED,
                    reason_code="none",
                    evidence_digest=result.evidence_digest,
                    crash_after=crash_after,
                )
            verified = self._transition(
                record,
                ControlEffectState.VERIFIED,
                reason_code=result.reason_code,
                evidence_digest=result.evidence_digest,
                crash_after=crash_after,
            )
            return self._receipt(verified, crash_after=crash_after)
        if result.disposition is ReconciliationDisposition.FAILED:
            failed = self._transition(
                record,
                ControlEffectState.FAILED,
                reason_code=result.reason_code,
                evidence_digest=result.evidence_digest,
                crash_after=crash_after,
            )
            return self._receipt(failed, crash_after=crash_after)
        if record.state is not ControlEffectState.UNKNOWN:
            record = self._transition(
                record,
                ControlEffectState.UNKNOWN,
                reason_code=result.reason_code,
                evidence_digest=result.evidence_digest,
                crash_after=crash_after,
            )
        return self._receipt(record, crash_after=crash_after)

    @staticmethod
    def _postcondition_error(
        effect_id: str,
        request: ControlRequest,
        result: AdapterReconciliationResult,
    ) -> str | None:
        postcondition = result.postcondition
        if postcondition is None:
            return "postcondition_missing"
        prior = request.snapshot
        target = request.target
        if (
            postcondition.snapshot_id == prior.snapshot_id
            or postcondition.observed_at_ms <= prior.observed_at_ms
            or postcondition.sequence <= prior.sequence
        ):
            return "postcondition_stale"
        if (
            postcondition.target_id != target.target_id
            or postcondition.epoch < target.epoch
            or postcondition.fencing_token < target.fencing_token
        ):
            return "postcondition_target"
        if postcondition.epoch == target.epoch and (
            postcondition.revision != target.revision
            or postcondition.fencing_token != target.fencing_token
        ):
            return "postcondition_target"
        if postcondition.epoch > target.epoch and (
            postcondition.fencing_token <= target.fencing_token
        ):
            return "postcondition_target"
        expected = fresh_postcondition_evidence(
            effect_id,
            result.reason_code,
            postcondition,
        )
        if result.evidence_digest != expected:
            return "postcondition_evidence"
        return None

    def _verify_recovery_binding(
        self,
        record: ControlEffectRecord,
        envelope: ControlEnvelope,
    ) -> ControlEnvelope:
        if type(envelope) is not ControlEnvelope:
            raise ControlRuntimeError("envelope_type")
        parsed = ControlEnvelope.from_dict(envelope.to_dict())
        try:
            recovered_route = verify_envelope_authority(
                parsed,
                self.verifier,
                self.policy,
                now_ms=parsed.permit.issued_at_ms,
                live_routes=(record.route,),
                live_snapshot=parsed.request.snapshot,
            )
        except (AuthorityRejected, PermitRejected, SchemaRejected):
            raise ControlRuntimeError("recovery_binding") from None
        bindings = (
            record.permit_id == parsed.permit.permit_id,
            record.grant_id == parsed.grant.grant_id,
            record.session_id == parsed.request.session_id,
            record.request_id == parsed.request.request_id,
            record.request_digest == parsed.request.digest,
            record.target_kind is parsed.request.target.kind,
            record.target_id == parsed.request.target.target_id,
            record.target_epoch == parsed.request.target.epoch,
            record.target_revision == parsed.request.target.revision,
            record.fencing_token == parsed.request.target.fencing_token,
            record.snapshot_id == parsed.request.snapshot.snapshot_id,
            record.sequence == parsed.request.sequence,
            record.operation is parsed.request.operation,
            record.route is recovered_route,
            record.authority_key_id == parsed.permit.authority_key_id,
        )
        if not all(bindings):
            raise ControlRuntimeError("recovery_binding")
        return parsed

    def recover(
        self,
        effect_id: str,
        envelope: ControlEnvelope,
        adapter: ControlAdapter,
        *,
        crash_after: ControlCrashPoint | None = None,
    ) -> ControlReceipt:
        record = self.journal.get(effect_id)
        parsed = self._verify_recovery_binding(record, envelope)
        if record.state is ControlEffectState.PREPARED:
            failed = self._transition(
                record,
                ControlEffectState.FAILED,
                reason_code="recovered_before_dispatch",
                crash_after=crash_after,
            )
            return self._receipt(failed, crash_after=crash_after)
        if record.state in {
            ControlEffectState.STARTED,
            ControlEffectState.APPLIED,
            ControlEffectState.UNKNOWN,
        }:
            return self._reconcile(
                record,
                parsed,
                adapter,
                crash_after=crash_after,
            )
        return self._receipt(record, crash_after=crash_after)


def structural_evidence(effect_id: str, state: str) -> str:
    """Create a content-free evidence digest for trusted finite adapters."""

    try:
        parsed_uuid = uuid.UUID(effect_id)
    except (ValueError, AttributeError):
        raise ValueError("effect_id") from None
    if (
        str(parsed_uuid) != effect_id
        or parsed_uuid.int == 0
        or parsed_uuid.variant != uuid.RFC_4122
        or not _SAFE_ID_RE.fullmatch(state)
    ):
        raise ValueError("structural_evidence")
    return content_digest({"effect_id": effect_id, "state": state})


def create_anchored_control_runtime(
    journal_path: str | Path,
    verifier: ControlVerifier,
    policy: ControlPolicy,
    receipt_signer: ControlSigner,
    *,
    clock_ms: Callable[[], int] | None = None,
    receipt_anchor_store: ReceiptHeadAnchorStore | None = None,
) -> ControlRuntime:
    """Construct the production posture with a mandatory OS-backed external head."""

    selected = receipt_anchor_store
    if selected is None:
        from .grace_key_store import GraceReceiptAnchorStore

        selected = GraceReceiptAnchorStore()
    journal = ControlJournal(
        journal_path,
        receipt_anchor_store=selected,
    )
    return ControlRuntime(
        journal,
        verifier,
        policy,
        receipt_signer,
        clock_ms=clock_ms,
        require_external_anchor=True,
    )


def fresh_postcondition_evidence(
    effect_id: str,
    state: str,
    postcondition: SnapshotRef,
) -> str:
    """Bind a content-free fresh observation to an adapter outcome."""

    structural_evidence(effect_id, state)
    if type(postcondition) is not SnapshotRef:
        raise ValueError("postcondition")
    parsed = SnapshotRef.from_dict(postcondition.to_dict())
    return content_digest(
        {
            "effect_id": effect_id,
            "state": state,
            "postcondition": parsed.to_dict(),
        }
    )


__all__ = [
    "AdapterDispatchResult",
    "AdapterReconciliationResult",
    "ControlAdapter",
    "ControlCrashPoint",
    "ControlRuntime",
    "ControlRuntimeError",
    "DispatchDisposition",
    "ReconciliationDisposition",
    "SimulatedControlCrash",
    "create_anchored_control_runtime",
    "fresh_postcondition_evidence",
    "structural_evidence",
]
