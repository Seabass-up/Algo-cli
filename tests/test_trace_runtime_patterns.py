"""Tests for Fable-5 and /System runtime pattern crystallization."""
from __future__ import annotations

from pathlib import Path
import json

from algo_cli import tool_runtime
from algo_cli.capability_mask import Capability, CapabilityMask, tier_mask
from algo_cli.config import Config
from algo_cli.tools import (
    capability_mask_describe,
    runtime_qos_hint,
    screenshot_description_verify,
    small_context_ledger_preview,
)
from algo_cli.evals.cot_quality import SequencePattern, score_tool_sequence
from algo_cli.evals.session_distribution import summarize_session_distribution
from algo_cli.perf_telemetry import runtime_quality_snapshot
from algo_cli.runtime_qos import SpawnClass, classify_tool_runtime, named_tool_log_path
from algo_cli.vision_screenshot_verify import verify_screenshot_description


def test_tool_sequence_detects_tdd_cadence() -> None:
    result = score_tool_sequence(["Edit", "Bash", "Edit"])

    assert result.pattern == SequencePattern.TDD_EDIT_TEST_EDIT
    assert result.sequence_score >= 0.9
    assert result.verification_present is True


def test_tool_sequence_detects_bash_read_loop() -> None:
    result = score_tool_sequence(["Bash", "Bash", "Read"])

    assert result.pattern == SequencePattern.SHELL_INSPECT_LOOP
    assert result.sequence_score >= 0.5


def test_tool_sequence_empty_safe() -> None:
    result = score_tool_sequence([])

    assert result.pattern == SequencePattern.EMPTY
    assert result.sequence_score == 0.0


def test_runtime_quality_snapshot_uses_attempt_ledger_without_private_reasoning() -> None:
    cfg = Config()
    cfg.attempt_ledger = [
        {"tool": "edit_file"},
        {"tool": "run_shell"},
        {"tool": "edit_file"},
    ]

    snapshot = runtime_quality_snapshot(cfg)

    assert snapshot["tool_sequence"]["pattern"] == "tdd_edit_test_edit"
    assert snapshot["tool_sequence"]["verification_present"] is True
    assert snapshot["reasoning_quality"]["status"] == "not_collected"


def test_screenshot_description_verification() -> None:
    result = verify_screenshot_description(
        description="The page shows a green success banner and a submit button.",
        expected_terms=["success", "submit"],
        forbidden_terms=["error"],
    )

    assert result.passed is True
    assert result.coverage == 1.0
    assert result.missing_terms == ()


def test_screenshot_description_reports_missing_and_forbidden() -> None:
    result = verify_screenshot_description(
        description="The page shows an error banner.",
        expected_terms=["success", "submit"],
        forbidden_terms=["error"],
    )

    assert result.passed is False
    assert result.missing_terms == ("success", "submit")
    assert result.forbidden_hits == ("error",)


def test_session_distribution_flags_heavy_tail() -> None:
    result = summarize_session_distribution({"a": 100, "b": 80, "c": 10, "d": 10})

    assert result.session_count == 4
    assert result.effective_top_n == 2
    assert result.top_n_share(2) == 0.9
    assert result.to_dict()["top_share"] == 0.9
    assert result.heavy_tail is True


def test_runtime_qos_classification_and_log_path(tmp_path: Path) -> None:
    hint = classify_tool_runtime("run_shell", {"command": "pytest -q"})
    path = named_tool_log_path("run_shell", log_root=tmp_path)

    assert hint.spawn_class == SpawnClass.ADAPTIVE
    assert hint.log_suppression is False
    assert path.parent == tmp_path / "run_shell"
    assert path.name.endswith(".log")


def test_runtime_qos_sensitive_output_uses_explicit_suppression(tmp_path: Path) -> None:
    hint = classify_tool_runtime("credential_helpers_get", {"key": "token"})
    path = named_tool_log_path("credential_helpers_get", log_root=tmp_path, suppress=True)

    assert hint.spawn_class == SpawnClass.BACKGROUND
    assert hint.log_suppression is True
    assert path == Path("/dev/null")


def test_tool_dispatch_records_runtime_qos_metadata(monkeypatch) -> None:
    cfg = Config()
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(tool_runtime, "show_tool_call", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_runtime, "show_tool_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_runtime, "ask_approval", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tool_runtime, "run_tool", lambda *_args, **_kwargs: "ok")
    monkeypatch.setattr(tool_runtime, "record_perf_event", lambda event, **fields: events.append((event, fields)))

    _message, result = tool_runtime.execute_tool_call_for_pipeline("read_file", {"path": "README.md"}, cfg)

    assert result == "ok"
    qos = next(fields for event, fields in events if event == "qos")
    completed = next(fields for event, fields in events if event == "tool")
    assert qos["spawn_class"] == "adaptive"
    assert qos["log_path"].endswith(".log")
    assert qos["log_suppression"] is False
    assert completed["spawn_class"] == "adaptive"


def test_capability_mask_tiers_are_composable() -> None:
    mask = tier_mask("tier2") | Capability.NETWORK.value

    assert CapabilityMask(mask).has(Capability.READ)
    assert CapabilityMask(mask).has(Capability.WRITE)
    assert CapabilityMask(mask).has(Capability.NETWORK)
    assert not CapabilityMask(mask).has(Capability.EXTERNAL_PUBLISH)


def test_runtime_tools_expose_qos_screenshot_capability_and_small_context() -> None:
    qos = json.loads(runtime_qos_hint("run_shell", '{"command":"pytest -q"}'))
    shot = json.loads(screenshot_description_verify("green success banner", "success", "error"))
    caps = json.loads(capability_mask_describe("tier1", "write"))
    small = json.loads(small_context_ledger_preview("tiny", 4096, "[]"))

    assert qos["spawn_class"] == "adaptive"
    assert shot["passed"] is True
    assert "write" in caps["capabilities"]
    assert small["enabled"] is True
