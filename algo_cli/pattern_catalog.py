"""Bounded catalog records and applicability, without executable Markdown authority."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import platform
import re
from typing import Any
from types import MappingProxyType

from .intelligence.catalog_verifier import CatalogVerifier


MAX_CATALOG_BYTES = 2_000_000
MAX_PATTERNS = 4096
MAX_PATTERN_INDEX_CHARS = 32_000
_RULE_FIELDS = {"environments", "prerequisites", "conflicts", "resource_costs", "fallback"}
_LIST_FIELDS = ("environments", "prerequisites", "conflicts")


@dataclass(frozen=True)
class PatternContext:
    environment: str = field(default_factory=lambda: platform.system().lower())
    capabilities: frozenset[str] = frozenset()
    active_patterns: frozenset[str] = frozenset()
    blocked_patterns: frozenset[str] = frozenset()
    resource_budgets: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.environment) is not str or not self.environment:
            raise ValueError("pattern_environment")
        if any(
            type(key) is not str or not key or type(value) not in {int, float} or not math.isfinite(value) or value < 0
            for key, value in self.resource_budgets.items()
        ):
            raise ValueError("pattern_resource_budget")
        for name in ("capabilities", "active_patterns", "blocked_patterns"):
            values = getattr(self, name)
            if not isinstance(values, (set, frozenset)) or any(type(value) is not str or not value for value in values):
                raise ValueError("pattern_context_set")
            object.__setattr__(self, name, frozenset(values))
        object.__setattr__(self, "resource_budgets", MappingProxyType(dict(self.resource_budgets)))


def with_active_conflicts(context: PatternContext | None, records: list[dict[str, Any]]) -> PatternContext:
    context = context or PatternContext()
    blocked = set(context.blocked_patterns)
    for record in records:
        if record.get("pattern_id") in context.active_patterns:
            if record.get("applicability_valid") is not True:
                raise ValueError("active_pattern_invalid_applicability")
            blocked.update(validate_applicability(record.get("applicability"))["conflicts"])
    return replace(context, blocked_patterns=frozenset(blocked))


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_applicability_key")
        result[key] = value
    return result


def validate_applicability(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) - _RULE_FIELDS:
        raise ValueError("pattern_applicability_schema")
    rules: dict[str, Any] = {}
    for key in _LIST_FIELDS:
        rows = value.get(key, [])
        if (
            type(rows) is not list
            or len(rows) > 64
            or any(type(row) is not str or not row or len(row) > 128 for row in rows)
        ):
            raise ValueError("pattern_applicability_list")
        if len(set(rows)) != len(rows):
            raise ValueError("pattern_applicability_duplicate")
        rules[key] = rows
    if set(rules["environments"]) - {"linux", "darwin", "windows"}:
        raise ValueError("pattern_applicability_environment")
    costs = value.get("resource_costs", {})
    if type(costs) is not dict or len(costs) > 16:
        raise ValueError("pattern_applicability_costs")
    if any(
        type(key) is not str
        or not key
        or len(key) > 64
        or type(cost) not in {int, float}
        or not math.isfinite(cost)
        or cost < 0
        for key, cost in costs.items()
    ):
        raise ValueError("pattern_applicability_cost")
    rules["resource_costs"] = costs
    fallback = value.get("fallback", "No automatic fallback declared; retain as reference only.")
    if type(fallback) is not str or not fallback or len(fallback) > 1024:
        raise ValueError("pattern_applicability_fallback")
    rules["fallback"] = fallback
    return rules


def parse_patterns(markdown: str) -> list[dict[str, Any]]:
    """Use catalog heading identity, excluding fenced examples and later sections."""
    if len(markdown.encode("utf-8")) > MAX_CATALOG_BYTES:
        raise ValueError("pattern_catalog_too_large")
    verifier = CatalogVerifier()
    if not verifier.lint_catalog(markdown).all_valid:
        raise ValueError("pattern_catalog_invalid")
    entries = verifier.parse_catalog(markdown)
    if len(entries) > MAX_PATTERNS:
        raise ValueError("pattern_catalog_count")
    lines = markdown.splitlines(keepends=True)
    boundaries: list[int] = []
    declarations: dict[int, str] = {}
    fence = ""
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        match = re.match(r"(`{3,}|~{3,})", stripped)
        if match:
            marker = match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not stripped[len(marker) :].strip():
                fence = ""
            continue
        if fence:
            continue
        if re.match(r"^#{1,3} ", line):
            boundaries.append(number)
        if stripped.startswith("**Applicability:**"):
            declarations[number] = stripped.removeprefix("**Applicability:**").strip()
    boundaries.append(len(lines) + 1)
    result: list[dict[str, Any]] = []
    source_digest = "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    for entry in entries:
        end = boundaries[bisect_right(boundaries, entry.line_number)] - 1
        body = "".join(lines[entry.line_number - 1 : end])
        declared = [value for line, value in declarations.items() if entry.line_number < line <= end]
        valid = True
        try:
            if len(declared) > 1:
                raise ValueError("pattern_applicability_multiple")
            rules = validate_applicability(
                json.loads(declared[0], object_pairs_hook=_closed_object) if declared else {}
            )
        except (ValueError, TypeError):
            valid = False
            rules = validate_applicability({})
        result.append(
            {
                "pattern_id": entry.id,
                "title": entry.title,
                "status": entry.status,
                "section": entry.section,
                "source_start_line": entry.line_number,
                "source_end_line": end,
                "source_digest": source_digest,
                "pattern_digest": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "body": body,
                "applicability": rules,
                "applicability_valid": valid,
                "applicability_declared": bool(declared),
            }
        )
    return result


def exclusion_reasons(record: Mapping[str, Any], context: PatternContext | None = None) -> list[str]:
    if not record.get("pattern_id"):
        return []
    if record.get("applicability_valid") is not True:
        return ["invalid_applicability"]
    try:
        rules = validate_applicability(record.get("applicability"))
    except ValueError:
        return ["invalid_applicability"]
    context = context or PatternContext()
    reasons: list[str] = []
    if record["pattern_id"] in context.blocked_patterns:
        reasons.append("conflicting_active_pattern")
    if rules["environments"] and context.environment not in rules["environments"]:
        reasons.append("unsupported_environment")
    reasons.extend(
        f"missing_prerequisite:{name}" for name in rules["prerequisites"] if name not in context.capabilities
    )
    reasons.extend(f"conflicting_pattern:{name}" for name in rules["conflicts"] if name in context.active_patterns)
    reasons.extend(
        f"resource_budget:{name}"
        for name, cost in rules["resource_costs"].items()
        if name in context.resource_budgets and cost > context.resource_budgets[name]
    )
    return reasons


def mutually_compatible(record: Mapping[str, Any], selected: list[Mapping[str, Any]]) -> bool:
    if not record.get("pattern_id"):
        return True
    conflicts = set(record.get("applicability", {}).get("conflicts", []))
    return not any(
        row.get("pattern_id") in conflicts or record["pattern_id"] in row.get("applicability", {}).get("conflicts", [])
        for row in selected
        if row.get("pattern_id")
    )
