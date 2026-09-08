from __future__ import annotations

import pytest

from algo_cli.evals.diagnostic_graph import DiagnosticNode, run_diagnostic_graph


def test_graph_order_partial_results_and_failure_isolation() -> None:
    observed = []

    def execute(name, status):
        def run(_results):
            observed.append(name)
            return {"status": status, "reason": name}

        return run

    results = run_diagnostic_graph(
        [
            DiagnosticNode("child", ("parent",), execute("child", "pass")),
            DiagnosticNode("unrelated", (), execute("unrelated", "fail")),
            DiagnosticNode("parent", (), execute("parent", "unavailable")),
        ]
    )
    assert observed == ["unrelated", "parent"]
    assert results["child"]["blocked_by"] == ["parent"]
    assert results["child"]["executed"] is False
    assert results["unrelated"]["status"] == "fail"


@pytest.mark.parametrize("kind", ["cycle", "missing", "duplicate"])
def test_invalid_graph_rejected_before_any_execution(kind: str) -> None:
    executed = []
    nodes = [DiagnosticNode("root", (), lambda _: executed.append(True))]
    if kind == "cycle":
        nodes += [DiagnosticNode("a", ("b",), lambda _: {}), DiagnosticNode("b", ("a",), lambda _: {})]
    elif kind == "missing":
        nodes += [DiagnosticNode("a", ("missing",), lambda _: {})]
    else:
        nodes += [DiagnosticNode("root", (), lambda _: {})]
    with pytest.raises(ValueError):
        run_diagnostic_graph(nodes)
    assert not executed


def test_invalid_callback_result_is_error_and_blocks_dependents() -> None:
    result = run_diagnostic_graph(
        [
            DiagnosticNode("invalid", (), lambda _: {"status": "success"}),
            DiagnosticNode("dependent", ("invalid",), lambda _: pytest.fail("must not run")),
            DiagnosticNode("independent", (), lambda _: {"status": "pass"}),
        ]
    )
    assert result["invalid"]["status"] == "error"
    assert result["dependent"]["status"] == "unavailable"
    assert result["independent"]["status"] == "pass"
