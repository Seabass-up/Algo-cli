"""Recovery stops must remain distinct from successful model-turn completion."""

import io
import json

import pytest

from algo_cli import config as config_module, display, main, oliver_oneshot
from test_main_helpers import _patch_agent_loop_for_tool_policy_test


class ScriptedClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return iter([{"message": self.responses(len(self.calls))}])


def call(name, args, turn):
    return {"tool_calls": [{"id": f"call-{turn}", "function": {"name": name, "arguments": args}}]}


def run_script(monkeypatch, tmp_path, responses, *, max_iterations=25, approve=None, invoke=None, safe_mode=True):
    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    cfg = config_module.Config(
        cwd=str(tmp_path),
        safe_mode=safe_mode,
        model="test",
        max_tool_iterations=max_iterations,
        skill_crystallize_enabled=False,
        code_rag_enabled=False,
    )
    monkeypatch.setattr(config_module.Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main, "json_sink", display.json_sink)
    monkeypatch.setattr(main, "show_tool_call", display.show_tool_call)
    monkeypatch.setattr(main, "start_streaming_response", lambda: None)
    monkeypatch.setattr(main, "show_stream_text", display.show_stream_text)
    monkeypatch.setattr(main, "record_perf_event", lambda *_args, **_kwargs: None)
    invocations, captures = [], []

    def invoke_tool(name, args, cfg):
        invocations.append(name)
        return invoke(name, args, cfg) if invoke is not None else "README contents"

    monkeypatch.setattr(main, "run_tool", invoke_tool)
    monkeypatch.setattr(
        main.memory_runtime,
        "capture_completed_user_turn",
        lambda *_args, **kwargs: captures.append(kwargs) or {"status": "skipped"},
    )
    if approve is not None:
        monkeypatch.setattr(main, "ask_approval", approve)
    client = ScriptedClient(responses)
    monkeypatch.setattr(main, "create_client", lambda _cfg: client)
    stream = io.StringIO()
    code = oliver_oneshot.run_oneshot(prompt="inspect this project", approval_mode="auto", stream=stream)
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    return code, events, client, invocations, captures


@pytest.mark.parametrize("max_iterations,expected_rounds", [(1, 1), (25, 3)])
def test_denied_only_loop_stops_without_claiming_completion(monkeypatch, tmp_path, max_iterations, expected_rounds):
    result = run_script(
        monkeypatch,
        tmp_path,
        lambda turn: call("run_shell", {"command": f"python -m pytest -q --maxfail={turn}"}, turn),
        max_iterations=max_iterations,
    )
    code, events, client, invoked, captures = result
    assert code == 2
    assert len(client.calls) == expected_rounds
    assert invoked == []
    assert events[-1]["status"] == "partial"
    assert any(event["type"] == "error" for event in events)
    assert captures[-1]["completed"] is False
    assert all("current result is verified" not in str(request["messages"]) for request in client.calls)


def test_allowed_alternative_resets_denied_round_budget(monkeypatch, tmp_path):
    def responses(turn):
        if turn == 3:
            return call("read_file", {"path": "README.md"}, turn)
        if turn == 6:
            return {"content": "Inspection finished; shell execution still requires approval."}
        return call("run_shell", {"command": f"python -m pytest --maxfail={turn}"}, turn)

    code, events, client, invoked, captures = run_script(monkeypatch, tmp_path, responses)
    assert code == 0
    assert len(client.calls) == 6
    assert invoked == ["read_file"]
    assert captures[-1]["completed"] is True
    assert events[-1]["completion_scope"] == "model_turn"
    assert events[-1]["task_verification"] == "not_evaluated"


@pytest.mark.parametrize("message", [{}, {"content": "   "}, {"thinking": "Only reasoning"}])
def test_empty_final_answer_is_not_a_completed_turn(monkeypatch, tmp_path, message):
    code, events, client, invoked, captures = run_script(monkeypatch, tmp_path, lambda _turn: message)
    assert code == 2
    assert len(client.calls) == 1
    assert invoked == []
    assert events[-1]["status"] == "partial"
    assert captures[-1]["completed"] is False


def test_ordinary_read_finalization_does_not_invent_verification(monkeypatch, tmp_path):
    code, events, client, invoked, _captures = run_script(
        monkeypatch,
        tmp_path,
        lambda turn: call("read_file", {"path": "README.md"}, turn) if turn == 1 else {"content": "Observed README."},
        max_iterations=1,
    )
    assert code == 0
    assert invoked == ["read_file"]
    assert client.calls[-1]["tools"] == []
    assert "current result is verified" not in str(client.calls[-1]["messages"])
    assert events[-1]["task_verification"] == "not_evaluated"
    from algo_cli.context_budget import estimate_message_tokens

    final_round = [event for event in events if event["type"] == "model_round"][-1]
    assert final_round["phase"] == "finalization"
    assert final_round["context_accounting_version"] == 2
    assert final_round["context_sources"]["tool_schemas"] == 0
    assert final_round["context_sources"]["verification_receipts"] == 0
    assert final_round["context_sources"]["tool_results"] > 0
    assert sum(final_round["context_sources"].values()) == sum(
        estimate_message_tokens(message) for message in client.calls[-1]["messages"]
    )
