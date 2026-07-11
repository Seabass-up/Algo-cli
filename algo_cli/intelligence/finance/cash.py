"""B94. 13-Week Cash Flow Forecast Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from .common import ReviewGate, SourceRef, decimalize, stable_exception_id


@dataclass(frozen=True)
class CashFlowItem:
    id: str
    amount: Decimal | int | str
    due_date: date | str
    category: str
    description: str = ""
    source_refs: list[SourceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimalize(self.amount))
        if isinstance(self.due_date, str):
            object.__setattr__(self, "due_date", datetime.fromisoformat(self.due_date[:10]).date())


@dataclass(frozen=True)
class CashForecastWeek:
    week_number: int
    week_start: date
    beginning_cash: Decimal
    receipts: Decimal
    disbursements: Decimal
    ending_cash: Decimal
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass(frozen=True)
class CashForecast:
    starting_cash: Decimal
    weeks: list[CashForecastWeek]
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW

    @property
    def ending_cash(self) -> Decimal:
        return self.weeks[-1].ending_cash if self.weeks else self.starting_cash


@dataclass(frozen=True)
class ForecastVariance:
    week_number: int
    forecast_receipts: Decimal
    actual_receipts: Decimal
    forecast_disbursements: Decimal
    actual_disbursements: Decimal
    net_difference: Decimal
    explanation: str
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass(frozen=True)
class ForecastVarianceReport:
    variances: list[ForecastVariance]
    total_net_difference: Decimal


class ThirteenWeekCashForecast:
    """Weekly cash bridge: beginning cash + receipts - disbursements."""

    def forecast(
        self,
        open_ar: Iterable[CashFlowItem | dict[str, Any]],
        open_ap: Iterable[CashFlowItem | dict[str, Any]],
        payroll: Iterable[CashFlowItem | dict[str, Any]],
        recurring: Iterable[CashFlowItem | dict[str, Any]],
        starting_cash: Decimal | int | str,
        start_date: date | str | None = None,
    ) -> CashForecast:
        start = _coerce_date(start_date) if start_date else date.today()
        start = start - timedelta(days=start.weekday())
        receipts = [_coerce_item(item, "ar") for item in open_ar]
        disbursements = [_coerce_item(item, "ap") for item in list(open_ap) + list(payroll) + list(recurring)]
        beginning = decimalize(starting_cash)
        weeks: list[CashForecastWeek] = []
        for idx in range(13):
            week_start = start + timedelta(days=idx * 7)
            week_end = week_start + timedelta(days=6)
            week_receipts = [item for item in receipts if week_start <= item.due_date <= week_end]
            week_disb = [item for item in disbursements if week_start <= item.due_date <= week_end]
            receipt_total = sum((abs(item.amount) for item in week_receipts), Decimal("0"))
            disb_total = sum((abs(item.amount) for item in week_disb), Decimal("0"))
            ending = beginning + receipt_total - disb_total
            weeks.append(CashForecastWeek(
                week_number=idx + 1,
                week_start=week_start,
                beginning_cash=beginning,
                receipts=receipt_total,
                disbursements=disb_total,
                ending_cash=ending,
                source_refs=[ref for item in [*week_receipts, *week_disb] for ref in item.source_refs],
            ))
            beginning = ending
        return CashForecast(starting_cash=decimalize(starting_cash), weeks=weeks)

    def variance(self, actuals: Iterable[dict[str, Any]], forecast: CashForecast) -> ForecastVarianceReport:
        by_week = {week.week_number: week for week in forecast.weeks}
        actual_by_week: dict[int, dict[str, Any]] = {}
        for actual in actuals:
            week = int(actual.get("week_number", 0))
            actual_by_week[week] = actual
        variances: list[ForecastVariance] = []
        for week_number, week in by_week.items():
            actual = actual_by_week.get(week_number, {})
            actual_receipts = decimalize(actual.get("receipts", 0))
            actual_disb = decimalize(actual.get("disbursements", 0))
            if actual_receipts == 0 and actual_disb == 0 and week.receipts == 0 and week.disbursements == 0:
                continue
            net_diff = (actual_receipts - actual_disb) - (week.receipts - week.disbursements)
            if net_diff == 0:
                continue
            explanation = _variance_explanation(week.receipts, actual_receipts, week.disbursements, actual_disb)
            variances.append(ForecastVariance(
                week_number=week_number,
                forecast_receipts=week.receipts,
                actual_receipts=actual_receipts,
                forecast_disbursements=week.disbursements,
                actual_disbursements=actual_disb,
                net_difference=net_diff,
                explanation=explanation,
                source_refs=list(actual.get("source_refs", [])),
            ))
        total = sum((variance.net_difference for variance in variances), Decimal("0"))
        return ForecastVarianceReport(variances=variances, total_net_difference=total)


def _coerce_item(item: CashFlowItem | dict[str, Any], default_category: str) -> CashFlowItem:
    if isinstance(item, CashFlowItem):
        return item
    return CashFlowItem(
        id=str(item.get("id") or stable_exception_id("B94", [item.get("amount"), item.get("due_date"), item.get("description")])) ,
        amount=item.get("amount", 0),
        due_date=item.get("due_date") or item.get("date"),
        category=str(item.get("category", default_category)),
        description=str(item.get("description", "")),
        source_refs=list(item.get("source_refs", [])),
    )


def _coerce_date(value: date | str) -> date:
    return datetime.fromisoformat(value[:10]).date() if isinstance(value, str) else value


def _variance_explanation(f_receipts: Decimal, a_receipts: Decimal, f_disb: Decimal, a_disb: Decimal) -> str:
    receipt_diff = a_receipts - f_receipts
    disb_diff = a_disb - f_disb
    parts: list[str] = []
    if receipt_diff:
        parts.append("receipt timing/amount variance")
    if disb_diff:
        parts.append("disbursement timing/amount variance")
    return "; ".join(parts) if parts else "no variance"
