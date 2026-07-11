from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from algo_cli import main
from algo_cli.context_budget import OptionalContextBlock
from algo_cli.small_context import (
    SMALL_CONTEXT_THRESHOLD,
    build_ledger_text,
    is_small_context,
    preview_small_context_ledger,
    refresh_trigger,
    write_ledger,
)
from algo_cli.tools import small_context_ledger_preview


@dataclass
class _Block:
    name: str
    title: str
    body: str


def test_is_small_context_uses_75k_threshold() -> None:
    assert is_small_context(4096) is True
    assert is_small_context(SMALL_CONTEXT_THRESHOLD - 1) is True
    assert is_small_context(SMALL_CONTEXT_THRESHOLD) is False
    assert is_small_context(1_000_000) is False
    assert is_small_context(None) is False


def test_build_ledger_text_includes_full_optional_blocks() -> None:
    text, names = build_ledger_text(
        model="tiny:latest",
        runtime_cap=32_768,
        cwd="/tmp/project",
        base_message="Fix the test.",
        optional_blocks=[_Block("memory", "Long-term Memories", "Important context")],
        session_summary="Already inspected foo.py",
        messages=[{"role": "assistant", "content": "prior answer"}],
    )

    assert names == ("memory",)
    assert "# Algo CLI Small-Context Ledger" in text
    assert "Fix the test." in text
    assert "Already inspected foo.py" in text
    assert "Important context" in text


def test_write_ledger_creates_temp_markdown_for_small_context(tmp_path: Path) -> None:
    ledger = write_ledger(
        model="tiny:latest",
        runtime_cap=4096,
        cwd="/tmp/project",
        base_message="Use context.",
        optional_blocks=[_Block("rag", "Relevant Context", "Full RAG block")],
        root=tmp_path,
    )

    assert ledger is not None
    assert ledger.path.exists()
    assert ledger.path.parent == tmp_path
    assert "Full RAG block" in ledger.path.read_text(encoding="utf-8")
    trigger = refresh_trigger(ledger)
    assert str(ledger.path) in trigger
    assert "read_file" in trigger


def test_write_ledger_is_disabled_for_large_context(tmp_path: Path) -> None:
    assert write_ledger(
        model="glm-5.2",
        runtime_cap=1_000_000,
        cwd="/tmp/project",
        base_message="Use context.",
        optional_blocks=[_Block("rag", "Relevant Context", "Full RAG block")],
        root=tmp_path,
    ) is None


def test_small_context_preview_tool_reports_decision() -> None:
    payload = json.loads(small_context_ledger_preview("tiny", 8192, '[{"name":"rag"}]'))

    assert payload["enabled"] is True
    assert payload["threshold"] == 75_000
    assert payload["block_count"] == 1


def test_preview_handles_bad_json() -> None:
    payload = json.loads(preview_small_context_ledger("tiny", 8192, "not-json"))

    assert payload["enabled"] is True
    assert payload["block_count"] == 0


def test_fit_request_user_message_keeps_fitting_live_context_with_ledger(monkeypatch, tmp_path: Path) -> None:
    cfg = main.Config()
    cfg.model = "tiny:latest"
    cfg.cwd = tmp_path
    cfg.messages = [{"role": "user", "content": "Do the work"}]
    optional_blocks = [OptionalContextBlock("rag", "Relevant Context", "Full RAG block")]

    monkeypatch.setattr(main, "json_sink", lambda: "jsonl")
    monkeypatch.setattr(
        main,
        "context_status",
        lambda *args, **kwargs: (1000, 8192, 7192, 8192, 8192),
    )

    base_used = main.estimate_usage_with_system_prompt("system", cfg)
    runtime_cap = 8192
    ledger = main.small_context.write_ledger(
        model=cfg.model,
        runtime_cap=runtime_cap,
        cwd=str(cfg.cwd),
        base_message="Do the work",
        optional_blocks=optional_blocks,
        root=tmp_path,
    )
    assert ledger is not None
    trigger = main.small_context.refresh_trigger(ledger)
    request_message, included, omitted, optional_used = main.fit_optional_context_blocks(
        f"Do the work\n\n{trigger}",
        optional_blocks,
        base_used_tokens=base_used + main.estimate_text_tokens("\n\n" + trigger),
        runtime_cap=runtime_cap,
        model_info=None,
    )

    assert str(ledger.path) in request_message
    assert "Full RAG block" in request_message
    assert included == ["rag"]
    assert omitted == []
    assert optional_used > 0
