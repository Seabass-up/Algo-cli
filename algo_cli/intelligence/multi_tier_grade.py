"""H26 — Multi-Tier Grading.

Strict / lenient / structured grading tiers for solution evaluation.
Mined from GLOSSOPETRAE experiments/lib/grade_rigor.mjs.

- gradeStrict: unforgiving — any error fails
- gradeLenient: attempts surface recovery before grading
- gradeStructured: requires control flow presence, rejects degenerate precompute
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field


@dataclass
class GradeResult:
    """Result of grading a solution."""

    tier: str  # "strict", "lenient", "structured"
    passed: bool
    score: float  # 0.0 - 1.0
    correct_output: bool = False
    has_required_constructs: bool = False
    is_degenerate: bool = False
    recovered: bool = False
    notes: list[str] = field(default_factory=list)


def _check_output_correctness(actual: str, expected: str) -> bool:
    """Check if actual output matches expected."""
    return actual.strip() == expected.strip()


def _has_required_constructs(code: str) -> bool:
    """Check if code has loops, functions, or conditionals."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    has_loop = False
    has_func = False
    has_conditional = False

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            has_loop = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            has_func = True
        elif isinstance(node, (ast.If, ast.IfExp)):
            has_conditional = True

    return has_loop or has_func or has_conditional


def _is_degenerate_precompute(code: str) -> bool:
    """Check if code precomputes the answer without required constructs."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    has_loop = any(isinstance(n, (ast.For, ast.While)) for n in ast.walk(tree))
    has_func_def = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        for n in ast.walk(tree)
    )
    has_conditional = any(isinstance(n, (ast.If, ast.IfExp)) for n in ast.walk(tree))
    has_call = any(isinstance(n, ast.Call) for n in ast.walk(tree))

    # Degenerate: has no loops, no function defs, no conditionals, and no function calls
    return not (has_loop or has_func_def or has_conditional or has_call)


def grade_strict(
    code: str,
    actual_output: str,
    expected_output: str,
) -> GradeResult:
    """Grade strictly — any error fails."""
    correct = _check_output_correctness(actual_output, expected_output)
    return GradeResult(
        tier="strict",
        passed=correct,
        score=1.0 if correct else 0.0,
        correct_output=correct,
        notes=["Strict: output must match exactly"],
    )


def grade_lenient(
    code: str,
    actual_output: str,
    expected_output: str,
) -> GradeResult:
    """Grade leniently — attempt surface recovery before grading."""
    # Try exact match first (no normalization)
    if actual_output == expected_output:
        return GradeResult(
            tier="lenient",
            passed=True,
            score=1.0,
            correct_output=True,
            recovered=False,
            notes=["Lenient: exact match"],
        )

    # Try surface recovery: normalize whitespace
    recovered_output = actual_output.strip()
    recovered_expected = expected_output.strip()

    # Try exact match after strip
    correct = recovered_output == recovered_expected
    if correct:
        return GradeResult(
            tier="lenient",
            passed=True,
            score=1.0,
            correct_output=True,
            recovered=True,
            notes=["Lenient: whitespace stripped"],
        )

    # Try case-insensitive match (recovery)
    correct = recovered_output.lower() == recovered_expected.lower()

    # Try removing extra whitespace
    if not correct:
        import re
        norm_actual = re.sub(r"\s+", " ", recovered_output).strip()
        norm_expected = re.sub(r"\s+", " ", recovered_expected).strip()
        correct = norm_actual == norm_expected
        recovered = correct
    else:
        recovered = True

    return GradeResult(
        tier="lenient",
        passed=correct,
        score=1.0 if correct else 0.0,
        correct_output=correct,
        recovered=recovered,
        notes=["Lenient: whitespace and case normalized"] if recovered else ["Lenient: output does not match"],
    )


def grade_structured(
    code: str,
    actual_output: str,
    expected_output: str,
) -> GradeResult:
    """Grade with structure requirements — must have control flow, no degenerate precompute."""
    correct = _check_output_correctness(actual_output, expected_output)
    has_constructs = _has_required_constructs(code)
    degenerate = _is_degenerate_precompute(code)

    notes: list[str] = []
    if degenerate:
        notes.append("Degenerate precompute: no loops, functions, or conditionals found")
    if not has_constructs:
        notes.append("Missing required constructs (loops, functions, or conditionals)")

    # Structured: must be correct AND have constructs AND not be degenerate
    passed = correct and has_constructs and not degenerate

    if passed:
        score = 1.0
    elif correct and not degenerate:
        score = 0.7  # Correct but missing some constructs
    elif correct:
        score = 0.3  # Correct but degenerate
    else:
        score = 0.0

    return GradeResult(
        tier="structured",
        passed=passed,
        score=score,
        correct_output=correct,
        has_required_constructs=has_constructs,
        is_degenerate=degenerate,
        notes=notes,
    )


def grade(
    code: str,
    actual_output: str,
    expected_output: str,
    tier: str = "strict",
) -> GradeResult:
    """Grade a solution at the specified tier.

    Args:
        code: The solution code.
        actual_output: What the code produced.
        expected_output: What was expected.
        tier: "strict", "lenient", or "structured".

    Returns:
        GradeResult with detailed grading information.
    """
    if tier == "strict":
        return grade_strict(code, actual_output, expected_output)
    elif tier == "lenient":
        return grade_lenient(code, actual_output, expected_output)
    elif tier == "structured":
        return grade_structured(code, actual_output, expected_output)
    else:
        raise ValueError(f"Unknown grading tier: {tier}")
