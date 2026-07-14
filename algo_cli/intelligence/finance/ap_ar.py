"""B91/B92/B93. AP duplicate, vendor SoD, and AR aging engines."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Iterable

from .common import (
    ReviewGate,
    RiskLevel,
    SourceRef,
    dateize,
    datetimeize,
    decimalize,
    normalize_invoice_number,
    normalize_vendor_name,
    stable_exception_id,
    within_tolerance,
)


@dataclass(frozen=True)
class APInvoice:
    id: str
    vendor: str
    invoice_number: str
    amount: Decimal
    invoice_date: date
    po_id: str | None = None
    source_refs: list[SourceRef] = dataclass_field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimalize(self.amount))
        object.__setattr__(self, "invoice_date", dateize(self.invoice_date, field_name="invoice date"))


@dataclass(frozen=True)
class APPayment:
    id: str
    vendor: str
    amount: Decimal
    payment_date: date
    invoice_number: str | None = None
    source_refs: list[SourceRef] = dataclass_field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimalize(self.amount))
        object.__setattr__(self, "payment_date", dateize(self.payment_date, field_name="payment date"))


@dataclass(frozen=True)
class DuplicateCandidate:
    invoice_ids: list[str]
    payment_ids: list[str]
    confidence: Decimal
    reasons: list[str]
    source_refs: list[SourceRef]
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


@dataclass(frozen=True)
class ThreeWayMatchResult:
    clean: bool
    exceptions: list[str]
    amount_difference: Decimal
    quantity_difference: Decimal
    tolerance_used: Decimal
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


class APDuplicateDetector:
    """Find duplicate AP candidates and three-way match exceptions."""

    def find_duplicates(
        self,
        invoices: Iterable[APInvoice | dict[str, Any]],
        payments: Iterable[APPayment | dict[str, Any]] | None = None,
        amount_tolerance: Decimal | int | str = Decimal("0"),
        days_window: int = 7,
    ) -> list[DuplicateCandidate]:
        inv = [_coerce_invoice(row) for row in invoices]
        pay = [_coerce_payment(row) for row in (payments or [])]
        tolerance = decimalize(amount_tolerance)
        candidates: list[DuplicateCandidate] = []
        for i, left in enumerate(inv):
            for right in inv[i + 1:]:
                vendor_match = normalize_vendor_name(left.vendor) == normalize_vendor_name(right.vendor)
                amount_match = within_tolerance(left.amount, right.amount, tolerance)
                days = abs((left.invoice_date - right.invoice_date).days)
                exact_invoice = normalize_invoice_number(left.invoice_number) == normalize_invoice_number(right.invoice_number)
                near_invoice = _similar(left.invoice_number, right.invoice_number) >= Decimal("0.85")
                if vendor_match and amount_match and (exact_invoice or (near_invoice and days <= days_window)):
                    reasons = ["same vendor", "same amount"]
                    confidence = Decimal("1.00") if exact_invoice else Decimal("0.75")
                    reasons.append("same normalized invoice number" if exact_invoice else "near invoice number")
                    candidates.append(DuplicateCandidate(
                        invoice_ids=[left.id, right.id],
                        payment_ids=[],
                        confidence=confidence,
                        reasons=reasons,
                        source_refs=[*left.source_refs, *right.source_refs],
                    ))
        for payment in pay:
            linked = [invoice for invoice in inv if normalize_vendor_name(invoice.vendor) == normalize_vendor_name(payment.vendor)
                      and within_tolerance(invoice.amount, payment.amount, tolerance)
                      and (not payment.invoice_number or normalize_invoice_number(invoice.invoice_number) == normalize_invoice_number(payment.invoice_number))]
            if len(linked) > 1:
                candidates.append(DuplicateCandidate(
                    invoice_ids=[invoice.id for invoice in linked],
                    payment_ids=[payment.id],
                    confidence=Decimal("0.80"),
                    reasons=["payment could apply to multiple matching invoices"],
                    source_refs=[*payment.source_refs, *[ref for invoice in linked for ref in invoice.source_refs]],
                ))
        return sorted(candidates, key=lambda row: (row.confidence, row.invoice_ids), reverse=True)

    def three_way_match(
        self,
        po: dict[str, Any],
        receipt: dict[str, Any],
        invoice: dict[str, Any],
        price_tolerance: Decimal | int | str = Decimal("0.01"),
        quantity_tolerance: Decimal | int | str = Decimal("0"),
    ) -> ThreeWayMatchResult:
        price_tol = decimalize(price_tolerance)
        qty_tol = decimalize(quantity_tolerance)
        po_qty = decimalize(po.get("quantity", 0))
        receipt_qty = decimalize(receipt.get("quantity", 0))
        invoice_qty = decimalize(invoice.get("quantity", 0))
        po_price = decimalize(po.get("unit_price", 0))
        invoice_price = decimalize(invoice.get("unit_price", 0))
        exceptions: list[str] = []
        if not within_tolerance(invoice_qty, receipt_qty, qty_tol):
            exceptions.append("invoice quantity does not match receipt quantity")
        if invoice_qty > po_qty + qty_tol:
            exceptions.append("invoice quantity exceeds PO quantity")
        if not within_tolerance(invoice_price, po_price, price_tol):
            exceptions.append("invoice unit price does not match PO unit price")
        po_amount = po_qty * po_price
        invoice_amount = invoice_qty * invoice_price
        amount_difference = invoice_amount - po_amount
        quantity_difference = invoice_qty - receipt_qty
        return ThreeWayMatchResult(
            clean=not exceptions,
            exceptions=exceptions,
            amount_difference=amount_difference,
            quantity_difference=quantity_difference,
            tolerance_used=max(price_tol, qty_tol),
        )


@dataclass(frozen=True)
class Vendor:
    id: str
    name: str
    tax_id: str | None = None
    bank_account: str | None = None
    created_by: str | None = None
    approved_by: str | None = None
    active: bool = True
    source_refs: list[SourceRef] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class VendorChangeEvent:
    id: str
    vendor_id: str
    field: str
    changed_by: str
    approved_by: str | None = None
    changed_at: datetime | None = None
    source_refs: list[SourceRef] = dataclass_field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_at", datetimeize(self.changed_at, field_name="changed at"))


@dataclass(frozen=True)
class VendorRisk:
    vendor_id: str
    score: Decimal
    level: RiskLevel
    reasons: list[str]
    source_refs: list[SourceRef]


@dataclass(frozen=True)
class SoDConflict:
    id: str
    vendor_id: str
    user: str
    action: str
    message: str
    source_refs: list[SourceRef]


class VendorRiskAnalyzer:
    def score(
        self,
        vendors: Iterable[Vendor | dict[str, Any]],
        changes: Iterable[VendorChangeEvent | dict[str, Any]] = (),
        users: dict[str, Any] | None = None,
    ) -> list[VendorRisk]:
        vendor_rows = [_coerce_vendor(vendor) for vendor in vendors]
        change_rows = [_coerce_change(change) for change in changes]
        changes_by_vendor: dict[str, list[VendorChangeEvent]] = {}
        for change in change_rows:
            changes_by_vendor.setdefault(change.vendor_id, []).append(change)
        risks: list[VendorRisk] = []
        for vendor in vendor_rows:
            score = Decimal("0")
            reasons: list[str] = []
            if not vendor.tax_id:
                score += Decimal("150")
                reasons.append("missing tax id")
            if vendor.created_by and vendor.approved_by and vendor.created_by == vendor.approved_by:
                score += Decimal("500")
                reasons.append("creator approved vendor")
            if not vendor.bank_account:
                score += Decimal("100")
                reasons.append("missing bank account")
            sensitive_changes = [c for c in changes_by_vendor.get(vendor.id, []) if c.field.lower() in {"bank_account", "tax_id", "address"}]
            if sensitive_changes:
                score += Decimal("100") * len(sensitive_changes)
                reasons.append("sensitive vendor master changes")
            level = RiskLevel.CRITICAL if score >= 700 else RiskLevel.HIGH if score >= 400 else RiskLevel.MEDIUM if score >= 150 else RiskLevel.LOW
            risks.append(VendorRisk(vendor_id=vendor.id, score=score, level=level, reasons=reasons, source_refs=list(vendor.source_refs)))
        return sorted(risks, key=lambda row: (row.score, row.vendor_id), reverse=True)

    def detect_sod_conflicts(self, events: Iterable[VendorChangeEvent | dict[str, Any]]) -> list[SoDConflict]:
        conflicts: list[SoDConflict] = []
        for event in [_coerce_change(e) for e in events]:
            if event.approved_by and event.changed_by == event.approved_by:
                conflicts.append(SoDConflict(
                    id=stable_exception_id("B92", [event.vendor_id, event.id, event.changed_by]),
                    vendor_id=event.vendor_id,
                    user=event.changed_by,
                    action=event.field,
                    message=f"Same user changed and approved vendor {event.vendor_id}",
                    source_refs=list(event.source_refs),
                ))
        return conflicts


@dataclass(frozen=True)
class ARInvoice:
    id: str
    customer: str
    amount: Decimal
    invoice_date: date
    due_date: date
    disputed: bool = False
    customer_risk: RiskLevel = RiskLevel.MEDIUM
    source_refs: list[SourceRef] = dataclass_field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimalize(self.amount))
        object.__setattr__(self, "invoice_date", dateize(self.invoice_date, field_name="invoice date"))
        object.__setattr__(self, "due_date", dateize(self.due_date, field_name="due date"))


@dataclass(frozen=True)
class AgingBucket:
    name: str
    invoices: list[ARInvoice]
    total: Decimal


@dataclass(frozen=True)
class AgingReport:
    as_of: date
    buckets: dict[str, AgingBucket]
    total: Decimal


@dataclass(frozen=True)
class CollectionAction:
    invoice_id: str
    customer: str
    priority_score: Decimal
    reason: str
    source_refs: list[SourceRef]


class ARAgingPrioritizer:
    def age(self, invoices: Iterable[ARInvoice | dict[str, Any]], as_of: date | str) -> AgingReport:
        as_of_date = datetime.fromisoformat(as_of[:10]).date() if isinstance(as_of, str) else as_of
        rows = [_coerce_ar_invoice(invoice) for invoice in invoices]
        bucket_rows: dict[str, list[ARInvoice]] = {"current": [], "1-30": [], "31-60": [], "61-90": [], "90+": []}
        for invoice in rows:
            days = (as_of_date - invoice.due_date).days
            bucket = "current" if days <= 0 else "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
            bucket_rows[bucket].append(invoice)
        buckets = {
            name: AgingBucket(name=name, invoices=values, total=sum((invoice.amount for invoice in values), Decimal("0")))
            for name, values in bucket_rows.items()
        }
        return AgingReport(as_of=as_of_date, buckets=buckets, total=sum((invoice.amount for invoice in rows), Decimal("0")))

    def prioritize_collections(self, invoices: Iterable[ARInvoice | dict[str, Any]], payments=None, as_of: date | str | None = None) -> list[CollectionAction]:
        as_of_date = datetime.fromisoformat(as_of[:10]).date() if isinstance(as_of, str) else as_of or date.today()
        actions: list[CollectionAction] = []
        for invoice in [_coerce_ar_invoice(row) for row in invoices]:
            days_past_due = max((as_of_date - invoice.due_date).days, 0)
            score = abs(invoice.amount) + Decimal(days_past_due) * Decimal("25") + Decimal(int(invoice.customer_risk)) * Decimal("200")
            reasons = [f"{days_past_due} days past due", f"{invoice.customer_risk.name.lower()} customer risk"]
            if invoice.disputed:
                score += Decimal("500")
                reasons.append("disputed")
            actions.append(CollectionAction(
                invoice_id=invoice.id,
                customer=invoice.customer,
                priority_score=score,
                reason="; ".join(reasons),
                source_refs=list(invoice.source_refs),
            ))
        return sorted(actions, key=lambda row: (row.priority_score, row.invoice_id), reverse=True)


def _coerce_invoice(row: APInvoice | dict[str, Any]) -> APInvoice:
    if isinstance(row, APInvoice):
        return row
    return APInvoice(str(row["id"]), str(row.get("vendor", "")), str(row.get("invoice_number", "")), row.get("amount", 0), dateize(row.get("invoice_date") or row.get("date"), field_name="invoice date"), row.get("po_id"), list(row.get("source_refs", [])))


def _coerce_payment(row: APPayment | dict[str, Any]) -> APPayment:
    if isinstance(row, APPayment):
        return row
    return APPayment(str(row["id"]), str(row.get("vendor", "")), row.get("amount", 0), dateize(row.get("payment_date") or row.get("date"), field_name="payment date"), row.get("invoice_number"), list(row.get("source_refs", [])))


def _similar(left: str, right: str) -> Decimal:
    return decimalize(str(SequenceMatcher(None, normalize_invoice_number(left), normalize_invoice_number(right)).ratio()))


def _coerce_vendor(row: Vendor | dict[str, Any]) -> Vendor:
    if isinstance(row, Vendor):
        return row
    return Vendor(str(row["id"]), str(row.get("name", "")), row.get("tax_id"), row.get("bank_account"), row.get("created_by"), row.get("approved_by"), bool(row.get("active", True)), list(row.get("source_refs", [])))


def _coerce_change(row: VendorChangeEvent | dict[str, Any]) -> VendorChangeEvent:
    if isinstance(row, VendorChangeEvent):
        return row
    return VendorChangeEvent(str(row["id"]), str(row.get("vendor_id", "")), str(row.get("field", "")), str(row.get("changed_by", "")), row.get("approved_by"), row.get("changed_at"), list(row.get("source_refs", [])))


def _coerce_ar_invoice(row: ARInvoice | dict[str, Any]) -> ARInvoice:
    if isinstance(row, ARInvoice):
        return row
    risk = row.get("customer_risk", RiskLevel.MEDIUM)
    if isinstance(risk, str):
        risk = RiskLevel[risk.upper()]
    return ARInvoice(str(row["id"]), str(row.get("customer", "")), row.get("amount", 0), dateize(row.get("invoice_date") or row.get("date"), field_name="invoice date"), dateize(row.get("due_date"), field_name="due date"), bool(row.get("disputed", False)), risk, list(row.get("source_refs", [])))
