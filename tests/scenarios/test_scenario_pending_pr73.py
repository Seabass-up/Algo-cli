"""Behaviors PR #73 changes (actionable denials, repeated-denial skip, email discovery).

Each scenario states the post-#73 behavior and is xfail(strict=False) until that PR lands, so
it flips to XPASS without breaking the suite; then remove the marker. Scenarios for today's
behavior avoid pinning anything #73 changes, so they keep passing across the merge.
"""

from __future__ import annotations

import pytest

from scenarios.scenario_harness import text, tool

PR73 = "PR #73 (not yet merged): actionable denials, repeated-denial skip, email discovery"


@pytest.mark.xfail(strict=False, reason=PR73)
def test_scenario_outside_workspace_denial_names_a_recovery_step(scenario):
    result = scenario.run([[tool("read_file", path="../../outside/secret.txt")], [text("I cannot read that.")]])

    explanation = result.denials[0]["summary"]
    assert "/cd" in explanation or "/mode yolo" in explanation


@pytest.mark.xfail(strict=False, reason=PR73)
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


@pytest.mark.xfail(strict=False, reason=PR73)
def test_scenario_check_my_email_discovers_gmail(scenario):
    result = scenario.run(
        [[tool("action_search", query="check my email")], [text("Gmail is available through /google.")]],
        prompt="check my email",
        real_tools={"action_search"},
    )

    tool_message = next(message for message in result.messages if message.get("role") == "tool")
    assert "gmail" in tool_message["content"].lower()
