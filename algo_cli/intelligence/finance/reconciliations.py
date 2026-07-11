"""B89/B99. Reconciliation triage and bank matching."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from itertools import combinations
from typing import Any, Iterable

from .common import RiskLevel, SourceRef, decimalize, stable_exception_id, within_tolerance


@dataclass(frozen=True)
class ReconciliationAccount:
    account: str
    balance: Decimal | int | str
    last_reconciled: date | None = None
    unreconciled_items: int = 0
    risk_level: RiskLevel = RiskLevel.MEDIUM
    owner: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "balance", decimalize(self.balance))


@dataclass(frozen=True)
class RiskScore:
    account: str
    score: Decimal
    level: RiskLevel
    reasons: list[str]
    source_refs: list[SourceRef] = field(default_factory=list)


class ReconciliationTriage:
    """Rank balance-sheet reconciliations by materiality, age, and risk."""

    def __init__(self, materiality: Decimal | int | str = Decimal("1000"), as_of: date | None = None) -> None:
        self.materiality = decimalize(materiality)
        self.as_of = as_of or date.today()

    def score(self, account: ReconciliationAccount) -> RiskScore:
        amount_score = Decimal("0") if self.materiality == 0 else min(abs(account.balance) / self.materiality, Decimal("10")) * 100
        age_score = Decimal("0")
        reasons: list[str] = []
        if account.last_reconciled is None:
            age_score = Decimal("500")
            reasons.append("never reconciled")
        else:
            days = (self.as_of - account.last_reconciled).days
            if days > 60:
                age_score = Decimal("300")
                reasons.append("stale >60 days")
            elif days > 30:
                age_score = Decimal("150")
                reasons.append("stale >30 days")
        item_score = Decimal(account.unreconciled_items) * Decimal("25")
        if account.unreconciled_items:
            reasons.append(f"{account.unreconciled_items} unreconciled items")
        risk_score = Decimal(int(account.risk_level)) * Decimal("250")
        score = amount_score + age_score + item_score + risk_score
        level = RiskLevel.CRITICAL if score >= 1200 else RiskLevel.HIGH if score >= 800 else RiskLevel.MEDIUM if score >= 400 else RiskLevel.LOW
        return RiskScore(account=account.account, score=score, level=level, reasons=reasons, source_refs=list(account.source_refs))

    def prioritize(self, accounts: Iterable[ReconciliationAccount]) -> list[ReconciliationAccount]:
        return sorted(accounts, key=lambda account: (self.score(account).score, abs(account.balance), account.account), reverse=True)


@dataclass(frozen=True)
class BankItem:
    id: str
    amount: Decimal | int | str
    date: date | str
    description: str = ""
    reference: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimalize(self.amount))
        if isinstance(self.date, str):
            object.__setattr__(self, "date", datetime.fromisoformat(self.date[:10]).date())


@dataclass(frozen=True)
class BankMatch:
    id: str
    book_ids: list[str]
    bank_ids: list[str]
    amount_difference: Decimal
    date_difference_days: int
    score: Decimal
    match_type: str
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass
class MatchResult:
    matches: list[BankMatch]
    unmatched_book: list[BankItem]
    unmatched_bank: list[BankItem]
    tolerance_used: Decimal
    date_window_days: int


class BankMatcher:
    """Deterministic bank reconciliation matcher.

    Uses exact/reference matches first, greedy one-to-one fuzzy matching second,
    and bounded subset matching for small many-to-one receipt/disbursement groups.
    """

    def match(
        self,
        book_items: Iterable[BankItem | dict[str, Any]],
        bank_items: Iterable[BankItem | dict[str, Any]],
        tolerance: Decimal | int | str = Decimal("0"),
        date_window_days: int = 3,
        subset_size: int = 3,
    ) -> MatchResult:
        tolerance = decimalize(tolerance)
        book = [_coerce_item(item) for item in book_items]
        bank = [_coerce_item(item) for item in bank_items]
        matched_book: set[str] = set()
        matched_bank: set[str] = set()
        matches: list[BankMatch] = []

        # Pass 1: exact amount/date/reference.
        for b in book:
            for k in bank:
                if b.id in matched_book or k.id in matched_bank:
                    continue
                if _reference_equal(b.reference, k.reference) and b.date == k.date and within_tolerance(b.amount, k.amount, tolerance):
                    matches.append(_make_match([b], [k], "exact-reference"))
                    matched_book.add(b.id)
                    matched_bank.add(k.id)

        # Pass 2: one-to-one fuzzy amount/date/description.
        candidates: list[tuple[Decimal, BankItem, BankItem]] = []
        for b in book:
            if b.id in matched_book:
                continue
            for k in bank:
                if k.id in matched_bank:
                    continue
                days = abs((b.date - k.date).days)
                if days <= date_window_days and within_tolerance(b.amount, k.amount, tolerance):
                    amount_diff = abs(b.amount - k.amount)
                    score = amount_diff + Decimal(days) / Decimal("100") - (_description_overlap(b.description, k.description) / Decimal("1000"))
                    candidates.append((score, b, k))
        for _score, b, k in sorted(candidates, key=lambda row: (row[0], row[1].id, row[2].id)):
            if b.id in matched_book or k.id in matched_bank:
                continue
            matches.append(_make_match([b], [k], "fuzzy-one-to-one"))
            matched_book.add(b.id)
            matched_bank.add(k.id)

        # Pass 3: bounded many-to-one/one-to-many subset matching.
        remaining_book = [b for b in book if b.id not in matched_book]
        remaining_bank = [k for k in bank if k.id not in matched_bank]
        subset_matches = self._subset_matches(remaining_book, remaining_bank, tolerance, date_window_days, subset_size)
        for match in subset_matches:
            if any(book_id in matched_book for book_id in match.book_ids) or any(bank_id in matched_bank for bank_id in match.bank_ids):
                continue
            matches.append(match)
            matched_book.update(match.book_ids)
            matched_bank.update(match.bank_ids)

        return MatchResult(
            matches=sorted(matches, key=lambda m: (m.match_type, m.id)),
            unmatched_book=[b for b in book if b.id not in matched_book],
            unmatched_bank=[k for k in bank if k.id not in matched_bank],
            tolerance_used=tolerance,
            date_window_days=date_window_days,
        )

    def _subset_matches(
        self,
        book: list[BankItem],
        bank: list[BankItem],
        tolerance: Decimal,
        date_window_days: int,
        subset_size: int,
    ) -> list[BankMatch]:
        out: list[BankMatch] = []
        # many book to one bank
        for size in range(2, min(subset_size, len(book)) + 1):
            for group in combinations(book, size):
                amount = sum((item.amount for item in group), Decimal("0"))
                latest = max(item.date for item in group)
                earliest = min(item.date for item in group)
                for bank_item in bank:
                    if within_tolerance(amount, bank_item.amount, tolerance) and min(
                        abs((latest - bank_item.date).days), abs((earliest - bank_item.date).days)
                    ) <= date_window_days:
                        out.append(_make_match(list(group), [bank_item], "many-book-to-one-bank"))
        # one book to many bank
        for size in range(2, min(subset_size, len(bank)) + 1):
            for group in combinations(bank, size):
                amount = sum((item.amount for item in group), Decimal("0"))
                latest = max(item.date for item in group)
                earliest = min(item.date for item in group)
                for book_item in book:
                    if within_tolerance(book_item.amount, amount, tolerance) and min(
                        abs((latest - book_item.date).days), abs((earliest - book_item.date).days)
                    ) <= date_window_days:
                        out.append(_make_match([book_item], list(group), "one-book-to-many-bank"))
        return sorted(out, key=lambda m: (abs(m.amount_difference), m.date_difference_days, m.id))


def _coerce_item(item: BankItem | dict[str, Any]) -> BankItem:
    if isinstance(item, BankItem):
        return item
    return BankItem(
        id=str(item["id"]),
        amount=item.get("amount", 0),
        date=item.get("date"),
        description=str(item.get("description", "")),
        reference=item.get("reference"),
        source_refs=list(item.get("source_refs", [])),
    )


def _reference_equal(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return "".join(str(left).lower().split()) == "".join(str(right).lower().split())


def _description_overlap(left: str, right: str) -> Decimal:
    a = {token for token in left.lower().split() if len(token) > 2}
    b = {token for token in right.lower().split() if len(token) > 2}
    if not a or not b:
        return Decimal("0")
    return Decimal(len(a & b)) / Decimal(len(a | b))


def _make_match(book: list[BankItem], bank: list[BankItem], match_type: str) -> BankMatch:
    book_amount = sum((item.amount for item in book), Decimal("0"))
    bank_amount = sum((item.amount for item in bank), Decimal("0"))
    book_date = max(item.date for item in book)
    bank_date = max(item.date for item in bank)
    refs = [ref for item in [*book, *bank] for ref in item.source_refs]
    return BankMatch(
        id=stable_exception_id("B99", [sorted(item.id for item in book), sorted(item.id for item in bank), match_type]),
        book_ids=[item.id for item in book],
        bank_ids=[item.id for item in bank],
        amount_difference=book_amount - bank_amount,
        date_difference_days=abs((book_date - bank_date).days),
        score=abs(book_amount - bank_amount) + Decimal(abs((book_date - bank_date).days)) / Decimal("100"),
        match_type=match_type,
        source_refs=refs,
    )
