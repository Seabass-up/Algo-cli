"""Release gates for deferred tool schemas and semantic supersession."""

from __future__ import annotations

from algo_cli import tools
from algo_cli.evals.tool_context_efficiency import (
    assert_tool_context_efficiency,
    run_tool_context_efficiency_benchmark,
    schema_token_estimate,
)
from algo_cli.tool_context import select_tools_for_prompt


def test_selected_schema_is_materially_smaller_than_full_catalog() -> None:
    selected = select_tools_for_prompt(
        "Fix the failing parser and run tests, then verify the diff.",
        tools.ALL_TOOLS,
    )

    assert schema_token_estimate(selected) < schema_token_estimate(tools.ALL_TOOLS) * 0.55


def test_repeated_efficiency_benchmark_passes_all_release_gates() -> None:
    result = run_tool_context_efficiency_benchmark(repeats=5)

    assert result["status"] == "pass", result["failures"]
    assert result["summary"]["stable_repeated_selection"] is True
    assert result["summary"]["schema_conversion_complete"] is True
    assert result["summary"]["required_tool_recall"] == 1.0
    assert result["summary"]["median_schema_reduction_pct"] >= 60.0
    assert result["semantic_supersession"]["reduction_pct"] >= 75.0
    assert result["typed_program"]["reduction_pct"] >= 70.0
    assert result["typed_program"]["correct_result"] is True
    assert result["typed_program"]["artifact_backed"] is True
    assert result["typed_program"]["receipt_chains_valid"] is True
    assert_tool_context_efficiency(result)


def test_efficiency_gate_rejects_missing_required_tool_recall() -> None:
    result = run_tool_context_efficiency_benchmark(repeats=3)
    result["summary"]["required_tool_recall"] = 0.5

    try:
        assert_tool_context_efficiency(result)
    except AssertionError as exc:
        assert "required tools" in str(exc)
    else:  # pragma: no cover - makes a silent gate regression explicit
        raise AssertionError("efficiency gate accepted incomplete tool recall")
