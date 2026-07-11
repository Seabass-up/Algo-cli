"""H19 — Statistical Stability Guard.

Warn when sample sizes are too small for stable results.
Mined from GLOSSOPETRAE VALIDATION.md D4 (3-seed default yields ±15pt swing).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class StabilityReport:
    """Report on statistical stability of a measurement."""

    sample_size: int
    mean: float
    std_dev: float
    margin_of_error: float
    confidence_level: float
    is_stable: bool
    recommended_sample_size: int
    warning: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_size": self.sample_size,
            "mean": self.mean,
            "std_dev": self.std_dev,
            "margin_of_error": self.margin_of_error,
            "confidence_level": self.confidence_level,
            "is_stable": self.is_stable,
            "recommended_sample_size": self.recommended_sample_size,
            "warning": self.warning,
        }


# t-distribution critical values (two-tailed, 95% confidence)
_T_VALUES = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    15: 2.131, 20: 2.086, 25: 2.060, 30: 2.042, 40: 2.021,
    50: 2.009, 100: 1.984, 200: 1.972, float("inf"): 1.960,
}


def _t_value(df: int) -> float:
    if df in _T_VALUES:
        return _T_VALUES[df]
    if df > 200:
        return _T_VALUES[float("inf")]
    # Find closest key
    closest = min(_T_VALUES.keys(), key=lambda k: abs(k - df) if k != float("inf") else abs(200 - df))
    return _T_VALUES[closest]


class StatisticalStabilityGuard:
    """Check if sample sizes are large enough for stable results."""

    def __init__(self, target_margin: float = 0.02, confidence: float = 0.95) -> None:
        self.target_margin = target_margin
        self.confidence = confidence

    def evaluate(self, values: list[float]) -> StabilityReport:
        n = len(values)
        if n == 0:
            return StabilityReport(
                sample_size=0, mean=0.0, std_dev=0.0, margin_of_error=float("inf"),
                confidence_level=self.confidence, is_stable=False,
                recommended_sample_size=1, warning="No samples",
            )
        mean = sum(values) / n
        if n == 1:
            return StabilityReport(
                sample_size=1, mean=mean, std_dev=0.0, margin_of_error=float("inf"),
                confidence_level=self.confidence, is_stable=False,
                recommended_sample_size=30, warning="Single sample — cannot estimate variance",
            )
        variance = sum((v - mean) ** 2 for v in values) / (n - 1)
        std_dev = math.sqrt(variance)
        t = _t_value(n - 1)
        margin = t * std_dev / math.sqrt(n)
        # Recommended sample size for target margin
        if std_dev > 0 and margin > 0:
            recommended = int(math.ceil((t * std_dev / self.target_margin) ** 2))
        else:
            recommended = n
        is_stable = margin <= self.target_margin
        warning = ""
        if not is_stable:
            warning = (
                f"Margin of error {margin:.4f} exceeds target {self.target_margin:.4f}. "
                f"Need ~{recommended} samples (have {n})."
            )
        return StabilityReport(
            sample_size=n,
            mean=mean,
            std_dev=std_dev,
            margin_of_error=margin,
            confidence_level=self.confidence,
            is_stable=is_stable,
            recommended_sample_size=max(recommended, n),
            warning=warning,
        )