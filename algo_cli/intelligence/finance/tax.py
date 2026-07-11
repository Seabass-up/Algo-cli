"""B102. Sales/Use Tax and Invoice Taxability Classifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from .common import ReviewGate, SourceRef, decimalize, stable_exception_id


@dataclass(frozen=True)
class TaxPolicy:
    id: str
    taxable_categories: set[str]
    rate: Decimal | int | str
    exempt_categories: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        object.__setattr__(self, "rate", decimalize(self.rate))
        object.__setattr__(self, "taxable_categories", {cat.lower() for cat in self.taxable_categories})
        object.__setattr__(self, "exempt_categories", {cat.lower() for cat in self.exempt_categories})


@dataclass(frozen=True)
class TaxLineResult:
    line_id: str
    category: str
    amount: Decimal
    taxable: bool
    tax: Decimal
    reason: str
    source_refs: list[SourceRef]


@dataclass(frozen=True)
class TaxClassification:
    policy_id: str
    taxable_amount: Decimal
    tax_amount: Decimal
    line_results: list[TaxLineResult]
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


class SalesUseTaxClassifier:
    def classify_invoice(self, lines: Iterable[dict[str, Any]], jurisdiction_policy: TaxPolicy | dict[str, Any]) -> TaxClassification:
        policy = _coerce_policy(jurisdiction_policy)
        results: list[TaxLineResult] = []
        for idx, line in enumerate(lines, start=1):
            category = str(line.get("category", "")).lower()
            amount = decimalize(line.get("amount", 0))
            line_id = str(line.get("id") or stable_exception_id("B102", [idx, category, amount]))
            if category in policy.exempt_categories:
                taxable = False
                reason = "category explicitly exempt"
            else:
                taxable = category in policy.taxable_categories
                reason = "category taxable" if taxable else "category not taxable under policy"
            tax = amount * policy.rate if taxable else Decimal("0")
            results.append(TaxLineResult(line_id, category, amount, taxable, tax, reason, list(line.get("source_refs", []))))
        taxable_amount = sum((row.amount for row in results if row.taxable), Decimal("0"))
        tax_amount = sum((row.tax for row in results), Decimal("0"))
        return TaxClassification(policy.id, taxable_amount, tax_amount, results)


def _coerce_policy(policy: TaxPolicy | dict[str, Any]) -> TaxPolicy:
    if isinstance(policy, TaxPolicy):
        return policy
    return TaxPolicy(
        id=str(policy.get("id", "policy")),
        taxable_categories=set(policy.get("taxable_categories", [])),
        exempt_categories=set(policy.get("exempt_categories", [])),
        rate=policy.get("rate", 0),
    )
