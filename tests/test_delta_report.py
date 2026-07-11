"""Tests for H30 — Delta Reporting."""
from __future__ import annotations

from algo_cli.intelligence.delta_report import DeltaReporter


def test_no_changes() -> None:
    report = DeltaReporter.compute({"a": 1, "b": 2}, {"a": 1, "b": 2})
    assert report.is_empty is True


def test_added() -> None:
    report = DeltaReporter.compute({"a": 1}, {"a": 1, "b": 2})
    assert len(report.added) == 1
    assert report.added[0].target_id == "b"
    assert report.added[0].new_value == 2


def test_removed() -> None:
    report = DeltaReporter.compute({"a": 1, "b": 2}, {"a": 1})
    assert len(report.removed) == 1
    assert report.removed[0].target_id == "b"
    assert report.removed[0].old_value == 2


def test_changed() -> None:
    report = DeltaReporter.compute({"a": 1}, {"a": 2})
    assert len(report.changed) == 1
    assert report.changed[0].old_value == 1
    assert report.changed[0].new_value == 2


def test_summary() -> None:
    report = DeltaReporter.compute({"a": 1, "b": 2}, {"a": 2, "c": 3})
    summary = report.summary()
    assert "added" in summary
    assert "removed" in summary
    assert "changed" in summary


def test_to_dict() -> None:
    report = DeltaReporter.compute({"a": 1}, {"a": 2})
    d = report.to_dict()
    assert "entries" in d
    assert "summary" in d


def test_empty_states() -> None:
    report = DeltaReporter.compute({}, {})
    assert report.is_empty is True


def test_all_added() -> None:
    report = DeltaReporter.compute({}, {"a": 1, "b": 2})
    assert len(report.added) == 2
    assert report.is_empty is False