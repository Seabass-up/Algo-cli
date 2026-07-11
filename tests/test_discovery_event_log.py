"""Tests for H4 — Harness Discovery Event Log."""
from __future__ import annotations

from algo_cli.intelligence.discovery_event_log import DiscoveryEventLog, EventType


def test_emit_event() -> None:
    log = DiscoveryEventLog()
    event = log.emit(EventType.DISCOVERED, "H1")
    assert event.event_type == EventType.DISCOVERED
    assert event.target_id == "H1"


def test_query_by_type() -> None:
    log = DiscoveryEventLog()
    log.emit(EventType.DISCOVERED, "H1")
    log.emit(EventType.VALIDATED, "H1")
    discovered = log.query(event_type=EventType.DISCOVERED)
    assert len(discovered) == 1


def test_query_by_target() -> None:
    log = DiscoveryEventLog()
    log.emit(EventType.DISCOVERED, "H1")
    log.emit(EventType.DISCOVERED, "H2")
    h1_events = log.query(target_id="H1")
    assert len(h1_events) == 1


def test_listener_fires() -> None:
    log = DiscoveryEventLog()
    received = []
    log.on(lambda e: received.append(e))
    log.emit(EventType.DISCOVERED, "H1")
    assert len(received) == 1
    assert received[0].target_id == "H1"


def test_clear_listeners() -> None:
    log = DiscoveryEventLog()
    received = []
    log.on(lambda e: received.append(e))
    log.clear_listeners()
    log.emit(EventType.DISCOVERED, "H1")
    assert len(received) == 0


def test_max_events_eviction() -> None:
    log = DiscoveryEventLog(max_events=3)
    log.emit(EventType.DISCOVERED, "H1")
    log.emit(EventType.DISCOVERED, "H2")
    log.emit(EventType.DISCOVERED, "H3")
    log.emit(EventType.DISCOVERED, "H4")
    assert log.count() == 3
    assert log.query(target_id="H1") == []


def test_count() -> None:
    log = DiscoveryEventLog()
    assert log.count() == 0
    log.emit(EventType.DISCOVERED, "H1")
    assert log.count() == 1


def test_to_dict() -> None:
    log = DiscoveryEventLog()
    event = log.emit(EventType.DISCOVERED, "H1", source="test", metadata={"k": "v"})
    d = event.to_dict()
    assert d["source"] == "test"
    assert d["metadata"] == {"k": "v"}