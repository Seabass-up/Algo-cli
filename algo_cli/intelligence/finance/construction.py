"""B96/B100/B101. Construction WIP, fixed assets, payroll reconciliations."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .common import ReviewGate, SourceRef, decimalize, stable_exception_id, within_tolerance


@dataclass(frozen=True)
class JobCost:
    id: str
    contract_value: Decimal | int | str
    costs_to_date: Decimal | int | str
    estimated_total_cost: Decimal | int | str
    billed_to_date: Decimal | int | str
    source_refs: list[SourceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        for attr in ("contract_value", "costs_to_date", "estimated_total_cost", "billed_to_date"):
            object.__setattr__(self, attr, decimalize(getattr(self, attr)))


@dataclass(frozen=True)
class WIPRow:
    job_id: str
    percent_complete: Decimal
    earned_revenue: Decimal
    billed_to_date: Decimal
    over_under_billing: Decimal
    source_refs: list[SourceRef]


@dataclass(frozen=True)
class WIPException:
    id: str
    job_id: str
    amount: Decimal
    message: str
    source_refs: list[SourceRef]
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


@dataclass(frozen=True)
class WIPReport:
    rows: list[WIPRow]
    total_over_under: Decimal
    exceptions: list[WIPException]


class ConstructionWIPController:
    def compute_wip(self, jobs: Iterable[JobCost | dict[str, Any]], materiality: Decimal | int | str = Decimal("1000")) -> WIPReport:
        rows: list[WIPRow] = []
        exceptions: list[WIPException] = []
        threshold = decimalize(materiality)
        for job in [_coerce_job(row) for row in jobs]:
            pct = Decimal("0") if job.estimated_total_cost == 0 else min(job.costs_to_date / job.estimated_total_cost, Decimal("1"))
            earned = job.contract_value * pct
            over_under = job.billed_to_date - earned
            row = WIPRow(job.id, pct, earned, job.billed_to_date, over_under, list(job.source_refs))
            rows.append(row)
            if abs(over_under) >= threshold:
                message = "overbilling" if over_under > 0 else "underbilling"
                exceptions.append(WIPException(stable_exception_id("B96", [job.id, over_under]), job.id, over_under, message, list(job.source_refs)))
        return WIPReport(rows=rows, total_over_under=sum((row.over_under_billing for row in rows), Decimal("0")), exceptions=exceptions)

    def flag_over_under_billing(self, jobs: Iterable[JobCost | dict[str, Any]], materiality: Decimal | int | str = Decimal("1000")) -> list[WIPException]:
        return self.compute_wip(jobs, materiality=materiality).exceptions


@dataclass(frozen=True)
class RollForwardResult:
    clean: bool
    beginning: Decimal
    additions: Decimal
    disposals: Decimal
    depreciation: Decimal
    ending: Decimal
    expected_ending: Decimal
    difference: Decimal
    source_refs: list[SourceRef] = field(default_factory=list)


class FixedAssetRollForward:
    def roll_forward(
        self,
        assets: Iterable[dict[str, Any]],
        additions: Iterable[dict[str, Any]],
        disposals: Iterable[dict[str, Any]],
        depreciation: Iterable[dict[str, Any]],
        tolerance: Decimal | int | str = Decimal("0"),
    ) -> RollForwardResult:
        beginning = sum((decimalize(row.get("beginning_nbv", row.get("amount", 0))) for row in assets), Decimal("0"))
        ending = sum((decimalize(row.get("ending_nbv", row.get("ending", row.get("amount", 0)))) for row in assets), Decimal("0"))
        add = sum((decimalize(row.get("amount", 0)) for row in additions), Decimal("0"))
        disp = sum((decimalize(row.get("amount", 0)) for row in disposals), Decimal("0"))
        dep = sum((decimalize(row.get("amount", 0)) for row in depreciation), Decimal("0"))
        expected = beginning + add - disp - dep
        refs = [ref for row in [*assets, *additions, *disposals, *depreciation] for ref in row.get("source_refs", [])]
        return RollForwardResult(within_tolerance(ending, expected, tolerance), beginning, add, disp, dep, ending, expected, ending - expected, refs)


@dataclass(frozen=True)
class PayrollReconResult:
    clean: bool
    register_total: Decimal
    gl_total: Decimal
    tax_liability_total: Decimal
    differences: dict[str, Decimal]
    source_refs: list[SourceRef]


class PayrollReconciler:
    def reconcile(
        self,
        payroll_register: Iterable[dict[str, Any]],
        gl: Iterable[dict[str, Any]],
        tax_liabilities: Iterable[dict[str, Any]],
        tolerance: Decimal | int | str = Decimal("0"),
    ) -> PayrollReconResult:
        register_total = sum((decimalize(row.get("gross_pay", row.get("amount", 0))) for row in payroll_register), Decimal("0"))
        gl_total = sum((decimalize(row.get("amount", 0)) for row in gl), Decimal("0"))
        tax_total = sum((decimalize(row.get("amount", 0)) for row in tax_liabilities), Decimal("0"))
        differences = {"register_to_gl": register_total - gl_total, "tax_liability": tax_total}
        refs = [ref for row in [*payroll_register, *gl, *tax_liabilities] for ref in row.get("source_refs", [])]
        clean = within_tolerance(register_total, gl_total, tolerance) and bool(tax_liabilities)
        return PayrollReconResult(clean, register_total, gl_total, tax_total, differences, refs)


def _coerce_job(row: JobCost | dict[str, Any]) -> JobCost:
    if isinstance(row, JobCost):
        return row
    return JobCost(str(row["id"]), row.get("contract_value", 0), row.get("costs_to_date", 0), row.get("estimated_total_cost", 0), row.get("billed_to_date", 0), list(row.get("source_refs", [])))
