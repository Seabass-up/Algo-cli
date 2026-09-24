"""/agent pipelines end to end: block failures stay visible and the run status stays honest."""

from __future__ import annotations

import pytest

from scenarios.scenario_harness import fail, interrupt, text


def test_scenario_agent_review_pipeline_completes(scenario):
    result = scenario.run_agent([[text("## Block Output\nNo defects found.")], [text("## Block Output\nShip it.")]])

    assert result.raised is None
    assert result.completed and result.pipeline.status == "complete"
    assert [block["role"] for block in result.pipeline.blocks] == ["review", "final"]
    assert result.thread["status"] == "complete"
    assert "[pipeline_complete] ## Block Output\nShip it." in result.display_text


def test_scenario_agent_first_block_model_error_is_reported_as_model_error(scenario):
    result = scenario.run_agent([[fail(ConnectionError("ollama is down"))]])

    assert isinstance(result.raised, ConnectionError)  # the REPL reports the provider error itself
    assert not result.completed
    assert result.thread["status"] == "failed"
    assert result.thread["turns"][-1]["error"] == "ollama is down"
    (review,) = result.thread["blocks"]
    assert review["status"] == "failed"
    # Regression: the post-block mutation audit used to overwrite this with a journal-corruption message.
    assert review["status_code"] == "model_error"
    assert review["status_reason"] == "ConnectionError: ollama is down"
    # The audit failure itself is still recorded, as a warning rather than the cause.
    assert review["verification_warning"].startswith("Completion check failed:")
    assert "[pipeline_complete]" not in result.display_text


def test_scenario_agent_final_block_model_error_keeps_earlier_block_evidence(scenario):
    result = scenario.run_agent([[text("## Block Output\nOne risky path.")], [fail(TimeoutError("read timed out"))]])

    assert isinstance(result.raised, TimeoutError)
    assert result.thread["status"] == "failed"
    review, final = result.thread["blocks"]
    assert (review["role"], review["status"]) == ("review", "complete")
    assert (final["role"], final["status"], final["status_code"]) == ("final", "failed", "model_error")
    assert result.thread["output"] == "## Block Output\nOne risky path."
    assert "[pipeline_complete]" not in result.display_text


def test_scenario_agent_ctrl_c_in_block_stops_without_claiming_success(scenario):
    result = scenario.run_agent([[text("## Block Output\nhalf a revi"), interrupt()]])

    assert result.raised is None
    assert not result.completed and result.pipeline.status != "complete"
    assert result.thread["blocks"][-1]["status"] == "cancelled"
    assert result.thread["blocks"][-1]["status_code"] == "interrupted"
    assert "[error] Agent pipeline cancelled." in result.display_text
    assert "[pipeline_complete]" not in result.display_text
    assert result.cancel_latency < 2.0


# Known defect found by this harness: the run journal has no event for a model round that
# ends by Ctrl+C or a provider error, so run_finished is rejected ("run finished with an
# active block") and the run's reported cause becomes a journal-finalization error.
_JOURNAL_ABORTED_ROUND = pytest.mark.xfail(
    strict=True,
    reason="run journal cannot record an aborted model round; pipeline error names the journal, not the cause",
)


@_JOURNAL_ABORTED_ROUND
def test_scenario_agent_ctrl_c_reports_cancelled_not_journal_failure(scenario):
    result = scenario.run_agent([[text("## Block Output\nhalf a revi"), interrupt()]])

    assert result.pipeline.status == "cancelled"
    assert result.thread["status"] == "cancelled"
    assert result.pipeline.error == "Agent pipeline cancelled."


@_JOURNAL_ABORTED_ROUND
def test_scenario_agent_model_error_names_the_provider_error_on_the_thread(scenario):
    result = scenario.run_agent([[fail(ConnectionError("ollama is down"))]])

    assert result.thread["status"] == "failed"
    assert "ollama is down" in result.thread["error"]
