"""Actionable denials, the repeated-denial skip, and email discovery across a whole turn."""

from __future__ import annotations

from scenarios.scenario_harness import text, tool


def test_scenario_outside_workspace_denial_names_a_recovery_step(scenario):
    result = scenario.run([[tool("read_file", path="../../outside/secret.txt")], [text("I cannot read that.")]])

    explanation = result.denials[0]["summary"]
    assert "/cd" in explanation or "/mode yolo" in explanation


def test_scenario_identical_denied_call_is_skipped_without_second_prompt(scenario):
    result = scenario.run(
        [
            [tool("run_shell", command="pytest -q")],
            [tool("run_shell", command="pytest -q")],
            [text("Tests were not run.")],
        ],
        driver="interactive",
    )

    assert result.approvals == [("run_shell", {"command": "pytest -q"})]
    assert [event["status"] for event in result.tool_results] == ["denied", "skipped"]
    assert result.history_well_formed


def test_scenario_check_my_email_discovers_gmail(scenario):
    result = scenario.run(
        [[tool("action_search", query="check my email")], [text("Gmail is available through /google.")]],
        prompt="check my email",
        real_tools={"action_search"},
    )

    tool_message = next(message for message in result.messages if message.get("role") == "tool")
    assert "gmail" in tool_message["content"].lower()
