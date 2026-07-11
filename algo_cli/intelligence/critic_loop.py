"""B64. Critic Loop + Budget Guard.

Quality-gated iteration with cost/iteration kill switch.
Source: deep-research-agent pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable


class CriticDimension(Enum):
    COVERAGE = auto()
    RECENCY = auto()
    DEPTH = auto()
    DIVERSITY = auto()
    ACCURACY = auto()


@dataclass
class CriticScore:
    dimension: CriticDimension
    score: float  # 0.0 to 1.0
    notes: str = ""


@dataclass
class CriticResult:
    scores: list[CriticScore] = field(default_factory=list)
    overall: float = 0.0
    passed: bool = False
    recommendations: list[str] = field(default_factory=list)
    iteration: int = 0


@dataclass
class BudgetGuard:
    max_iterations: int = 5
    max_cost: float = 1.0  # abstract cost units
    max_time_s: float = 120.0
    current_cost: float = 0.0
    current_time: float = 0.0
    current_iterations: int = 0
    _start_time: float = field(default_factory=time.time)

    def can_proceed(self) -> bool:
        self.current_time = time.time() - self._start_time
        return (
            self.current_iterations < self.max_iterations
            and self.current_cost < self.max_cost
            and self.current_time < self.max_time_s
        )

    def spend(self, cost: float = 0.1) -> None:
        self.current_cost += cost
        self.current_iterations += 1

    def remaining_budget(self) -> dict[str, float]:
        return {
            "iterations": max(0, self.max_iterations - self.current_iterations),
            "cost": max(0, self.max_cost - self.current_cost),
            "time_s": max(0, self.max_time_s - (time.time() - self._start_time)),
        }


class CriticLoop:
    """Quality-gated iteration loop with budget guard."""

    def __init__(self, threshold: float = 0.8,
                 budget: BudgetGuard | None = None) -> None:
        self._threshold = threshold
        self._budget = budget or BudgetGuard()
        self._scorers: dict[CriticDimension, Callable[[Any], float]] = {}

    def register_scorer(self, dimension: CriticDimension,
                        scorer: Callable[[Any], float]) -> None:
        self._scorers[dimension] = scorer

    def evaluate(self, artifact: Any, iteration: int) -> CriticResult:
        result = CriticResult(iteration=iteration)
        for dim, scorer in self._scorers.items():
            try:
                score = scorer(artifact)
                result.scores.append(CriticScore(dimension=dim, score=score))
            except Exception:
                result.scores.append(CriticScore(dimension=dim, score=0.0, notes="scorer error"))

        result.overall = sum(s.score for s in result.scores) / len(result.scores) if result.scores else 0.0
        result.passed = result.overall >= self._threshold

        if not result.passed:
            for s in result.scores:
                if s.score < self._threshold:
                    result.recommendations.append(f"Improve {s.dimension.name.lower()}: {s.score:.0%}")

        return result

    def run(self, produce_fn: Callable[[int], Any],
            improve_fn: Callable[[Any, CriticResult], Any] | None = None,
            cost_per_iteration: float = 0.1) -> tuple[Any, CriticResult]:
        """Run the critic loop until threshold or budget exhausted."""
        artifact = None
        result = CriticResult()

        while self._budget.can_proceed():
            self._budget.spend(cost_per_iteration)
            artifact = produce_fn(self._budget.current_iterations)
            result = self.evaluate(artifact, self._budget.current_iterations)

            if result.passed:
                break
            if improve_fn:
                artifact = improve_fn(artifact, result)
        else:
            result.recommendations.append("Budget exhausted")

        return artifact, result