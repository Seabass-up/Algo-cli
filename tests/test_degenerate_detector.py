"""Tests for H27 — Degenerate Solution Detection."""
from __future__ import annotations

from algo_cli.intelligence.degenerate_detector import DegenerateDetector


def test_detect_degenerate_no_loops() -> None:
    detector = DegenerateDetector()
    code = "result = 42"
    report = detector.analyze(code)
    assert report.is_degenerate is True
    assert "loops" in report.reason


def test_detect_degenerate_no_functions() -> None:
    detector = DegenerateDetector()
    code = "x = 5\ny = 10\nprint(x + y)"
    report = detector.analyze(code)
    assert report.is_degenerate is True


def test_valid_solution_with_loops() -> None:
    detector = DegenerateDetector()
    code = """
def solve(arr):
    total = 0
    for x in arr:
        total += x
    return total
"""
    report = detector.analyze(code)
    assert report.is_degenerate is False
    assert report.has_loops is True
    assert report.has_functions is True


def test_valid_solution_no_literals() -> None:
    detector = DegenerateDetector()
    code = "x = a + b"
    report = detector.analyze(code)
    # No literals, so even without loops/functions it's not degenerate
    assert report.is_degenerate is False


def test_syntax_error() -> None:
    detector = DegenerateDetector()
    report = detector.analyze("def broken(")
    assert report.is_degenerate is True
    assert "Syntax error" in report.reason


def test_has_conditionals() -> None:
    detector = DegenerateDetector(require_loops=False, require_functions=False)
    code = "if x > 0:\n    pass"
    report = detector.analyze(code)
    assert report.has_conditionals is True


def test_to_dict() -> None:
    detector = DegenerateDetector()
    report = detector.analyze("x = 42")
    d = report.to_dict()
    assert "is_degenerate" in d
    assert "literal_count" in d


def test_relaxed_requirements() -> None:
    detector = DegenerateDetector(require_loops=False, require_functions=False)
    code = "x = 42"
    report = detector.analyze(code)
    assert report.is_degenerate is False