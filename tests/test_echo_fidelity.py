"""Tests for H7 — Echo-Fidelity Guard."""
from __future__ import annotations

from algo_cli.intelligence.echo_fidelity import EchoFidelityGuard


class TestEchoFidelityGuard:
    def test_none_is_unmeasured(self) -> None:
        guard = EchoFidelityGuard()
        result = guard.check(None)
        assert result.status == "unmeasured"
        assert result.value is None
        assert not result.is_measured

    def test_zero_is_measured(self) -> None:
        guard = EchoFidelityGuard()
        result = guard.check(0.0)
        assert result.status == "measured"
        assert result.value == 0.0
        assert result.is_measured

    def test_int_zero_is_measured(self) -> None:
        guard = EchoFidelityGuard()
        result = guard.check(0)
        assert result.status == "measured"
        assert result.value == 0.0

    def test_positive_float_is_measured(self) -> None:
        guard = EchoFidelityGuard()
        result = guard.check(0.85)
        assert result.status == "measured"
        assert result.value == 0.85

    def test_bool_is_invalid(self) -> None:
        guard = EchoFidelityGuard()
        result = guard.check(True)
        assert result.status == "invalid"
        assert result.value is None

    def test_string_is_invalid(self) -> None:
        guard = EchoFidelityGuard()
        result = guard.check("hello")
        assert result.status == "invalid"

    def test_check_many(self) -> None:
        guard = EchoFidelityGuard()
        results = guard.check_many([None, 0.0, 0.5, "bad"])
        assert len(results) == 4
        assert results[0].status == "unmeasured"
        assert results[1].status == "measured"
        assert results[2].status == "measured"
        assert results[3].status == "invalid"

    def test_measured_values_extracts_only_floats(self) -> None:
        guard = EchoFidelityGuard()
        values = guard.measured_values([None, 0.0, 0.5, True, "bad", 1.0])
        assert values == [0.0, 0.5, 1.0]

    def test_none_vs_zero_distinction(self) -> None:
        """The core invariant: None and 0.0 must produce different statuses."""
        guard = EchoFidelityGuard()
        none_result = guard.check(None)
        zero_result = guard.check(0.0)
        assert none_result.status != zero_result.status
        assert none_result.status == "unmeasured"
        assert zero_result.status == "measured"
