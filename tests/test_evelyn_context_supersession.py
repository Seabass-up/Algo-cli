"""Semantic supersession keeps provider transcripts valid while saving tokens."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from algo_cli import chatgpt_client, evelyn_context_supersession as context_supersession, main, xai_client
from algo_cli.config import Config


_MISSING = object()


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


def _external_exchange(
    call_id: str,
    url: str,
    content: str,
    *,
    binding: object = _MISSING,
    assistant_binding: object = _MISSING,
    argument_binding: object = _MISSING,
) -> list[dict]:
    arguments: dict = {"url": url}
    if argument_binding is not _MISSING:
        arguments[context_supersession.TARGET_EPOCH_FIELD] = argument_binding
    assistant: dict = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "arguments": arguments,
                },
            }
        ],
    }
    if assistant_binding is not _MISSING:
        assistant[context_supersession.TARGET_EPOCH_FIELD] = assistant_binding
    result: dict = {
        "role": "tool",
        "name": "web_fetch",
        "tool_name": "web_fetch",
        "tool_call_id": call_id,
        "content": content,
    }
    if binding is not _MISSING:
        result[context_supersession.TARGET_EPOCH_FIELD] = binding
    return [assistant, result]


def _binding(
    *,
    target: str = "https://private.example/account",
    epoch: int = 1,
    revision: str = "loader-a",
    fencing_token: int = 1,
    target_kind: str = "external_resource",
) -> context_supersession.ExternalTargetEpoch:
    return context_supersession.issue_external_target_epoch(
        target_kind=target_kind,
        target=target,
        epoch=epoch,
        revision=revision,
        fencing_token=fencing_token,
    )


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
    assert all(not context_supersession.is_supersession_receipt(message.get("content")) for message in messages)


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
                        "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": {"path": "file.py"}}}],
                    },
                    {"role": "tool", "name": name, "tool_call_id": call_id, "content": content},
                ]
            )
    before = copy.deepcopy(messages)

    stats = context_supersession.supersede_tool_results(messages, cwd="/workspace")

    assert stats.superseded == 0
    assert messages == before


def test_long_successful_verifier_collapses_to_hash_and_final_excerpt() -> None:
    raw = ("test_module.py::test_case PASSED\n" * 100) + "100 passed\n[exit code: 0]"
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "verify-long",
                    "function": {
                        "name": "run_shell",
                        "arguments": {"command": "pytest -q"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "run_shell",
            "tool_call_id": "verify-long",
            "content": raw,
        },
    ]

    stats = context_supersession.supersede_tool_results(messages, cwd="/workspace")

    receipt = messages[1]["content"]
    assert stats.superseded == 1
    assert stats.saved_tokens > 0
    assert context_supersession.is_verification_receipt(receipt)
    assert hashlib.sha256(raw.encode()).hexdigest() in receipt
    assert "100 passed" in receipt
    assert "[exit code: 0]" in receipt


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
    from algo_cli import context_budget, dorothy_perf_telemetry as perf_telemetry

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


def test_external_epoch_round_trip_is_signed_and_content_free() -> None:
    raw_target = "https://private.example/account?token=never-persist-this"
    binding = _binding(target=raw_target)
    payload = binding.to_dict()

    assert context_supersession.parse_external_target_epoch(payload) == binding
    assert raw_target not in json.dumps(payload, sort_keys=True)
    assert payload["target_id"].startswith("hmac-sha256:")
    assert payload["auth_tag"].startswith("hmac-sha256:")
    assert set(payload) == {
        "schema_version",
        "target_kind",
        "target_id",
        "epoch",
        "revision",
        "fencing_token",
        "auth_tag",
    }


def test_external_results_require_result_side_epoch_metadata() -> None:
    binding = _binding().to_dict()
    raw = "private external observation\n" * 1_000
    messages = [
        *_external_exchange(
            "old",
            "https://private.example/account",
            raw,
            assistant_binding=binding,
            argument_binding=binding,
        ),
        *_external_exchange(
            "new",
            "https://private.example/account",
            raw,
            assistant_binding=binding,
            argument_binding=binding,
        ),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 0
    assert messages[1]["content"] == raw
    assert messages[3]["content"] == raw


def test_captured_binding_cannot_be_replayed_for_another_invocation_target() -> None:
    captured = _binding(target="https://a.example/private").to_dict()
    url = "https://b.example/private"
    messages = [
        *_external_exchange("old", url, "old\n" * 1_000, binding=captured),
        *_external_exchange("new", url, "new\n" * 1_000, binding=captured),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 0
    assert messages[1]["content"].startswith("old")


def test_same_authenticated_external_epoch_uses_hmac_receipt() -> None:
    raw_target = "https://private.example/account?customer=secret"
    binding = _binding(target=raw_target).to_dict()
    old_content = "private external observation\n" * 1_000
    new_content = "new private external observation\n" * 1_000
    messages = [
        *_external_exchange("old", raw_target, old_content, binding=binding),
        *_external_exchange("new", raw_target, new_content, binding=binding),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    receipt = messages[1]["content"]
    assert stats.superseded == 1
    assert stats.saved_tokens > 1_000
    assert receipt.startswith(context_supersession.RECEIPT_PREFIX)
    assert "content_id=hmac-sha256:" in receipt
    assert " sha256=" not in receipt
    assert hashlib.sha256(old_content.encode()).hexdigest() not in receipt
    assert raw_target not in receipt
    assert "private external observation" not in receipt
    assert messages[1][context_supersession.TARGET_EPOCH_FIELD] == binding
    assert messages[3]["content"] == new_content
    for provider_payload in (
        chatgpt_client._build_openai_messages(messages),
        chatgpt_client._build_responses_input(messages),
        xai_client._build_openai_messages(messages),
    ):
        rendered = json.dumps(provider_payload, sort_keys=True)
        assert context_supersession.TARGET_EPOCH_FIELD not in rendered
        assert binding["target_id"] not in rendered
        assert binding["auth_tag"] not in rendered


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        (_binding(), _binding(target="https://other.example/account")),
        (_binding(), _binding(epoch=2, revision="loader-b")),
        (_binding(), _binding(revision="loader-b", fencing_token=2)),
        (_binding(), _binding(fencing_token=2)),
        (_binding(), _binding(target_kind="browser_document")),
    ],
    ids=["target", "epoch", "revision", "fence", "tool-kind"],
)
def test_external_binding_changes_do_not_cross_supersede(
    older: context_supersession.ExternalTargetEpoch,
    newer: context_supersession.ExternalTargetEpoch,
) -> None:
    url = "https://private.example/account"
    messages = [
        *_external_exchange("old", url, "old\n" * 1_000, binding=older.to_dict()),
        *_external_exchange("new", url, "new\n" * 1_000, binding=newer.to_dict()),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 0
    assert not context_supersession.is_supersession_receipt(messages[1]["content"])


def test_unbound_observation_is_a_barrier_between_valid_epochs() -> None:
    binding = _binding().to_dict()
    url = "https://private.example/account"
    messages = [
        *_external_exchange("a-1", url, "a1\n" * 1_000, binding=binding),
        *_external_exchange("a-2", url, "a2\n" * 1_000, binding=binding),
        *_external_exchange("unbound", url, "unknown\n" * 1_000),
        *_external_exchange("a-3", url, "a3\n" * 1_000, binding=binding),
        *_external_exchange("a-4", url, "a4\n" * 1_000, binding=binding),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 2
    assert context_supersession.is_supersession_receipt(messages[1]["content"])
    assert messages[3]["content"].startswith("a2")
    assert messages[5]["content"].startswith("unknown")
    assert context_supersession.is_supersession_receipt(messages[7]["content"])
    assert messages[9]["content"].startswith("a4")


def test_unpaired_external_protocol_message_disables_external_compaction() -> None:
    binding = _binding().to_dict()
    url = "https://private.example/account"
    messages = [
        *_external_exchange("paired-1", url, "one\n" * 1_000, binding=binding),
        {
            "role": "tool",
            "name": "web_fetch",
            "tool_call_id": "missing-call",
            "content": "unpaired\n" * 1_000,
            context_supersession.TARGET_EPOCH_FIELD: binding,
        },
        *_external_exchange("paired-2", url, "two\n" * 1_000, binding=binding),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 0
    assert messages[1]["content"].startswith("one")


def test_target_identity_round_trip_is_segmented() -> None:
    url = "https://private.example/account"
    target_a = _binding(target=url).to_dict()
    target_b = _binding(target="https://redirect.example/account").to_dict()
    messages = [
        *_external_exchange("a-1", url, "a1\n" * 1_000, binding=target_a),
        *_external_exchange("b", url, "b\n" * 1_000, binding=target_b),
        *_external_exchange("a-2", url, "a2\n" * 1_000, binding=target_a),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 0


@pytest.mark.parametrize(
    "bindings",
    [
        [_binding(epoch=2, revision="loader-b"), _binding(epoch=1)],
        [_binding(fencing_token=2), _binding(fencing_token=1)],
        [_binding(revision="loader-a"), _binding(revision="loader-b")],
    ],
    ids=["epoch-regression", "fence-regression", "revision-without-advance"],
)
def test_external_epoch_regression_invalidates_all_target_compaction(
    bindings: list[context_supersession.ExternalTargetEpoch],
) -> None:
    url = "https://private.example/account"
    first = bindings[0].to_dict()
    messages = [
        *_external_exchange("same-1", url, "one\n" * 1_000, binding=first),
        *_external_exchange("same-2", url, "two\n" * 1_000, binding=first),
        *_external_exchange("regressed", url, "three\n" * 1_000, binding=bindings[1].to_dict()),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 0
    assert all(not context_supersession.is_supersession_receipt(message.get("content")) for message in messages)


def test_one_regressed_target_does_not_block_another_target() -> None:
    url_a = "https://a.example/private"
    url_b = "https://b.example/private"
    a_high = _binding(target=url_a, epoch=2, revision="loader-2").to_dict()
    a_low = _binding(target=url_a, epoch=1, revision="loader-1").to_dict()
    b = _binding(target=url_b).to_dict()
    messages = [
        *_external_exchange("a-high", url_a, "a high\n" * 1_000, binding=a_high),
        *_external_exchange("b-1", url_b, "b one\n" * 1_000, binding=b),
        *_external_exchange("a-low", url_a, "a low\n" * 1_000, binding=a_low),
        *_external_exchange("b-2", url_b, "b two\n" * 1_000, binding=b),
    ]

    stats = context_supersession.supersede_tool_results(messages)

    assert stats.superseded == 1
    assert messages[1]["content"].startswith("a high")
    assert context_supersession.is_supersession_receipt(messages[3]["content"])
    assert messages[5]["content"].startswith("a low")
    assert messages[7]["content"].startswith("b two")


def test_malformed_external_bindings_fail_closed_without_crashing() -> None:
    valid = _binding().to_dict()
    malformed: list[object] = [
        None,
        {key: value for key, value in valid.items() if key != "auth_tag"},
        {**valid, "extra": "field"},
        {**valid, "epoch": True},
        {**valid, "epoch": 2},
        {**valid, "revision": "x" * 129},
        {**valid, "auth_tag": "hmac-sha256:" + ("0" * 64)},
        [valid],
    ]

    class DictSubclass(dict):
        pass

    malformed.append(DictSubclass(valid))
    for index, candidate in enumerate(malformed):
        assert context_supersession.parse_external_target_epoch(candidate) is None
        messages = [
            *_external_exchange(
                f"old-{index}",
                "https://private.example/account",
                "old\n" * 1_000,
                binding=candidate,
            ),
            *_external_exchange(
                f"new-{index}",
                "https://private.example/account",
                "new\n" * 1_000,
                binding=candidate,
            ),
        ]
        assert context_supersession.supersede_tool_results(messages).superseded == 0


def test_target_epoch_key_change_fails_closed(monkeypatch) -> None:
    payload = _binding().to_dict()

    monkeypatch.setattr(
        context_supersession,
        "keyed_action_fingerprint",
        lambda _action, _args: "hmac-sha256:" + ("f" * 64),
    )

    assert context_supersession.parse_external_target_epoch(payload) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_kind": "unknown"},
        {"target_kind": 1},
        {"target": ""},
        {"target": "https://example.test/\nprivate"},
        {"target": "https://example.test/\u200bprivate"},
        {"target": "\ud800"},
        {"target": "x" * 8_193},
        {"epoch": True},
        {"epoch": 0},
        {"epoch": 1 << 63},
        {"revision": ""},
        {"revision": "x" * 129},
        {"fencing_token": True},
        {"fencing_token": -1},
        {"fencing_token": 1 << 63},
    ],
)
def test_external_epoch_issuer_rejects_invalid_fields(overrides: dict) -> None:
    fields: dict = {
        "target_kind": "external_resource",
        "target": "https://private.example/account",
        "epoch": 1,
        "revision": "loader-a",
        "fencing_token": 1,
    }
    fields.update(overrides)

    with pytest.raises(ValueError):
        context_supersession.issue_external_target_epoch(**fields)


def test_count_pruning_never_deletes_external_observations() -> None:
    binding = _binding().to_dict()
    cfg = Config()
    cfg.prune_after_messages = 4
    cfg.prune_keep_recent = 2
    cfg.messages = [
        *_external_exchange(
            "external-old",
            "https://private.example/account",
            "old\n" * 1_000,
            binding=binding,
        ),
        *_external_exchange(
            "external-new",
            "https://private.example/account",
            "new\n" * 1_000,
            binding=binding,
        ),
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "done"},
    ]

    removed = main.prune_stale_tool_messages(cfg)

    assert removed == 0
    assert len(cfg.messages) == 6
    assert context_supersession.is_supersession_receipt(cfg.messages[1]["content"])
    assert cfg.messages[3]["content"].startswith("new")
