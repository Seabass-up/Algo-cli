"""Tests for H17 — Negative Control Samples."""
from __future__ import annotations

from algo_cli.intelligence.negative_controls import NegativeControlSuite


def test_add_sample() -> None:
    suite = NegativeControlSuite()
    suite.add_sample("s1", "clean code", expected_finding=False)
    assert suite.count() == 1


def test_evaluate_all_correct() -> None:
    suite = NegativeControlSuite()
    suite.add_sample("s1", "clean", expected_finding=False)
    suite.add_sample("s2", "vuln", expected_finding=True)
    cm = suite.evaluate({"s1": False, "s2": True})
    assert cm.true_negatives == 1
    assert cm.true_positives == 1
    assert cm.accuracy == 1.0


def test_evaluate_false_positive() -> None:
    suite = NegativeControlSuite()
    suite.add_sample("s1", "clean", expected_finding=False)
    suite.add_sample("s2", "clean2", expected_finding=False)
    cm = suite.evaluate({"s1": True, "s2": False})
    assert cm.false_positives == 1
    assert cm.true_negatives == 1
    assert cm.false_positive_rate == 0.5


def test_evaluate_false_negative() -> None:
    suite = NegativeControlSuite()
    suite.add_sample("s1", "vuln", expected_finding=True)
    cm = suite.evaluate({"s1": False})
    assert cm.false_negatives == 1
    assert cm.false_negative_rate == 1.0


def test_evaluate_missing_result_treated_as_not_detected() -> None:
    suite = NegativeControlSuite()
    suite.add_sample("s1", "vuln", expected_finding=True)
    cm = suite.evaluate({})
    assert cm.false_negatives == 1


def test_get_sample() -> None:
    suite = NegativeControlSuite()
    suite.add_sample("s1", "content")
    s = suite.get_sample("s1")
    assert s is not None
    assert s.content == "content"


def test_get_sample_missing() -> None:
    suite = NegativeControlSuite()
    assert suite.get_sample("nope") is None


def test_to_dict() -> None:
    suite = NegativeControlSuite()
    suite.add_sample("s1", "clean", expected_finding=False)
    suite.add_sample("s2", "vuln", expected_finding=True)
    cm = suite.evaluate({"s1": False, "s2": True})
    d = cm.to_dict()
    assert d["accuracy"] == 1.0
    assert d["false_positive_rate"] == 0.0