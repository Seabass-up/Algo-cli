"""H24 — Bonferroni Correction for Multiple Comparisons.

Prevents false discoveries when running many tests by adjusting the
significance threshold: divide alpha by the number of comparisons.

Source: GLOSSOPETRAE ``e3s_multiconstruction_stego.mjs``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BonferroniResult:
    """Result of a Bonferroni correction check."""

    p_value: float
    adjusted_alpha: float
    is_significant: bool
    n_tests: int


class BonferroniGuard:
    """Bonferroni correction guard for multiple comparisons."""

    @staticmethod
    def correct(alpha: float, n_tests: int) -> float:
        """Return the Bonferroni-adjusted alpha threshold.

        Args:
            alpha: Original significance level (e.g. 0.05).
            n_tests: Number of comparisons performed.

        Returns:
            Adjusted alpha = alpha / n_tests.
        """
        if n_tests < 1:
            raise ValueError(f"n_tests must be >= 1, got {n_tests}")
        return alpha / n_tests

    @staticmethod
    def is_significant(p_value: float, alpha: float, n_tests: int) -> bool:
        """Check if a p-value is significant after Bonferroni correction."""
        adjusted = BonferroniGuard.correct(alpha, n_tests)
        return p_value < adjusted

    @staticmethod
    def evaluate(p_value: float, alpha: float, n_tests: int) -> BonferroniResult:
        """Full evaluation with details."""
        adjusted = BonferroniGuard.correct(alpha, n_tests)
        return BonferroniResult(
            p_value=p_value,
            adjusted_alpha=adjusted,
            is_significant=p_value < adjusted,
            n_tests=n_tests,
        )

    @staticmethod
    def evaluate_many(
        p_values: list[float], alpha: float
    ) -> list[BonferroniResult]:
        """Evaluate multiple p-values with Bonferroni correction."""
        n = len(p_values)
        return [
            BonferroniGuard.evaluate(p, alpha, n) for p in p_values
        ]