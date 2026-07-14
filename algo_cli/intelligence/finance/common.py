"""Shared finance/controller data contracts.

These primitives back Track C patterns B87-B105.  They are deliberately
local-only and deterministic: engines produce draft analysis, provenance, and
review gates; they never post JEs, approve payments, file taxes, or distribute
external work product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, IntEnum
import hashlib
import json
import re
from typing import Any, Iterable


class Severity(IntEnum):
    INFO = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class RiskLevel(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class EvidenceStatus(Enum):
    OPEN = "open"
    REQUESTED = "requested"
    RECEIVED = "received"
    REVIEWED = "reviewed"
    STALE = "stale"
    WAIVED = "waived"


class ReviewGate(Enum):
    DRAFT = "draft"
    CONTROLLER_REVIEW = "controller_review"
    CPA_REVIEW = "cpa_review"
    EXTERNAL_APPROVAL_REQUIRED = "external_approval_required"


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", decimalize(self.amount))
        object.__setattr__(self, "currency", self.currency.upper())


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    path: str | None = None
    row: int | None = None
    column: str | None = None
    document_hash: str | None = None
    note: str | None = None


@dataclass
class EvidenceItem:
    id: str
    description: str
    source_refs: list[SourceRef] = field(default_factory=list)
    prepared_by: str | None = None
    reviewed_by: str | None = None
    status: EvidenceStatus = EvidenceStatus.OPEN

    @property
    def has_support(self) -> bool:
        return bool(self.source_refs)


@dataclass
class FinanceException:
    id: str
    pattern_id: str
    severity: Severity
    amount: Decimal | None
    message: str
    source_refs: list[SourceRef] = field(default_factory=list)
    owner: str | None = None
    due_date: date | None = None
    tags: set[str] = field(default_factory=set)
    requires_human_review: bool = True
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW
    tolerance_used: Decimal | None = None
    materiality_policy_id: str | None = None
    waived: bool = False
    waiver_rationale: str | None = None
    waiver_approver: str | None = None
    waiver_expires: date | None = None

    def __post_init__(self) -> None:
        if self.amount is not None:
            self.amount = decimalize(self.amount)
        if not isinstance(self.severity, Severity):
            self.severity = Severity[self.severity]
        self.tags = {str(tag).lower() for tag in self.tags}


@dataclass
class MaterialityPolicy:
    """Small deterministic scoring policy for controller exception queues."""

    id: str = "default"
    quantitative_threshold: Decimal = Decimal("1000")
    qualitative_tags: set[str] = field(default_factory=lambda: {
        "fraud",
        "sod",
        "segregation-of-duties",
        "tax",
        "covenant",
        "related-party",
        "external-reporting",
        "executive-override",
    })
    severity_weight: Decimal = Decimal("1000")
    qualitative_weight: Decimal = Decimal("10000")
    deadline_weight: Decimal = Decimal("100")
    recurrence_weight: Decimal = Decimal("250")

    def __post_init__(self) -> None:
        self.quantitative_threshold = decimalize(self.quantitative_threshold)
        self.qualitative_tags = {tag.lower() for tag in self.qualitative_tags}

    def quantitative_score(self, amount: Decimal | int | float | str | None) -> Decimal:
        if amount is None:
            return Decimal("0")
        absolute = abs(decimalize(amount))
        if self.quantitative_threshold <= 0:
            return absolute
        return min(absolute / self.quantitative_threshold, Decimal("10")) * Decimal("100")

    def qualitative_score(self, tags: Iterable[str]) -> Decimal:
        normalized = {str(tag).lower() for tag in tags}
        hits = normalized & self.qualitative_tags
        return self.qualitative_weight * len(hits)

    def score_exception(self, exception: FinanceException, as_of: date | None = None) -> Decimal:
        score = Decimal(int(exception.severity)) * self.severity_weight
        score += self.quantitative_score(exception.amount)
        score += self.qualitative_score(exception.tags)
        recurrence = _tag_int(exception.tags, "recurrence:")
        score += Decimal(recurrence) * self.recurrence_weight
        if exception.due_date and as_of:
            days = (exception.due_date - as_of).days
            if days <= 0:
                score += self.deadline_weight * Decimal("10")
            elif days <= 5:
                score += self.deadline_weight * Decimal(6 - days)
        return score


def decimalize(value: Decimal | int | float | str | None) -> Decimal:
    """Convert common finance values to Decimal without binary-float surprises."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    text = str(value).strip()
    if not text:
        return Decimal("0")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace("$", "").replace(",", "").replace("%", "")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot convert to Decimal: {value!r}") from exc
    return -amount if negative else amount


def dateize(value: date | datetime | str | None, *, field_name: str = "date") -> date:
    """Normalize a required date value and reject missing inputs early."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.strip()[:10]).date()
    raise ValueError(f"{field_name} is required")


def datetimeize(
    value: datetime | str | None,
    *,
    field_name: str = "datetime",
) -> datetime | None:
    """Normalize an optional datetime value while preserving ``None``."""

    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.strip())
    raise ValueError(f"{field_name} must be an ISO datetime")


def within_tolerance(left: Any, right: Any, tolerance: Any = Decimal("0")) -> bool:
    return abs(decimalize(left) - decimalize(right)) <= abs(decimalize(tolerance))


def stable_exception_id(pattern_id: str, key_fields: Iterable[Any]) -> str:
    payload = json.dumps([pattern_id, list(key_fields)], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def hash_source_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_VENDOR_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "l.l.c",
    "ltd",
    "co",
    "company",
    "corp",
    "corporation",
}


def normalize_vendor_name(name: str | None) -> str:
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = [word for word in text.split() if word not in _VENDOR_SUFFIXES]
    return " ".join(words)


def normalize_invoice_number(number: str | int | None) -> str:
    text = str(number or "").upper().strip()
    text = re.sub(r"[^A-Z0-9]", "", text)
    # Keep a fully-zero number as "0", but remove padding for common invoice keys.
    stripped = text.lstrip("0")
    return stripped or ("0" if text else "")


def month_bucket(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    elif isinstance(value, str):
        value = datetime.fromisoformat(value[:10]).date()
    return f"{value.year:04d}-{value.month:02d}"


def _tag_int(tags: Iterable[str], prefix: str) -> int:
    for tag in tags:
        if tag.startswith(prefix):
            try:
                return int(tag.split(":", 1)[1])
            except (IndexError, ValueError):
                return 0
    return 0
