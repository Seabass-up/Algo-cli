from __future__ import annotations

import pytest

from algo_cli.intelligence.structural_validator import StructuralValidator
from algo_cli.reasoning.mcts import MCTSReasoner


def test_structural_validator_tracks_plain_function_calls() -> None:
    validator = StructuralValidator()

    violations = validator.compile_file(
        "caller.py",
        "def caller():\n    removed_function()\n",
    )
    broken = validator.check_broken_callers("removed_function")

    assert any(violation.symbol == "caller" for violation in violations)
    assert len(broken) == 1
    assert broken[0].code == "E004"


def test_mcts_selection_requires_an_initialized_root() -> None:
    reasoner = MCTSReasoner()

    with pytest.raises(RuntimeError, match="root must be initialized"):
        reasoner._select()
