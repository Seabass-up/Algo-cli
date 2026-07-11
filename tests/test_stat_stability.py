"""Tests for H19 — Statistical Stability Guard."""
from __future__ import annotations

from algo_cli.intelligence.stat_stability import StatisticalStabilityGuard


def test_empty_values() -> None:
    guard = StatisticalStabilityGuard()
    report = guard.evaluate([])
    assert report.is_stable is False
    assert "No samples" in report.warning


def test_single_sample() -> None:
    guard = StatisticalStabilityGuard()
    report = guard.evaluate([0.5])
    assert report.is_stable is False
    assert "Single sample" in report.warning


def test_stable_with_many_samples() -> None:
    guard = StatisticalStabilityGuard(target_margin=0.5)
    # 50 samples with low variance
    values = [0.5 + 0.01 * (i % 3) for i in range(50)]
    report = guard.evaluate(values)
    assert report.is_stable is True


def test_unstable_with_few_samples() -> None:
    guard = StatisticalStabilityGuard(target_margin=0.01)
    values = [0.1, 0.5, 0.9]
    report = guard.evaluate(values)
    assert report.is_stable is False
    assert report.recommended_sample_size > 3


def test_mean_calculation() -> None:
    guard = StatisticalStabilityGuard()
    report = guard.evaluate([0.2, 0.4, 0.6, 0.8])
    assert abs(report.mean - 0.5) < 0.001


def test_std_dev_calculation() -> None:
    guard = StatisticalStabilityGuard()
    report = guard.evaluate([0.5, 0.5, 0.5, 0.5])
    assert report.std_dev == 0.0


def test_to_dict() -> None:
    guard = StatisticalStabilityGuard()
    report = guard.evaluate([0.3, 0.5, 0.7])
    d = report.to_dict()
    assert "sample_size" in d
    assert "margin_of_error" in d
    assert "is_stable" in d


def test_recommended_sample_size_increases_with_variance() -> None:
    guard = StatisticalStabilityGuard(target_margin=0.01)
    low_var = guard.evaluate([0.49, 0.50, 0.51])
    high_var = guard.evaluate([0.1, 0.5, 0.9])
    assert high_var.recommended_sample_size > low_var.recommended_sample_size