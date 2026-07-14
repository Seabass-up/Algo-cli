"""B105. Controller Exception Queue with Materiality Gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable

from .common import (
    FinanceException,
    MaterialityPolicy,
    ReviewGate,
    Severity,
    decimalize,
    stable_exception_id,
)


@dataclass
class ExceptionCluster:
    key: str
    exceptions: list[FinanceException]
    highest_severity: Severity
    total_amount: Decimal
    representative: FinanceException


@dataclass
class WaiverResult:
    approved: bool
    exception: FinanceException
    message: str


@dataclass
class QueueSummary:
    total: int
    open_count: int
    waived_count: int
    requires_review_count: int
    by_pattern: dict[str, int] = field(default_factory=dict)


class ControllerExceptionQueue:
    """Finite controller queue that ranks exceptions by materiality and risk.

    This queue is intentionally non-mutating with respect to accounting systems:
    it routes review work and waiver metadata only.  It never posts entries,
    approves payments, or sends external communications.
    """

    def __init__(self) -> None:
        self._exceptions: dict[str, FinanceException] = {}

    def add(self, exception: FinanceException) -> None:
        self._exceptions[exception.id] = exception

    def extend(self, exceptions: Iterable[FinanceException]) -> None:
        for exception in exceptions:
            self.add(exception)

    def all(self) -> list[FinanceException]:
        return list(self._exceptions.values())

    def rank(
        self,
        materiality_policy: MaterialityPolicy | None = None,
        as_of: date | None = None,
        include_waived: bool = False,
    ) -> list[FinanceException]:
        policy = materiality_policy or MaterialityPolicy()
        rows = [ex for ex in self._exceptions.values() if include_waived or not ex.waived]
        return sorted(
            rows,
            key=lambda ex: (
                policy.score_exception(ex, as_of=as_of),
                int(ex.severity),
                abs(ex.amount or Decimal("0")),
                ex.id,
            ),
            reverse=True,
        )

    def cluster_duplicates(self) -> list[ExceptionCluster]:
        clusters: dict[str, list[FinanceException]] = {}
        for exception in self._exceptions.values():
            key = _cluster_key(exception)
            clusters.setdefault(key, []).append(exception)

        out: list[ExceptionCluster] = []
        for key, exceptions in clusters.items():
            total = sum((abs(ex.amount or Decimal("0")) for ex in exceptions), Decimal("0"))
            representative = sorted(
                exceptions,
                key=lambda ex: (int(ex.severity), abs(ex.amount or Decimal("0")), ex.id),
                reverse=True,
            )[0]
            out.append(ExceptionCluster(
                key=key,
                exceptions=exceptions,
                highest_severity=max((ex.severity for ex in exceptions), default=Severity.INFO),
                total_amount=total,
                representative=representative,
            ))
        return sorted(out, key=lambda c: (len(c.exceptions), int(c.highest_severity), c.total_amount), reverse=True)

    def require_approval_for_waiver(
        self,
        exception_id: str,
        rationale: str | None,
        approver: str | None,
        expires: date | None,
    ) -> WaiverResult:
        exception = self._exceptions[exception_id]
        if not rationale or not approver or expires is None:
            return WaiverResult(False, exception, "Waiver requires rationale, approver, and expiration period.")
        exception.waived = True
        exception.waiver_rationale = rationale
        exception.waiver_approver = approver
        exception.waiver_expires = expires
        exception.review_gate = ReviewGate.CONTROLLER_REVIEW
        return WaiverResult(True, exception, "Waiver recorded for controller review trail.")

    def summary(self) -> QueueSummary:
        by_pattern: dict[str, int] = {}
        for exception in self._exceptions.values():
            by_pattern[exception.pattern_id] = by_pattern.get(exception.pattern_id, 0) + 1
        return QueueSummary(
            total=len(self._exceptions),
            open_count=sum(1 for ex in self._exceptions.values() if not ex.waived),
            waived_count=sum(1 for ex in self._exceptions.values() if ex.waived),
            requires_review_count=sum(1 for ex in self._exceptions.values() if ex.requires_human_review),
            by_pattern=by_pattern,
        )


def make_exception(
    pattern_id: str,
    message: str,
    severity: Severity = Severity.MEDIUM,
    amount: Decimal | int | str | None = None,
    tags: Iterable[str] = (),
    source_refs=None,
) -> FinanceException:
    key = [pattern_id, message, str(amount), sorted(str(tag).lower() for tag in tags)]
    return FinanceException(
        id=stable_exception_id(pattern_id, key),
        pattern_id=pattern_id,
        severity=severity,
        amount=decimalize(amount) if amount is not None else None,
        message=message,
        source_refs=list(source_refs or []),
        tags=set(tags),
    )


def _cluster_key(exception: FinanceException) -> str:
    root_tags = sorted(tag for tag in exception.tags if tag.startswith("root:") or tag.startswith("account:"))
    if root_tags:
        return "|".join([exception.pattern_id, *root_tags])
    source_ids = sorted(ref.source_id for ref in exception.source_refs)
    normalized_message = " ".join(exception.message.lower().split()[:8])
    return "|".join([exception.pattern_id, normalized_message, *source_ids])
