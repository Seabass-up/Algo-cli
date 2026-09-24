"""Ctrl+C across whole turns: partial answers kept, history paired, bounded waits."""

from __future__ import annotations

import threading

import pytest

from algo_cli import main
from scenarios.scenario_harness import interrupt, text, thinking, tool

# Generous for slow CI hosts; the runtime's own grace is patched far lower below.
CANCEL_BUDGET_SECONDS = 2.0


@pytest.mark.parametrize("driver", ["oneshot", "interactive"])
def test_scenario_ctrl_c_at_stream_start_keeps_no_phantom_answer(scenario, driver):
    result = scenario.run([[interrupt()]], driver=driver)

    assert not result.completed
    assert result.final_answer == ""
    assert all(message.get("role") != "assistant" for message in result.messages)
    assert result.history_well_formed
    assert result.invocations == []
    assert result.cancel_latency is not None and result.cancel_latency < CANCEL_BUDGET_SECONDS
    if driver == "oneshot":
        assert result.done["status"] == "failed" and result.done["status_reason"] == "interrupted"
        assert result.exit_code == 2
    else:
        assert isinstance(result.raised, KeyboardInterrupt)
        assert "No response text was received" in result.display_text


@pytest.mark.parametrize("driver", ["oneshot", "interactive"])
def test_scenario_ctrl_c_mid_stream_keeps_partial_answer(scenario, driver):
    result = scenario.run(
        [[thinking("Looking at the layout first."), text("The project has two packages"), interrupt()]],
        driver=driver,
    )

    assert not result.completed
    assert result.final_answer == "The project has two packages" + main.GENERATION_INTERRUPTED_MARKER
    assert result.messages[-2]["role"] == "user"
    assert result.history_well_formed
    assert result.model.streams[-1].closed
    assert result.cancel_latency < CANCEL_BUDGET_SECONDS
    if driver == "oneshot":
        assert [event["text"] for event in result.events if event["type"] == "content"] == [
            "The project has two packages"
        ]
        assert result.done["status_reason"] == "interrupted"
    else:
        assert "[text] The project has two packages" in result.display_text
        assert "Partial response kept in the conversation" in result.display_text


def test_scenario_ctrl_c_after_tool_round_keeps_paired_history(scenario):
    result = scenario.run(
        [
            [text("Checking the README."), tool("read_file", path="README.md")],
            [text("The README says"), interrupt()],
        ],
        tools={"read_file": "# Demo project"},
    )

    assert not result.completed
    assert [request.name for request in result.tool_calls] == ["read_file"]
    assert result.status_by_call == {"call-1": "ok"}
    assert result.history_well_formed
    assert result.final_answer.startswith("The README says")
    assert result.model_calls == 2


def test_scenario_ctrl_c_during_parallel_tools_bounds_the_wait(scenario, monkeypatch):
    hung_entered = threading.Event()
    fast_done = threading.Event()
    release = threading.Event()

    def read(args):
        if args["path"] == "hung.txt":
            hung_entered.set()
            release.wait(timeout=30)
            return "late observation"
        fast_done.set()
        return "fast observation"

    def interrupted_wait(_futures):
        assert hung_entered.wait(timeout=5) and fast_done.wait(timeout=5)
        scenario.clock.mark_interrupt()
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "as_completed", interrupted_wait)
    monkeypatch.setattr(main, "PARALLEL_INTERRUPT_GRACE_SECONDS", 0.3)
    try:
        result = scenario.run(
            [[tool("read_file", id="fast", path="fast.txt"), tool("read_file", id="hung", path="hung.txt")]],
            tools={"read_file": read},
        )
    finally:
        release.set()

    assert not result.completed and result.done["status_reason"] == "interrupted"
    assert result.model_calls == 1
    assert result.status_by_call == {"fast": "ok", "hung": "cancelled"}
    assert result.history_well_formed
    tool_messages = [message for message in result.messages if message.get("role") == "tool"]
    assert "fast observation" in tool_messages[0]["content"]
    assert tool_messages[1]["content"] == main.PARALLEL_INTERRUPTED_RESULT
    assert result.cancel_latency < CANCEL_BUDGET_SECONDS
