"""Tests for H26 — Multi-Tier Grading."""
from __future__ import annotations

from algo_cli.intelligence.multi_tier_grade import (
    grade_strict,
    grade_lenient,
    grade_structured,
    grade,
)


def test_grade_strict_correct() -> None:
    result = grade_strict("x = 1", "42", "42")
    assert result.passed is True
    assert result.score == 1.0
    assert result.tier == "strict"


def test_grade_strict_incorrect() -> None:
    result = grade_strict("x = 1", "43", "42")
    assert result.passed is False
    assert result.score == 0.0


def test_grade_lenient_whitespace_recovery() -> None:
    result = grade_lenient("x = 1", "  42  \n", "42")
    assert result.passed is True
    assert result.recovered is True


def test_grade_lenient_case_insensitive() -> None:
    result = grade_lenient("x = 1", "HELLO", "hello")
    assert result.passed is True
    assert result.recovered is True


def test_grade_lenient_no_recovery_needed() -> None:
    result = grade_lenient("x = 1", "42", "42")
    assert result.passed is True
    assert result.recovered is False


def test_grade_structured_correct_with_constructs() -> None:
    code = """
def solve():
    for i in range(10):
        if i == 5:
            return i
    return 0
"""
    result = grade_structured(code, "5", "5")
    assert result.passed is True
    assert result.score == 1.0
    assert result.is_degenerate is False
    assert result.has_required_constructs is True


def test_grade_structured_degenerate_precompute() -> None:
    code = "result = 42"
    result = grade_structured(code, "42", "42")
    assert result.passed is False
    assert result.is_degenerate is True
    assert result.score == 0.3  # Correct but degenerate


def test_grade_structured_correct_no_constructs() -> None:
    code = "x = 1 + 2\nprint(x)"
    result = grade_structured(code, "3", "3")
    assert result.passed is False
    assert result.has_required_constructs is False
    assert result.score == 0.7  # Correct but missing constructs


def test_grade_structured_incorrect() -> None:
    code = "for i in range(10):\n    pass"
    result = grade_structured(code, "99", "42")
    assert result.passed is False
    assert result.score == 0.0


def test_grade_dispatch_strict() -> None:
    result = grade("x = 1", "42", "42", tier="strict")
    assert result.tier == "strict"


def test_grade_dispatch_lenient() -> None:
    result = grade("x = 1", "42", "42", tier="lenient")
    assert result.tier == "lenient"


def test_grade_dispatch_structured() -> None:
    result = grade("def f():\n    return 1", "1", "1", tier="structured")
    assert result.tier == "structured"


def test_grade_dispatch_unknown_tier() -> None:
    import pytest
    with pytest.raises(ValueError, match="Unknown grading tier"):
        grade("x = 1", "42", "42", tier="bogus")
