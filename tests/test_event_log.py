"""Tests for H4 — Harness Discovery Event Log."""
from __future__ import annotations

import time

from algo_cli.intelligence.event_log import EventLogger, EventType


def test_emit_event() -> None:
    logger = EventLogger()
    logger.emit(EventType.DISCOVERED, "entry-1", {"key": "value"})
    assert logger.count() == 1


def test_event_has_timestamp() -> None:
    logger = EventLogger()
    before = time.time()
    logger.emit(EventType.DISCOVERED, "entry-1")
    events = logger.query()
    assert events[0].timestamp >= before


def test_query_by_type() -> None:
    logger = EventLogger()
    logger.emit(EventType.DISCOVERED, "e1")
    logger.emit(EventType.RETRACTED, "e2")
    discovered = logger.query(event_type=EventType.DISCOVERED)
    assert len(discovered) == 1
    assert discovered[0].target_id == "e1"


def test_query_by_target() -> None:
    logger = EventLogger()
    logger.emit(EventType.DISCOVERED, "e1")
    logger.emit(EventType.RETRACTED, "e1")
    logger.emit(EventType.DISCOVERED, "e2")
    e1_events = logger.query(target_id="e1")
    assert len(e1_events) == 2


def test_query_by_both() -> None:
    logger = EventLogger()
    logger.emit(EventType.DISCOVERED, "e1")
    logger.emit(EventType.RETRACTED, "e1")
    result = logger.query(event_type=EventType.DISCOVERED, target_id="e1")
    assert len(result) == 1


def test_subscribe() -> None:
    logger = EventLogger()
    received = []
    logger.subscribe(lambda e: received.append(e.target_id))
    logger.emit(EventType.DISCOVERED, "e1")
    assert received == ["e1"]


def test_subscribe_filtered() -> None:
    logger = EventLogger()
    received = []
    logger.subscribe(lambda e: received.append(e.target_id), event_type=EventType.RETRACTED)
    logger.emit(EventType.DISCOVERED, "e1")
    logger.emit(EventType.RETRACTED, "e2")
    assert received == ["e2"]


def test_clear() -> None:
    logger = EventLogger()
    logger.emit(EventType.DISCOVERED, "e1")
    logger.clear()
    assert logger.count() == 0


def test_to_dict() -> None:
    logger = EventLogger()
    logger.emit(EventType.DISCOVERED, "e1", {"meta": "data"})
    events = logger.query()
    d = events[0].to_dict()
    assert d["event_type"] == "discovered"
    assert d["target_id"] == "e1"
    assert d["metadata"]["meta"] == "data"


def test_all_event_types() -> None:
    logger = EventLogger()
    for et in EventType:
        logger.emit(et, "e1")
    assert logger.count() == len(EventType)