"""B104. CPA Workpaper Tie-Out and Crossfoot Rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .common import ReviewGate, SourceRef, decimalize, within_tolerance


@dataclass
class TieOutResult:
    clean: bool
    check_type: str
    expected: Decimal
    actual: Decimal
    difference: Decimal
    tolerance_used: Decimal
    message: str
    source_refs: list[SourceRef] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


class WorkpaperTieOut:
    """Foot, crossfoot, trace, and sign-convention checks for schedules."""

    def foot(
        self,
        table: Iterable[dict[str, Any]],
        columns: Iterable[str],
        expected_totals: dict[str, Any] | None = None,
        tolerance: Any = Decimal("0"),
    ) -> dict[str, TieOutResult]:
        rows = list(table)
        results: dict[str, TieOutResult] = {}
        for column in columns:
            actual = sum((decimalize(row.get(column, 0)) for row in rows), Decimal("0"))
            expected = decimalize((expected_totals or {}).get(column, actual))
            difference = actual - expected
            clean = within_tolerance(actual, expected, tolerance)
            results[column] = TieOutResult(
                clean=clean,
                check_type="foot",
                expected=expected,
                actual=actual,
                difference=difference,
                tolerance_used=decimalize(tolerance),
                message="Foots" if clean else f"Column {column} does not foot",
            )
        return results

    def crossfoot(
        self,
        table: Iterable[dict[str, Any]],
        row_total_col: str,
        component_cols: Iterable[str],
        tolerance: Any = Decimal("0"),
    ) -> list[TieOutResult]:
        results: list[TieOutResult] = []
        comps = list(component_cols)
        for index, row in enumerate(table, start=1):
            expected = sum((decimalize(row.get(col, 0)) for col in comps), Decimal("0"))
            actual = decimalize(row.get(row_total_col, 0))
            clean = within_tolerance(actual, expected, tolerance)
            results.append(TieOutResult(
                clean=clean,
                check_type="crossfoot",
                expected=expected,
                actual=actual,
                difference=actual - expected,
                tolerance_used=decimalize(tolerance),
                message="Crossfoots" if clean else f"Row {index} does not crossfoot",
            ))
        return results

    def trace_to_source(
        self,
        schedule_total: Any,
        source_total: Any,
        tolerance: Any = Decimal("0"),
        source_refs: Iterable[SourceRef] = (),
    ) -> TieOutResult:
        actual = decimalize(schedule_total)
        expected = decimalize(source_total)
        clean = within_tolerance(actual, expected, tolerance)
        return TieOutResult(
            clean=clean,
            check_type="trace",
            expected=expected,
            actual=actual,
            difference=actual - expected,
            tolerance_used=decimalize(tolerance),
            message="Ties to source" if clean else "Schedule total does not tie to source",
            source_refs=list(source_refs),
        )

    def sign_convention_check(self, amount: Any, expected_sign: str, mapped: bool = False) -> TieOutResult:
        actual = decimalize(amount)
        expected_positive = expected_sign.lower() in {"positive", "+", "debit"}
        sign_ok = actual >= 0 if expected_positive else actual <= 0
        clean = sign_ok or mapped
        return TieOutResult(
            clean=clean,
            check_type="sign_convention",
            expected=Decimal("1") if expected_positive else Decimal("-1"),
            actual=Decimal("1") if actual >= 0 else Decimal("-1"),
            difference=Decimal("0") if clean else actual,
            tolerance_used=Decimal("0"),
            message="Sign convention explicit" if clean else "Sign flip requires explicit mapping",
        )
