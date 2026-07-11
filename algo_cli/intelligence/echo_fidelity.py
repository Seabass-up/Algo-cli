"""H7 — Echo-Fidelity Guard.

Distinguishes ``None`` (unmeasured) from ``0.0`` (measured failure) from a
valid float.  Prevents fabrications where a missing measurement is silently
treated as a zero (or vice-versa).

Source: GLOSSOPETRAE experiments/lib — null vs 0 distinction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GuardResult:
    """Outcome of an echo-fidelity check."""

    value: Optional[float]
    status: str  # "unmeasured" | "measured" | "invalid"
    message: str

    @property
    def is_measured(self) -> bool:
        return self.status == "measured"


class EchoFidelityGuard:
    """Guard that distinguishes unmeasured from measured-zero values."""

    def check(self, value: object) -> GuardResult:
        """Check a value and return its fidelity status.

        - ``None`` → unmeasured (no data collected)
        - ``float`` or ``int`` in valid range → measured
        - ``float`` or ``int`` outside [lo, hi] → invalid
        """
        if value is None:
            return GuardResult(
                value=None,
                status="unmeasured",
                message="No measurement was taken (value is None).",
            )
        if isinstance(value, bool):
            return GuardResult(
                value=None,
                status="invalid",
                message="Boolean is not a valid measurement.",
            )
        if isinstance(value, (int, float)):
            v = float(value)
            return GuardResult(
                value=v,
                status="measured",
                message=f"Measurement recorded: {v}",
            )
        return GuardResult(
            value=None,
            status="invalid",
            message=f"Expected numeric or None, got {type(value).__name__}.",
        )

    def check_many(self, values: list[object]) -> list[GuardResult]:
        """Check a batch of values."""
        return [self.check(v) for v in values]

    def measured_values(self, values: list[object]) -> list[float]:
        """Return only the measured float values, skipping unmeasured and invalid."""
        return [
            r.value
            for r in self.check_many(values)
            if r.is_measured and r.value is not None
        ]