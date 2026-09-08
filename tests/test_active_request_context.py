"""Keep runtime control messages distinct from the original active request."""

import copy

import pytest

from algo_cli import ada_memory_echo_veil as echo, context_budget, main
from algo_cli.config import Config
from algo_cli.tool_schema import estimate_tool_schema_tokens
from test_main_helpers import _patch_agent_loop_for_tool_policy_test


@pytest.mark.parametrize("oneshot", [False, True])
@pytest.mark.parametrize("protection", ["optional", "required"])
@pytest.mark.parametrize(
    "original",
    ["[Internal recovery boundary] is the literal text I need explained.", "", "Reply with exactly: CONTEXT_OK"],
)
def test_explicit_request_controls_recall_even_with_later_runtime_messages(monkeypatch, oneshot, protection, original):
    queries = []
    doctors = []
    monkeypatch.setattr(context_budget, "json_sink", lambda: object() if oneshot else None)
    monkeypatch.setattr(context_budget.identity, "build_identity_block", lambda **_kwargs: "immutable identity")
    monkeypatch.setattr(echo, "recall_with_echo_veil", lambda _cfg, query, **_kw: queries.append(query) or [])
    monkeypatch.setattr(echo, "protected_prompt_context", lambda _cfg, query, **_kw: queries.append(query) or "")
    monkeypatch.setattr(
        echo,
        "get_echo_veil_readiness",
        lambda _cfg, **_kw: (
            doctors.append(True)
            or {
                "healthy": True,
                "all_records_shielded": True,
                "local_protection_ready": True,
                "protection_policy": "required",
            }
        ),
    )
    cfg = Config(
        model="test",
        echo_veil_enabled=True,
        echo_veil_protection=protection,
        messages=[{"role": "user", "content": "[Internal finalization turn] Stop using tools."}],
        memories=["PLAINTEXT_FALLBACK_CANARY"],
        session_summary="PLAINTEXT_SUMMARY_CANARY",
    )

    prompt = context_budget.build_system_prompt(cfg, user_message=original)

    exact_required = protection == "required" and original.startswith("Reply with exactly:")
    assert queries == ([] if not original or exact_required else [original])
    assert doctors == ([True] if exact_required else [])
    assert "PLAINTEXT_FALLBACK_CANARY" not in prompt
    assert "PLAINTEXT_SUMMARY_CANARY" not in prompt


@pytest.mark.parametrize("boundary", ["recovery", "finalization"])
@pytest.mark.parametrize("compacted", [False, True])
@pytest.mark.parametrize("protection", ["optional", "required"])
def test_agent_loop_keeps_request_and_control_boundaries_separate(
    monkeypatch, tmp_path, boundary, compacted, protection
):
    _patch_agent_loop_for_tool_policy_test(monkeypatch)
    original = "Inspect the project using the current protected decision."
    queries, requests, rounds, admissions, errors, invoked = [], [], [], [], [], []

    class Sink:
        def model_round(self, **fields):
            rounds.append(fields)

    class Client:
        def chat(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) > 1:
                return iter([{"message": {"content": "Observed the available evidence; no unexecuted work claimed."}}])
            name = "run_shell" if boundary == "recovery" else "read_file"
            args = {"command": "pytest -q"} if boundary == "recovery" else {"path": "README.md"}
            return iter(
                [{"message": {"tool_calls": [{"id": "inspect", "function": {"name": name, "arguments": args}}]}}]
            )

    def compact(_client, cfg, **_kwargs):
        if not compacted or not requests:
            return False
        return context_budget.maybe_compact_context(_client, cfg, precomputed_used=cfg.num_ctx)

    def fitted(base, blocks, **kwargs):
        admissions.append((kwargs["base_used_tokens"], list(cfg.messages)))
        return context_budget.fit_optional_context_blocks(base, blocks, **kwargs)

    sink = Sink()
    original_approval = main.ask_approval
    monkeypatch.setattr(main, "json_sink", lambda: sink)
    monkeypatch.setattr(context_budget, "json_sink", lambda: sink)
    monkeypatch.setattr(context_budget.identity, "build_identity_block", lambda **_kw: "immutable identity")
    monkeypatch.setattr(echo, "recall_with_echo_veil", lambda _cfg, query, **_kw: queries.append(query) or [])
    monkeypatch.setattr(echo, "protected_prompt_context", lambda _cfg, query, **_kw: queries.append(query) or "")
    monkeypatch.setattr(
        main.reconciliation,
        "guidance_for_prompt",
        lambda text: "RETRIEVED_NAVIGATION_CANARY" if text == original else "",
    )
    monkeypatch.setattr(main, "maybe_compact_context", compact)
    monkeypatch.setattr(context_budget, "context_compaction_policy", lambda _info: (0.5, 3))
    monkeypatch.setattr(context_budget, "summarize_message_batch", lambda *_args, **_kw: "PLAINTEXT_SUMMARY_CANARY")
    monkeypatch.setattr(Config, "save", lambda _cfg: None)
    monkeypatch.setattr(main, "fit_optional_context_blocks", fitted)
    monkeypatch.setattr(
        main,
        "ask_approval",
        lambda name, args, cfg, **kw: original_approval(name, args, cfg, **kw) if name == "read_file" else False,
    )
    monkeypatch.setattr(main, "run_tool", lambda name, *_args, **_kw: invoked.append(name) or "README contents")
    monkeypatch.setattr(main, "record_perf_event", lambda *_args, **_kw: None)
    monkeypatch.setattr(main, "show_error", errors.append)
    monkeypatch.setattr(main.memory_runtime, "capture_completed_user_turn", lambda *_args, **_kw: {"status": "skipped"})
    cfg = Config(
        cwd=str(tmp_path),
        model="test",
        num_ctx=131072,
        model_adaptive=False,
        safe_mode=True,
        max_tool_iterations=1 if boundary == "finalization" else 3,
        tool_think_every=100,
        skill_crystallize_enabled=False,
        memory_auto_capture_enabled=False,
        code_rag_enabled=False,
        echo_veil_enabled=True,
        echo_veil_protection=protection,
        messages=[{"role": "user", "content": original}, {"role": "assistant", "content": "A prior completed turn."}],
    )

    main.agent_loop(Client(), cfg, original)

    assert len(requests) == len(rounds) == 2
    assert queries == [original] * (3 if compacted else 2)
    assert errors == []
    assert invoked == ([] if boundary == "recovery" else ["read_file"])
    for index, request in enumerate(requests):
        messages = request["messages"]
        users = [message["content"] for message in messages if message["role"] == "user"]
        active = [content for content in users if content.startswith(original)]
        assert len(active) == (1 if compacted and index == 1 else 2)
        expect_optional = boundary == "recovery" or index == 0
        assert sum("RETRIEVED_NAVIGATION_CANARY" in content for content in active) == int(expect_optional)
        if index == 1:
            assert users[-1].startswith(f"[Internal {boundary}")
            assert "RETRIEVED_NAVIGATION_CANARY" not in users[-1]
        estimated = sum(context_budget.estimate_message_tokens(message) for message in messages)
        estimated += estimate_tool_schema_tokens(request["tools"])
        assert sum(rounds[index]["context_sources"].values()) == estimated
        assert "PLAINTEXT_SUMMARY_CANARY" not in str(messages)
    assert "RETRIEVED_NAVIGATION_CANARY" not in str(cfg.messages)
    if compacted:
        assert all(message.get("content") != original for message in cfg.messages)
        # Restoration is provider-only and its envelope/body must consume budget.
        restored_cost = context_budget.estimate_message_tokens({"role": "user", "content": original})
        before, previous = admissions[-2]
        after, kept = admissions[-1]
        removed = previous[: -len(kept)]
        assert (
            after
            == before - sum(context_budget.estimate_message_tokens(message) for message in removed) + restored_cost
        )
