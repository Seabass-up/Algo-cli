"""B55. ContextOps: Token Budget Compiler + JIT References.

Deterministic context selection with inclusion/exclusion reasons.
Lazy references: don't load a file unless it fits.
Source: ctxbudgeter pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class InclusionReason(Enum):
    INCLUDED = auto()
    EXCLUDED_BUDGET = auto()
    EXCLUDED_PRIORITY = auto()
    EXCLUDED_DUPLICATE = auto()
    EXCLUDED_STALE = auto()


@dataclass
class ContextItem:
    name: str
    content: str
    priority: int = 5  # 1=highest, 10=lowest
    token_estimate: int = 0
    source: str = "unknown"
    inclusion: InclusionReason = InclusionReason.INCLUDED
    exclusion_reason: str = ""


@dataclass
class ContextBOM:
    """Bill of Materials for context — auditable record of what entered/was excluded."""
    included: list[ContextItem] = field(default_factory=list)
    excluded: list[ContextItem] = field(default_factory=list)
    total_tokens: int = 0
    budget: int = 0


class TokenBudgetCompiler:
    """Compile context deterministically within a token budget."""

    def __init__(self, budget_tokens: int = 8000):
        self.budget = budget_tokens

    def _estimate_tokens(self, content: str) -> int:
        return max(1, len(content) // 4)

    def compile(self, items: list[ContextItem]) -> ContextBOM:
        """Select items to fit within budget, sorted by priority."""
        # Sort by priority (1=highest), then by token estimate (smaller first)
        sorted_items = sorted(items, key=lambda x: (x.priority, self._estimate_tokens(x.content)))

        bom = ContextBOM(budget=self.budget)
        used = 0

        for item in sorted_items:
            tokens = self._estimate_tokens(item.content)
            item.token_estimate = tokens

            if used + tokens <= self.budget:
                item.inclusion = InclusionReason.INCLUDED
                bom.included.append(item)
                used += tokens
            else:
                item.inclusion = InclusionReason.EXCLUDED_BUDGET
                item.exclusion_reason = f"Would exceed budget ({used + tokens}/{self.budget})"
                bom.excluded.append(item)

        bom.total_tokens = used
        return bom

    def jit_reference(self, name: str, loader: Callable[[], str], current_tokens: int) -> str | None:
        """Load a reference only if it fits in remaining budget."""
        remaining = self.budget - current_tokens
        if remaining <= 100:
            return None
        content = loader()
        tokens = self._estimate_tokens(content)
        if tokens <= remaining:
            return content
        return None  # Doesn't fit — skip

    def render_bom(self, bom: ContextBOM) -> str:
        """Render BOM as auditable markdown."""
        lines = [f"# Context BOM ({bom.total_tokens}/{bom.budget} tokens)", ""]
        lines.append("## Included")
        for item in bom.included:
            lines.append(f"- {item.name} ({item.token_estimate}t, P{item.priority})")
        lines.append("")
        lines.append("## Excluded")
        for item in bom.excluded:
            lines.append(f"- {item.name} ({item.token_estimate}t) — {item.exclusion_reason}")
        return "\n".join(lines)
