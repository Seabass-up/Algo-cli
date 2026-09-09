"""Regression tests: context accounting must track the real request window.

Bug: compaction thresholded against the model's native window (e.g. 128k)
while requests ran at min(num_ctx, native) (e.g. 8k) — so Ollama silently
truncated history long before compaction ever fired.
"""

import pytest

from algo_cli import context_budget, model_info
from algo_cli.config import Config
from algo_cli.nathan_runtime import tool_result_message
from algo_cli.tool_schema import estimate_tool_schema_tokens
from algo_cli import tools


def _big_native_info() -> dict:
    return {"context_length": 131072, "parameter_size": "8B"}


def test_runtime_limit_is_request_window_not_native():
    cfg = Config()
    cfg.model_adaptive = False
    runtime_cap, native = model_info.effective_context_limits(cfg, _big_native_info())
    assert native == 131072
    assert runtime_cap == cfg.num_ctx  # 8192, the window actually requested


def test_display_total_uses_runtime_cap():
    cfg = Config()
    cfg.model_adaptive = False
    used, total, remaining, runtime_cap, native = context_budget.context_status(
        cfg, model_info=_big_native_info(), runtime_status={}
    )
    assert total == runtime_cap
    assert native == 131072
    assert total < native


def test_request_estimate_accounts_for_visible_tool_schemas():
    cfg = Config()
    cfg.messages = [{"role": "user", "content": "inspect the repository"}]
    without_tools = context_budget.estimate_usage_with_system_prompt("system", cfg)
    selected = tools.ALL_TOOLS[:3]

    with_tools = context_budget.estimate_usage_with_system_prompt("system", cfg, tools=selected)

    assert with_tools - without_tools == estimate_tool_schema_tokens(selected)


def test_oneshot_prompt_defers_interactive_capability_tutorials(monkeypatch):
    cfg = Config(model="test-model")
    monkeypatch.setattr(context_budget.identity, "build_identity_block", lambda **_kwargs: "")

    monkeypatch.setattr(context_budget, "json_sink", lambda: None)
    interactive = context_budget.build_system_prompt(cfg, user_message="fix the tests")
    monkeypatch.setattr(context_budget, "json_sink", lambda: object())
    automated = context_budget.build_system_prompt(cfg, user_message="fix the tests")

    assert "## One-shot Runtime Contract" in automated
    assert "## Session Slash Commands" not in automated
    assert "## Grok / xAI model compatibility" not in automated
    assert "## PDF Handling" not in automated
    assert "Prefer action_program" in automated
    assert context_budget.estimate_text_tokens(automated) < (context_budget.estimate_text_tokens(interactive) * 0.45)


def test_adaptive_window_feeds_accounting():
    cfg = Config()
    cfg.model_adaptive = True
    # Native context is a ceiling; local adaptive defaults avoid oversized KV allocations.
    info = {"context_length": 131072, "parameter_size": "70B"}
    runtime_cap, _ = model_info.effective_context_limits(cfg, info)
    assert runtime_cap == 32768


def test_gemma4_adaptive_context_accounting_uses_bounded_local_window():
    cfg = Config(model="gemma4:12b-mlx-bf16")
    cfg.model_adaptive = True
    info = {"context_length": 262144, "parameter_size": "12.4B"}
    runtime_cap, native = model_info.effective_context_limits(cfg, info)
    assert runtime_cap == 16384
    assert native == 262144


def test_user_ctx_override_wins_over_adaptive():
    cfg = Config()
    cfg.model_adaptive = True
    cfg.num_ctx = 4096  # explicit /ctx override
    runtime_cap, _ = model_info.effective_context_limits(cfg, {"context_length": 131072, "parameter_size": "70B"})
    assert runtime_cap == 4096


def test_compaction_fires_against_real_window(monkeypatch):
    """History at ~9k tokens with an 8k request window must compact, even
    though the native window is 131k (the old code compared against native)."""
    cfg = Config()
    cfg.model_adaptive = False
    # ~36k chars -> ~9k estimated tokens, spread over enough messages to keep
    # CONTEXT_KEEP_MESSAGES satisfied.
    cfg.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 900} for i in range(40)]
    monkeypatch.setattr(
        context_budget,
        "summarize_message_batch",
        lambda cfg_, batch, client, maintenance_client_fn=None: "summary of old turns",
    )
    monkeypatch.setattr(context_budget, "estimate_context_usage", lambda *a, **k: 9000)
    compacted = context_budget.maybe_compact_context(client=None, cfg=cfg, model_info=_big_native_info())
    assert compacted is True
    assert cfg.session_summary == "summary of old turns"
    assert len(cfg.messages) == context_budget.SMALL_CONTEXT_KEEP_MESSAGES


def test_no_compaction_when_under_threshold(monkeypatch):
    cfg = Config()
    cfg.model_adaptive = False
    cfg.messages = [{"role": "user", "content": "hi"} for _ in range(20)]
    monkeypatch.setattr(context_budget, "estimate_context_usage", lambda *a, **k: 1000)
    assert context_budget.maybe_compact_context(client=None, cfg=cfg, model_info=_big_native_info()) is False


def test_optional_context_blocks_fit_when_budget_allows():
    message, included, omitted, used = context_budget.fit_optional_context_blocks(
        "review the harness",
        [
            context_budget.OptionalContextBlock(
                "harness",
                "Relevant Context",
                "Use harness_read with IDs for deeper source verification.",
            )
        ],
        base_used_tokens=1000,
        runtime_cap=4096,
        model_info={"context_length": 4096, "parameter_size": "7B"},
    )

    assert "## Relevant Context" in message
    assert included == ["harness"]
    assert omitted == []
    assert used > 0


def test_optional_context_blocks_omitted_when_budget_is_exhausted():
    message, included, omitted, used = context_budget.fit_optional_context_blocks(
        "review the harness",
        [
            context_budget.OptionalContextBlock(
                "harness",
                "Relevant Context",
                "x" * 4000,
            )
        ],
        base_used_tokens=3900,
        runtime_cap=4096,
        model_info={"context_length": 4096, "parameter_size": "7B"},
    )

    assert message == "review the harness"
    assert included == []
    assert omitted == ["harness"]
    assert used == 0


def test_tool_result_message_preserves_tool_name_metadata():
    message = tool_result_message("read_file", "file contents", tool_call_id="call_1")

    assert message["name"] == "read_file"
    assert message["tool_name"] == "read_file"
    assert message["tool_call_id"] == "call_1"


def test_tool_result_message_tool_name_contributes_to_token_estimate():
    with_tool_name = tool_result_message("read_file", "x")
    without_tool_name = {"role": "tool", "name": "read_file", "content": "x"}

    assert context_budget.estimate_message_tokens(with_tool_name) > context_budget.estimate_message_tokens(
        without_tool_name
    )


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("automated", [False, True])
@pytest.mark.parametrize("memory", ["", "## Memory\nPRIVATE_MEMORY_CANARY " * 17], ids=["empty", "populated"])
def test_rendered_system_source_counts_are_content_free_and_do_not_recall_twice(
    monkeypatch, protected, automated, memory
):
    cfg = Config(
        model="test-model", echo_veil_enabled=protected, echo_veil_protection="required" if protected else "optional"
    )
    identity_calls = []
    memory_calls = []
    monkeypatch.setattr(context_budget, "json_sink", lambda: object() if automated else None)
    monkeypatch.setattr(
        context_budget.identity,
        "build_identity_block",
        lambda **kwargs: identity_calls.append(kwargs) or "immutable identity",
    )
    monkeypatch.setattr(
        context_budget, "_memory_prompt_section", lambda _cfg, **_kwargs: memory_calls.append(True) or memory
    )
    counts = {"stale": 123}

    prompt = context_budget.build_system_prompt(cfg, source_token_counts=counts)

    assert len(identity_calls) == len(memory_calls) == 1
    assert set(counts) == {"identity", "memory"}
    assert counts["identity"] == context_budget.estimate_text_tokens("immutable identity\n\n")
    if memory:
        assert memory in prompt
        assert (
            context_budget.estimate_text_tokens(memory)
            <= counts["memory"]
            <= context_budget.estimate_text_tokens(memory) + 1
        )
    else:
        assert counts["memory"] == 0
    assert all(type(value) is int and value >= 0 for value in counts.values())
    assert "PRIVATE_MEMORY_CANARY" not in str(counts)


@pytest.mark.parametrize("budget", range(96, 104))
def test_optional_truncation_includes_separator_in_its_budget(budget):
    cap = 1024
    base = "base"
    fitted, _, _, used = context_budget.fit_optional_context_blocks(
        base,
        [context_budget.OptionalContextBlock("harness", "Sources", "word " * 1000)],
        base_used_tokens=cap - context_budget.context_response_reserve(cap) - budget,
        runtime_cap=cap,
    )

    assert used <= budget
    assert context_budget.estimate_text_tokens(fitted) - context_budget.estimate_text_tokens(base) <= budget


@pytest.mark.parametrize("base", ["", "x", "xx", "xxx", "xxxx"])
def test_optional_source_counts_use_rendered_truncated_text_and_accumulate_names(base):
    counts = {"stale": 100000}
    fitted, included, omitted, used = context_budget.fit_optional_context_blocks(
        base,
        [
            context_budget.OptionalContextBlock("harness", "First", "word " * 8),
            context_budget.OptionalContextBlock("harness", "Second", "word " * 1000),
            context_budget.OptionalContextBlock("memory", "Empty", ""),
        ],
        base_used_tokens=500,
        runtime_cap=1024,
        source_token_counts=counts,
    )

    assert included == ["harness", "harness"]
    assert omitted == []
    assert "...[truncated by context budget]" in fitted
    assert set(counts) == {"harness"}
    assert counts["harness"] == context_budget.estimate_text_tokens(fitted) - context_budget.estimate_text_tokens(base)
    assert counts["harness"] <= used <= counts["harness"] + len(included)
    assert used < context_budget.estimate_text_tokens("word " * 1000)
    context_budget.fit_optional_context_blocks(
        base,
        [],
        base_used_tokens=500,
        runtime_cap=1024,
        source_token_counts=counts,
    )
    assert counts == {}
