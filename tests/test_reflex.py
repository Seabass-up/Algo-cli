"""Tests for reflex loop v0.1."""

from __future__ import annotations

from algo_cli import reflex
from algo_cli.config import Config
from algo_cli.nathan_runtime import record_tool_attempt, tool_runtime_args


def test_detect_loop_on_third_same_signature():
    cfg = Config()
    sig = reflex.tool_signature("harness_search", {"query": "foo"})
    cfg.attempt_ledger = [
        {"signature": sig, "status": "failed"},
        {"signature": sig, "status": "failed"},
    ]
    trigger = reflex.detect_trigger(cfg, "harness_search", {"query": "foo"}, "Error: x", "failed")
    assert trigger is not None
    assert trigger.label == "loop_detected"


def test_maybe_augment_disabled_by_default():
    cfg = Config(reflex_enabled=False)
    result, note = reflex.maybe_augment_tool_result(
        cfg, "harness_search", {"query": "x"}, "No harness matches.", "worked"
    )
    assert result == "No harness matches."
    assert note is None


def test_maybe_augment_on_failed_search():
    cfg = Config(reflex_enabled=True)
    sig = reflex.tool_signature("harness_search", {"query": "algo-cli reflex"})
    cfg.attempt_ledger = [{"signature": sig, "status": "worked"}]
    result, note = reflex.maybe_augment_tool_result(
        cfg,
        "harness_search",
        {"query": "algo-cli reflex"},
        "No harness matches.",
        "worked",
    )
    assert "---" in result
    assert note is not None
    assert reflex._reflex_cycles(cfg) == 1


def test_reflex_plans_recovery_without_direct_tool_execution(monkeypatch):
    from algo_cli import tools

    monkeypatch.setattr(
        tools,
        "harness_search",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("direct action bypass")),
    )
    monkeypatch.setattr(
        tools,
        "available_actions",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("direct action bypass")),
    )
    cfg = Config(reflex_enabled=True)

    result, note = reflex.maybe_augment_tool_result(
        cfg,
        "harness_search",
        {"query": "algo-cli reflex"},
        "Error: unavailable",
        "failed",
    )

    assert "canonical dispatcher" in result
    assert note is not None


def test_runtime_default_args_share_attempt_signature_with_reflex(tmp_path):
    cfg = Config(reflex_enabled=True, cwd=tmp_path)
    raw_args = {"path": "note.md"}
    runtime_args = tool_runtime_args("read_file", raw_args, cfg)
    record_tool_attempt(
        cfg,
        name="read_file",
        args=runtime_args,
        result="No matches",
        status="worked",
    )

    trigger = reflex.detect_trigger(cfg, "read_file", runtime_args, "No matches", "worked")

    assert trigger is not None
    assert trigger.label == "empty_repeat"
    signature = reflex.tool_signature("read_file", runtime_args)
    assert signature.startswith("hmac-sha256:")
    assert "note.md" not in signature
    assert str(tmp_path) not in signature


def test_repeated_search_miss_uses_specific_trigger():
    cfg = Config(reflex_enabled=True)
    args = {"query": "missing"}
    cfg.attempt_ledger = [{"signature": reflex.tool_signature("harness_search", args)}]

    trigger = reflex.detect_trigger(cfg, "harness_search", args, "No harness matches", "worked")

    assert trigger is not None
    assert trigger.label == "search_miss"


def test_git_not_repo_is_benign():
    assert reflex._is_benign_exploration_failure(
        "git_status",
        {},
        "Error: git status failed (128): fatal: not a git repository",
    )


def test_read_file_algo_cli_path_from_home_is_benign():
    assert reflex._is_benign_exploration_failure(
        "read_file",
        {"path": "algo_cli/reflex.py"},
        "Error: file not found: C:\\Users\\example\\algo_cli\\reflex.py",
    )


def test_session_cap_warns_once_without_polluting_tool_output():
    cfg = Config(reflex_enabled=True)
    cfg.context_state[reflex.REFLEX_LEDGER_CONTEXT_KEY] = reflex.REFLEX_MAX_CYCLES
    result1, note1 = reflex.maybe_augment_tool_result(
        cfg, "read_file", {"path": "missing.md"}, "Error: not found", "failed"
    )
    result2, note2 = reflex.maybe_augment_tool_result(
        cfg, "read_file", {"path": "missing.md"}, "Error: not found", "failed"
    )
    assert result1 == "Error: not found"
    assert note1 is not None
    assert "cap" in note1
    assert result2 == "Error: not found"
    assert note2 is None
