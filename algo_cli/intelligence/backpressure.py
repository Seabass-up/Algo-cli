"""B86. Backpressure Signals for Token-Aware Agents.

PRESSURE=LOW/MED/HIGH tells agent when to expand or contract output.
BUDGET directives.  Source: Keel pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class PressureLevel(Enum):
    LOW = auto()      # plenty of budget — be thorough
    MEDIUM = auto()   # budget tightening — be concise
    HIGH = auto()     # budget critical — minimal output only
    CRITICAL = auto() # budget exhausted — stop


@dataclass
class BackpressureSignal:
    level: PressureLevel
    remaining_tokens: int = 0
    total_budget: int = 0
    used_pct: float = 0.0
    directive: str = ""
    recommended_max_output: int = 0


class BackpressureMonitor:
    """Monitor token budget and emit pressure signals."""

    THRESHOLDS: dict[PressureLevel, float] = {
        PressureLevel.LOW: 0.5,       # >50% remaining
        PressureLevel.MEDIUM: 0.25,   # >25% remaining
        PressureLevel.HIGH: 0.10,     # >10% remaining
        PressureLevel.CRITICAL: 0.0,  # exhausted
    }

    DIRECTIVES: dict[PressureLevel, str] = {
        PressureLevel.LOW: "BUDGET:EXPAND — be thorough, include examples and context",
        PressureLevel.MEDIUM: "BUDGET:NORMAL — be concise but complete",
        PressureLevel.HIGH: "BUDGET:CONTRACT — minimal output, skip explanations",
        PressureLevel.CRITICAL: "BUDGET:STOP — output budget exhausted, stop immediately",
    }

    MAX_OUTPUT: dict[PressureLevel, int] = {
        PressureLevel.LOW: 4000,
        PressureLevel.MEDIUM: 2000,
        PressureLevel.HIGH: 500,
        PressureLevel.CRITICAL: 0,
    }

    def __init__(self, total_budget: int = 8000) -> None:
        self._total = total_budget
        self._used = 0

    def consume(self, tokens: int) -> BackpressureSignal:
        """Consume tokens and return current pressure signal."""
        self._used += tokens
        return self.signal()

    def signal(self) -> BackpressureSignal:
        """Get current backpressure signal without consuming."""
        remaining = max(0, self._total - self._used)
        used_pct = self._used / self._total if self._total > 0 else 1.0
        remaining_pct = 1.0 - used_pct

        # Determine level
        level = PressureLevel.CRITICAL
        for lvl, threshold in self.THRESHOLDS.items():
            if remaining_pct >= threshold:
                level = lvl
                break

        return BackpressureSignal(
            level=level,
            remaining_tokens=remaining,
            total_budget=self._total,
            used_pct=used_pct,
            directive=self.DIRECTIVES[level],
            recommended_max_output=self.MAX_OUTPUT[level],
        )

    def reset(self) -> None:
        self._used = 0

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self._total - self._used)

    def should_stop(self) -> bool:
        return self.signal().level == PressureLevel.CRITICAL

    def should_contract(self) -> bool:
        return self.signal().level in (PressureLevel.HIGH, PressureLevel.CRITICAL)