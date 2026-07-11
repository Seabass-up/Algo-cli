"""H4 — Harness Discovery Event Log.

Structured events for the full discovery lifecycle.
Mined from T3MP3ST EventEmitter (finding:discovered, finding:retracted, etc.).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class EventType(str, Enum):
    DISCOVERED = "discovered"
    PROPOSED = "proposed"
    VERIFIED = "verified"
    RETRACTED = "retracted"
    DEPRECATED = "deprecated"
    PROMOTED = "promoted"


@dataclass
class Event:
    """A single discovery event."""

    event_type: EventType
    target_id: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "target_id": self.target_id,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


class EventLogger:
    """Append-only event log with subscription support."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._subscribers: list[tuple[Callable[[Event], None], EventType | None]] = []

    def emit(
        self,
        event_type: EventType,
        target_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> Event:
        """Emit an event and notify subscribers."""
        event = Event(
            event_type=event_type,
            target_id=target_id,
            metadata=metadata or {},
        )
        self._events.append(event)
        for callback, filter_type in self._subscribers:
            if filter_type is None or filter_type == event_type:
                callback(event)
        return event

    def subscribe(
        self,
        callback: Callable[[Event], None],
        event_type: EventType | None = None,
    ) -> None:
        """Subscribe to events. Optionally filter by event type."""
        self._subscribers.append((callback, event_type))

    def query(
        self,
        event_type: EventType | None = None,
        target_id: str | None = None,
    ) -> list[Event]:
        """Query events by type and/or target."""
        results = self._events
        if event_type is not None:
            results = [e for e in results if e.event_type == event_type]
        if target_id is not None:
            results = [e for e in results if e.target_id == target_id]
        return list(results)

    def count(self) -> int:
        return len(self._events)

    def clear(self) -> None:
        self._events.clear()
        self._subscribers.clear()