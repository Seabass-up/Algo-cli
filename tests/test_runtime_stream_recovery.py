"""Recovery may repeat inference, never delivered actions or stale answers."""

from pathlib import Path
import subprocess

import pytest

from algo_cli import (
    agent_blocks, agent_pipeline, execution_guardrails, git_evidence, main, model_stream, run_contract, task_router,
)
from algo_cli.config import Config
from test_agent_pipeline import ScriptedClient, _quiet_display
from test_agent_progress_recovery import call, run_script


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(model_stream.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize("first", [{}, {"thinking": "Planning"}, {"content": "  "}])
def test_chat_recovers_an_empty_provider_response(monkeypatch, tmp_path, first):
    notices = []
    monkeypatch.setattr(main, "show_info", notices.append)
    code, events, client, invoked, captures = run_script(
        monkeypatch, tmp_path,
        lambda turn: first if turn == 1 else {"content": "Observed result."},
    )
    assert code == 0
    assert len(client.calls) == 2
    assert invoked == []
    assert captures[-1]["completed"] is True
    assert any("retrying inference" in notice for notice in notices)


def test_chat_empty_response_recovery_is_bounded(monkeypatch, tmp_path):
    code, _events, client, invoked, captures = run_script(monkeypatch, tmp_path, lambda _turn: {})
    assert code == 2
    assert len(client.calls) == 3
    assert invoked == []
    assert captures[-1]["completed"] is False


def test_chat_recovery_after_a_completed_tool_does_not_replay_it(monkeypatch, tmp_path):
    def responses(turn):
        if turn == 1:
            return call("read_file", {"path": "made.py"}, turn)
        return {} if turn == 2 else {"content": "Read the file; nothing was changed."}

    code, _events, client, invoked, captures = run_script(monkeypatch, tmp_path, responses)
    assert code == 0 and len(client.calls) == 3
    assert invoked == ["read_file"]
    assert captures[-1]["completed"] is True


def test_auto_verified_chat_requests_a_fresh_visible_final_answer(monkeypatch, tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "made.py").write_text("x = 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "made.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed"],
        cwd=tmp_path, check=True, capture_output=True,
    )

    def responses(turn):
        if turn == 1:
            return call("read_file", {"path": "made.py"}, turn)
        if turn == 2:
            return call("edit_file", {"path": "made.py", "old": "x = 0", "new": "x = 1"}, turn)
        if turn in {3, 4}:
            return {"content": "Premature: all functional tests passed."}
        return {"content": "Changed the file. Only git diff --check passed; functional tests were not run."}

    def invoke(name, _args, cfg):
        if name == "read_file":
            return "x = 0\n"
        (Path(cfg.cwd) / "made.py").write_text("x = 1\n", encoding="utf-8")
        return "Edited made.py"

    code, events, client, invoked, captures = run_script(
        monkeypatch, tmp_path, responses, invoke=invoke, approve=lambda *_a, **_kw: True,
    )
    assert code == 0
    assert len(client.calls) == 5
    assert client.calls[-1]["tools"] == []
    assert invoked == ["read_file", "edit_file"]
    assert captures[-1]["completed"] is True
    text = "".join(str(event.get("text", "")) for event in events if event["type"] == "content")
    assert "functional tests were not run" in text
    assert "all functional tests passed" not in text


def test_block_does_not_reuse_progress_as_an_empty_final_answer(monkeypatch, tmp_path):
    _quiet_display(monkeypatch)
    cfg = Config(cwd=str(tmp_path), decision_checkpoints_enabled=False)
    block = agent_blocks.AgentBlock(role="review", prompt="p", allowed_tools=frozenset({"read_file"}), max_iterations=4)
    client = ScriptedClient([
        {"content": "I will inspect it.", **call("read_file", {"path": "made.py"}, 1)},
        {}, {}, {},
    ])

    def execute(_name, _args, _cfg, **_kwargs):
        execution_guardrails.record_read("made.py", success=True)
        return {"role": "tool", "content": "x = 1"}, "x = 1"

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", execute)
    main.run_agent_block(block, task="Inspect it", completed=[], cfg=cfg, client=client)
    assert block.status != "complete"
    assert block.output != "I will inspect it."


@pytest.mark.parametrize("contracted", [False, True])
def test_agent_block_hidden_retries_cannot_expand_a_signed_budget(monkeypatch, tmp_path, contracted):
    _quiet_display(monkeypatch)
    cfg = Config(cwd=str(tmp_path), model="test-model", decision_checkpoints_enabled=False)
    cfg._nathan_approval_mode = "never"
    block = agent_blocks.review_pipeline()[0]
    client = ScriptedClient([{}, {"content": "## Block Output\nNo action was performed."}])
    kwargs = {}
    if contracted:
        contract = run_contract.compile_agent_run_contract(
            task="Review a public fixture", route=task_router.route_task("Review a fixture"),
            pipeline_name="review", blocks=[block], cfg=cfg, approval_mode="never",
            snapshot=git_evidence.GitSnapshot(False, "not git", None, "", "", ()),
        )
        tracker = run_contract.RunContractTracker(contract)
        tracker.start_block(0)
        kwargs = {"run_contract": contract, "block_contract": contract.block(0), "contract_tracker": tracker}
    main.run_agent_block(block, task="Review a public fixture", completed=[], cfg=cfg, client=client, **kwargs)
    assert len(client.calls) == (1 if contracted else 2)
    assert (block.status == "complete") is (not contracted)


@pytest.fixture
def one_read_block(monkeypatch, tmp_path):
    _quiet_display(monkeypatch)
    cfg = Config(cwd=str(tmp_path), model="test-model", decision_checkpoints_enabled=False)
    cfg._nathan_approval_mode = "never"
    block = agent_blocks.AgentBlock(
        role="review", prompt="Inspect a fixture", allowed_tools=frozenset({"read_file"}), max_iterations=1,
    )

    def execute(_name, _args, _cfg, **_kwargs):
        execution_guardrails.record_read("fixture.txt", success=True)
        return {"role": "tool", "content": "public fixture"}, "public fixture"

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", execute)
    return cfg, block


@pytest.mark.parametrize("contracted", [False, True])
def test_iteration_exhaustion_cannot_add_an_untracked_signed_summary(one_read_block, contracted):
    cfg, block = one_read_block
    client = ScriptedClient([
        {"content": "Unverified progress claim.", **call("read_file", {"path": "fixture.txt"}, 1)},
        {"content": "## Block Output\nPartial fixture inspection."},
    ])
    kwargs = {}
    if contracted:
        contract = run_contract.compile_agent_run_contract(
            task="Review a public fixture", route=task_router.route_task("Review a fixture"),
            pipeline_name="review", blocks=[block], cfg=cfg, approval_mode="never",
            snapshot=git_evidence.GitSnapshot(False, "not git", None, "", "", ()),
        )
        tracker = run_contract.RunContractTracker(contract)
        tracker.start_block(0)
        kwargs = {"run_contract": contract, "block_contract": contract.block(0), "contract_tracker": tracker}
    agent_pipeline.run_agent_block(block, task="Review a public fixture", completed=[], cfg=cfg, client=client, **kwargs)
    assert block.status == "partial"
    assert block.status_code == "max_iterations"
    assert len(client.calls) == (1 if contracted else 2)
    if contracted:
        assert tracker.model_rounds == len(client.calls)
        assert "INCOMPLETE" in block.output
        assert "Unverified reported output:\nUnverified progress claim." in block.output
    else:
        assert client.calls[-1]["tools"] == []
        assert "Partial fixture inspection" in block.output
        assert "Unverified progress claim" not in block.output


@pytest.mark.parametrize("cancelled", [False, True])
def test_partial_summary_close_failure_preserves_handoff_or_cancellation(one_read_block, cancelled):
    cfg, block = one_read_block
    closed = []

    class PartialStream:
        def __iter__(self):
            if cancelled:
                raise KeyboardInterrupt
            yield {"message": {"content": "## Block Output\nPartial fixture inspection."}}

        def close(self):
            closed.append(True)
            raise RuntimeError("synthetic provider teardown failure")

    class ClosingClient(ScriptedClient):
        def chat(self, **kwargs):
            if not self.calls:
                return super().chat(**kwargs)
            self.calls.append(kwargs)
            return PartialStream()

    client = ClosingClient([call("read_file", {"path": "fixture.txt"}, 1)])
    if cancelled:
        with pytest.raises(KeyboardInterrupt):
            agent_pipeline.run_agent_block(block, task="Inspect", completed=[], cfg=cfg, client=client)
        assert block.status == "cancelled"
    else:
        agent_pipeline.run_agent_block(block, task="Inspect", completed=[], cfg=cfg, client=client)
        assert block.status == "partial"
        assert block.status_code == "max_iterations"
        assert "Partial fixture inspection" in block.output
    assert closed == [True]
    assert len(client.calls) == 2
