"""B90. Journal Entry Anomaly Scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from statistics import median
from typing import Any, Iterable

from .common import ReviewGate, RiskLevel, SourceRef, dateize, datetimeize, decimalize


@dataclass(frozen=True)
class JournalEntry:
    id: str
    account: str
    amount: Decimal
    entry_date: date
    user: str
    description: str = ""
    manual: bool = True
    approved_by: str | None = None
    posted_at: datetime | None = None
    source_refs: list[SourceRef] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimalize(self.amount))
        object.__setattr__(self, "entry_date", dateize(self.entry_date, field_name="entry date"))
        object.__setattr__(self, "posted_at", datetimeize(self.posted_at, field_name="posted at"))


@dataclass(frozen=True)
class JournalEntryScore:
    entry: JournalEntry
    score: Decimal
    level: RiskLevel
    reasons: list[str]
    source_refs: list[SourceRef]
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


class JournalEntryAnomalyScorer:
    """Weighted JE risk scoring for controller review, not auto-rejection."""

    def score(
        self,
        entries: Iterable[JournalEntry | dict[str, Any]],
        history: Iterable[JournalEntry | dict[str, Any]] | None = None,
    ) -> list[JournalEntryScore]:
        rows = [_coerce_entry(entry) for entry in entries]
        hist = [_coerce_entry(entry) for entry in (history or [])]
        account_medians = _median_amount_by_account(hist)
        user_accounts = _accounts_by_user(hist)
        scored = [self._score_one(entry, account_medians, user_accounts) for entry in rows]
        return sorted(scored, key=lambda row: (row.score, abs(row.entry.amount), row.entry.id), reverse=True)

    def _score_one(
        self,
        entry: JournalEntry,
        account_medians: dict[str, Decimal],
        user_accounts: dict[str, set[str]],
    ) -> JournalEntryScore:
        score = Decimal("0")
        reasons: list[str] = []
        amount = abs(entry.amount)
        if entry.manual:
            score += Decimal("100")
            reasons.append("manual entry")
        if not entry.approved_by:
            score += Decimal("300")
            reasons.append("missing approval")
        if _is_round_amount(amount):
            score += Decimal("125")
            reasons.append("round-dollar amount")
        if entry.posted_at and _outside_business_hours(entry.posted_at):
            score += Decimal("175")
            reasons.append("posted outside business hours")
        if entry.entry_date.day >= 28:
            score += Decimal("100")
            reasons.append("period-end posting")
        if not entry.description.strip():
            score += Decimal("150")
            reasons.append("blank description")
        median_amount = account_medians.get(entry.account)
        if median_amount and median_amount > 0 and amount > median_amount * Decimal("3"):
            score += Decimal("250")
            reasons.append("amount exceeds 3x historical median")
        known_accounts = user_accounts.get(entry.user, set())
        if known_accounts and entry.account not in known_accounts:
            score += Decimal("200")
            reasons.append("user unusual for account")
        level = RiskLevel.CRITICAL if score >= 900 else RiskLevel.HIGH if score >= 600 else RiskLevel.MEDIUM if score >= 250 else RiskLevel.LOW
        return JournalEntryScore(entry=entry, score=score, level=level, reasons=reasons, source_refs=list(entry.source_refs))


def _coerce_entry(entry: JournalEntry | dict[str, Any]) -> JournalEntry:
    if isinstance(entry, JournalEntry):
        return entry
    return JournalEntry(
        id=str(entry["id"]),
        account=str(entry.get("account", "")),
        amount=entry.get("amount", 0),
        entry_date=dateize(entry.get("entry_date") or entry.get("date"), field_name="entry date"),
        user=str(entry.get("user", "")),
        description=str(entry.get("description", "")),
        manual=bool(entry.get("manual", True)),
        approved_by=entry.get("approved_by"),
        posted_at=entry.get("posted_at"),
        source_refs=list(entry.get("source_refs", [])),
    )


def _median_amount_by_account(entries: list[JournalEntry]) -> dict[str, Decimal]:
    grouped: dict[str, list[Decimal]] = {}
    for entry in entries:
        grouped.setdefault(entry.account, []).append(abs(entry.amount))
    return {account: decimalize(median(values)) for account, values in grouped.items() if values}


def _accounts_by_user(entries: list[JournalEntry]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for entry in entries:
        out.setdefault(entry.user, set()).add(entry.account)
    return out


def _is_round_amount(amount: Decimal) -> bool:
    return amount != 0 and amount % Decimal("1000") == 0


def _outside_business_hours(value: datetime) -> bool:
    return value.weekday() >= 5 or value.time() < time(6, 0) or value.time() > time(20, 0)
