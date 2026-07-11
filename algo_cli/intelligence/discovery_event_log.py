"""H4 — Harness Discovery Event Log.

Structured events for the full discovery lifecycle.
Mined from T3MP3ST EventEmitter (finding:discovered, finding:validated, finding:retracted).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


class EventType:
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    RETRACTED = "retracted"
    PROPOSED = "proposed"
    ENRICHED = "enriched"
    PROPAGATED = "propagated"


@dataclass
class DiscoveryEvent:
    """A single lifecycle event."""

    event_type: str
    target_id: str
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "target_id": self.target_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


class DiscoveryEventLog:
    """Append-only event log with listener support."""

    def __init__(self, max_events: int = 10_000) -> None:
        self._events: list[DiscoveryEvent] = []
        self._listeners: list[Callable[[DiscoveryEvent], None]] = []
        self._max_events = max_events

    def emit(
        self,
        event_type: str,
        target_id: str,
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> DiscoveryEvent:
        event = DiscoveryEvent(
            event_type=event_type,
            target_id=target_id,
            source=source,
            metadata=metadata or {},
        )
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]
        for listener in self._listeners:
            listener(event)
        return event

    def on(self, listener: Callable[[DiscoveryEvent], None]) -> None:
        self._listeners.append(listener)

    def query(
        self,
        event_type: str | None = None,
        target_id: str | None = None,
    ) -> list[DiscoveryEvent]:
        results = self._events
        if event_type is not None:
            results = [e for e in results if e.event_type == event_type]
        if target_id is not None:
            results = [e for e in results if e.target_id == target_id]
        return list(results)

    def all(self) -> list[DiscoveryEvent]:
        return list(self._events)

    def count(self) -> int:
        return len(self._events)

    def clear_listeners(self) -> None:
        self._listeners.clear()