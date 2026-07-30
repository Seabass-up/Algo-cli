"""Typed, fail-closed outcomes for every Algo CLI action invocation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .marcus_authority import (
    EffectClass,
    IdempotencyClass,
    OutcomeModel,
    ResolvedAction,
    VerificationRequirement,
)


class OutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN_OUTCOME = "unknown_outcome"


class VerificationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PASSED = "passed"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActionOutcome:
    """One in-memory outcome; durable stores must use its content-free receipt."""

    action: str
    status: OutcomeStatus
    result: str
    invoked: bool
    retry_allowed: bool
    verification: VerificationStatus
    effect_id: str = ""
    idempotency_key: str = ""
    fencing_token: int = 0
    error_code: str = ""
    deduplicated: bool = False
    compensation_action: str = ""

    @property
    def worked(self) -> bool:
        return self.status is OutcomeStatus.SUCCEEDED

    @property
    def is_unknown(self) -> bool:
        return self.status is OutcomeStatus.UNKNOWN_OUTCOME

    def model_text(self) -> str:
        text = str(self.result)
        if self.is_unknown and "unknown outcome" not in text.casefold():
            warning = (
                "Unknown outcome: the action may have taken effect. Do not retry automatically; "
                "reconcile with a fresh observation first."
            )
            return f"{text}\n\n{warning}" if text.strip() else warning
        if self.status is OutcomeStatus.TIMED_OUT and "timed out outcome" not in text.casefold():
            warning = "Timed out outcome: the action exceeded its dispatch deadline."
            return f"{text}\n\n{warning}" if text.strip() else warning
        if self.status is OutcomeStatus.CANCELLED and "cancelled outcome" not in text.casefold():
            warning = "Cancelled outcome: the caller stopped the action."
            return f"{text}\n\n{warning}" if text.strip() else warning
        return text

    def receipt(self) -> dict[str, Any]:
        """Return content-free fields safe for an effect or telemetry ledger."""

        return {
            "action": self.action,
            "status": self.status.value,
            "invoked": self.invoked,
            "retry_allowed": self.retry_allowed,
            "verification": self.verification.value,
            "effect_id": self.effect_id,
            "idempotency_key": self.idempotency_key,
            "fencing_token": self.fencing_token,
            "error_code": self.error_code,
            "deduplicated": self.deduplicated,
            "compensation_action": self.compensation_action,
        }


def normalize_action_outcome(
    action: ResolvedAction,
    result: str,
    *,
    reported_status: str,
    invoked: bool,
    effect_id: str = "",
    idempotency_key: str = "",
    fencing_token: int = 0,
    error_code: str = "",
    deduplicated: bool = False,
) -> ActionOutcome:
    """Normalize legacy result signals without treating uncertainty as failure."""

    normalized = str(reported_status or "").strip().casefold()
    if normalized == "denied":
        status = OutcomeStatus.DENIED
    elif normalized == "skipped":
        status = OutcomeStatus.SKIPPED
    elif normalized in {"timed_out", "timeout"}:
        status = OutcomeStatus.TIMED_OUT
    elif normalized in {"cancelled", "canceled"}:
        status = OutcomeStatus.CANCELLED
    elif normalized in {"worked", "succeeded", "success"}:
        status = OutcomeStatus.SUCCEEDED
    elif (
        invoked
        and action.effect_class is not EffectClass.OBSERVE
        and action.outcome_model is OutcomeModel.UNKNOWN_POSSIBLE
    ):
        status = OutcomeStatus.UNKNOWN_OUTCOME
    else:
        status = OutcomeStatus.FAILED

    retry_allowed = (
        status
        in {
            OutcomeStatus.FAILED,
            OutcomeStatus.TIMED_OUT,
            OutcomeStatus.CANCELLED,
        }
        and (not invoked or action.idempotency in {IdempotencyClass.PURE, IdempotencyClass.IDEMPOTENT})
    )
    if status is OutcomeStatus.UNKNOWN_OUTCOME:
        verification = VerificationStatus.UNKNOWN
        retry_allowed = False
    elif status is not OutcomeStatus.SUCCEEDED:
        verification = VerificationStatus.FAILED
    elif action.verification in {
        VerificationRequirement.NONE,
        VerificationRequirement.STRUCTURED_RESULT,
    }:
        verification = VerificationStatus.PASSED
    else:
        verification = VerificationStatus.PENDING

    return ActionOutcome(
        action=action.name,
        status=status,
        result=str(result),
        invoked=invoked,
        retry_allowed=retry_allowed,
        verification=verification,
        effect_id=effect_id,
        idempotency_key=idempotency_key,
        fencing_token=fencing_token,
        error_code=error_code,
        deduplicated=deduplicated,
        compensation_action=action.compensation_action,
    )


__all__ = [
    "ActionOutcome",
    "OutcomeStatus",
    "VerificationStatus",
    "normalize_action_outcome",
]
