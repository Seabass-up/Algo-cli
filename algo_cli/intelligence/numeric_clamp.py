"""H10 — Numeric Clamp Guard.

Prevents impossible telemetry values by clamping to a valid range and
recording warnings when out-of-range values are encountered.

Source: GLOSSOPETRAE ``clampCheck()``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ClampWarning:
    """Recorded when a value is clamped."""

    original: float
    clamped: float
    lo: float
    hi: float


class NumericClampGuard:
    """Clamp numeric values to [lo, hi] and record out-of-range events."""

    def __init__(self, lo: float = 0.0, hi: float = 1.0) -> None:
        if lo > hi:
            raise ValueError(f"lo ({lo}) must be <= hi ({hi})")
        self.lo = lo
        self.hi = hi
        self.warnings: list[ClampWarning] = []

    def clamp(self, value: float) -> float:
        """Clamp *value* to [lo, hi].  Records a warning if clamping occurred."""
        if value < self.lo:
            self.warnings.append(
                ClampWarning(original=value, clamped=self.lo, lo=self.lo, hi=self.hi)
            )
            return self.lo
        if value > self.hi:
            self.warnings.append(
                ClampWarning(original=value, clamped=self.hi, lo=self.lo, hi=self.hi)
            )
            return self.hi
        return value

    def clamp_many(self, values: list[float]) -> list[float]:
        """Clamp a batch of values."""
        return [self.clamp(v) for v in values]

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def clear_warnings(self) -> None:
        self.warnings.clear()

    def stats(self) -> dict:
        return {
            "lo": self.lo,
            "hi": self.hi,
            "warnings": len(self.warnings),
            "clamped_count": len(self.warnings),
        }