from __future__ import annotations

import threading
from typing import Any

import pytest

from algo_cli import (
    agent_blocks,
    agent_pipeline,
    agent_threads,
    execution_guardrails,
    git_evidence,
    harness,
    main,
    tool_runtime,
)
from algo_cli.config import Config


class FakeClient:
    def __init__(self, contents: list[str] | None = None, tool_call: dict[str, Any] | None = None):
        self.contents = contents or []
        self.tool_call = tool_call
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.tool_call is not None:
            return iter([{"message": {"tool_calls": [self.tool_call]}}])
        idx = len(self.calls) - 1
        content = self.contents[idx] if idx < len(self.contents) else "## Block Output\nfallback"
        return iter([{"message": {"content": content}}])


class ScriptedClient:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses[len(self.calls) - 1]
        return iter([{"message": response}])


def _quiet_display(monkeypatch):
    def noop(*_args, **_kwargs):
        return None
    for name in (
        "show_agent_block_start",
        "show_agent_block_complete",
        "show_agent_recovery_start",
        "show_agent_pipeline_complete",
        "show_error",
        "show_info",
        "finish_thinking_block",
        "show_recalled_context",
        "record_chat_metrics",
        "flush_perf_records",
    ):
        monkeypatch.setattr(agent_pipeline, name, noop)
    monkeypatch.setattr(tool_runtime, "show_tool_call", noop)
    monkeypatch.setattr(tool_runtime, "show_tool_result", noop)


def test_run_agent_pipeline_threads_compacted_context(monkeypatch):
    cfg = Config()
    client = FakeClient(
        [
            "## Block Output\nplan output",
            "## Block Output\nresearch output",
            "## Block Output\nimplement output",
            "## Block Output\nreview output",
            "## Block Output\nfinal output",
        ]
    )
    _quiet_display(monkeypatch)
    monkeypatch.setattr(main.agent_blocks, "compact_block_output", lambda output: f"COMPACT::{output}")
    monkeypatch.setattr(agent_pipeline, "should_recover_implementation", lambda _block: False)

    main.run_agent_pipeline("build it", cfg, client)

    assert len(client.calls) == 5
    assert "## Original Task\nbuild it" in client.calls[0]["messages"][1]["content"]
    assert "COMPACT::## Block Output\nplan output" in client.calls[1]["messages"][1]["content"]
    assert "COMPACT::## Block Output\nreview output" in client.calls[4]["messages"][1]["content"]


def test_run_agent_pipeline_records_resumable_thread(monkeypatch):
    cfg = Config()
    client = FakeClient(["## Block Output\nreview", "## Block Output\nfinal"])
    _quiet_display(monkeypatch)

    result = main.run_agent_pipeline("Review the runtime", cfg, client, pipeline_name="review")
    record = agent_threads.resolve_thread(result.thread_id)

    assert result.status == "complete"
    assert record["status"] == "complete"
    assert record["pipeline"] == "review"
    assert [block["role"] for block in record["blocks"]] == ["review", "final"]
    assert record["turns"][-1]["output"] == "## Block Output\nfinal"


def test_run_agent_block_falls_back_to_active_model_when_block_client_falls_back(monkeypatch):
    cfg = Config()
    cfg.model = "qwen3:latest"
    client = FakeClient(contents=["## Block Output\ncomplete"])
    block = agent_blocks.AgentBlock(role="plan", prompt="p", allowed_tools=agent_blocks.NO_TOOLS, model="grok-4")
    _quiet_display(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "client_for_model", lambda _model, _cfg, active_client: active_client)

    main.run_agent_block(block, task="plan it", completed=[], cfg=cfg, client=client)

    assert client.calls[0]["model"] == "qwen3:latest"


def test_run_agent_block_injects_inference_harness_contract_for_eosd_task(monkeypatch):
    cfg = Config()
    client = FakeClient(contents=["## Block Output\ncomplete"])
    block = agent_blocks.AgentBlock(role="plan", prompt="p", allowed_tools=agent_blocks.NO_TOOLS)
    _quiet_display(monkeypatch)

    main.run_agent_block(block, task="Add EOSD into the agent loop", completed=[], cfg=cfg, client=client)

    system_prompt = client.calls[0]["messages"][0]["content"]
    assert "## Inference Harness Integration Contract" in system_prompt
    assert "EOSD: decode loop before each speculative draft round" in system_prompt
    assert "Do not claim these algorithms can be implemented through hosted Anthropic/OpenAI API calls alone." in system_prompt


def test_run_agent_block_skips_inference_harness_contract_for_ordinary_task(monkeypatch):
    cfg = Config()
    client = FakeClient(contents=["## Block Output\ncomplete"])
    block = agent_blocks.AgentBlock(role="plan", prompt="p", allowed_tools=agent_blocks.NO_TOOLS)
    _quiet_display(monkeypatch)

    main.run_agent_block(block, task="Update the README heading", completed=[], cfg=cfg, client=client)

    system_prompt = client.calls[0]["messages"][0]["content"]
    assert "## Inference Harness Integration Contract" not in system_prompt


@pytest.mark.parametrize("enabled", [False, True])
def test_run_agent_block_respects_mercury_external_opt_in(monkeypatch, tmp_path, enabled):
    sentinel = "PRIVATE-MERCURY-AGENT-SENTINEL"
    stop_conditions = tmp_path / "stop-conditions.md"
    stop_conditions.write_text(sentinel, encoding="utf-8")
    reads: list[object] = []

    def tracked_read(path, max_chars):
        reads.append(path)
        return sentinel[:max_chars]

    monkeypatch.setattr(harness, "MERCURY_STOP_CONDITIONS_PATH", stop_conditions)
    monkeypatch.setattr(harness, "read_text", tracked_read)
    cfg = Config(
        external_harness_sources_enabled=enabled,
        session_mode="publish",
    )
    client = FakeClient(contents=["## Block Output\ncomplete"])
    block = agent_blocks.AgentBlock(
        role="plan",
        prompt="p",
        allowed_tools=agent_blocks.NO_TOOLS,
    )
    _quiet_display(monkeypatch)

    main.run_agent_block(
        block,
        task="Prepare the external release announcement",
        completed=[],
        cfg=cfg,
        client=client,
    )

    system_prompt = client.calls[0]["messages"][0]["content"]
    assert (sentinel in system_prompt) is enabled
    assert bool(reads) is enabled
    if not enabled:
        assert harness.MERCURY_STOP_CONDITIONS_COMPACT in system_prompt



def test_run_agent_block_rejects_disallowed_tool(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(role="plan", prompt="p", allowed_tools=agent_blocks.NO_TOOLS)
    client = FakeClient(tool_call={"function": {"name": "write_file", "arguments": {"path": "x", "content": "y"}}})
    _quiet_display(monkeypatch)

    main.run_agent_block(block, task="build it", completed=[], cfg=cfg, client=client)

    assert block.status == "failed"
    assert block.status_reason == "Tool policy violation: write_file is not allowed in the plan block."
    assert "Tool not allowed" in block.output


def test_run_agent_block_displays_policy_without_enforcing_it(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(role="implement", prompt="p", allowed_tools=agent_blocks.IMPLEMENT_TOOLS)
    client = FakeClient(contents=["## Block Output\ncomplete"])
    displayed: dict[str, str] = {}
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "show_agent_block_start",
        lambda _role, _model, _count, policy_summary="", **_kwargs: displayed.update(summary=policy_summary),
    )

    main.run_agent_block(block, task="Fix credential handling", completed=[], cfg=cfg, client=client)

    submitted_names = {tool.__name__ for tool in client.calls[0]["tools"]}
    assert "write_file" in submitted_names
    assert "write_file" in displayed["summary"]
    assert "denied" in displayed["summary"]


def test_run_agent_block_enforces_policy_tool_intersection(monkeypatch):
    cfg = Config()
    cfg.algorithmic_tool_policy_enabled = True
    block = agent_blocks.AgentBlock(role="implement", prompt="p", allowed_tools=agent_blocks.IMPLEMENT_TOOLS)
    client = FakeClient(contents=["## Block Output\ncomplete"])
    _quiet_display(monkeypatch)

    main.run_agent_block(block, task="Fix credential handling", completed=[], cfg=cfg, client=client)

    submitted_names = {tool.__name__ for tool in client.calls[0]["tools"]}
    assert "write_file" not in submitted_names
    assert "run_shell" not in submitted_names
    assert "read_file" in submitted_names


def test_run_agent_block_prompt_does_not_disclose_absolute_workspace(monkeypatch, tmp_path):
    private_workspace = tmp_path / "private-project-name"
    private_workspace.mkdir()
    cfg = Config(cwd=str(private_workspace))
    block = agent_blocks.AgentBlock(role="review", prompt="p", allowed_tools=agent_blocks.REVIEW_TOOLS)
    client = FakeClient(contents=["## Block Output\ncomplete"])
    _quiet_display(monkeypatch)

    main.run_agent_block(block, task="Review", completed=[], cfg=cfg, client=client)

    system_prompt = client.calls[0]["messages"][0]["content"]
    assert str(private_workspace) not in system_prompt
    assert "Relative tool paths resolve from the active session workspace" in system_prompt


def test_run_agent_block_allows_medium_risk_write_with_approval_policy(monkeypatch):
    cfg = Config()
    cfg.algorithmic_tool_policy_enabled = True
    block = agent_blocks.AgentBlock(role="implement", prompt="p", allowed_tools=agent_blocks.IMPLEMENT_TOOLS)
    client = FakeClient(contents=["## Block Output\ncomplete"])
    displayed: dict[str, str] = {}
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "show_agent_block_start",
        lambda _role, _model, _count, policy_summary="", **_kwargs: displayed.update(summary=policy_summary),
    )

    main.run_agent_block(block, task="Fix the failing login test", completed=[], cfg=cfg, client=client)

    submitted_names = {tool.__name__ for tool in client.calls[0]["tools"]}
    assert "write_file" in submitted_names
    assert "approval: batch_edit, edit_file, run_shell, write_file" in displayed["summary"]


def test_run_agent_block_rejects_policy_denied_return_when_enforced(monkeypatch):
    cfg = Config()
    cfg.algorithmic_tool_policy_enabled = True
    block = agent_blocks.AgentBlock(role="implement", prompt="p", allowed_tools=agent_blocks.IMPLEMENT_TOOLS)
    client = FakeClient(tool_call={"function": {"name": "write_file", "arguments": {"path": "x", "content": "y"}}})
    _quiet_display(monkeypatch)

    main.run_agent_block(block, task="Fix credential handling", completed=[], cfg=cfg, client=client)

    assert block.status == "failed"
    assert block.status_reason == "Tool policy violation: write_file is not allowed in the implement block."
    assert "Tool not allowed" in block.output


def test_run_agent_block_policy_denial_balances_sibling_tool_results(monkeypatch):
    cfg = Config()
    cfg.algorithmic_tool_policy_enabled = False
    block = agent_blocks.AgentBlock(role="review", prompt="p", allowed_tools=frozenset({"read_file"}))
    client = ScriptedClient(
        [
            {
                "tool_calls": [
                    {"id": "bad", "function": {"name": "write_file", "arguments": {"path": "x", "content": "y"}}},
                    {"id": "sibling", "function": {"name": "read_file", "arguments": {"path": "x"}}},
                ]
            }
        ]
    )
    _quiet_display(monkeypatch)

    main.run_agent_block(block, task="Review only", completed=[], cfg=cfg, client=client)

    tool_messages = [msg for msg in block.messages if msg.get("role") == "tool"]
    assert block.status == "failed"
    assert [msg.get("tool_call_id") for msg in tool_messages] == ["bad", "sibling"]
    assert "Tool not allowed" in tool_messages[0]["content"]
    assert "Skipped because another tool call" in tool_messages[1]["content"]


def test_agent_batch_preflight_quarantines_safe_sibling_before_late_violation(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(
        role="review",
        prompt="p",
        allowed_tools=frozenset({"read_file"}),
    )
    client = ScriptedClient(
        [
            {
                "tool_calls": [
                    {
                        "id": "safe-first",
                        "function": {"name": "read_file", "arguments": {"path": "x"}},
                    },
                    {
                        "id": "bad-second",
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": "x", "content": "y"},
                        },
                    },
                ]
            }
        ]
    )
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        tool_runtime,
        "run_tool",
        lambda *_args, **_kwargs: pytest.fail("quarantined safe sibling executed"),
    )

    main.run_agent_block(block, task="Review only", completed=[], cfg=cfg, client=client)

    tool_messages = [message for message in block.messages if message.get("role") == "tool"]
    assert block.status == "failed"
    assert len(tool_messages) == 2
    assert "Skipped because another tool call" in tool_messages[0]["content"]
    assert "Tool not allowed" in tool_messages[1]["content"]


def test_run_agent_block_gemini_collapse_uses_block_model(monkeypatch):
    cfg = Config(model="qwen3")
    block = agent_blocks.AgentBlock(role="review", prompt="p", model="gemini-3-flash-preview:cloud")
    block_client = ScriptedClient([{"content": "## Block Output\nDone."}])
    collapsed: dict[str, bool] = {}
    _quiet_display(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "client_for_model", lambda *_args, **_kwargs: block_client)

    def fake_collapse(messages):
        collapsed["called"] = True
        return messages

    monkeypatch.setattr(agent_pipeline, "collapse_tool_history_for_gemini", fake_collapse)

    main.run_agent_block(block, task="Review", completed=[], cfg=cfg, client=object())

    assert collapsed["called"] is True


def test_run_agent_block_enforces_medium_risk_approval(monkeypatch):
    cfg = Config()
    cfg.safe_mode = False
    cfg.auto_mode = True
    cfg.algorithmic_tool_policy_enabled = True
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        requires_change=True,
    )
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "git add main.py"}}}]},
            {"content": "## Block Output\nDone."},
        ]
    )
    captured: dict[str, bool] = {}
    _quiet_display(monkeypatch)

    def fake_execute(_name, _args, _cfg, *, tool_call_id=None, force_approval=False):
        captured["force_approval"] = force_approval
        return {"role": "tool", "content": "ok"}, "ok"

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", fake_execute)
    main.run_agent_block(block, task="Fix the failing test", completed=[], cfg=cfg, client=client)

    assert captured["force_approval"] is True


def test_run_agent_block_records_successful_write_evidence(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(role="implement", prompt="p", allowed_tools=agent_blocks.IMPLEMENT_TOOLS)
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "write_file", "arguments": {"path": "ollama_cli/main.py", "content": "x"}}}]},
            {"content": "## Block Output\nImplemented."},
        ]
    )
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "execute_tool_call_for_pipeline",
        lambda *_args, **_kwargs: ({"role": "tool", "content": "Wrote 1 characters"}, "Wrote 1 characters to file"),
    )

    main.run_agent_block(block, task="Fix the failing test", completed=[], cfg=cfg, client=client)

    assert block.successful_writes == ["ollama_cli/main.py"]


def test_run_agent_block_nudges_then_completes_only_after_verifier(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        max_iterations=5,
    )
    client = ScriptedClient(
        [
            {
                "tool_calls": [
                    {"function": {"name": "write_file", "arguments": {"path": "made.py", "content": "x"}}}
                ]
            },
            {"content": "## Block Output\nPremature claim."},
            {"tool_calls": [{"function": {"name": "git_diff", "arguments": {}}}]},
            {"content": "## Block Output\nVerified."},
        ]
    )
    _quiet_display(monkeypatch)

    def fake_execute(name, _args, _cfg, **_kwargs):
        if name == "write_file":
            execution_guardrails.record_mutation("made.py", success=True, operation="write_file")
            return {"role": "tool", "content": "Wrote"}, "Wrote 1 characters to made.py"
        execution_guardrails.record_verification("git_diff", success=True)
        return {"role": "tool", "content": "+x"}, "+x"

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", fake_execute)

    main.run_agent_block(block, task="Make and verify it", completed=[], cfg=cfg, client=client)

    assert block.status == "complete"
    assert block.output == "## Block Output\nVerified."
    assert len(client.calls) == 4
    assert any(
        "[Internal completion gate]" in str(message.get("content") or "")
        for message in client.calls[2]["messages"]
    )


def test_run_agent_block_replaces_unverified_claim_with_partial_warning(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        max_iterations=3,
    )
    client = ScriptedClient(
        [
            {
                "tool_calls": [
                    {"function": {"name": "write_file", "arguments": {"path": "made.py", "content": "x"}}}
                ]
            },
            {"content": "## Block Output\nFirst unsupported claim."},
            {"content": "## Block Output\nSecond unsupported claim."},
        ]
    )
    _quiet_display(monkeypatch)

    def fake_execute(_name, _args, _cfg, **_kwargs):
        execution_guardrails.record_mutation("made.py", success=True, operation="write_file")
        return {"role": "tool", "content": "Wrote"}, "Wrote 1 characters to made.py"

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", fake_execute)

    main.run_agent_block(block, task="Make it", completed=[], cfg=cfg, client=client)

    assert block.status == "partial"
    assert block.status_code == "verification_missing"
    assert block.output.startswith("## Block Output\n\nUNVERIFIED:")
    assert "unsupported claim" not in block.output


def test_required_change_block_instructs_model_to_use_write_file(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        requires_change=True,
    )
    client = FakeClient(contents=["## Block Output\ncomplete"])
    displayed: dict[str, str] = {}
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "show_agent_block_start",
        lambda _role, _model, _count, policy_summary="", **_kwargs: displayed.update(summary=policy_summary),
    )

    main.run_agent_block(block, task="Fix a bug", completed=[], cfg=cfg, client=client)

    assert agent_blocks.REQUIRED_CHANGE_PROMPT in client.calls[0]["messages"][0]["content"]
    assert "file edits: write_file only" in displayed["summary"]


def test_required_change_safe_mode_blocks_shell_mutation(monkeypatch):
    cfg = Config()
    cfg.safe_mode = True
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        requires_change=True,
    )
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "git add main.py"}}}]},
            {"content": "## Block Output\nUsed write_file instead."},
        ]
    )
    _quiet_display(monkeypatch)
    original_dispatch = agent_pipeline.execute_tool_call_for_pipeline
    ceiling_codes: list[str] = []

    def tracked_dispatch(*args, **kwargs):
        ceiling_codes.append(str(kwargs.get("policy_ceiling_code") or ""))
        return original_dispatch(*args, **kwargs)

    monkeypatch.setattr(
        agent_pipeline,
        "execute_tool_call_for_pipeline",
        tracked_dispatch,
    )
    monkeypatch.setattr(
        tool_runtime,
        "run_tool",
        lambda *_args, **_kwargs: pytest.fail("blocked mutation must not execute"),
    )

    main.run_agent_block(block, task="Fix a bug", completed=[], cfg=cfg, client=client)

    assert ceiling_codes == ["required_change_shell_blocked"]
    assert "Blocked by required-change policy" in block.messages[3]["content"]


def test_required_change_shell_mutation_forces_approval_outside_safe_mode(monkeypatch):
    cfg = Config()
    cfg.safe_mode = False
    cfg.auto_mode = True
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        requires_change=True,
    )
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "git add main.py"}}}]},
            {"content": "## Block Output\nDone."},
        ]
    )
    captured: dict[str, bool] = {}
    _quiet_display(monkeypatch)

    def fake_execute(_name, _args, _cfg, *, tool_call_id=None, force_approval=False):
        captured["force_approval"] = force_approval
        return {"role": "tool", "content": "approved"}, "approved"

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", fake_execute)

    main.run_agent_block(block, task="Fix a bug", completed=[], cfg=cfg, client=client)

    assert captured["force_approval"] is True


def test_required_change_shell_verification_does_not_force_approval(monkeypatch):
    cfg = Config()
    cfg.safe_mode = False
    cfg.auto_mode = True
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        requires_change=True,
    )
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "python -m pytest -q"}}}]},
            {"content": "## Block Output\nVerified."},
        ]
    )
    captured: dict[str, bool] = {}
    _quiet_display(monkeypatch)

    def fake_execute(_name, _args, _cfg, *, tool_call_id=None, force_approval=False):
        captured["force_approval"] = force_approval
        return {"role": "tool", "content": "ok"}, "ok"

    monkeypatch.setattr(agent_pipeline, "execute_tool_call_for_pipeline", fake_execute)

    main.run_agent_block(block, task="Fix a bug", completed=[], cfg=cfg, client=client)

    assert captured["force_approval"] is False


def test_run_agent_pipeline_falls_back_from_invalid_blocks_config(monkeypatch, tmp_path):
    cfg = Config()
    client = FakeClient(
        [
            "## Block Output\nplan output",
            "## Block Output\nresearch output",
            "## Block Output\nimplement output",
            "## Block Output\nreview output",
            "## Block Output\nfinal output",
        ]
    )
    path = tmp_path / "blocks.toml"
    path.write_text("version = 2\n[pipelines.default]\n", encoding="utf-8")
    errors: list[str] = []
    infos: list[str] = []
    _quiet_display(monkeypatch)
    monkeypatch.setattr(main.agent_blocks, "BLOCKS_CONFIG_PATH", path)
    monkeypatch.setattr(agent_pipeline, "should_recover_implementation", lambda _block: False)
    monkeypatch.setattr(agent_pipeline, "show_error", lambda message: errors.append(message))
    monkeypatch.setattr(agent_pipeline, "show_info", lambda message: infos.append(message))

    main.run_agent_pipeline("build it", cfg, client)

    assert len(client.calls) == 5
    assert any("version must be 1" in message for message in errors)
    assert any("Using built-in 'default'" in message for message in infos)


def test_run_agent_pipeline_recalls_and_injects_intuition_context(monkeypatch):
    cfg = Config()
    cfg.intuition_recall_enabled = True
    client = FakeClient(
        [
            "## Block Output\nplan output",
            "## Block Output\nresearch output",
            "## Block Output\nimplement output",
            "## Block Output\nreview output",
            "## Block Output\nfinal output",
        ]
    )
    shown: list[list[dict[str, Any]]] = []

    class FakeEngine:
        def recall(self, query, *, enabled, embed_fn):
            assert query == "build it"
            assert enabled is True
            return [{"id": "pattern:1", "type": "pattern", "content": "Use Rich Console.", "score": 0.91}]

        def format_for_injection(self, blocks):
            assert blocks
            return "## Relevant Context (from memory)\n- Use Rich Console."

    _quiet_display(monkeypatch)
    monkeypatch.setattr(main, "_intuition_engine", FakeEngine())
    monkeypatch.setattr(main, "intuition_embed_fn", lambda _cfg: object())
    monkeypatch.setattr(agent_pipeline, "show_recalled_context", lambda blocks: shown.append(blocks))

    main.run_agent_pipeline("build it", cfg, client)

    assert shown[0][0]["id"] == "pattern:1"
    assert "## Relevant Context (from memory)" in client.calls[0]["messages"][1]["content"]


def test_run_agent_pipeline_passes_no_git_delta_to_review(monkeypatch):
    cfg = Config()
    implement = agent_blocks.AgentBlock(role="implement", prompt="implement", requires_change=True)
    review = agent_blocks.AgentBlock(role="review", prompt="review")
    final = agent_blocks.AgentBlock(role="final", prompt="final")
    client = FakeClient(["## Block Output\nimplement", "## Block Output\nreview", "## Block Output\nfinal"])
    snapshot = git_evidence.GitSnapshot(
        available=True,
        error=None,
        head="abc",
        status="## branch\n M ollama_cli/main.py",
        tracked_diff="+old dirty work",
        untracked_files=(),
        tracked_diff_digest="same",
        untracked_digest="same",
    )
    displayed_status: dict[str, str] = {}
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "show_agent_block_complete",
        lambda role, _output, *, status, status_reason="", **_kwargs: displayed_status.update(
            {role: f"{status}:{status_reason}"}
        ),
    )
    monkeypatch.setattr(agent_pipeline, "resolve_pipeline_for_cli", lambda _name: ([implement, review, final], "test"))
    monkeypatch.setattr(git_evidence, "capture_git_snapshot", lambda _cwd: snapshot)

    main.run_agent_pipeline("build it", cfg, client, pipeline_name="code-change")

    assert "No Git delta was introduced during this block" in implement.git_evidence
    assert "ollama_cli/main.py" not in implement.git_evidence
    assert "No Git delta was introduced" in client.calls[1]["messages"][1]["content"]
    assert implement.status == "partial"
    assert displayed_status["implement"].startswith("partial:Required change not verified:")
    assert "No verified code change was produced" in implement.output


def test_required_change_allows_clean_attributable_git_delta():
    empty = git_evidence._digest("")
    before = git_evidence.GitSnapshot(True, None, "abc", "## main", "", (), empty, empty)
    after = git_evidence.GitSnapshot(True, None, "abc", "## main\n M main.py", "+change", (), "changed", empty)
    block = agent_blocks.AgentBlock(role="implement", prompt="p", requires_change=True, status="complete", output="done")

    main.enforce_required_change_contract(block, before, after)

    assert block.status == "complete"
    assert "Verified Git state change" in block.git_evidence


def test_required_change_allows_recorded_write_on_changed_dirty_baseline():
    before = git_evidence.GitSnapshot(True, None, "abc", "## main\n M old.py", "+old", (), "old", "empty")
    after = git_evidence.GitSnapshot(True, None, "abc", "## main\n M old.py", "+old\n+new", (), "new", "empty")
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        requires_change=True,
        status="complete",
        output="done",
        successful_writes=["old.py"],
    )

    main.enforce_required_change_contract(block, before, after)

    assert block.status == "complete"
    assert "previously dirty working tree" in block.git_evidence


def test_required_change_rejects_write_that_leaves_no_final_delta():
    snapshot = git_evidence.GitSnapshot(True, None, "abc", "## main", "", (), "same", "same")
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        requires_change=True,
        status="complete",
        output="Implemented.",
        successful_writes=["main.py"],
    )

    main.enforce_required_change_contract(block, snapshot, snapshot)

    assert block.status == "partial"
    assert block.status_code == "no_verified_delta"
    assert "recorded writes left no attributable" in block.status_reason
    assert "No verified code change was produced" in block.output


def test_required_change_allows_recorded_write_when_git_is_unavailable_with_warning():
    snapshot = git_evidence.GitSnapshot(False, "not a Git repository", None, "", "", ())
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        requires_change=True,
        status="complete",
        output="Implemented.",
        successful_writes=["main.py"],
    )

    main.enforce_required_change_contract(block, snapshot, snapshot)

    assert block.status == "complete"
    assert block.output == "Implemented."
    assert "Verification Warning" not in block.output
    assert "manually confirm" in block.verification_warning
    assert "manual" not in block.git_evidence.lower()
    context = agent_blocks.pipeline_context("Fix it", [block])
    assert "Implemented." in context
    assert "## Verification\nGit verification was unavailable." in context
    assert "manually confirm" in context


def test_required_change_rejects_no_write_when_git_is_unavailable():
    snapshot = git_evidence.GitSnapshot(False, "not a Git repository", None, "", "", ())
    block = agent_blocks.AgentBlock(role="implement", prompt="p", requires_change=True, status="complete", output="Done.")

    main.enforce_required_change_contract(block, snapshot, snapshot)

    assert block.status == "partial"
    assert block.status_code == "no_write_evidence"
    assert "Git evidence is unavailable" in block.status_reason
    assert "No verified code change was produced" in block.output


def test_required_change_rejects_recorded_write_across_head_change():
    before = git_evidence.GitSnapshot(True, None, "abc", "## main", "", (), "empty", "empty")
    after = git_evidence.GitSnapshot(True, None, "def", "## main", "+change", (), "changed", "empty")
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        requires_change=True,
        status="complete",
        output="Done.",
        successful_writes=["main.py"],
    )

    main.enforce_required_change_contract(block, before, after)

    assert block.status == "partial"
    assert block.status_code == "attribution_unsafe"
    assert "HEAD changed" in block.status_reason
    assert "ATTRIBUTION UNSAFE" in block.git_evidence


def test_non_change_mutation_is_audited_without_status_gate(monkeypatch):
    cfg = Config()
    review = agent_blocks.AgentBlock(role="review", prompt="review", allowed_tools=agent_blocks.REVIEW_TOOLS)
    final = agent_blocks.AgentBlock(role="final", prompt="final")
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "git add main.py"}}}]},
            {"content": "## Block Output\nReview complete."},
            {"content": "## Block Output\nFinal."},
        ]
    )
    empty = git_evidence._digest("")
    before = git_evidence.GitSnapshot(True, None, "abc", "## main", "", (), empty, empty)
    after = git_evidence.GitSnapshot(True, None, "abc", "## main\n M main.py", "+change", (), "changed", empty)
    snapshots = iter([before, after])
    infos: list[str] = []
    _quiet_display(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "resolve_pipeline_for_cli", lambda _name: ([review, final], "test"))
    monkeypatch.setattr(agent_pipeline, "_capture_thread_workspace", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(git_evidence, "capture_git_snapshot", lambda _cwd: next(snapshots))
    monkeypatch.setattr(agent_pipeline, "show_info", lambda message: infos.append(message))
    monkeypatch.setattr(
        agent_pipeline,
        "execute_tool_call_for_pipeline",
        lambda *_args, **_kwargs: ({"role": "tool", "content": "ran"}, "ran\n[exit code: 0]"),
    )

    main.run_agent_pipeline("Review it", cfg, client, pipeline_name="review")

    assert review.status == "complete"
    assert review.mutation_actions == ["run_shell: git add main.py"]
    assert "Audit notice" in review.audit_evidence
    assert "## Mutation Audit Evidence" in client.calls[2]["messages"][1]["content"]
    assert any("Mutation audit" in info for info in infos)


def test_non_change_denied_shell_mutation_is_not_audited(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(role="review", prompt="review", allowed_tools=agent_blocks.REVIEW_TOOLS)
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "git add main.py"}}}]},
            {"content": "## Block Output\nReview complete."},
        ]
    )
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "execute_tool_call_for_pipeline",
        lambda *_args, **_kwargs: ({"role": "tool", "content": "denied"}, "User denied this operation."),
    )

    main.run_agent_block(block, task="Review it", completed=[], cfg=cfg, client=client)

    assert block.mutation_actions == []


def test_failed_shell_mutation_is_not_audited_as_success(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(role="review", prompt="review", allowed_tools=agent_blocks.REVIEW_TOOLS)
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "git add main.py"}}}]},
            {"content": "## Block Output\nReview complete."},
        ]
    )
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "execute_tool_call_for_pipeline",
        lambda *_args, **_kwargs: (
            {"role": "tool", "content": "failed"},
            "command failed\n[exit code: 1]",
        ),
    )

    main.run_agent_block(block, task="Review it", completed=[], cfg=cfg, client=client)

    assert block.mutation_actions == []


def test_run_agent_block_synthesizes_partial_output_at_iteration_limit(monkeypatch):
    cfg = Config()
    block = agent_blocks.AgentBlock(
        role="review",
        prompt="p",
        allowed_tools=agent_blocks.REVIEW_TOOLS,
        max_iterations=1,
    )
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "notes.md"}}}]},
            {"content": "## Block Output\n\nPartial finding: stale notes were identified."},
        ]
    )
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "execute_tool_call_for_pipeline",
        lambda *_args, **_kwargs: ({"role": "tool", "content": "read evidence"}, "read evidence"),
    )

    main.run_agent_block(block, task="Review the wiki", completed=[], cfg=cfg, client=client)

    assert block.status == "partial"
    assert block.status_code == "max_iterations"
    assert "Iteration budget exhausted after 1 cycles" in block.status_reason
    assert "Partial finding" in block.output
    assert client.calls[1]["tools"] == []


def test_partial_block_allows_pipeline_finalizer_to_run(monkeypatch):
    cfg = Config()
    review = agent_blocks.AgentBlock(
        role="review",
        prompt="review",
        allowed_tools=agent_blocks.REVIEW_TOOLS,
        max_iterations=1,
    )
    final = agent_blocks.AgentBlock(role="final", prompt="final")
    client = ScriptedClient(
        [
            {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "notes.md"}}}]},
            {"content": "## Block Output\n\nPartial evidence summary."},
            {"content": "## Block Output\n\nFinal answer from partial evidence."},
        ]
    )
    _quiet_display(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "resolve_pipeline_for_cli", lambda _name: ([review, final], "test"))
    monkeypatch.setattr(
        agent_pipeline,
        "execute_tool_call_for_pipeline",
        lambda *_args, **_kwargs: ({"role": "tool", "content": "read evidence"}, "read evidence"),
    )

    main.run_agent_pipeline("Review the wiki", cfg, client, pipeline_name="review")

    assert review.status == "partial"
    assert final.status == "complete"
    assert "Partial evidence summary." in client.calls[2]["messages"][1]["content"]
    assert "## Block Status\nStatus: PARTIAL" in client.calls[2]["messages"][1]["content"]
    assert "Iteration budget exhausted" in client.calls[2]["messages"][1]["content"]


def test_pipeline_recovery_runs_one_replan_and_reduced_budget_retry(monkeypatch):
    cfg = Config()
    implement = agent_blocks.AgentBlock(
        role="implement",
        prompt="implement",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        max_iterations=12,
        requires_change=True,
    )
    final = agent_blocks.AgentBlock(role="final", prompt="final")
    roles: list[str] = []
    final_context: list[str] = []
    notices: list[tuple[str, str, int]] = []
    _quiet_display(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "resolve_pipeline_for_cli", lambda _name: ([implement, final], "test"))
    monkeypatch.setattr(
        agent_pipeline,
        "show_agent_recovery_start",
        lambda role, reason, budget: notices.append((role, reason, budget)),
    )

    def fake_run(block, *, task, completed, **_kwargs):
        roles.append(block.role)
        if block.role == "implement":
            block.status = "partial"
            block.status_code = "no_write_evidence"
            block.status_reason = "Required change not verified: no write evidence."
            block.output = "## Block Output\n\nOriginal attempt."
        elif block.role == "recovery-plan":
            block.status = "complete"
            block.output = "## Block Output\n\nWrite the confirmed target directly."
        elif block.role == "implement-retry":
            assert block.max_iterations == 8
            block.status = "complete"
            block.output = "## Block Output\n\nRetry implemented."
        else:
            final_context.append(agent_blocks.pipeline_context(task, completed))
            block.status = "complete"
            block.output = "## Block Output\n\nFinal."

    monkeypatch.setattr(agent_pipeline, "run_agent_block", fake_run)

    main.run_agent_pipeline("Fix it", cfg, object(), pipeline_name="code-change")

    assert roles == ["implement", "recovery-plan", "implement-retry", "final"]
    assert notices == [("implement", implement.status_reason, 8)]
    assert "## Output from implement\n## Block Output\n\nOriginal attempt." in final_context[0]
    assert "## Output from recovery-plan" in final_context[0]
    assert "## Output from implement-retry" in final_context[0]


def test_pipeline_recovery_does_not_recurse_after_partial_retry(monkeypatch):
    cfg = Config()
    implement = agent_blocks.AgentBlock(
        role="implement",
        prompt="implement",
        allowed_tools=agent_blocks.IMPLEMENT_TOOLS,
        requires_change=True,
    )
    final = agent_blocks.AgentBlock(role="final", prompt="final")
    roles: list[str] = []
    _quiet_display(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "resolve_pipeline_for_cli", lambda _name: ([implement, final], "test"))

    def fake_run(block, **_kwargs):
        roles.append(block.role)
        block.output = f"## Block Output\n\n{block.role}."
        if block.role in {"implement", "implement-retry"}:
            block.status = "partial"
            block.status_code = "no_write_evidence"
            block.status_reason = "No verified write."
        else:
            block.status = "complete"

    monkeypatch.setattr(agent_pipeline, "run_agent_block", fake_run)

    main.run_agent_pipeline("Fix it", cfg, object(), pipeline_name="code-change")

    assert roles == ["implement", "recovery-plan", "implement-retry", "final"]


def test_pipeline_recovery_skips_policy_denied_partial(monkeypatch):
    block = agent_blocks.AgentBlock(role="implement", prompt="p", requires_change=True)
    block.status = "partial"
    block.status_code = "policy_denied"
    block.status_reason = "Mutation denied by policy."
    block.mutation_denied = True

    assert main.should_recover_implementation(block) is False


def test_recovery_plan_includes_bounded_attempt_summary():
    cfg = Config()
    cfg.attempt_ledger = [
        {"status": "failed", "tool": "write_file", "summary": "File exists."},
        {"status": "worked", "tool": "read_file", "summary": "Read target."},
    ]
    block = agent_blocks.AgentBlock(role="implement", prompt="p", requires_change=True)

    recovery = main.recovery_plan_block(block, cfg)

    assert recovery.allowed_tools == agent_blocks.NO_TOOLS
    assert "FAILED write_file: File exists." in recovery.prompt
    assert "WORKED read_file: Read target." in recovery.prompt


def test_required_change_write_failure_is_recoverable():
    snapshot = git_evidence.GitSnapshot(True, None, "abc", "## main", "", (), "same", "same")
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="p",
        requires_change=True,
        status="complete",
        output="Tried writing.",
        failed_writes=["Error: file exists; pass overwrite=True."],
    )

    main.enforce_required_change_contract(block, snapshot, snapshot)

    assert block.status_code == "write_blocked"
    assert main.should_recover_implementation(block) is True


def test_parse_agent_invocation_accepts_pipeline_flag():
    pipeline, task = main.parse_agent_invocation('--pipeline code-change "fix auth"')

    assert pipeline == "code-change"
    assert task == "fix auth"


def test_parse_agent_invocation_reports_missing_pipeline_args():
    pipeline, task, error = main.parse_agent_invocation_checked("--pipeline")

    assert pipeline == "default"
    assert task == ""
    assert "Usage: /agent [--pipeline NAME] <task>" in error


def test_agent_command_rejects_missing_pipeline_args(monkeypatch):
    from algo_cli import oliver_slash_dispatch as slash_dispatch

    cfg = Config()
    errors: list[str] = []
    started: list[str] = []

    monkeypatch.setattr(agent_pipeline, "show_error", lambda message: errors.append(str(message)))
    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", lambda *args, **kwargs: started.append("ran"))

    handled, _client = slash_dispatch.handle_command("/agent --pipeline", cfg, object())

    assert handled is True
    assert started == []
    assert errors
    assert "Usage: /agent [--pipeline NAME] <task>" in errors[0]


def test_agent_help_shows_usage_without_starting_pipeline(monkeypatch):
    from algo_cli import oliver_slash_dispatch as slash_dispatch

    cfg = Config()
    infos: list[str] = []
    started: list[str] = []

    monkeypatch.setattr(agent_pipeline, "show_info", lambda message: infos.append(str(message)))
    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", lambda *args, **kwargs: started.append("ran"))

    handled, _client = slash_dispatch.handle_command("/agent help", cfg, object())

    assert handled is True
    assert started == []
    assert infos
    assert "Usage: /agent [--pipeline NAME] <task>" in infos[0]
    assert "Available pipelines:" in infos[0]
    assert "code-change" in infos[0]


def test_parse_agent_invocation_defaults_to_default_pipeline():
    pipeline, task = main.parse_agent_invocation("build a tool")

    assert pipeline == "default"
    assert task == "build a tool"


def test_parse_agent_team_invocation_supports_bounded_named_roles():
    roles, task, error = main.parse_agent_team_invocation(
        '--roles "code scout,critic,verifier" "Review auth and tests"'
    )

    assert roles == ["code-scout", "critic", "verifier"]
    assert task == "Review auth and tests"
    assert error == ""


def test_parse_agent_team_invocation_rejects_unbounded_or_duplicate_roles():
    _roles, _task, too_many = main.parse_agent_team_invocation(
        "--roles a,b,c,d,e Review the project"
    )
    _roles, _task, duplicate = main.parse_agent_team_invocation(
        "--roles scout,scout Review the project"
    )

    assert "2-4" in too_many
    assert "unique" in duplicate


def test_agent_team_fans_out_specialists_then_passes_bounded_handoff(monkeypatch):
    cfg = Config()
    captured: dict[str, Any] = {}
    roles_seen: list[str] = []
    barrier = threading.Barrier(3)
    _quiet_display(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "create_client", lambda _cfg: object())

    def fake_run_block(block, **_kwargs):
        roles_seen.append(block.role)
        barrier.wait(timeout=2)
        block.status = "complete"
        block.output = f"## Block Output\nEvidence from {block.role}"

    def fake_run_pipeline(task, _cfg, _client, pipeline_name="default", **kwargs):
        captured.update({"task": task, "pipeline": pipeline_name, **kwargs})
        return agent_pipeline.AgentRunResult(
            thread_id=str(kwargs.get("thread_id") or "parent"),
            status="complete",
            pipeline=f"team:{pipeline_name}",
            output="integrated",
        )

    monkeypatch.setattr(agent_pipeline, "run_agent_block", fake_run_block)
    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", fake_run_pipeline)

    result = agent_pipeline.run_agent_team(
        "Review the authentication code",
        cfg,
        object(),
        roles=["correctness", "security", "tests"],
    )

    assert set(roles_seen) == {"correctness", "security", "tests"}
    assert captured["pipeline"] == "review"
    assert "Evidence from correctness" in captured["prior_context"]
    assert "Evidence from security" in captured["prior_context"]
    assert captured["thread_pipeline_label"] == "team:review"
    assert len(result.children) == 3
    parent = agent_threads.resolve_thread(result.thread_id)
    assert set(parent["children"]) == set(result.children)


def test_execute_agent_memory_seam_uses_original_task_and_completion_status(monkeypatch):
    statuses = iter(("complete", "failed"))
    capture_calls: list[dict] = []

    def fake_run_pipeline(task, _cfg, _client, pipeline_name="default", **_kwargs):
        return agent_pipeline.AgentRunResult(
            thread_id="thread-1",
            status=next(statuses),
            pipeline=pipeline_name,
            output="done",
        )

    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        agent_pipeline.memory_runtime,
        "capture_completed_user_turn",
        lambda _cfg, text, **kwargs: capture_calls.append({"text": text, **kwargs})
        or {"status": "rejected"},
    )
    cfg = Config()

    agent_pipeline.execute_agent_command(
        "Remember that our standard shell is zsh.",
        cfg,
        object(),
    )
    agent_pipeline.execute_agent_command(
        "Remember that our standard formatter is Ruff.",
        cfg,
        object(),
    )

    assert capture_calls == [
        {
            "text": "Remember that our standard shell is zsh.",
            "completed": True,
            "source": "agent",
        },
        {
            "text": "Remember that our standard formatter is Ruff.",
            "completed": False,
            "source": "agent",
        },
    ]


def test_agent_resume_and_fork_pass_prior_thread_handoff(monkeypatch):
    cfg = Config()
    original = agent_threads.create_thread(
        "Original task",
        pipeline="review",
        status="queued",
    )
    agent_threads.begin_turn(original["id"], "Original task")
    agent_threads.finish_turn(
        original["id"],
        status="complete",
        output="Verified prior evidence",
        blocks=[{"role": "review", "status": "complete"}],
    )
    calls: list[dict[str, Any]] = []

    def fake_run_pipeline(task, _cfg, _client, pipeline_name="default", **kwargs):
        calls.append({"task": task, "pipeline": pipeline_name, **kwargs})
        return agent_pipeline.AgentRunResult(
            thread_id=str(kwargs.get("thread_id") or "forked"),
            status="complete",
            pipeline=pipeline_name,
            output="done",
        )

    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", fake_run_pipeline)

    resumed = agent_pipeline.execute_agent_command(
        f"resume {original['id']} Finish verification",
        cfg,
        object(),
    )
    forked = agent_pipeline.execute_agent_command(
        f"fork {original['id']} Explore an alternative",
        cfg,
        object(),
    )

    assert "Agent thread" in resumed
    assert "Agent thread" in forked
    assert calls[0]["thread_id"] == original["id"]
    assert calls[0]["parent_id"] == ""
    assert calls[1]["thread_id"] is None
    assert calls[1]["parent_id"] == original["id"]
    assert "Verified prior evidence" in calls[0]["prior_context"]


def test_agent_fork_creates_and_activates_isolated_worktree(monkeypatch, tmp_path):
    parent_path = tmp_path / "parent"
    child_path = tmp_path / "child"
    parent_path.mkdir()
    child_path.mkdir()
    cfg = Config(cwd=str(tmp_path))
    record = {
        "id": "abc12345",
        "task": "Original task",
        "pipeline": "review",
        "status": "complete",
        "output": "Prior evidence",
        "blocks": [],
        "workspace": {
            "available": True,
            "workspace_root": str(parent_path),
            "branch": "algo/parent",
            "head": "a" * 40,
            "initial_head": "a" * 40,
        },
    }
    calls: dict[str, Any] = {}

    monkeypatch.setattr(agent_threads, "resolve_thread", lambda _ref: record)

    def fake_restore(_record, loaded_cfg):
        loaded_cfg.cwd = str(parent_path)
        return True

    def fake_create(cwd, name, *, base_ref):
        calls["create"] = {"cwd": cwd, "name": name, "base_ref": base_ref}
        return {"id": "work1234", "branch": "algo/child", "path": str(child_path)}

    def fake_activate(ref, loaded_cfg):
        calls["activate"] = ref
        loaded_cfg.cwd = str(child_path)

    def fake_run(task, loaded_cfg, _client, pipeline_name="default", **kwargs):
        calls["run"] = {"task": task, "cwd": loaded_cfg.cwd, **kwargs}
        return agent_pipeline.AgentRunResult(
            thread_id="forked01",
            status="complete",
            pipeline=pipeline_name,
            output="done",
        )

    monkeypatch.setattr(agent_pipeline.worktree_runtime, "activate_thread_workspace", fake_restore)
    monkeypatch.setattr(
        agent_pipeline.worktree_runtime,
        "capture_workspace",
        lambda _cwd: {"available": True, "clean": True, "head": "b" * 40},
    )
    monkeypatch.setattr(agent_pipeline.worktree_runtime, "create_worktree", fake_create)
    monkeypatch.setattr(agent_pipeline.worktree_runtime, "activate_worktree", fake_activate)
    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", fake_run)
    monkeypatch.setattr(
        agent_pipeline.memory_runtime,
        "capture_completed_user_turn",
        lambda *_args, **_kwargs: {"status": "rejected"},
    )

    result = agent_pipeline.execute_agent_command(
        "fork abc12345 Explore an alternative",
        cfg,
        object(),
    )

    assert "Agent thread forked01" in result
    assert calls["create"]["cwd"] == str(parent_path)
    assert calls["create"]["base_ref"] == "b" * 40
    assert calls["activate"] == "work1234"
    assert calls["run"]["cwd"] == str(child_path)
    assert calls["run"]["parent_id"] == "abc12345"


def test_agent_fork_refuses_to_drop_dirty_parent_state(monkeypatch, tmp_path):
    parent_path = tmp_path / "parent"
    parent_path.mkdir()
    cfg = Config(cwd=str(tmp_path))
    record = {
        "id": "abc12345",
        "task": "Original task",
        "pipeline": "review",
        "status": "complete",
        "output": "Prior evidence",
        "blocks": [],
        "workspace": {
            "available": True,
            "workspace_root": str(parent_path),
            "branch": "algo/parent",
            "head": "a" * 40,
        },
    }
    create_calls: list[str] = []

    monkeypatch.setattr(agent_threads, "resolve_thread", lambda _ref: record)

    def fake_restore(_record, loaded_cfg):
        loaded_cfg.cwd = str(parent_path)
        return True

    monkeypatch.setattr(agent_pipeline.worktree_runtime, "activate_thread_workspace", fake_restore)
    monkeypatch.setattr(
        agent_pipeline.worktree_runtime,
        "capture_workspace",
        lambda _cwd: {"available": True, "clean": False},
    )
    monkeypatch.setattr(
        agent_pipeline.worktree_runtime,
        "create_worktree",
        lambda *_args, **_kwargs: create_calls.append("created"),
    )

    result = agent_pipeline.execute_agent_command(
        "fork abc12345 Explore an alternative",
        cfg,
        object(),
    )

    assert result.startswith("Error:")
    assert "start a new /worktree and agent thread" in result
    assert "--same-worktree" in result
    assert create_calls == []


def test_agent_fork_refuses_missing_or_invalid_verified_head(monkeypatch, tmp_path):
    parent_path = tmp_path / "parent"
    parent_path.mkdir()
    cfg = Config(cwd=str(tmp_path))
    record = {
        "id": "abc12345",
        "task": "Original task",
        "pipeline": "review",
        "status": "complete",
        "output": "Prior evidence",
        "blocks": [],
        "workspace": {"available": True, "workspace_root": str(parent_path)},
    }
    create_calls: list[str] = []

    monkeypatch.setattr(agent_threads, "resolve_thread", lambda _ref: record)

    def fake_restore(_record, loaded_cfg):
        loaded_cfg.cwd = str(parent_path)
        return True

    monkeypatch.setattr(agent_pipeline.worktree_runtime, "activate_thread_workspace", fake_restore)
    monkeypatch.setattr(
        agent_pipeline.worktree_runtime,
        "capture_workspace",
        lambda _cwd: {"available": True, "clean": True, "head": "not-an-oid"},
    )
    monkeypatch.setattr(
        agent_pipeline.worktree_runtime,
        "create_worktree",
        lambda *_args, **_kwargs: create_calls.append("created"),
    )

    result = agent_pipeline.execute_agent_command(
        "fork abc12345 Explore an alternative",
        cfg,
        object(),
    )

    assert result.startswith("Error:")
    assert "HEAD is missing or invalid" in result
    assert create_calls == []


def test_thread_workspace_capture_uses_fresh_full_state_evidence(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    evidence = {
        "available": True,
        "workspace_root": str(tmp_path),
        "head": "a" * 40,
        "status": "## feature",
        "tracked_diff_digest": "b" * 64,
        "untracked_digest": "c" * 64,
        "clean": False,
    }
    calls: list[str] = []

    def fake_capture(cwd):
        calls.append(cwd)
        return dict(evidence)

    monkeypatch.setattr(agent_pipeline.worktree_runtime, "capture_workspace", fake_capture)

    captured = agent_pipeline._capture_thread_workspace(cfg)

    assert captured == evidence
    assert calls == [str(tmp_path)]


def test_resumed_pipeline_does_not_run_heuristic_workspace_resolver(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path))
    final = agent_blocks.AgentBlock(role="final", prompt="final")
    client = ScriptedClient([{"content": "## Block Output\nDone."}])
    resolver_calls: list[str] = []
    _quiet_display(monkeypatch)
    monkeypatch.setattr(
        agent_pipeline,
        "resolve_pipeline_for_cli",
        lambda _name: ([final], "test"),
    )
    monkeypatch.setattr(
        agent_pipeline,
        "resolve_agent_workspace",
        lambda task, _cfg: resolver_calls.append(task) or True,
    )
    monkeypatch.setattr(agent_pipeline, "_start_thread_record", lambda *_args, **_kwargs: "")

    agent_pipeline.run_agent_pipeline(
        "Continue work on Algo CLI",
        cfg,
        client,
        pipeline_name="default",
        prior_context="verified parent handoff",
    )

    assert resolver_calls == []
