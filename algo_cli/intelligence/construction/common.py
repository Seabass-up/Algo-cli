"""Shared contracts for construction contract intelligence patterns B106-B126."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable, Mapping


class RiskLevel(str, Enum):
    """Coarse risk levels used by construction contract analyzers."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewGate(str, Enum):
    """Human-review gates for consequential construction-contract actions."""

    NONE = "none"
    BUSINESS_REVIEW = "business_review"
    ATTORNEY_REVIEW = "attorney_review"
    INSURANCE_REVIEW = "insurance_review"
    TAX_HR_REVIEW = "tax_hr_review"
    SAFETY_REVIEW = "safety_review"
    STOP_EXTERNAL_ACTION = "stop_external_action"


class ProjectType(str, Enum):
    """Project routing used for lien/bond/payment overlays."""

    PRIVATE = "private"
    PUBLIC = "public"
    FEDERAL = "federal"
    UNKNOWN = "unknown"


class ProjectUse(str, Enum):
    """Commercial/residential routing where statutes differ."""

    COMMERCIAL = "commercial"
    RESIDENTIAL = "residential"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceRef:
    """Reference to a clause, document, row, or supporting artifact."""

    document: str
    clause: str | None = None
    page: int | None = None
    row: int | None = None
    quote: str | None = None
    url: str | None = None
    document_hash: str | None = None


@dataclass(frozen=True)
class ConstructionIssue:
    """A contract issue/red flag produced by a pattern engine."""

    id: str
    pattern_id: str
    title: str
    message: str
    risk_level: RiskLevel = RiskLevel.MEDIUM
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW
    source_refs: tuple[SourceRef, ...] = ()
    tags: frozenset[str] = frozenset()
    amount: Decimal | None = None
    deadline: date | None = None

    @property
    def requires_human_review(self) -> bool:
        return self.review_gate is not ReviewGate.NONE


@dataclass(frozen=True)
class Jurisdiction:
    """State/project context for contract overlays."""

    state: str | None = None
    county: str | None = None
    city: str | None = None
    project_type: ProjectType = ProjectType.UNKNOWN
    project_use: ProjectUse = ProjectUse.UNKNOWN
    tier: str | None = None

    @property
    def normalized_state(self) -> str | None:
        if not self.state:
            return None
        value = self.state.strip().upper()
        aliases = {"KANSAS": "KS", "MISSOURI": "MO", "FEDERAL": "FEDERAL"}
        return aliases.get(value, value)

    @property
    def is_kansas_private(self) -> bool:
        return self.normalized_state == "KS" and self.project_type is ProjectType.PRIVATE

    @property
    def is_federal(self) -> bool:
        return self.project_type is ProjectType.FEDERAL or self.normalized_state == "FEDERAL"


@dataclass(frozen=True)
class Deadline:
    """A computed review deadline. Not a legal deadline/sign-off."""

    name: str
    due_date: date
    source: str
    pattern_id: str
    review_gate: ReviewGate = ReviewGate.ATTORNEY_REVIEW
    confidence: str = "review"
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClauseHit:
    """A simple keyword/regex extraction hit."""

    label: str
    text: str
    source_ref: SourceRef


@dataclass
class PatternResult:
    """Generic result envelope for construction pattern engines."""

    pattern_id: str
    issues: list[ConstructionIssue] = field(default_factory=list)
    source_refs: list[SourceRef] = field(default_factory=list)
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW

    @property
    def requires_human_review(self) -> bool:
        return self.review_gate is not ReviewGate.NONE or any(issue.requires_human_review for issue in self.issues)


def decimalize(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Parse construction dollar/percent-ish numeric values safely."""

    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return default
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace("$", "").replace(",", "").replace("%", "")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return default
    return -number if negative else number


def parse_date(value: Any) -> date | None:
    """Parse common date forms used in tests/imports."""

    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def add_business_days(start: date, days: int) -> date:
    """Add business days, skipping Saturday/Sunday."""

    current = start
    remaining = days
    step = 1 if days >= 0 else -1
    while remaining:
        current += timedelta(days=step)
        if current.weekday() < 5:
            remaining -= step
    return current


def add_months_approx(start: date, months: int) -> date:
    """Add months while clamping day to the target month length."""

    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(start.day, month_lengths[month - 1]))


def hash_source_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def contains_any(text: str, terms: Iterable[str]) -> bool:
    haystack = (text or "").lower()
    return any(term.lower() in haystack for term in terms)


def stable_issue_id(pattern_id: str, *parts: object) -> str:
    payload = "|".join(str(part) for part in (pattern_id, *parts))
    return f"{pattern_id.lower()}-{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:10]}"


def make_issue(
    pattern_id: str,
    title: str,
    message: str,
    *,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    review_gate: ReviewGate = ReviewGate.BUSINESS_REVIEW,
    source_refs: Iterable[SourceRef] = (),
    tags: Iterable[str] = (),
    amount: Decimal | str | int | None = None,
    deadline: date | None = None,
) -> ConstructionIssue:
    parsed_amount = decimalize(amount) if amount is not None else None
    return ConstructionIssue(
        id=stable_issue_id(pattern_id, title, message, deadline or ""),
        pattern_id=pattern_id,
        title=title,
        message=message,
        risk_level=risk_level,
        review_gate=review_gate,
        source_refs=tuple(source_refs),
        tags=frozenset(tags),
        amount=parsed_amount,
        deadline=deadline,
    )


def source_from_mapping(data: Mapping[str, Any], default_doc: str = "input") -> tuple[SourceRef, ...]:
    refs = data.get("source_refs")
    if refs:
        return tuple(ref for ref in refs if isinstance(ref, SourceRef))
    document = str(data.get("document") or default_doc)
    return (SourceRef(document, clause=data.get("clause"), quote=data.get("text") or data.get("quote")),)
