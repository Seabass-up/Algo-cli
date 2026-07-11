"""Tests for H10 — Numeric Clamp Guard."""
from __future__ import annotations

from algo_cli.intelligence.numeric_clamp import NumericClampGuard


class TestNumericClampGuard:
    def test_value_in_range_passes_through(self) -> None:
        guard = NumericClampGuard(0.0, 1.0)
        assert guard.clamp(0.5) == 0.5
        assert not guard.has_warnings()

    def test_value_below_lo_is_clamped(self) -> None:
        guard = NumericClampGuard(0.0, 1.0)
        assert guard.clamp(-0.5) == 0.0
        assert guard.has_warnings()
        assert guard.warnings[0].original == -0.5
        assert guard.warnings[0].clamped == 0.0

    def test_value_above_hi_is_clamped(self) -> None:
        guard = NumericClampGuard(0.0, 1.0)
        assert guard.clamp(1.5) == 1.0
        assert guard.has_warnings()
        assert guard.warnings[0].original == 1.5
        assert guard.warnings[0].clamped == 1.0

    def test_clamp_many(self) -> None:
        guard = NumericClampGuard(0.0, 1.0)
        result = guard.clamp_many([0.5, -0.1, 1.2, 0.0, 1.0])
        assert result == [0.5, 0.0, 1.0, 0.0, 1.0]
        assert len(guard.warnings) == 2

    def test_clear_warnings(self) -> None:
        guard = NumericClampGuard(0.0, 1.0)
        guard.clamp(2.0)
        assert guard.has_warnings()
        guard.clear_warnings()
        assert not guard.has_warnings()

    def test_stats(self) -> None:
        guard = NumericClampGuard(0.0, 1.0)
        guard.clamp(-0.1)
        guard.clamp(1.5)
        stats = guard.stats()
        assert stats["lo"] == 0.0
        assert stats["hi"] == 1.0
        assert stats["warnings"] == 2
        assert stats["clamped_count"] == 2

    def test_custom_range(self) -> None:
        guard = NumericClampGuard(-10.0, 10.0)
        assert guard.clamp(5.0) == 5.0
        assert guard.clamp(-15.0) == -10.0
        assert guard.clamp(15.0) == 10.0

    def test_invalid_range_raises(self) -> None:
        import pytest
        with pytest.raises(ValueError, match="lo.*must be <= hi"):
            NumericClampGuard(1.0, 0.0)