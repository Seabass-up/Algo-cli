"""Self-checks for the scenario harness: its metrics must not hide the defects it exists to find."""

from __future__ import annotations

import os
from pathlib import Path

from scenarios.scenario_harness import (
    REAL_ALGO_DIR,
    InterruptClock,
    ScenarioResult,
    ScriptedModel,
    format_metrics_table,
    interrupt,
    text,
    thinking,
    tool,
)


def _result(messages, events=(), marks=()):
    model = ScriptedModel([], clock=InterruptClock())
    model.event_marks = list(marks)
    model.calls = [{} for _ in marks]
    return ScenarioResult(
        name="synthetic",
        driver="oneshot",
        exit_code=0,
        events=list(events),
        display_text="",
        messages=list(messages),
        model=model,
        invocations=[],
        raised=None,
        elapsed=0.0,
        cancel_latency=None,
    )


def _call(call_id, name="read_file", **args):
    return {"id": call_id, "function": {"name": name, "arguments": args}}


def _assistant(*calls):
    return {"role": "assistant", "tool_calls": list(calls)}


def _tool(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _outcome(call_id, status, name="read_file"):
    return {"type": "tool_result", "call_id": call_id, "name": name, "status": status}


def test_scenario_harness_detects_tool_call_without_result():
    result = _result(
        [
            {"role": "user", "content": "go"},
            _assistant(_call("a", path="x"), _call("b", path="y")),
            _tool("a"),
            {"role": "assistant", "content": "done"},
        ]
    )

    assert result.unpaired_tool_calls == ["b"]
    assert not result.history_well_formed


def test_scenario_harness_counts_identical_repeats_and_denied_retries_once_each():
    messages = [
        _assistant(_call("1", path="a")),
        _tool("1"),
        _assistant(_call("2", path="a")),  # identical repeat
        _tool("2"),
        _assistant(_call("3", "run_shell", command="pytest")),
        _tool("3"),
        _assistant(_call("4", "run_shell", command="pytest -q")),  # spelling variant after a denial
        _tool("4"),
        _assistant(_call("5", "run_shell", command="pytest")),  # identical and after a denial
        _tool("5"),
    ]
    events = [_outcome("1", "ok"), _outcome("2", "ok"), _outcome("3", "denied", "run_shell"),
              _outcome("4", "denied", "run_shell"), _outcome("5", "skipped", "run_shell")]
    result = _result(messages, events)

    assert [r.call_id for r in result.identical_repeats] == ["2", "5"]
    assert [r.call_id for r in result.denied_retries] == ["4", "5"]
    assert result.wasted_calls == 3
    assert len(result.denials) == 2 and len(result.skips) == 1


def test_scenario_harness_recovery_turns_count_rounds_after_first_bad_outcome():
    events = [{"type": "session_start"}, _outcome("1", "ok"), _outcome("2", "failed"), {"type": "done"}]
    result = _result([], events, marks=[1, 2, 3, 3])

    assert result.recovery_turns == 2


def test_scenario_scripted_stream_groups_tool_calls_and_marks_interrupts():
    clock = InterruptClock()
    model = ScriptedModel([[thinking("t"), text("a"), tool("read_file", path="x"), tool("list_directory", path=".")]],
                          clock=clock)
    chunks = list(model.chat(messages=[]))

    assert [list(chunk["message"]) for chunk in chunks] == [["thinking"], ["content"], ["tool_calls"]]
    assert [call["id"] for call in chunks[-1]["message"]["tool_calls"]] == ["call-1", "call-2"]

    interrupted = ScriptedModel([[text("partial"), interrupt()]], clock=clock).chat(messages=[])
    iterator = iter(interrupted)
    next(iterator)
    try:
        next(iterator)
    except KeyboardInterrupt:
        pass
    assert clock.interrupted_at is not None


def test_scenario_metrics_table_reports_totals():
    rows = [
        {"scenario": "a", "completed": True, "model_calls": 2, "tool_calls": 1, "denied": 0, "skipped": 0,
         "wasted": 1, "recovery_turns": 0, "history_ok": True, "cancel_ms": None},
        {"scenario": "b", "completed": False, "model_calls": 1, "tool_calls": 0, "denied": 1, "skipped": 0,
         "wasted": 0, "recovery_turns": 2, "history_ok": True, "cancel_ms": 3.5},
    ]
    table = format_metrics_table(rows)

    assert table.splitlines()[0].startswith("scenario")
    assert "totals: 2 runs, 1 tasks completed, 1 wasted calls, 2 recovery turns" in table


def test_scenario_runs_are_isolated_from_the_real_home(scenario):
    from algo_cli import config as config_module

    result = scenario.run([[text("hello")]])

    assert result.completed
    assert Path(os.environ["HOME"]).resolve() != REAL_ALGO_DIR.parent
    assert not Path(config_module.CONFIG_DIR).resolve().is_relative_to(REAL_ALGO_DIR)
    assert result.saves >= 1  # recorded, never written to disk by the harness
