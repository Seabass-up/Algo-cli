"""A successful repeated read must not keep a blocked turn alive indefinitely."""

import pytest

from algo_cli.session_mode import select_mode
from test_agent_progress_recovery import call, run_script


@pytest.mark.parametrize("yolo", [False, True])
def test_unchanged_reads_end_in_tool_free_partial_handoff(monkeypatch, tmp_path, yolo):
    def responses(turn):
        # The fallback bounds this fixture on the broken runtime too.
        if turn > 12:
            return {"content": "Fixture fallback: no new trace was executed."}
        return call("read_file", {"path": "trace-summary.json"}, turn)

    def configure(cfg):
        if yolo:
            select_mode(cfg, "yolo", user_initiated=True)

    code, events, client, invoked, captures = run_script(
        monkeypatch, tmp_path, responses, configure=configure,
    )
    assert code == 2
    assert len(invoked) <= 6
    assert client.calls[-1]["tools"] == []
    assert events[-1]["status"] == "partial"
    assert captures[-1]["completed"] is False
    assert any("no new evidence" in str(event).lower() for event in events)


def test_new_read_ranges_are_allowed_to_finish(monkeypatch, tmp_path):
    code, _events, client, invoked, captures = run_script(
        monkeypatch,
        tmp_path,
        lambda turn: (
            call("read_file", {"path": "trace.raw", "start_char": turn * 100}, turn)
            if turn <= 10 else {"content": "Reviewed ten different trace ranges."}
        ),
        configure=lambda cfg: select_mode(cfg, "yolo", user_initiated=True),
    )
    assert code == 0
    assert len(client.calls) == 11
    assert len(invoked) == 10
    assert captures[-1]["completed"] is True


def test_model_summary_cannot_turn_a_stall_into_success(monkeypatch, tmp_path):
    code, _events, client, invoked, captures = run_script(
        monkeypatch, tmp_path,
        lambda turn: call("read_file", {"path": "trace.json"}, turn) if turn <= 6
        else {"content": "Everything is complete."},
        configure=lambda cfg: select_mode(cfg, "yolo", user_initiated=True),
    )
    assert code == 2
    assert len(invoked) == 6
    assert len(client.calls) == 7
    assert client.calls[-1]["tools"] == []
    assert captures[-1]["completed"] is False


def test_alternative_route_after_warning_keeps_a_good_run_alive(monkeypatch, tmp_path):
    def responses(turn):
        if turn <= 4:
            return call("read_file", {"path": "same-trace.json"}, turn)
        if turn <= 16:
            return call("search_files", {"path": ".", "pattern": f"new-evidence-{turn}"}, turn)
        return {"content": "Resolved the question using a different evidence route."}

    code, _events, client, invoked, captures = run_script(
        monkeypatch, tmp_path, responses,
        configure=lambda cfg: select_mode(cfg, "yolo", user_initiated=True),
        invoke=lambda name, args, cfg: "same trace" if name == "read_file"
        else f"New matching evidence: {args['pattern']}",
    )
    assert code == 0
    assert len(invoked) == 16
    assert len(client.calls) == 17
    assert captures[-1]["completed"] is True


def test_agent_block_stops_repeated_inspection_with_partial_output(monkeypatch, tmp_path):
    from algo_cli import agent_blocks, agent_pipeline
    from algo_cli.config import Config
    from test_agent_pipeline import _quiet_display
    from test_agent_progress_recovery import ScriptedClient

    _quiet_display(monkeypatch)
    cfg = Config(cwd=str(tmp_path), model="test", algorithmic_tool_policy_enabled=False)
    block = agent_blocks.AgentBlock(
        role="research", prompt="inspect", allowed_tools=frozenset({"read_file"}), max_iterations=20,
    )
    client = ScriptedClient(
        lambda turn: call("read_file", {"path": "trace.json"}, turn) if turn <= 6
        else {"content": "## Block Output\nThe trace still needs execution."},
    )
    monkeypatch.setattr(agent_pipeline, "client_for_model", lambda _model, _cfg, active: active)
    monkeypatch.setattr(
        agent_pipeline, "execute_tool_call_for_pipeline",
        lambda name, args, cfg, **kwargs: (
            {"role": "tool", "tool_call_id": kwargs.get("tool_call_id"), "content": "same trace"}, "same trace",
        ),
    )
    agent_pipeline.run_agent_block(block, task="inspect trace", completed=[], cfg=cfg, client=client)
    assert block.status == "partial"
    assert block.status_code == "tool_progress_stalled"
    assert block.tool_calls == 6
    assert len(client.calls) == 7
    assert client.calls[-1]["tools"] == []


def test_goal_does_not_restart_a_stalled_turn_or_accept_its_completion_marker(monkeypatch):
    from algo_cli import ada_task_ledger, main
    from algo_cli.config import Config

    calls = []

    def stalled(_client, cfg, _prompt):
        calls.append(True)
        setattr(cfg, "_tool_progress_stop_reason", "No new evidence; execution unavailable.")
        cfg.messages.append({"role": "assistant", "content": main.GOAL_COMPLETE_MARKER})

    monkeypatch.setattr(main, "agent_loop", stalled)
    main.run_goal_loop(client=None, cfg=Config(), arg="capture a trace")
    record = ada_task_ledger.load_goal()
    assert len(calls) == 1
    assert record.status == ada_task_ledger.STATUS_BLOCKED
    assert "execution unavailable" in record.reason
