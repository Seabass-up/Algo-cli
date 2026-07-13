"""Semantic supersession keeps provider transcripts valid while saving tokens."""

from __future__ import annotations

import copy
import hashlib

from algo_cli import chatgpt_client, context_supersession, main, xai_client
from algo_cli.config import Config


def _read_exchange(call_id: str, path: str, content: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "thinking": f"signature-bound-{call_id}",
            "thought_signature": f"sig-{call_id}",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": path},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "read_file",
            "tool_name": "read_file",
            "tool_call_id": call_id,
            "content": content,
        },
    ]


def test_repeated_reads_save_measurable_tokens_and_keep_protocol_pairs() -> None:
    raw = "x" * 20_000
    messages: list[dict] = []
    for index in range(5):
        messages.extend(_read_exchange(f"call-{index}", "README.md", raw))
    original_assistants = copy.deepcopy(messages[::2])

    stats = context_supersession.supersede_tool_results(messages, cwd="/workspace")

    assert stats.superseded == 4
    assert stats.saved_tokens > 19_000
    assert stats.reduction_pct >= 75.0
    assert len(messages) == 10
    assert messages[::2] == original_assistants
    assert messages[-1]["content"] == raw
    expected_hash = hashlib.sha256(raw.encode()).hexdigest()
    for result in messages[1:-1:2]:
        assert result["content"].startswith(context_supersession.RECEIPT_PREFIX)
        assert f"sha256={expected_hash}" in result["content"]
        assert "bytes=20000" in result["content"]

    openai_chat = chatgpt_client._build_openai_messages(messages)
    assert len([item for item in openai_chat if item.get("role") == "tool"]) == 5
    responses = chatgpt_client._build_responses_input(messages)
    assert len([item for item in responses if item.get("type") == "function_call"]) == 5
    assert len([item for item in responses if item.get("type") == "function_call_output"]) == 5
    xai_chat = xai_client._build_openai_messages(messages)
    assert len([item for item in xai_chat if item.get("role") == "tool"]) == 5


def test_supersession_is_idempotent() -> None:
    messages = [
        *_read_exchange("old", "a.py", "old snapshot\n" * 1_000),
        *_read_exchange("new", "a.py", "new snapshot\n" * 1_000),
    ]

    first = context_supersession.supersede_tool_results(messages, cwd="/workspace")
    after_first = copy.deepcopy(messages)
    second = context_supersession.supersede_tool_results(messages, cwd="/workspace")

    assert first.superseded == 1
    assert second.superseded == 0
    assert second.saved_tokens == 0
    assert messages == after_first


def test_exact_snapshot_key_does_not_merge_different_ranges_or_paths() -> None:
    messages = [
        *_read_exchange("a1", "a.py", "a" * 2_000),
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "a2",
                    "function": {
                        "name": "read_file",
                        "arguments": {"path": "a.py", "start_line": 20},
                    },
                }
            ],
        },
        {"role": "tool", "name": "read_file", "tool_call_id": "a2", "content": "b" * 2_000},
        *_read_exchange("b1", "b.py", "c" * 2_000),
    ]

    stats = context_supersession.supersede_tool_results(messages, cwd="/workspace")

    assert stats.superseded == 0
    assert all(
        not context_supersession.is_supersession_receipt(message.get("content"))
        for message in messages
    )


def test_failures_mutations_and_verification_evidence_are_never_superseded() -> None:
    messages: list[dict] = []
    for index in range(2):
        messages.extend(_read_exchange(f"read-{index}", "missing.py", "Error: file not found"))
        for name, content in (
            ("edit_file", f"Edited file.py pass {index}"),
            ("run_shell", f"pytest pass {index}\n[exit code: 0]"),
            ("git_diff", f"diff evidence {index}"),
        ):
            call_id = f"{name}-{index}"
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": call_id, "function": {"name": name, "arguments": {"path": "file.py"}}}
                        ],
                    },
                    {"role": "tool", "name": name, "tool_call_id": call_id, "content": content},
                ]
            )
    before = copy.deepcopy(messages)

    stats = context_supersession.supersede_tool_results(messages, cwd="/workspace")

    assert stats.superseded == 0
    assert messages == before


def test_mutation_epoch_preserves_pre_mutation_snapshot() -> None:
    baseline = "baseline\n" * 2_000
    changed_once = "changed once\n" * 2_000
    changed_twice = "changed twice\n" * 2_000
    messages = [
        *_read_exchange("before", "config.py", baseline),
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "edit",
                    "function": {
                        "name": "edit_file",
                        "arguments": {"path": "config.py", "old_string": "a", "new_string": "b"},
                    },
                }
            ],
        },
        {"role": "tool", "name": "edit_file", "tool_call_id": "edit", "content": "Edited config.py"},
        *_read_exchange("after-one", "config.py", changed_once),
        *_read_exchange("after-two", "config.py", changed_twice),
    ]

    stats = context_supersession.supersede_tool_results(messages, cwd="/workspace")

    assert stats.superseded == 1
    assert messages[1]["content"] == baseline
    assert context_supersession.is_supersession_receipt(messages[5]["content"])
    assert messages[7]["content"] == changed_twice


def test_prune_integrates_supersession_and_emits_token_savings(monkeypatch) -> None:
    cfg = Config()
    cfg.cwd = "/workspace"
    cfg.messages = [
        *_read_exchange("old", "README.md", "old\n" * 4_000),
        *_read_exchange("new", "README.md", "new\n" * 4_000),
    ]
    events: list[dict] = []
    from algo_cli import context_budget, perf_telemetry

    context_budget.CONTEXT_USAGE_CACHE = (("stale",), 99_999)

    monkeypatch.setattr(
        perf_telemetry,
        "record_perf_event",
        lambda event, **fields: events.append({"event": event, **fields}),
    )

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 0
    assert context_budget.CONTEXT_USAGE_CACHE is None
    assert context_supersession.is_supersession_receipt(cfg.messages[1]["content"])
    event = next(item for item in events if item["event"] == "semantic_supersession")
    assert event["superseded"] == 1
    assert event["saved_tokens"] > 1_000
    assert event["after_tokens"] < event["before_tokens"]


def test_count_pruning_protects_mutation_and_verification_receipts() -> None:
    cfg = Config()
    cfg.prune_after_messages = 4
    cfg.prune_keep_recent = 2
    cfg.messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "write-1", "function": {"name": "edit_file", "arguments": {"path": "a.py"}}},
                {"id": "verify-1", "function": {"name": "run_shell", "arguments": {"command": "pytest -q"}}},
            ],
        },
        {"role": "tool", "name": "edit_file", "tool_call_id": "write-1", "content": "Edited a.py"},
        {"role": "tool", "name": "run_shell", "tool_call_id": "verify-1", "content": "1 passed\n[exit code: 0]"},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "done"},
    ]
    before = copy.deepcopy(cfg.messages)

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 0
    assert cfg.messages == before
