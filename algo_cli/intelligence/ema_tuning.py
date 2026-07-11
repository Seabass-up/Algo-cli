"""H13 — Feedback-Driven Parameter Tuning (EMA).

Exponential moving average tuning: user feedback (binary ratings) updates
an EMA that adjusts harness parameters over time.  Includes sample-count
gating (no adjustments until minimum samples collected) and bounded history.

Source: G0DM0D3 AutoTune.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


MAX_HISTORY = 500
MIN_SAMPLES_TO_APPLY = 3


@dataclass
class TuningState:
    """Snapshot of the EMA tuner state."""

    ema: float
    samples: int
    applied: bool
    history_size: int


class EMATuner:
    """EMA-based parameter tuner with sample-count gating."""

    def __init__(
        self,
        param_name: str,
        initial_value: float = 0.5,
        alpha: float = 0.3,
        min_samples: int = MIN_SAMPLES_TO_APPLY,
        max_history: int = MAX_HISTORY,
    ) -> None:
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self.param_name = param_name
        self.alpha = alpha
        self.min_samples = min_samples
        self.max_history = max_history
        self._ema = initial_value
        self._samples = 0
        self._history: deque[float] = deque(maxlen=max_history)

    def update(self, rating: float) -> float:
        """Update EMA with a new rating (0.0 to 1.0).

        Returns the updated EMA value.
        """
        if not 0.0 <= rating <= 1.0:
            raise ValueError(f"rating must be in [0, 1], got {rating}")
        self._samples += 1
        self._history.append(rating)
        self._ema = self.alpha * rating + (1 - self.alpha) * self._ema
        return self._ema

    def update_binary(self, positive: bool) -> float:
        """Update with a binary rating (True=1.0, False=0.0)."""
        return self.update(1.0 if positive else 0.0)

    @property
    def ema(self) -> float:
        return self._ema

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def can_apply(self) -> bool:
        """True when enough samples have been collected to apply tuning."""
        return self._samples >= self.min_samples

    def apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply the tuned value to a params dict if enough samples exist.

        Returns a new dict with the parameter updated.
        """
        if not self.can_apply:
            return params
        result = dict(params)
        result[self.param_name] = self._ema
        return result

    def state(self) -> TuningState:
        """Return a snapshot of the current state."""
        return TuningState(
            ema=round(self._ema, 6),
            samples=self._samples,
            applied=self.can_apply,
            history_size=len(self._history),
        )

    def reset(self) -> None:
        """Reset to initial state."""
        self._ema = 0.5
        self._samples = 0
        self._history.clear()
