"""CUSUM detection for sustained runtime performance changes (ALGO.md L3)."""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterable


class RegressionState(str, Enum):
    INSUFFICIENT_DATA = "insufficient_data"
    STABLE = "stable"
    IMPROVING = "improving"
    REGRESSING = "regressing"


@dataclass(frozen=True)
class CUSUMResult:
    state: RegressionState
    sample_count: int
    baseline: float | None
    scale: float | None
    positive_score: float
    negative_score: float
    change_index: int | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload


def detect_cusum(
    samples: Iterable[float],
    *,
    warmup: int = 5,
    slack_scale: float = 0.5,
    threshold_scale: float = 5.0,
    min_consecutive: int = 2,
) -> CUSUMResult:
    """Detect a sustained upward/downward shift from a robust warmup baseline."""
    values: list[float] = []
    for sample in samples:
        try:
            value = float(sample)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= 0.0:
            values.append(value)
    warmup = max(3, int(warmup))
    min_consecutive = max(2, int(min_consecutive))
    if len(values) < warmup + min_consecutive:
        return CUSUMResult(
            RegressionState.INSUFFICIENT_DATA,
            len(values),
            None,
            None,
            0.0,
            0.0,
            None,
            f"need at least {warmup + min_consecutive} finite samples",
        )

    baseline_values = values[:warmup]
    baseline = statistics.median(baseline_values)
    deviations = [abs(value - baseline) for value in baseline_values]
    robust_scale = statistics.median(deviations) * 1.4826
    scale = max(robust_scale, abs(baseline) * 0.02, 1e-6)
    slack = max(0.0, float(slack_scale)) * scale
    threshold = max(1.0, float(threshold_scale)) * scale

    positive = 0.0
    negative = 0.0
    positive_run = 0
    negative_run = 0
    positive_start: int | None = None
    negative_start: int | None = None
    for index, value in enumerate(values[warmup:], start=warmup):
        delta = value - baseline
        positive = max(0.0, positive + delta - slack)
        negative = min(0.0, negative + delta + slack)

        if delta > slack:
            positive_run += 1
            positive_start = index if positive_start is None else positive_start
        else:
            positive = 0.0
            positive_run = 0
            positive_start = None
        if delta < -slack:
            negative_run += 1
            negative_start = index if negative_start is None else negative_start
        else:
            negative = 0.0
            negative_run = 0
            negative_start = None

        if positive >= threshold and positive_run >= min_consecutive:
            return CUSUMResult(
                RegressionState.REGRESSING,
                len(values),
                baseline,
                scale,
                positive,
                negative,
                positive_start,
                "sustained latency increase crossed the CUSUM decision threshold",
            )
        if abs(negative) >= threshold and negative_run >= min_consecutive:
            return CUSUMResult(
                RegressionState.IMPROVING,
                len(values),
                baseline,
                scale,
                positive,
                negative,
                negative_start,
                "sustained latency decrease crossed the CUSUM decision threshold",
            )

    return CUSUMResult(
        RegressionState.STABLE,
        len(values),
        baseline,
        scale,
        positive,
        negative,
        None,
        "no sustained shift crossed the CUSUM decision threshold",
    )


__all__ = ["CUSUMResult", "RegressionState", "detect_cusum"]
