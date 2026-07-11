"""H16 — Detection Risk Circuit Breaker.

Accumulate risk per failure, auto-disable at threshold.
Mined from T3MP3ST WHITEPAPER §3.5.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CircuitState:
    """State for a single circuit."""

    name: str
    risk: float = 0.0
    threshold: float = 1.0
    increment: float = 0.1
    tripped: bool = False
    trip_count: int = 0
    last_failure: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


class CircuitBreaker:
    """Manage multiple named circuit breakers."""

    def __init__(self) -> None:
        self._circuits: dict[str, CircuitState] = {}
        self._on_trip: list[Callable[[str, CircuitState], None]] = []

    def register(
        self,
        name: str,
        threshold: float = 1.0,
        increment: float = 0.1,
    ) -> CircuitState:
        circuit = CircuitState(name=name, threshold=threshold, increment=increment)
        self._circuits[name] = circuit
        return circuit

    def record_failure(self, name: str, reason: str = "") -> CircuitState:
        circuit = self._circuits.get(name)
        if circuit is None:
            raise KeyError(f"Circuit {name!r} not registered")
        if circuit.tripped:
            return circuit
        circuit.risk += circuit.increment
        circuit.last_failure = reason
        if circuit.risk >= circuit.threshold:
            circuit.tripped = True
            circuit.trip_count += 1
            for cb in self._on_trip:
                cb(name, circuit)
        return circuit

    def record_success(self, name: str) -> CircuitState:
        circuit = self._circuits.get(name)
        if circuit is None:
            raise KeyError(f"Circuit {name!r} not registered")
        circuit.risk = max(0.0, circuit.risk - circuit.increment)
        return circuit

    def is_tripped(self, name: str) -> bool:
        circuit = self._circuits.get(name)
        return circuit is not None and circuit.tripped

    def reset(self, name: str) -> CircuitState:
        circuit = self._circuits.get(name)
        if circuit is None:
            raise KeyError(f"Circuit {name!r} not registered")
        circuit.risk = 0.0
        circuit.tripped = False
        circuit.last_failure = ""
        return circuit

    def on_trip(self, callback: Callable[[str, CircuitState], None]) -> None:
        self._on_trip.append(callback)

    def get(self, name: str) -> CircuitState | None:
        return self._circuits.get(name)

    def all(self) -> list[CircuitState]:
        return list(self._circuits.values())

    def count(self) -> int:
        return len(self._circuits)