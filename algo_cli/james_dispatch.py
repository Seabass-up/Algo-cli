"""Canonical typed dispatcher for every model-invoked Algo CLI action."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any

from .arthur_outcomes import (
    ActionOutcome,
    OutcomeStatus,
    VerificationStatus,
    normalize_action_outcome,
)
from .clara_effect_ledger import EffectLedger, EffectState, default_effect_ledger
from .henry_effect_control import EffectLeaseError, TargetLease, TargetLeaseManager
from .marcus_authority import EffectClass, ResolvedAction
from . import nathan_runtime as runtime
from .theodore_runtime_services import scoped_tool_runtime_env
from .samuel_policy_engine import resolve_action


EffectVerifier = Callable[[ResolvedAction, str], bool | None]
ToolInvoker = Callable[[str, dict[str, Any], Any], str]
ApprovalCallback = Callable[..., bool]
TRUSTED_ADAPTER_ACTIONS = frozenset(
    {"x_account_post", "x_account_reply", "x_account_post_action"}
)
_SAFE_VERIFIER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass
class DispatchDependencies:
    invoke: ToolInvoker
    effect_ledger: EffectLedger
    lease_manager: TargetLeaseManager
    effect_verifiers: Mapping[str, EffectVerifier] = field(default_factory=dict)
    monotonic: Callable[[], float] = time.monotonic
    approve: ApprovalCallback | None = None


class DispatchCancellation:
    """Thread-safe cooperative cancellation signal with a content-free reason."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason_code = "caller_cancelled"
        self._lock = threading.Lock()

    def cancel(self, reason_code: str = "caller_cancelled") -> None:
        normalized = str(reason_code or "").strip()
        if not _SAFE_VERIFIER_ID.fullmatch(normalized):
            raise ValueError("cancellation reason must be a bounded identifier")
        with self._lock:
            if not self._event.is_set():
                self._reason_code = normalized
                self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason_code(self) -> str:
        with self._lock:
            return self._reason_code


@dataclass
class _DispatchControl:
    dependencies: DispatchDependencies
    deadline_monotonic: float | None
    cancellation: DispatchCancellation | None
    last_monotonic: float | None = None

    def signal(self) -> tuple[str, str] | None:
        if self.cancellation is not None and self.cancellation.cancelled:
            return "cancelled", self.cancellation.reason_code
        deadline = self.deadline_monotonic
        if deadline is None:
            return None
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
            or float(deadline) < 0
        ):
            return "failed", "invalid_deadline"
        try:
            now = float(self.dependencies.monotonic())
        except Exception:
            return "failed", "dispatch_clock_error"
        if not math.isfinite(now) or now < 0:
            return "failed", "dispatch_clock_error"
        if self.last_monotonic is not None and now < self.last_monotonic:
            return "failed", "clock_regression"
        self.last_monotonic = now
        if now >= float(deadline):
            return "timed_out", "deadline_elapsed"
        return None


@dataclass(frozen=True)
class DispatchResult:
    message: dict[str, Any]
    outcome: ActionOutcome
    duration_ms: float
    preflight: runtime.RuntimeToolPreflight

    @property
    def result(self) -> str:
        return self.outcome.model_text()

    @property
    def status(self) -> str:
        """Return the bounded legacy status used by run summaries and telemetry."""

        return _attempt_status(self.outcome)


def _default_effect_root() -> Path:
    from . import config as config_module

    return config_module.CONFIG_DIR / "private"


def default_dispatch_dependencies() -> DispatchDependencies:
    root = _default_effect_root()
    return DispatchDependencies(
        invoke=_trusted_invoke,
        effect_ledger=default_effect_ledger(),
        lease_manager=TargetLeaseManager(root / "henry-effect-leases"),
    )


def batch_policy_ceiling_codes(
    batch: list[tuple[tuple[str, dict[str, Any]], str | None]],
    cfg: Any,
) -> list[str]:
    """Quarantine a malformed mixed batch before any sibling can execute."""

    call_ids = [tool_call_id for _call, tool_call_id in batch if tool_call_id]
    if len(set(call_ids)) != len(call_ids):
        return ["batch_duplicate_call_id"] * len(batch)

    specific: dict[int, str] = {}
    for index, ((name, args), tool_call_id) in enumerate(batch):
        try:
            action = resolve_action(
                name,
                runtime.tool_runtime_args(name, args, cfg),
                cwd=cfg.cwd,
            )
        except Exception:
            specific[index] = "batch_unclassified_action"
            continue
        if action.effect_class is EffectClass.UNCLASSIFIED:
            specific[index] = "batch_unclassified_action"
        elif action.effect_class is EffectClass.EXTERNAL_MUTATION and not tool_call_id:
            specific[index] = "batch_missing_idempotency_id"
    if not specific:
        return [""] * len(batch)
    return [specific.get(index, "batch_quarantined") for index in range(len(batch))]


def _trusted_invoke(name: str, args: dict[str, Any], cfg: Any) -> str:
    """Invoke trusted adapters; confirmation-only fields never come from the model."""

    if name in TRUSTED_ADAPTER_ACTIONS:
        from . import x_account

        if name == "x_account_post":
            return x_account.post(str(args.get("text") or ""), confirm=True).to_json()
        if name == "x_account_reply":
            return x_account.reply(
                str(args.get("post") or ""),
                str(args.get("text") or ""),
                confirm=True,
            ).to_json()
        return x_account.post_action(
            str(args.get("action") or ""),
            str(args.get("post") or ""),
            confirm=True,
        ).to_json()
    return runtime.run_tool(name, args, cfg)


def _idempotency_key(action: ResolvedAction, invocation_id: str) -> str:
    payload = json.dumps(
        {"action_digest": action.action_digest, "invocation_id": invocation_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _invocation_id_hash(invocation_id: str) -> str:
    return hashlib.sha256(str(invocation_id).encode("utf-8")).hexdigest()


def _attempt_status(outcome: ActionOutcome) -> str:
    return {
        OutcomeStatus.SUCCEEDED: "worked",
        OutcomeStatus.FAILED: "failed",
        OutcomeStatus.DENIED: "denied",
        OutcomeStatus.SKIPPED: "skipped",
        OutcomeStatus.TIMED_OUT: "timed_out",
        OutcomeStatus.CANCELLED: "cancelled",
        OutcomeStatus.UNKNOWN_OUTCOME: "unknown_outcome",
    }[outcome.status]


def _finalize(
    *,
    name: str,
    args: dict[str, Any],
    cfg: Any,
    tool_call_id: str | None,
    preflight: runtime.RuntimeToolPreflight,
    outcome: ActionOutcome,
    duration_ms: float,
    render: bool,
) -> DispatchResult:
    status = _attempt_status(outcome)
    result = outcome.model_text()
    if outcome.invoked:
        result = runtime.augment_tool_result_with_reflex(
            cfg,
            name,
            preflight.signature_args,
            result,
            status,
        )
        outcome = replace(outcome, result=result)
    if render:
        runtime.show_typed_tool_result(
            name,
            result,
            outcome_status=outcome.status,
            duration_ms=duration_ms if outcome.invoked else None,
            call_id=tool_call_id,
        )
    runtime.record_tool_attempt(
        cfg,
        name=name,
        args=preflight.signature_args,
        result=result,
        status=status,
        retry_allowed=outcome.retry_allowed,
    )
    runtime.record_perf_event(
        "tool",
        tool=name,
        status=status,
        duration_ms=duration_ms,
        outcome=outcome.receipt(),
        **preflight.qos_fields,
    )
    return DispatchResult(
        message=runtime.tool_result_message(name, result, tool_call_id),
        outcome=outcome,
        duration_ms=duration_ms,
        preflight=preflight,
    )


def _preinvoke_outcome(
    action: ResolvedAction,
    result: str,
    *,
    status: str,
    error_code: str = "",
) -> ActionOutcome:
    return normalize_action_outcome(
        action,
        result,
        reported_status=status,
        invoked=False,
        error_code=error_code,
    )


def _termination_ceiling_code(signal: tuple[str, str]) -> str:
    status, reason = signal
    if status == "cancelled":
        return "dispatch_cancelled"
    if status == "timed_out":
        return "dispatch_deadline_elapsed"
    if reason in {"clock_regression", "dispatch_clock_error"}:
        return "dispatch_clock_error"
    return "dispatch_invalid_deadline"


def _termination_outcome(
    action: ResolvedAction,
    signal: tuple[str, str],
    *,
    invoked: bool,
    result: str = "",
    effect_id: str = "",
    idempotency_key: str = "",
    fencing_token: int = 0,
) -> ActionOutcome:
    status, reason_code = signal
    if result:
        message = result
    elif status == "cancelled":
        message = "Action was cancelled before dispatch."
    elif status == "timed_out":
        message = "Action deadline elapsed before dispatch."
    else:
        message = "Action was not dispatched because its timing control was invalid."
    reported_status = status
    if invoked and action.effect_class is not EffectClass.OBSERVE:
        reported_status = "failed"
        reason_code = (
            "cancelled_after_dispatch"
            if status == "cancelled"
            else "deadline_after_dispatch"
            if status == "timed_out"
            else reason_code
        )
    return normalize_action_outcome(
        action,
        message,
        reported_status=reported_status,
        invoked=invoked,
        effect_id=effect_id,
        idempotency_key=idempotency_key,
        fencing_token=fencing_token,
        error_code=reason_code,
    )


def _pre_dispatch_termination_reason(signal: tuple[str, str]) -> str:
    status, _reason = signal
    if status == "cancelled":
        return "cancelled_before_dispatch"
    if status == "timed_out":
        return "deadline_before_dispatch"
    return "dispatch_control_invalid"


def _post_dispatch_termination_reason(signal: tuple[str, str]) -> str:
    status, _reason = signal
    if status == "cancelled":
        return "cancelled_after_dispatch"
    if status == "timed_out":
        return "deadline_after_dispatch"
    return "dispatch_control_invalid"


def _deduplicated_external_outcome(
    record: Any,
    action: ResolvedAction,
    *,
    dependencies: DispatchDependencies,
    lease: TargetLease,
) -> ActionOutcome:
    if record.state is EffectState.VERIFIED:
        return replace(
            normalize_action_outcome(
                action,
                "Effect was already verified; duplicate invocation was not executed.",
                reported_status="worked",
                invoked=False,
                effect_id=record.effect_id,
                idempotency_key=record.idempotency_key,
                fencing_token=record.fencing_token,
                deduplicated=True,
            ),
            verification=VerificationStatus.PASSED,
        )
    if record.state is EffectState.FAILED:
        return replace(
            normalize_action_outcome(
                action,
                "Effect was previously reconciled as not applied; duplicate invocation was not executed.",
                reported_status="failed",
                invoked=False,
                effect_id=record.effect_id,
                idempotency_key=record.idempotency_key,
                fencing_token=record.fencing_token,
                error_code="deduplicated_failed_effect",
                deduplicated=True,
            ),
            retry_allowed=False,
        )
    if record.state is EffectState.PREPARED:
        try:
            recovered = dependencies.effect_ledger.recover_prepared(
                record.effect_id,
                recovery_fencing_token=lease.fencing_token,
            )
        except Exception as exc:
            return replace(
                normalize_action_outcome(
                    action,
                    "Pre-dispatch effect recovery failed; duplicate invocation was not executed.",
                    reported_status="failed",
                    invoked=False,
                    effect_id=record.effect_id,
                    idempotency_key=record.idempotency_key,
                    fencing_token=record.fencing_token,
                    error_code=type(exc).__name__,
                    deduplicated=True,
                ),
                retry_allowed=False,
            )
        return replace(
            normalize_action_outcome(
                action,
                "A prepared effect was recovered as not dispatched after restart.",
                reported_status="failed",
                invoked=False,
                effect_id=recovered.effect_id,
                idempotency_key=recovered.idempotency_key,
                fencing_token=recovered.fencing_token,
                error_code="recovered_before_dispatch",
                deduplicated=True,
            ),
            retry_allowed=False,
        )
    return normalize_action_outcome(
        action,
        "A matching effect is pending or uncertain; duplicate invocation was not executed.",
        reported_status="failed",
        invoked=True,
        effect_id=record.effect_id,
        idempotency_key=record.idempotency_key,
        fencing_token=record.fencing_token,
        error_code="deduplicated_unknown_effect",
        deduplicated=True,
    )


def _run_external_effect(
    name: str,
    args: dict[str, Any],
    cfg: Any,
    action: ResolvedAction,
    *,
    invocation_id: str,
    dependencies: DispatchDependencies,
    lease: TargetLease,
    control: _DispatchControl,
) -> tuple[ActionOutcome, float]:
    key = _idempotency_key(action, invocation_id)
    try:
        prepared = dependencies.effect_ledger.prepare(
            idempotency_key=key,
            invocation_id_hash=_invocation_id_hash(invocation_id),
            action=name,
            target=action.target,
            fencing_token=lease.fencing_token,
        )
    except Exception as exc:
        return (
            normalize_action_outcome(
                action,
                "External effect was not started because its durable prepare receipt failed.",
                reported_status="failed",
                invoked=False,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
                error_code=type(exc).__name__,
            ),
            0.0,
        )
    if not prepared.created:
        return (
            _deduplicated_external_outcome(
                prepared.record,
                action,
                dependencies=dependencies,
                lease=lease,
            ),
            0.0,
        )

    effect_id = prepared.record.effect_id
    termination = control.signal()
    if termination is not None:
        try:
            dependencies.effect_ledger.transition(
                effect_id,
                EffectState.FAILED,
                fencing_token=lease.fencing_token,
                reason_code=_pre_dispatch_termination_reason(termination),
            )
        except Exception as exc:
            return (
                normalize_action_outcome(
                    action,
                    "External effect was not invoked, but its cancellation receipt could not be persisted.",
                    reported_status="failed",
                    invoked=False,
                    effect_id=effect_id,
                    idempotency_key=key,
                    fencing_token=lease.fencing_token,
                    error_code=type(exc).__name__,
                ),
                0.0,
            )
        return (
            _termination_outcome(
                action,
                termination,
                invoked=False,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
            ),
            0.0,
        )
    try:
        dependencies.effect_ledger.transition(
            effect_id,
            EffectState.STARTED,
            fencing_token=lease.fencing_token,
        )
    except Exception as exc:
        return (
            normalize_action_outcome(
                action,
                "External effect was not started because its durable start receipt failed.",
                reported_status="failed",
                invoked=False,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
                error_code=type(exc).__name__,
            ),
            0.0,
        )
    termination = control.signal()
    if termination is not None:
        try:
            dependencies.effect_ledger.transition(
                effect_id,
                EffectState.FAILED,
                fencing_token=lease.fencing_token,
                reason_code=_pre_dispatch_termination_reason(termination),
            )
        except Exception as exc:
            return (
                normalize_action_outcome(
                    action,
                    "External effect was not invoked, but its dispatch-cancellation receipt failed.",
                    reported_status="failed",
                    invoked=False,
                    effect_id=effect_id,
                    idempotency_key=key,
                    fencing_token=lease.fencing_token,
                    error_code=type(exc).__name__,
                ),
                0.0,
            )
        return (
            _termination_outcome(
                action,
                termination,
                invoked=False,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
            ),
            0.0,
        )
    if not lease.validate():
        try:
            dependencies.effect_ledger.transition(
                effect_id,
                EffectState.FAILED,
                fencing_token=lease.fencing_token,
                reason_code="stale_fence_before_invoke",
            )
        except Exception as exc:
            return (
                normalize_action_outcome(
                    action,
                    "External effect was not invoked, but its stale-fence receipt could not be persisted.",
                    reported_status="failed",
                    invoked=False,
                    effect_id=effect_id,
                    idempotency_key=key,
                    fencing_token=lease.fencing_token,
                    error_code=type(exc).__name__,
                ),
                0.0,
            )
        return (
            normalize_action_outcome(
                action,
                "External effect was not started because its fencing lease became stale.",
                reported_status="failed",
                invoked=False,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
                error_code="stale_fence",
            ),
            0.0,
        )

    started = time.perf_counter()
    try:
        with scoped_tool_runtime_env(cfg):
            raw_result = str(dependencies.invoke(name, dict(args), cfg))
    except BaseException as exc:
        raw_result = f"Tool error for {name}: {type(exc).__name__}"
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    fence_error_code = ""
    try:
        if not lease.validate():
            fence_error_code = "stale_fence_after_dispatch"
    except Exception as exc:
        fence_error_code = f"fence_validation_{type(exc).__name__}"
    if fence_error_code:
        try:
            dependencies.effect_ledger.transition(
                effect_id,
                EffectState.UNKNOWN,
                fencing_token=lease.fencing_token,
                reason_code="stale_fence_after_dispatch",
            )
        except Exception as exc:
            fence_error_code = f"fence_receipt_{type(exc).__name__}"
        return (
            normalize_action_outcome(
                action,
                raw_result,
                reported_status="failed",
                invoked=True,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
                error_code=fence_error_code,
            ),
            duration_ms,
        )
    raw_status = runtime.classify_tool_status(raw_result, name=name)
    post_termination = control.signal()
    current_state = EffectState.STARTED
    try:
        if raw_status == "worked":
            dependencies.effect_ledger.transition(
                effect_id,
                EffectState.APPLIED,
                fencing_token=lease.fencing_token,
            )
            current_state = EffectState.APPLIED
        else:
            dependencies.effect_ledger.transition(
                effect_id,
                EffectState.UNKNOWN,
                fencing_token=lease.fencing_token,
                reason_code=(
                    _post_dispatch_termination_reason(post_termination)
                    if post_termination is not None
                    else "invocation_reported_failure"
                ),
            )
            current_state = EffectState.UNKNOWN

        verifier = dependencies.effect_verifiers.get(name)
        observed: bool | None = None
        verifier_name = ""
        if verifier is not None:
            raw_verifier_name = str(getattr(verifier, "__name__", ""))
            verifier_name = (
                raw_verifier_name
                if _SAFE_VERIFIER_ID.fullmatch(raw_verifier_name)
                else "effect_verifier"
            )
            try:
                observed = verifier(action, raw_result)
            except Exception:
                observed = None
        if observed is True:
            if current_state is EffectState.APPLIED:
                dependencies.effect_ledger.transition(
                    effect_id,
                    EffectState.VERIFIED,
                    fencing_token=lease.fencing_token,
                    reason_code="postcondition_passed",
                    verifier=verifier_name,
                )
            else:
                dependencies.effect_ledger.reconcile(
                    effect_id,
                    fencing_token=lease.fencing_token,
                    observed_applied=True,
                    verifier=verifier_name,
                )
            outcome = normalize_action_outcome(
                action,
                raw_result,
                reported_status="worked",
                invoked=True,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
            )
            return (
                replace(
                    outcome,
                    verification=VerificationStatus.PASSED,
                    error_code=(
                        _post_dispatch_termination_reason(post_termination)
                        if post_termination is not None
                        else ""
                    ),
                ),
                duration_ms,
            )
        if observed is False:
            if current_state is EffectState.APPLIED:
                dependencies.effect_ledger.transition(
                    effect_id,
                    EffectState.UNKNOWN,
                    fencing_token=lease.fencing_token,
                    reason_code="postcondition_failed",
                    verifier=verifier_name,
                )
            dependencies.effect_ledger.reconcile(
                effect_id,
                fencing_token=lease.fencing_token,
                observed_applied=False,
                verifier=verifier_name,
            )
            known_failure = normalize_action_outcome(
                action,
                raw_result,
                reported_status="failed",
                invoked=False,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
                error_code="postcondition_failed",
            )
            return replace(known_failure, invoked=True, retry_allowed=False), duration_ms
        if current_state is EffectState.APPLIED:
            dependencies.effect_ledger.transition(
                effect_id,
                EffectState.UNKNOWN,
                fencing_token=lease.fencing_token,
                reason_code=(
                    _post_dispatch_termination_reason(post_termination)
                    if post_termination is not None
                    else "postcondition_unavailable"
                ),
            )
    except Exception as exc:
        return (
            normalize_action_outcome(
                action,
                raw_result,
                reported_status="failed",
                invoked=True,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
                error_code=type(exc).__name__,
            ),
            duration_ms,
        )
    if post_termination is not None:
        return (
            _termination_outcome(
                action,
                post_termination,
                invoked=True,
                result=raw_result,
                effect_id=effect_id,
                idempotency_key=key,
                fencing_token=lease.fencing_token,
            ),
            duration_ms,
        )
    return (
        normalize_action_outcome(
            action,
            raw_result,
            reported_status="failed",
            invoked=True,
            effect_id=effect_id,
            idempotency_key=key,
            fencing_token=lease.fencing_token,
            error_code="postcondition_unavailable",
        ),
        duration_ms,
    )


def dispatch_action(
    name: str,
    args: dict[str, Any],
    cfg: Any,
    *,
    tool_call_id: str | None = None,
    force_approval: bool = False,
    dependencies: DispatchDependencies | None = None,
    render: bool = True,
    queue_position: int | None = None,
    policy_ceiling_code: str = "",
    deadline_monotonic: float | None = None,
    cancellation: DispatchCancellation | None = None,
) -> DispatchResult:
    """Authorize, fence, invoke, normalize, record, and render one action."""

    deps = dependencies or default_dispatch_dependencies()
    control = _DispatchControl(
        dependencies=deps,
        deadline_monotonic=deadline_monotonic,
        cancellation=cancellation,
    )
    termination = control.signal()
    effective_ceiling = policy_ceiling_code or (
        _termination_ceiling_code(termination) if termination is not None else ""
    )
    if render:
        runtime.show_tool_call(name, args, call_id=tool_call_id)
    preflight = runtime.preflight_runtime_tool(
        name,
        args,
        cfg,
        queue_position=queue_position,
        policy_ceiling_code=effective_ceiling,
    )
    action = preflight.policy.action
    if action.effect_class is EffectClass.UNCLASSIFIED or policy_ceiling_code:
        outcome = _preinvoke_outcome(
            action,
            preflight.blocked_result,
            status="denied",
            error_code="preflight_denied",
        )
        return _finalize(
            name=name,
            args=args,
            cfg=cfg,
            tool_call_id=tool_call_id,
            preflight=preflight,
            outcome=outcome,
            duration_ms=0.0,
            render=render,
        )
    if termination is not None:
        outcome = _termination_outcome(action, termination, invoked=False)
        return _finalize(
            name=name,
            args=args,
            cfg=cfg,
            tool_call_id=tool_call_id,
            preflight=preflight,
            outcome=outcome,
            duration_ms=0.0,
            render=render,
        )
    if not preflight.allowed:
        outcome = _preinvoke_outcome(
            action,
            preflight.blocked_result,
            status="denied",
            error_code="preflight_denied",
        )
        return _finalize(
            name=name,
            args=args,
            cfg=cfg,
            tool_call_id=tool_call_id,
            preflight=preflight,
            outcome=outcome,
            duration_ms=0.0,
            render=render,
        )

    try:
        signature = runtime.tool_attempt_signature(name, preflight.signature_args)
    except Exception as exc:
        outcome = _preinvoke_outcome(
            action,
            "Action was not invoked because its audit identity could not be derived safely.",
            status="denied",
            error_code=f"privacy_identity_{type(exc).__name__}",
        )
        return _finalize(
            name=name,
            args=args,
            cfg=cfg,
            tool_call_id=tool_call_id,
            preflight=preflight,
            outcome=outcome,
            duration_ms=0.0,
            render=render,
        )
    previous = runtime.find_failed_attempt(cfg, signature)
    if previous:
        prior_status = str(previous.get("status") or "failed")
        result = (
            "Skipped repeated action with an unresolved outcome. Reconcile it before retrying."
            if prior_status == "unknown_outcome"
            else "Skipped repeated failed attempt. "
            f"Prior outcome: {previous.get('summary', 'same tool path already failed or was denied')}."
        )
        outcome = _preinvoke_outcome(action, result, status="skipped", error_code=prior_status)
        return _finalize(
            name=name,
            args=args,
            cfg=cfg,
            tool_call_id=tool_call_id,
            preflight=preflight,
            outcome=outcome,
            duration_ms=0.0,
            render=render,
        )

    if action.effect_class is EffectClass.EXTERNAL_MUTATION and not tool_call_id:
        outcome = _preinvoke_outcome(
            action,
            "External mutation was not invoked because it has no stable idempotency ID.",
            status="denied",
            error_code="missing_idempotency_id",
        )
        return _finalize(
            name=name,
            args=args,
            cfg=cfg,
            tool_call_id=tool_call_id,
            preflight=preflight,
            outcome=outcome,
            duration_ms=0.0,
            render=render,
        )

    approve = deps.approve or runtime.ask_approval
    if not approve(
        name,
        args,
        cfg,
        force=force_approval,
        preflight=preflight,
    ):
        outcome = _preinvoke_outcome(
            action,
            "User denied this operation.",
            status="denied",
            error_code="approval_denied",
        )
        return _finalize(
            name=name,
            args=args,
            cfg=cfg,
            tool_call_id=tool_call_id,
            preflight=preflight,
            outcome=outcome,
            duration_ms=0.0,
            render=render,
        )

    lease: TargetLease | None = None
    release_error: Exception | None = None
    try:
        termination = control.signal()
        if termination is not None:
            outcome = _termination_outcome(action, termination, invoked=False)
            duration_ms = 0.0
        else:
            if action.effect_class is not EffectClass.OBSERVE:
                lease = deps.lease_manager.acquire(action.target)
                if not lease.validate():
                    raise EffectLeaseError("fresh effect lease failed validation")
            termination = control.signal()
            if termination is not None:
                outcome = _termination_outcome(
                    action,
                    termination,
                    invoked=False,
                    fencing_token=lease.fencing_token if lease is not None else 0,
                )
                duration_ms = 0.0
            elif action.effect_class is EffectClass.EXTERNAL_MUTATION:
                if lease is None:  # pragma: no cover - defensive invariant
                    raise EffectLeaseError("external effect requires a target lease")
                invocation_id = tool_call_id
                if not invocation_id:  # pragma: no cover - checked before approval
                    raise EffectLeaseError("external effect requires an idempotency ID")
                outcome, duration_ms = _run_external_effect(
                    name,
                    preflight.signature_args,
                    cfg,
                    action,
                    invocation_id=invocation_id,
                    dependencies=deps,
                    lease=lease,
                    control=control,
                )
            else:
                started = time.perf_counter()
                try:
                    with scoped_tool_runtime_env(cfg):
                        raw_result = str(deps.invoke(name, dict(preflight.signature_args), cfg))
                except BaseException as exc:
                    raw_result = f"Tool error for {name}: {type(exc).__name__}"
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                termination = control.signal()
                if termination is not None:
                    outcome = _termination_outcome(
                        action,
                        termination,
                        invoked=True,
                        result=raw_result,
                        fencing_token=lease.fencing_token if lease is not None else 0,
                    )
                else:
                    raw_status = runtime.classify_tool_status(raw_result, name=name)
                    outcome = normalize_action_outcome(
                        action,
                        raw_result,
                        reported_status=raw_status,
                        invoked=True,
                        fencing_token=lease.fencing_token if lease is not None else 0,
                        error_code="tool_reported_failure" if raw_status == "failed" else "",
                    )
    except Exception as exc:
        outcome = _preinvoke_outcome(
            action,
            "Action was not invoked because its target effect lease was unavailable.",
            status="failed",
            error_code=type(exc).__name__,
        )
        duration_ms = 0.0
    finally:
        if lease is not None:
            try:
                lease.release()
            except Exception as exc:
                release_error = exc

    if release_error is not None:
        release_error_code = f"lease_release_{type(release_error).__name__}"
        if (
            outcome.status is OutcomeStatus.SUCCEEDED
            and outcome.verification is not VerificationStatus.PASSED
        ):
            outcome = normalize_action_outcome(
                action,
                outcome.result,
                reported_status="failed",
                invoked=outcome.invoked,
                effect_id=outcome.effect_id,
                idempotency_key=outcome.idempotency_key,
                fencing_token=outcome.fencing_token,
                error_code=release_error_code,
            )
        else:
            outcome = replace(outcome, error_code=release_error_code)

    return _finalize(
        name=name,
        args=args,
        cfg=cfg,
        tool_call_id=tool_call_id,
        preflight=preflight,
        outcome=outcome,
        duration_ms=duration_ms,
        render=render,
    )


__all__ = [
    "ApprovalCallback",
    "DispatchCancellation",
    "DispatchDependencies",
    "DispatchResult",
    "EffectVerifier",
    "TRUSTED_ADAPTER_ACTIONS",
    "batch_policy_ceiling_codes",
    "dispatch_action",
    "default_dispatch_dependencies",
]
