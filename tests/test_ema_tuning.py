"""Tests for H13 — EMA Tuning."""
from __future__ import annotations

import pytest

from algo_cli.intelligence.ema_tuning import EMATuner, TuningState


class TestEMATuner:
    def test_initial_state(self) -> None:
        tuner = EMATuner("threshold", initial_value=0.5)
        assert tuner.ema == 0.5
        assert tuner.samples == 0
        assert not tuner.can_apply

    def test_update_moves_ema(self) -> None:
        tuner = EMATuner("threshold", initial_value=0.5, alpha=0.3)
        new_ema = tuner.update(1.0)
        # EMA = 0.3 * 1.0 + 0.7 * 0.5 = 0.65
        assert new_ema == pytest.approx(0.65)

    def test_update_binary_positive(self) -> None:
        tuner = EMATuner("threshold", initial_value=0.5, alpha=0.3)
        tuner.update_binary(True)
        assert tuner.ema == pytest.approx(0.65)

    def test_update_binary_negative(self) -> None:
        tuner = EMATuner("threshold", initial_value=0.5, alpha=0.3)
        tuner.update_binary(False)
        # EMA = 0.3 * 0.0 + 0.7 * 0.5 = 0.35
        assert tuner.ema == pytest.approx(0.35)

    def test_sample_count_gating(self) -> None:
        tuner = EMATuner("threshold", min_samples=3)
        tuner.update(1.0)
        tuner.update(1.0)
        assert not tuner.can_apply
        tuner.update(1.0)
        assert tuner.can_apply

    def test_apply_returns_unchanged_when_not_enough_samples(self) -> None:
        tuner = EMATuner("threshold", min_samples=5)
        params = {"threshold": 0.1, "other": "val"}
        result = tuner.apply(params)
        assert result == params

    def test_apply_updates_param_when_enough_samples(self) -> None:
        tuner = EMATuner("threshold", initial_value=0.5, alpha=0.5, min_samples=2)
        tuner.update(1.0)
        tuner.update(1.0)
        params = {"threshold": 0.1, "other": "val"}
        result = tuner.apply(params)
        assert result["threshold"] == pytest.approx(tuner.ema)
        assert result["other"] == "val"

    def test_state_snapshot(self) -> None:
        tuner = EMATuner("threshold", min_samples=2)
        tuner.update(0.8)
        state = tuner.state()
        assert isinstance(state, TuningState)
        assert state.samples == 1
        assert not state.applied

    def test_reset(self) -> None:
        tuner = EMATuner("threshold", min_samples=2)
        tuner.update(1.0)
        tuner.update(1.0)
        tuner.reset()
        assert tuner.ema == 0.5
        assert tuner.samples == 0

    def test_bounded_history(self) -> None:
        tuner = EMATuner("threshold", max_history=5)
        for i in range(10):
            tuner.update(float(i) / 10.0)
        state = tuner.state()
        assert state.history_size == 5

    def test_invalid_alpha_raises(self) -> None:
        with pytest.raises(ValueError, match="alpha"):
            EMATuner("threshold", alpha=0.0)

    def test_invalid_rating_raises(self) -> None:
        tuner = EMATuner("threshold")
        with pytest.raises(ValueError, match="rating"):
            tuner.update(1.5)