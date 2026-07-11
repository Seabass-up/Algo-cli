"""Construction payment, lien/bond, waiver, and change-notice patterns B109-B113."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from .common import (
    ConstructionIssue,
    Deadline,
    Jurisdiction,
    ReviewGate,
    RiskLevel,
    SourceRef,
    add_business_days,
    add_months_approx,
    decimalize,
    make_issue,
    parse_date,
    source_from_mapping,
)


@dataclass(frozen=True)
class ChangeEvent:
    id: str
    description: str
    event_date: date
    trigger: str = "change"
    written_authorization: bool = False
    work_started: bool = False
    backup_complete: bool = False
    source_refs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class ChangeNotice:
    event_id: str
    notice_due: date
    backup_due: date | None
    status: str
    issues: tuple[ConstructionIssue, ...] = ()


@dataclass
class ChangeNoticeResult:
    pattern_id: str = "B109"
    notices: list[ChangeNotice] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW


class ChangeNoticeEngine:
    """B109: compute change-order notice/time-bar review dates."""

    pattern_id = "B109"

    def analyze(
        self,
        events: Iterable[ChangeEvent | Mapping[str, Any]],
        clause_text: str = "",
        *,
        default_notice_days: int = 7,
        backup_days: int | None = None,
    ) -> ChangeNoticeResult:
        result = ChangeNoticeResult()
        notice_days = self._extract_days(clause_text, default_notice_days)
        for raw in events:
            event = raw if isinstance(raw, ChangeEvent) else self._coerce_event(raw)
            notice_due = event.event_date + timedelta(days=notice_days)
            backup_due = event.event_date + timedelta(days=backup_days) if backup_days is not None else None
            issues: list[ConstructionIssue] = []
            if event.work_started and not event.written_authorization:
                issues.append(
                    make_issue(
                        self.pattern_id,
                        "verbal or unauthorized extra work",
                        f"Event {event.id} appears to have started without written change authorization.",
                        risk_level=RiskLevel.HIGH,
                        review_gate=ReviewGate.BUSINESS_REVIEW,
                        source_refs=event.source_refs,
                        tags={"change_order", "written_authorization"},
                        deadline=notice_due,
                    )
                )
            if not event.backup_complete:
                issues.append(
                    make_issue(
                        self.pattern_id,
                        "change backup incomplete",
                        f"Event {event.id} lacks complete cost/schedule backup for change package.",
                        risk_level=RiskLevel.MEDIUM,
                        review_gate=ReviewGate.BUSINESS_REVIEW,
                        source_refs=event.source_refs,
                        tags={"change_order", "backup"},
                        deadline=backup_due or notice_due,
                    )
                )
            status = "late-risk" if date.today() > notice_due else "open"
            notice = ChangeNotice(event.id, notice_due, backup_due, status, tuple(issues))
            result.notices.append(notice)
            result.issues.extend(issues)
        return result

    @staticmethod
    def _extract_days(text: str, fallback: int) -> int:
        lowered = text.lower()
        if "immediate" in lowered or "immediately" in lowered or "24 hour" in lowered:
            return 1
        match = re.search(r"(\d+)\s*(?:calendar\s*)?days?", lowered)
        return int(match.group(1)) if match else fallback

    @staticmethod
    def _coerce_event(raw: Mapping[str, Any]) -> ChangeEvent:
        event_date = parse_date(raw.get("event_date") or raw.get("date")) or date.today()
        return ChangeEvent(
            id=str(raw.get("id") or raw.get("description") or "event"),
            description=str(raw.get("description") or raw.get("text") or ""),
            event_date=event_date,
            trigger=str(raw.get("trigger") or "change"),
            written_authorization=bool(raw.get("written_authorization") or raw.get("written_auth")),
            work_started=bool(raw.get("work_started")),
            backup_complete=bool(raw.get("backup_complete")),
            source_refs=source_from_mapping(raw, "change_event"),
        )


@dataclass(frozen=True)
class PayApplication:
    id: str
    amount: Decimal
    submitted_date: date
    proper: bool = True
    disputed_amount: Decimal = Decimal("0")
    owner_payment_date: date | None = None
    contractor_payment_date: date | None = None
    source_refs: tuple[SourceRef, ...] = ()

    @property
    def undisputed_amount(self) -> Decimal:
        return max(Decimal("0"), self.amount - self.disputed_amount)


@dataclass
class PaymentAnalysis:
    pattern_id: str = "B110"
    retainage_rate: Decimal | None = None
    deadlines: list[Deadline] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW


class PaymentRetainageAnalyzer:
    """B110: extract/validate payment, retainage, and pay-app timing risks."""

    pattern_id = "B110"

    def analyze(
        self,
        pay_apps: Iterable[PayApplication | Mapping[str, Any]] = (),
        *,
        retainage_rate: Decimal | str | int | None = None,
        jurisdiction: Jurisdiction | None = None,
        substantial_completion: date | str | None = None,
    ) -> PaymentAnalysis:
        jurisdiction = jurisdiction or Jurisdiction()
        result = PaymentAnalysis(retainage_rate=decimalize(retainage_rate) if retainage_rate is not None else None)
        if jurisdiction.is_kansas_private and result.retainage_rate is not None:
            if result.retainage_rate > Decimal("10"):
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "Kansas retainage cap review",
                        "Kansas construction retainage appears above 10%; review K.S.A. 16-1904 cap/exception requirements.",
                        risk_level=RiskLevel.CRITICAL,
                        review_gate=ReviewGate.ATTORNEY_REVIEW,
                        tags={"kansas", "retainage", "cap"},
                    )
                )
            elif result.retainage_rate > Decimal("5"):
                result.issues.append(
                    make_issue(
                        self.pattern_id,
                        "Kansas retainage above default",
                        "Kansas retainage appears above 5%; confirm documented reason and statutory compliance.",
                        risk_level=RiskLevel.MEDIUM,
                        review_gate=ReviewGate.ATTORNEY_REVIEW,
                        tags={"kansas", "retainage"},
                    )
                )
        completion = parse_date(substantial_completion)
        if jurisdiction.is_kansas_private and completion:
            result.deadlines.append(
                Deadline(
                    "Kansas retainage release review",
                    completion + timedelta(days=30),
                    "K.S.A. 16-1904 remaining retainage due review after substantial completion on undisputed amounts",
                    self.pattern_id,
                    ReviewGate.ATTORNEY_REVIEW,
                )
            )
        for raw in pay_apps:
            pay_app = raw if isinstance(raw, PayApplication) else self._coerce_pay_app(raw)
            if not pay_app.proper or pay_app.undisputed_amount <= 0:
                continue
            if jurisdiction.is_kansas_private:
                owner_due = pay_app.submitted_date + timedelta(days=30)
                result.deadlines.append(
                    Deadline(f"owner payment review {pay_app.id}", owner_due, "K.S.A. 16-1803 30-day owner payment review", self.pattern_id)
                )
                if pay_app.owner_payment_date:
                    sub_due = add_business_days(pay_app.owner_payment_date, 7)
                    result.deadlines.append(
                        Deadline(f"subcontractor payment review {pay_app.id}", sub_due, "K.S.A. 16-1803 seven-business-day subcontractor payment review", self.pattern_id)
                    )
                    if pay_app.contractor_payment_date and pay_app.contractor_payment_date > sub_due:
                        result.issues.append(
                            make_issue(
                                self.pattern_id,
                                "Kansas subcontractor payment late-risk",
                                f"Pay app {pay_app.id} appears paid after the seven-business-day Kansas review window.",
                                risk_level=RiskLevel.HIGH,
                                review_gate=ReviewGate.ATTORNEY_REVIEW,
                                source_refs=pay_app.source_refs,
                                tags={"kansas", "prompt_pay"},
                                amount=pay_app.undisputed_amount,
                                deadline=sub_due,
                            )
                        )
        return result

    @staticmethod
    def _coerce_pay_app(raw: Mapping[str, Any]) -> PayApplication:
        return PayApplication(
            id=str(raw.get("id") or raw.get("pay_app") or "pay_app"),
            amount=decimalize(raw.get("amount")),
            submitted_date=parse_date(raw.get("submitted_date") or raw.get("date")) or date.today(),
            proper=bool(raw.get("proper", True)),
            disputed_amount=decimalize(raw.get("disputed_amount")),
            owner_payment_date=parse_date(raw.get("owner_payment_date")),
            contractor_payment_date=parse_date(raw.get("contractor_payment_date") or raw.get("paid_date")),
            source_refs=source_from_mapping(raw, "pay_app"),
        )


@dataclass
class RightsCalendar:
    pattern_id: str = "B111"
    deadlines: list[Deadline] = field(default_factory=list)
    issues: list[ConstructionIssue] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW


class LienBondDeadlineCalendar:
    """B111: compute review dates for lien/bond rights. Draft calendar only."""

    pattern_id = "B111"

    def build(
        self,
        *,
        jurisdiction: Jurisdiction,
        last_work_date: date | str | None,
        unpaid: bool = True,
        second_tier: bool = False,
    ) -> RightsCalendar:
        result = RightsCalendar()
        last_work = parse_date(last_work_date)
        if not last_work:
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "missing last-work date",
                    "Cannot compute lien/bond review dates without a supported last labor/material date.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"deadline", "missing_date"},
                )
            )
            return result
        if jurisdiction.is_federal:
            if second_tier:
                result.deadlines.append(
                    Deadline("Miller Act 90-day prime notice review", last_work + timedelta(days=90), "40 U.S.C. 3133(b)(2)", self.pattern_id)
                )
            if unpaid:
                result.deadlines.append(
                    Deadline("Miller Act unpaid-90-day action review", last_work + timedelta(days=90), "40 U.S.C. 3133(b)(1)", self.pattern_id)
                )
            result.deadlines.append(
                Deadline("Miller Act one-year suit review", last_work + timedelta(days=365), "40 U.S.C. 3133(b)(4)", self.pattern_id)
            )
        elif jurisdiction.is_kansas_private:
            result.deadlines.append(
                Deadline("Kansas lien filing review", add_months_approx(last_work, 3), "K.S.A. 60-1103(a)(1)", self.pattern_id)
            )
            if jurisdiction.project_use.value != "residential":
                result.deadlines.append(
                    Deadline("Kansas nonresidential extension notice review", add_months_approx(last_work, 3), "K.S.A. 60-1103(e)", self.pattern_id)
                )
                result.deadlines.append(
                    Deadline("Kansas extended lien filing review", add_months_approx(last_work, 5), "K.S.A. 60-1103(e)", self.pattern_id)
                )
        else:
            result.issues.append(
                make_issue(
                    self.pattern_id,
                    "jurisdiction unknown for rights calendar",
                    "Project state/type is not sufficient to compute reliable lien/bond review dates.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"jurisdiction", "deadline"},
                )
            )
        return result


class WaiverType(str, Enum):
    CONDITIONAL_PROGRESS = "conditional_progress"
    UNCONDITIONAL_PROGRESS = "unconditional_progress"
    CONDITIONAL_FINAL = "conditional_final"
    UNCONDITIONAL_FINAL = "unconditional_final"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LienWaiverReview:
    pattern_id: str
    waiver_type: WaiverType
    covered_amount: Decimal
    stop: bool
    issues: tuple[ConstructionIssue, ...]
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW


class LienWaiverClassifier:
    """B112: classify lien waivers and stop overbroad/unpaid final waivers."""

    pattern_id = "B112"

    def classify(
        self,
        waiver_text: str,
        *,
        covered_amount: Decimal | str | int = Decimal("0"),
        cleared_payment_amount: Decimal | str | int = Decimal("0"),
        open_retainage: Decimal | str | int = Decimal("0"),
        open_claims: Decimal | str | int = Decimal("0"),
    ) -> LienWaiverReview:
        wtype = self._waiver_type(waiver_text)
        covered = decimalize(covered_amount)
        cleared = decimalize(cleared_payment_amount)
        retainage = decimalize(open_retainage)
        claims = decimalize(open_claims)
        issues: list[ConstructionIssue] = []
        stop = False
        if "unconditional" in wtype.value and covered > cleared:
            stop = True
            issues.append(
                make_issue(
                    self.pattern_id,
                    "unconditional waiver before cleared payment",
                    "Unconditional waiver covers more than cleared payment; do not sign/send without attorney/business approval.",
                    risk_level=RiskLevel.CRITICAL,
                    review_gate=ReviewGate.STOP_EXTERNAL_ACTION,
                    tags={"lien_waiver", "unconditional", "payment"},
                    amount=covered - cleared,
                )
            )
        if "final" in wtype.value and (retainage > 0 or claims > 0):
            stop = True
            issues.append(
                make_issue(
                    self.pattern_id,
                    "final waiver with open items",
                    "Final waiver appears to coexist with open retainage/claims/change orders.",
                    risk_level=RiskLevel.CRITICAL,
                    review_gate=ReviewGate.STOP_EXTERNAL_ACTION,
                    tags={"lien_waiver", "final", "open_items"},
                    amount=retainage + claims,
                )
            )
        if wtype is WaiverType.UNKNOWN:
            issues.append(
                make_issue(
                    self.pattern_id,
                    "waiver type unknown",
                    "Could not classify waiver as conditional/unconditional and progress/final.",
                    risk_level=RiskLevel.HIGH,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    tags={"lien_waiver", "unknown"},
                )
            )
        return LienWaiverReview(self.pattern_id, wtype, covered, stop, tuple(issues))

    @staticmethod
    def _waiver_type(text: str) -> WaiverType:
        lowered = text.lower()
        conditional = "conditional" in lowered and "unconditional" not in lowered
        unconditional = "unconditional" in lowered
        final = "final" in lowered
        progress = "progress" in lowered or "partial" in lowered
        if conditional and final:
            return WaiverType.CONDITIONAL_FINAL
        if unconditional and final:
            return WaiverType.UNCONDITIONAL_FINAL
        if conditional and progress:
            return WaiverType.CONDITIONAL_PROGRESS
        if unconditional and progress:
            return WaiverType.UNCONDITIONAL_PROGRESS
        if conditional:
            return WaiverType.CONDITIONAL_PROGRESS
        if unconditional:
            return WaiverType.UNCONDITIONAL_PROGRESS
        return WaiverType.UNKNOWN


class ContingentPaymentType(str, Enum):
    PAY_IF_PAID = "pay_if_paid_candidate"
    PAY_WHEN_PAID = "pay_when_paid_candidate"
    ORDINARY = "ordinary_payment_term"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContingentPaymentReview:
    pattern_id: str
    payment_type: ContingentPaymentType
    notes: tuple[str, ...]
    issues: tuple[ConstructionIssue, ...]
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW


class ContingentPaymentRiskGate:
    """B113: distinguish pay-if-paid/pay-when-paid candidates with jurisdiction overlay."""

    pattern_id = "B113"

    def analyze(self, clause_text: str, *, jurisdiction: Jurisdiction | None = None) -> ContingentPaymentReview:
        jurisdiction = jurisdiction or Jurisdiction()
        lowered = clause_text.lower()
        if "condition precedent" in lowered or "only if" in lowered and "owner" in lowered:
            ptype = ContingentPaymentType.PAY_IF_PAID
        elif "receipt" in lowered and "owner" in lowered or "paid by owner" in lowered:
            ptype = ContingentPaymentType.PAY_WHEN_PAID
        elif "payment" in lowered:
            ptype = ContingentPaymentType.ORDINARY
        else:
            ptype = ContingentPaymentType.UNKNOWN
        notes: list[str] = []
        issues: list[ConstructionIssue] = []
        if ptype in {ContingentPaymentType.PAY_IF_PAID, ContingentPaymentType.PAY_WHEN_PAID}:
            issues.append(
                make_issue(
                    self.pattern_id,
                    "contingent payment clause",
                    f"Clause classified as {ptype.value}; enforceability and payment-right impact require attorney review.",
                    risk_level=RiskLevel.HIGH if ptype is ContingentPaymentType.PAY_IF_PAID else RiskLevel.MEDIUM,
                    review_gate=ReviewGate.ATTORNEY_REVIEW,
                    source_refs=(SourceRef("payment_clause", quote=clause_text),),
                    tags={"payment", "contingent", ptype.value},
                )
            )
        if jurisdiction.is_kansas_private and ptype in {ContingentPaymentType.PAY_IF_PAID, ContingentPaymentType.PAY_WHEN_PAID}:
            notes.append("Kansas private construction overlay: K.S.A. 16-1803(c) says contingent payment is no defense to mechanic's lien/bond enforcement.")
        return ContingentPaymentReview(self.pattern_id, ptype, tuple(notes), tuple(issues))
