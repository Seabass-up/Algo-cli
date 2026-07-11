"""H21 — Context-Adaptive Parameter Selection.

Classify context → select parameters before execution.
Mined from G0DM0D3 PAPER.md §3.2 AutoTune.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextPattern:
    """A pattern that matches a context and maps to parameters."""

    name: str
    keywords: list[str]
    weight: float = 1.0
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextClassification:
    """Result of classifying a context."""

    matched_patterns: list[str] = field(default_factory=list)
    confidence: float = 0.0
    selected_parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_patterns": list(self.matched_patterns),
            "confidence": self.confidence,
            "selected_parameters": dict(self.selected_parameters),
        }


class ContextAdaptiveSelector:
    """Score context against patterns and interpolate parameters."""

    def __init__(self) -> None:
        self._patterns: list[ContextPattern] = []
        self._defaults: dict[str, Any] = {}

    def set_defaults(self, params: dict[str, Any]) -> None:
        self._defaults = dict(params)

    def register_pattern(
        self,
        name: str,
        keywords: list[str],
        parameters: dict[str, Any],
        weight: float = 1.0,
    ) -> ContextPattern:
        pattern = ContextPattern(
            name=name, keywords=keywords, weight=weight, parameters=dict(parameters)
        )
        self._patterns.append(pattern)
        return pattern

    def classify(self, context_text: str, history: list[str] | None = None) -> ContextClassification:
        """Classify context using weighted pattern scoring (3× current, 1× history)."""
        text_lower = context_text.lower()
        scores: dict[str, float] = {}
        # Current message: 3× weight
        for pattern in self._patterns:
            score = 0.0
            for kw in pattern.keywords:
                if re.search(re.escape(kw.lower()), text_lower):
                    score += pattern.weight
            scores[pattern.name] = score * 3.0
        # History: 1× weight
        if history:
            for h in history:
                h_lower = h.lower()
                for pattern in self._patterns:
                    for kw in pattern.keywords:
                        if re.search(re.escape(kw.lower()), h_lower):
                            scores[pattern.name] = scores.get(pattern.name, 0.0) + pattern.weight
        # Select matched patterns (score > 0)
        matched = [name for name, score in scores.items() if score > 0]
        total_score = sum(scores.values())
        confidence = total_score / (total_score + 1.0) if total_score > 0 else 0.0
        # Interpolate parameters
        selected = dict(self._defaults)
        if matched:
            for name in matched:
                pattern = next(p for p in self._patterns if p.name == name)
                for k, v in pattern.parameters.items():
                    selected[k] = v
        return ContextClassification(
            matched_patterns=matched,
            confidence=min(confidence, 1.0),
            selected_parameters=selected,
        )

    def get_patterns(self) -> list[ContextPattern]:
        return list(self._patterns)

    def count(self) -> int:
        return len(self._patterns)