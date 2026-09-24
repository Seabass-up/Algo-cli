"""Tool outcomes, repeats and denials as the user and the model see them across a turn."""

from __future__ import annotations

from scenarios.scenario_harness import text, tool


def test_scenario_read_then_answer_completes_cleanly(scenario):
    result = scenario.run(
        [[tool("read_file", path="README.md")], [text("The README describes a demo.")]],
        tools={"read_file": "# Demo"},
    )

    assert result.completed and result.exit_code == 0
    assert result.final_answer == "The README describes a demo."
    assert [name for name, _args in result.invocations] == ["read_file"]
    assert result.wasted_calls == 0 and result.recovery_turns == 0
    assert result.history_well_formed
    assert result.done["task_verification"] == "not_evaluated"


def test_scenario_error_looking_file_body_is_a_successful_read(scenario):
    (scenario.workspace / "notes.txt").write_text(
        "Error: this first line is ordinary file content\nUnknown outcome: so is this one\n", encoding="utf-8"
    )
    result = scenario.run(
        [[tool("read_file", path="notes.txt")], [text("The notes mention an error string.")]],
        real_tools={"read_file"},
    )

    assert result.status_by_call == {"call-1": "ok"}
    assert result.completed
    tool_message = next(message for message in result.messages if message.get("role") == "tool")
    assert "ordinary file content" in tool_message["content"]


def test_scenario_real_read_error_is_reported_failed(scenario):
    result = scenario.run(
        [[tool("read_file", path="missing.txt")], [text("That file does not exist.")]],
        real_tools={"read_file"},
    )

    assert result.status_by_call == {"call-1": "failed"}
    assert result.tool_results[0]["summary"].startswith("Error: file not found")
    # The model turn finished, but the run never claims the task was verified.
    assert result.completed and result.done["task_verification"] == "not_evaluated"


def test_scenario_tool_exception_is_failed_not_success(scenario):
    result = scenario.run(
        [[tool("read_file", path="README.md")], [text("The read failed.")]],
        tools={"read_file": RuntimeError("disk vanished")},
    )

    assert result.status_by_call == {"call-1": "failed"}
    assert result.tool_results[0]["summary"] == "Tool error for read_file: RuntimeError"
    assert result.history_well_formed


def test_scenario_repeated_failed_call_is_skipped_not_reinvoked(scenario):
    result = scenario.run(
        [
            [tool("read_file", path="missing.txt")],
            [tool("read_file", path="missing.txt")],
            [text("The file is missing.")],
        ],
        real_tools={"read_file"},
    )

    assert [event["status"] for event in result.tool_results] == ["failed", "skipped"]
    assert len(result.invocations) == 1
    assert result.skips[0]["summary"].startswith("Skipped repeated failed attempt.")
    assert result.wasted_calls == 1 and result.recovery_turns == 2
    assert result.history_well_formed and result.completed


def test_scenario_repeated_identical_successful_read_is_counted_as_waste(scenario):
    # Current behavior: a successful observation may be repeated; the harness must still count it.
    result = scenario.run(
        [[tool("read_file", path="README.md")], [tool("read_file", path="README.md")], [text("Same file.")]],
        tools={"read_file": "# Demo"},
    )

    assert [event["status"] for event in result.tool_results] == ["ok", "ok"]
    assert len(result.invocations) == 2
    assert [request.call_id for request in result.identical_repeats] == ["call-2"]
    assert result.wasted_calls == 1 and result.recovery_turns == 0


def test_scenario_read_outside_workspace_is_denied_with_explanation(scenario):
    result = scenario.run([[tool("read_file", path="../../outside/secret.txt")], [text("I cannot read that file.")]])

    assert result.invocations == [] and result.approvals == []
    assert result.status_by_call == {"call-1": "denied"}
    explanation = result.denials[0]["summary"]
    assert explanation.startswith("Blocked by runtime authority:") and len(explanation) > 40
    tool_message = next(message for message in result.messages if message.get("role") == "tool")
    assert tool_message["content"] == explanation  # the model sees the same explanation
    assert result.history_well_formed and result.completed


def test_scenario_denied_shell_in_noninteractive_run_tells_the_model_why(scenario):
    result = scenario.run([[tool("run_shell", command="pytest -q")], [text("Tests need approval.")]])

    assert result.invocations == []
    assert result.status_by_call == {"call-1": "denied"}
    assert "Approval is unavailable in this noninteractive run" in result.denials[0]["summary"]
    assert result.completed and result.history_well_formed


def test_scenario_oneshot_emits_done_even_when_config_save_refuses(scenario):
    result = scenario.run(
        [[text("All set.")]],
        save_error=RuntimeError("Memory configuration requires repair"),
    )

    assert result.events[0]["type"] == "session_start"
    assert result.events[-1]["type"] == "done"
    assert result.done["status"] == "partial"
    assert any(event["type"] == "error" and "requires repair" in event["message"] for event in result.events)
    assert result.saves >= 1 and result.exit_code == 2
    assert result.final_answer == "All set."


def test_scenario_interactive_safe_mode_blocks_destructive_shell_without_prompting(scenario):
    result = scenario.run(
        [[tool("run_shell", command="rm -rf build")], [text("I did not delete anything.")]],
        driver="interactive",
    )

    assert result.invocations == [] and result.approvals == []
    assert result.status_by_call == {"call-1": "denied"}
    assert "safe mode blocks destructive commands" in result.denials[0]["summary"]
    assert "[tool_result] run_shell denied: Blocked by runtime authority" in result.display_text
    assert result.completed and result.history_well_formed


def test_scenario_interactive_user_declines_shell_and_model_finishes(scenario):
    result = scenario.run(
        [[tool("run_shell", command="pytest -q")], [text("Skipped the test run as requested.")]],
        driver="interactive",
    )

    assert result.approvals == [("run_shell", {"command": "pytest -q"})]
    assert result.invocations == []
    assert result.status_by_call == {"call-1": "denied"}
    assert result.denials[0]["summary"] == "This operation was not approved and was not executed."
    assert result.completed and result.final_answer == "Skipped the test run as requested."
    assert result.history_well_formed
