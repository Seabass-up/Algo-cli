"""B95. ASC 606 Revenue Recognition Decision DAG."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .common import ReviewGate, SourceRef, decimalize, stable_exception_id


@dataclass(frozen=True)
class PerformanceObligation:
    id: str
    description: str
    transaction_price: Decimal | int | str
    satisfied: bool = False
    measure: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_price", decimalize(self.transaction_price))


@dataclass(frozen=True)
class RevenueContract:
    id: str
    customer: str
    approved: bool
    rights_identified: bool
    payment_terms_identified: bool
    commercial_substance: bool
    collectability_probable: bool
    obligations: list[PerformanceObligation]
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass(frozen=True)
class RevenueDecision:
    contract_id: str
    recognized_amount: Decimal
    review_required: bool
    reasons: list[str]
    review_gate: ReviewGate
    source_refs: list[SourceRef]


class ASC606DecisionDAG:
    """Five-step ASC 606 decision support, not accounting sign-off."""

    def evaluate(self, contract: RevenueContract | dict[str, Any]) -> RevenueDecision:
        c = _coerce_contract(contract)
        reasons: list[str] = []
        criteria = {
            "contract not approved": c.approved,
            "rights not identified": c.rights_identified,
            "payment terms not identified": c.payment_terms_identified,
            "commercial substance missing": c.commercial_substance,
            "collectability not probable": c.collectability_probable,
        }
        for reason, ok in criteria.items():
            if not ok:
                reasons.append(reason)
        if not c.obligations:
            reasons.append("missing performance obligations")
        source_refs = list(c.source_refs)
        for obligation in c.obligations:
            source_refs.extend(obligation.source_refs)
            if not obligation.source_refs:
                reasons.append(f"performance obligation {obligation.id} lacks source evidence")
        recognized = sum((ob.transaction_price for ob in c.obligations if ob.satisfied and ob.source_refs), Decimal("0"))
        review_required = bool(reasons)
        return RevenueDecision(
            contract_id=c.id,
            recognized_amount=recognized if not reasons else Decimal("0") if any("contract" in r or "collectability" in r for r in reasons) else recognized,
            review_required=review_required,
            reasons=reasons,
            review_gate=ReviewGate.CPA_REVIEW if review_required else ReviewGate.CONTROLLER_REVIEW,
            source_refs=source_refs,
        )


def _coerce_contract(contract: RevenueContract | dict[str, Any]) -> RevenueContract:
    if isinstance(contract, RevenueContract):
        return contract
    obligations = []
    for row in contract.get("obligations", []):
        if isinstance(row, PerformanceObligation):
            obligations.append(row)
        else:
            obligations.append(PerformanceObligation(
                id=str(row.get("id") or stable_exception_id("B95", [row.get("description"), row.get("transaction_price")])) ,
                description=str(row.get("description", "")),
                transaction_price=row.get("transaction_price", 0),
                satisfied=bool(row.get("satisfied", False)),
                measure=row.get("measure"),
                source_refs=list(row.get("source_refs", [])),
            ))
    return RevenueContract(
        id=str(contract.get("id", "")),
        customer=str(contract.get("customer", "")),
        approved=bool(contract.get("approved", False)),
        rights_identified=bool(contract.get("rights_identified", False)),
        payment_terms_identified=bool(contract.get("payment_terms_identified", False)),
        commercial_substance=bool(contract.get("commercial_substance", False)),
        collectability_probable=bool(contract.get("collectability_probable", False)),
        obligations=obligations,
        source_refs=list(contract.get("source_refs", [])),
    )
