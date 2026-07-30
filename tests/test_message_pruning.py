"""Intra-session pruning of stale tool messages."""

from __future__ import annotations

from algo_cli import main
from algo_cli.config import Config


def _msgs_with_user_assistant_tool_pattern(n_groups: int) -> list[dict]:
    """Generate n_groups of (user, assistant+tool_calls, tool) — 3 msgs per group."""
    out: list[dict] = []
    for i in range(n_groups):
        out.append({"role": "user", "content": f"user {i}"})
        out.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call-{i}", "function": {"name": "read_file"}}],
        })
        out.append({
            "role": "tool",
            "tool_call_id": f"call-{i}",
            "content": f"result {i}",
        })
    return out


def test_no_op_below_threshold():
    cfg = Config()
    cfg.messages = _msgs_with_user_assistant_tool_pattern(10)  # 30 msgs, below 80
    before = list(cfg.messages)

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 0
    assert cfg.messages == before


def test_no_op_when_no_tool_messages_present():
    cfg = Config()
    cfg.messages = [{"role": "user", "content": f"u{i}"} for i in range(100)]
    before = list(cfg.messages)

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 0
    assert cfg.messages == before


def test_no_op_when_all_tool_messages_in_keep_window():
    cfg = Config()
    # 60 user/assistant filler + 30 tool msgs at the tail (within keep_recent=40)
    msgs: list[dict] = [{"role": "user", "content": f"u{i}"} for i in range(60)]
    msgs.extend(
        {"role": "tool", "tool_call_id": f"c{i}", "content": f"t{i}"} for i in range(30)
    )
    cfg.messages = msgs

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 0


def test_drops_old_tool_messages_outside_keep_window():
    cfg = Config()
    cfg.messages = _msgs_with_user_assistant_tool_pattern(40)  # 120 msgs
    # With default 80/40: total=120, keep_from=80. Indices 0..79 prunable.
    # Pattern: every 3rd message starting at index 2 is a tool message.
    # Tool messages in [0, 80): indices 2, 5, 8, ..., 77 → 26 tool messages.

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 26
    assert len(cfg.messages) == 120 - 26
    # Keep window preserved exactly.
    assert cfg.messages[-40:] == _msgs_with_user_assistant_tool_pattern(40)[-40:]
    # No tool messages remain in the pruned prefix.
    pruned_prefix = cfg.messages[:-40]
    assert all(m.get("role") != "tool" for m in pruned_prefix)


def test_strips_matching_tool_calls_from_assistant_messages():
    cfg = Config()
    cfg.messages = _msgs_with_user_assistant_tool_pattern(40)

    main.prune_stale_tool_messages(cfg)

    # After pruning, no assistant should retain a tool_calls entry that
    # references a tool_call_id no longer present in cfg.messages.
    surviving_tool_ids = {
        msg.get("tool_call_id") for msg in cfg.messages if msg.get("role") == "tool"
    }
    for msg in cfg.messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls", []) or []:
            from algo_cli.context_budget import _tool_call_id

            assert _tool_call_id(call) in surviving_tool_ids, (
                f"orphaned tool_call survived prune: {call}"
            )


def test_strips_tool_calls_when_pruned_tool_result_has_no_id():
    cfg = Config()
    cfg.prune_after_messages = 10
    cfg.prune_keep_recent = 5
    cfg.messages = [
        {"role": "user", "content": "u0"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "x"}}}],
        },
        {"role": "tool", "content": "old result without id"},
    ]
    cfg.messages.extend({"role": "user", "content": f"tail {i}"} for i in range(20))

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 1
    assistant = next(msg for msg in cfg.messages if msg.get("role") == "assistant")
    assert "tool_calls" not in assistant


def test_partial_strip_when_assistant_has_multiple_tool_calls():
    cfg = Config()
    # Fill below the prunable window so only the first assistant is candidate.
    cfg.messages = [
        {"role": "user", "content": "u0"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "keep-1", "function": {"name": "read_file"}},
                {"id": "drop-1", "function": {"name": "read_file"}},
            ],
        },
        {"role": "tool", "tool_call_id": "drop-1", "content": "drop me"},
        {"role": "tool", "tool_call_id": "keep-1", "content": "keep me too — but in recent window"},
    ]
    # Pad with 80 recent messages so the keep window covers the second tool result.
    cfg.messages.extend({"role": "user", "content": f"u{i}"} for i in range(80))
    # total=84, keep_from=44. First tool (drop-1) at index 2 is prunable. Second tool
    # (keep-1) at index 3 is also prunable. Both get dropped.

    main.prune_stale_tool_messages(cfg)

    # The assistant message lost both entries → tool_calls is gone entirely.
    asst = next(m for m in cfg.messages if m.get("role") == "assistant")
    assert "tool_calls" not in asst
    # Both tool messages dropped.
    assert not any(m.get("role") == "tool" for m in cfg.messages)


def test_emits_perf_event_only_on_actual_prune(monkeypatch):
    cfg = Config()
    cfg.messages = _msgs_with_user_assistant_tool_pattern(40)
    events: list[dict] = []
    from algo_cli import dorothy_perf_telemetry as perf_telemetry

    monkeypatch.setattr(
        perf_telemetry,
        "record_perf_event",
        lambda event, **fields: events.append({"event": event, **fields}),
    )

    main.prune_stale_tool_messages(cfg)
    assert any(e["event"] == "prune" and e["removed"] > 0 for e in events)

    # Second call: nothing to prune now. No event should fire.
    events.clear()
    main.prune_stale_tool_messages(cfg)
    assert events == []


def test_respects_custom_threshold_and_keep_recent():
    cfg = Config()
    cfg.prune_after_messages = 10
    cfg.prune_keep_recent = 5
    cfg.messages = _msgs_with_user_assistant_tool_pattern(5)  # 15 msgs, exceeds 10

    removed = main.prune_stale_tool_messages(cfg)

    assert removed > 0
    assert len(cfg.messages) == 15 - removed
    # Most recent 5 messages preserved exactly.
    assert cfg.messages[-5:] == _msgs_with_user_assistant_tool_pattern(5)[-5:]
