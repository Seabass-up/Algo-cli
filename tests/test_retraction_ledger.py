"""Tests for H3 — Retraction Ledger."""
from __future__ import annotations

from algo_cli.intelligence.retraction_ledger import RetractionLedger


def test_add_retraction() -> None:
    ledger = RetractionLedger()
    entry = ledger.add("H1", "Superseded by H2")
    assert entry.target_id == "H1"
    assert entry.reason == "Superseded by H2"


def test_is_retracted() -> None:
    ledger = RetractionLedger()
    ledger.add("H1", "reason")
    assert ledger.is_retracted("H1") is True
    assert ledger.is_retracted("H2") is False


def test_get_retractions() -> None:
    ledger = RetractionLedger()
    ledger.add("H1", "reason 1")
    ledger.add("H1", "reason 2")
    entries = ledger.get_retractions("H1")
    assert len(entries) == 2


def test_delete_always_returns_false() -> None:
    ledger = RetractionLedger()
    ledger.add("H1", "reason")
    assert ledger.delete("anything") is False


def test_count() -> None:
    ledger = RetractionLedger()
    assert ledger.count() == 0
    ledger.add("H1", "r")
    assert ledger.count() == 1


def test_all() -> None:
    ledger = RetractionLedger()
    ledger.add("H1", "r1")
    ledger.add("H2", "r2")
    assert len(ledger.all()) == 2


def test_to_dict() -> None:
    ledger = RetractionLedger()
    entry = ledger.add("H1", "r", retracted_by="reviewer")
    d = entry.to_dict()
    assert d["target_id"] == "H1"
    assert d["retracted_by"] == "reviewer"
