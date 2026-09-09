"""Successful discovery and new writes cannot indefinitely extend verification."""

from pathlib import Path

import pytest

from algo_cli import agent_blocks, agent_pipeline, execution_guardrails, main, nathan_runtime
from algo_cli.config import Config
from test_agent_pipeline import ScriptedClient, _quiet_display
from test_agent_progress_recovery import call, run_script


def _invoke(name, args, cfg):
    if name == "write_file":
        (Path(cfg.cwd) / args["path"]).write_text(args["content"], encoding="utf-8")
        return "Wrote 6 characters to file"
    if name == "run_shell":
        return "1 passed\n[exit code: 0]"
    return "Read or discovery result, not a verifier"


@pytest.mark.parametrize("recovery_action", ["action_search", "read_file", "write_file"])
def test_chat_bounds_post_nudge_recovery_without_discarding_writes(monkeypatch, tmp_path, recovery_action):
    def responses(turn):
        if turn == 2:
            return {"content": "Premature completion claim."}
        if turn == 1 or recovery_action == "write_file":
            return call("write_file", {"path": f"made-{turn}.py", "content": "x = 1\n"}, turn)
        if recovery_action == "read_file":
            return call("read_file", {"path": "made-1.py"}, turn)
        return call("action_search", {"query": f"verification option {turn}"}, turn)

    code, events, client, invoked, captures = run_script(
        monkeypatch, tmp_path, responses, invoke=_invoke, approve=lambda *_args, **_kwargs: True
    )
    assert code == 2 and events[-1]["status"] == "partial"
    assert len(client.calls) == 6
    assert len(invoked) == 5
    assert captures[-1]["completed"] is False
    assert (tmp_path / "made-1.py").read_text(encoding="utf-8") == "x = 1\n"
    assert any("verification recovery" in str(event).lower() for event in events if event["type"] == "error")


@pytest.mark.parametrize("extra_tool", [False, True])
def test_chat_final_recovery_verifier_gets_only_a_tool_free_answer(monkeypatch, tmp_path, extra_tool):
    def responses(turn):
        if turn == 1 or (turn == 7 and extra_tool):
            return call("write_file", {"path": f"made-{turn}.py", "content": "x = 1\n"}, turn)
        if turn == 2:
            return {"content": "Premature completion claim."}
        if turn == 6:
            return call("run_shell", {"command": "pytest -q"}, turn)
        if turn == 7:
            return {"content": "The test passed."}
        return call("action_search", {"query": f"verification option {turn}"}, turn)

    code, events, client, invoked, captures = run_script(
        monkeypatch, tmp_path, responses, invoke=_invoke, approve=lambda *_args, **_kwargs: True
    )
    assert code == (2 if extra_tool else 0)
    assert len(client.calls) == 7
    assert client.calls[-1]["tools"] == []
    assert invoked[-1] == "run_shell"
    assert captures[-1]["completed"] is (not extra_tool)
    assert not (tmp_path / "made-7.py").exists()


@pytest.mark.parametrize("max_iterations", [1, 4])
def test_recovery_does_not_enlarge_the_configured_work_budget(monkeypatch, tmp_path, max_iterations):
    def responses(turn):
        if turn == 1:
            return call("write_file", {"path": "made.py", "content": "x = 1\n"}, turn)
        if turn == 2:
            return {"content": "Premature claim."}
        return call("action_search", {"query": "verification"}, turn)

    code, _events, client, _invoked, captures = run_script(
        monkeypatch,
        tmp_path,
        responses,
        max_iterations=max_iterations,
        invoke=_invoke,
        approve=lambda *_args, **_kwargs: True,
    )
    assert code == 2 and len(client.calls) == max_iterations
    assert captures[-1]["completed"] is False


@pytest.mark.parametrize("enabled,protection", [(True, "required"), (True, "optional"), (False, "required")])
def test_protected_completion_prompt_does_not_request_shell_approval(enabled, protection):
    prompt = nathan_runtime.completion_recovery_prompt(
        Config(echo_veil_enabled=enabled, echo_veil_protection=protection)
    )
    assert "Echo Veil" in prompt and "run_shell is unavailable" in prompt
    assert "Do not seek shell approval" in prompt
    assert "git_diff" in prompt and "tracked" in prompt
    assert "read_file" in prompt and "not" in prompt
    assert "Python -c" not in prompt


@pytest.mark.parametrize("verifier_turn,extra_tool", [(None, False), (6, False), (6, True)])
def test_agent_block_bounds_the_same_recovery_window(monkeypatch, tmp_path, verifier_turn, extra_tool):
    _quiet_display(monkeypatch)
    cfg = Config(cwd=str(tmp_path))
    block = agent_blocks.AgentBlock(
        role="implement", prompt="p", allowed_tools=agent_blocks.IMPLEMENT_TOOLS, max_iterations=20
    )
    responses = [
        call("write_file", {"path": "made.py", "content": "x = 1\n"}, 1),
        {"content": "## Block Output\nPremature completion claim."},
    ]
    for turn in range(3, 21):
        if turn == verifier_turn:
            responses.append(call("git_diff", {}, turn))
        elif turn == 7 and verifier_turn and not extra_tool:
            responses.append({"content": "## Block Output\nVerified."})
        else:
            responses.append(call("read_file", {"path": "made.py"}, turn))
    client = ScriptedClient(responses)

    def execute(name, _args, _cfg, **_kwargs):
        if name == "write_file":
            execution_guardrails.record_mutation("made.py", success=True, operation="write_file")
            result = "Wrote 6 characters to made.py"
        elif name == "git_diff":
            execution_guardrails.record_verification("git_diff", success=True)
            result = "+x = 1"
        else:
            execution_guardrails.record_read("made.py", success=True)
            result = "x = 1"
        return {"role": "tool", "content": result}, result

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", execute)
    main.run_agent_block(block, task="Make and verify it", completed=[], cfg=cfg, client=client)
    assert len(client.calls) == (7 if verifier_turn else 6)
    assert block.status == ("complete" if verifier_turn and not extra_tool else "partial")
    assert "Premature completion claim" not in block.output
    if verifier_turn:
        assert client.calls[-1]["tools"] == []
    else:
        assert block.status_code == "verification_missing"
        assert "verification recovery" in block.status_reason.lower()
