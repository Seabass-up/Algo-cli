"""Regression tests: context accounting must track the real request window.

Bug: compaction thresholded against the model's native window (e.g. 128k)
while requests ran at min(num_ctx, native) (e.g. 8k) — so Ollama silently
truncated history long before compaction ever fired.
"""

from algo_cli import context_budget, model_info
from algo_cli.config import Config
from algo_cli.tool_runtime import tool_result_message
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

    with_tools = context_budget.estimate_usage_with_system_prompt(
        "system", cfg, tools=selected
    )

    assert with_tools - without_tools == estimate_tool_schema_tokens(selected)


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
    runtime_cap, _ = model_info.effective_context_limits(
        cfg, {"context_length": 131072, "parameter_size": "70B"}
    )
    assert runtime_cap == 4096


def test_compaction_fires_against_real_window(monkeypatch):
    """History at ~9k tokens with an 8k request window must compact, even
    though the native window is 131k (the old code compared against native)."""
    cfg = Config()
    cfg.model_adaptive = False
    # ~36k chars -> ~9k estimated tokens, spread over enough messages to keep
    # CONTEXT_KEEP_MESSAGES satisfied.
    cfg.messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 900}
        for i in range(40)
    ]
    monkeypatch.setattr(
        context_budget,
        "summarize_message_batch",
        lambda cfg_, batch, client, maintenance_client_fn=None: "summary of old turns",
    )
    monkeypatch.setattr(context_budget, "estimate_context_usage", lambda *a, **k: 9000)
    compacted = context_budget.maybe_compact_context(
        client=None, cfg=cfg, model_info=_big_native_info()
    )
    assert compacted is True
    assert cfg.session_summary == "summary of old turns"
    assert len(cfg.messages) == context_budget.SMALL_CONTEXT_KEEP_MESSAGES


def test_no_compaction_when_under_threshold(monkeypatch):
    cfg = Config()
    cfg.model_adaptive = False
    cfg.messages = [{"role": "user", "content": "hi"} for _ in range(20)]
    monkeypatch.setattr(context_budget, "estimate_context_usage", lambda *a, **k: 1000)
    assert (
        context_budget.maybe_compact_context(client=None, cfg=cfg, model_info=_big_native_info())
        is False
    )


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

    assert context_budget.estimate_message_tokens(with_tool_name) > context_budget.estimate_message_tokens(without_tool_name)
