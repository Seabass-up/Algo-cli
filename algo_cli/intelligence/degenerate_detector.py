"""H27 — Degenerate Solution Detection.

Catch solutions that precompute answers without required constructs.
Mined from GLOSSOPETRAE gradeStructured() — degenerate_precompute detection.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass
class DegenerateReport:
    """Report on whether a solution is degenerate."""

    is_degenerate: bool
    reason: str = ""
    has_loops: bool = False
    has_functions: bool = False
    has_conditionals: bool = False
    literal_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "is_degenerate": self.is_degenerate,
            "reason": self.reason,
            "has_loops": self.has_loops,
            "has_functions": self.has_functions,
            "has_conditionals": self.has_conditionals,
            "literal_count": self.literal_count,
        }


class DegenerateDetector:
    """Detect programs that precompute answers without algorithmic structure."""

    def __init__(self, require_loops: bool = True, require_functions: bool = True) -> None:
        self.require_loops = require_loops
        self.require_functions = require_functions

    def analyze(self, code: str) -> DegenerateReport:
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return DegenerateReport(
                is_degenerate=True,
                reason=f"Syntax error: {e}",
            )
        has_loops = False
        has_functions = False
        has_conditionals = False
        literal_count = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.While)):
                has_loops = True
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                has_functions = True
            elif isinstance(node, ast.If):
                has_conditionals = True
            elif isinstance(node, ast.Constant):
                literal_count += 1
        missing = []
        if self.require_loops and not has_loops:
            missing.append("loops")
        if self.require_functions and not has_functions:
            missing.append("functions")
        is_degenerate = len(missing) > 0 and literal_count > 0
        reason = ""
        if is_degenerate:
            reason = f"Solution has no {' or '.join(missing)} but contains {literal_count} literals — likely precomputed"
        return DegenerateReport(
            is_degenerate=is_degenerate,
            reason=reason,
            has_loops=has_loops,
            has_functions=has_functions,
            has_conditionals=has_conditionals,
            literal_count=literal_count,
        )