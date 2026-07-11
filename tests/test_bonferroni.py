"""Tests for H24 — Bonferroni Correction."""
from __future__ import annotations

import pytest

from algo_cli.intelligence.bonferroni import BonferroniGuard, BonferroniResult


class TestBonferroniGuard:
    def test_correct_divides_alpha(self) -> None:
        assert BonferroniGuard.correct(0.05, 10) == pytest.approx(0.005)

    def test_correct_single_test(self) -> None:
        assert BonferroniGuard.correct(0.05, 1) == pytest.approx(0.05)

    def test_is_significant_after_correction(self) -> None:
        # p=0.003, alpha=0.05, n=10 → adjusted=0.005 → 0.003 < 0.005 → significant
        assert BonferroniGuard.is_significant(0.003, 0.05, 10) is True

    def test_is_not_significant_after_correction(self) -> None:
        # p=0.006, alpha=0.05, n=10 → adjusted=0.005 → 0.006 > 0.005 → not significant
        assert BonferroniGuard.is_significant(0.006, 0.05, 10) is False

    def test_would_be_significant_without_correction(self) -> None:
        # p=0.006 < 0.05 → significant without correction, but not after
        assert 0.006 < 0.05
        assert not BonferroniGuard.is_significant(0.006, 0.05, 10)

    def test_evaluate_returns_result(self) -> None:
        result = BonferroniGuard.evaluate(0.003, 0.05, 10)
        assert isinstance(result, BonferroniResult)
        assert result.p_value == 0.003
        assert result.adjusted_alpha == pytest.approx(0.005)
        assert result.is_significant is True
        assert result.n_tests == 10

    def test_evaluate_many(self) -> None:
        # n=3 → adjusted_alpha = 0.05/3 ≈ 0.0167
        p_values = [0.001, 0.02, 0.004]
        results = BonferroniGuard.evaluate_many(p_values, 0.05)
        assert len(results) == 3
        assert results[0].is_significant is True   # 0.001 < 0.0167
        assert results[1].is_significant is False   # 0.02 > 0.0167
        assert results[2].is_significant is True    # 0.004 < 0.0167

    def test_zero_tests_raises(self) -> None:
        with pytest.raises(ValueError, match="n_tests"):
            BonferroniGuard.correct(0.05, 0)