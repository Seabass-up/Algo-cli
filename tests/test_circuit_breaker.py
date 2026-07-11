"""Tests for H16 — Detection Risk Circuit Breaker."""
from __future__ import annotations

from algo_cli.intelligence.circuit_breaker import CircuitBreaker


def test_register_circuit() -> None:
    cb = CircuitBreaker()
    cb.register("tool-a", threshold=1.0, increment=0.1)
    assert cb.count() == 1


def test_record_failure_accumulates() -> None:
    cb = CircuitBreaker()
    cb.register("tool-a", threshold=1.0, increment=0.1)
    cb.record_failure("tool-a", "err1")
    state = cb.get("tool-a")
    assert state.risk == 0.1


def test_circuit_trips_at_threshold() -> None:
    cb = CircuitBreaker()
    cb.register("tool-a", threshold=0.3, increment=0.1)
    cb.record_failure("tool-a")
    cb.record_failure("tool-a")
    cb.record_failure("tool-a")
    assert cb.is_tripped("tool-a") is True


def test_circuit_does_not_trip_below_threshold() -> None:
    cb = CircuitBreaker()
    cb.register("tool-a", threshold=1.0, increment=0.1)
    cb.record_failure("tool-a")
    assert cb.is_tripped("tool-a") is False


def test_record_success_reduces_risk() -> None:
    cb = CircuitBreaker()
    cb.register("tool-a", threshold=1.0, increment=0.1)
    cb.record_failure("tool-a")
    cb.record_success("tool-a")
    assert cb.get("tool-a").risk == 0.0


def test_reset_clears_circuit() -> None:
    cb = CircuitBreaker()
    cb.register("tool-a", threshold=0.2, increment=0.1)
    cb.record_failure("tool-a")
    cb.record_failure("tool-a")
    assert cb.is_tripped("tool-a") is True
    cb.reset("tool-a")
    assert cb.is_tripped("tool-a") is False
    assert cb.get("tool-a").risk == 0.0


def test_on_trip_callback() -> None:
    cb = CircuitBreaker()
    tripped = []
    cb.on_trip(lambda name, state: tripped.append(name))
    cb.register("tool-a", threshold=0.2, increment=0.1)
    cb.record_failure("tool-a")
    cb.record_failure("tool-a")
    assert tripped == ["tool-a"]


def test_failure_after_trip_ignored() -> None:
    cb = CircuitBreaker()
    cb.register("tool-a", threshold=0.2, increment=0.1)
    cb.record_failure("tool-a")
    cb.record_failure("tool-a")
    assert cb.is_tripped("tool-a") is True
    state = cb.record_failure("tool-a")
    assert state.risk == 0.2  # Didn't increase


def test_record_failure_missing_raises() -> None:
    cb = CircuitBreaker()
    try:
        cb.record_failure("nope")
        assert False
    except KeyError:
        pass


def test_all() -> None:
    cb = CircuitBreaker()
    cb.register("a")
    cb.register("b")
    assert len(cb.all()) == 2