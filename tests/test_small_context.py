from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import subprocess
import time

import pytest

from algo_cli import main, small_context
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
    assert (
        write_ledger(
            model="glm-5.2",
            runtime_cap=1_000_000,
            cwd="/tmp/project",
            base_message="Use context.",
            optional_blocks=[_Block("rag", "Relevant Context", "Full RAG block")],
            root=tmp_path,
        )
        is None
    )


def test_echo_authority_disables_small_context_file_creation(tmp_path: Path) -> None:
    root = tmp_path / "ledgers"
    canary = "SMALL_CONTEXT_ECHO_CANARY"

    ledger = write_ledger(
        model="tiny:latest",
        runtime_cap=4096,
        cwd="/tmp/project",
        base_message=canary,
        optional_blocks=[_Block("memory", "Protected", canary)],
        session_summary=canary,
        messages=[{"role": "tool", "content": canary}],
        root=root,
        echo_authority=True,
    )

    assert ledger is None
    assert not root.exists()


def test_small_context_ledger_uses_private_modes_and_bounded_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ledgers"
    root.mkdir()
    expired = root / "1234567890-old-0000000000000000.md"
    expired.write_text("old", encoding="utf-8")
    old = time.time() - (25 * 60 * 60)
    os.utime(expired, (old, old))
    unrelated = root / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    ledger = write_ledger(
        model="tiny:latest",
        runtime_cap=4096,
        cwd="/tmp/project",
        base_message="safe",
        optional_blocks=[],
        root=root,
    )

    assert ledger is not None
    assert not expired.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600


def test_small_context_ledger_rejects_symlink_root_without_writing_victim(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    root = tmp_path / "ledgers"
    try:
        root.symlink_to(victim, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(OSError, match="private directory"):
        write_ledger(
            model="tiny:latest",
            runtime_cap=4096,
            cwd="/tmp/project",
            base_message="must not write",
            optional_blocks=[],
            root=root,
        )

    assert list(victim.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_small_context_ledger_rejects_junctioned_ancestor_before_creation(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    alias = tmp_path / "alias"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(victim)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")

    with pytest.raises(OSError, match="ancestry is unsafe"):
        write_ledger(
            model="tiny:latest",
            runtime_cap=4096,
            cwd="C:\\project",
            base_message="must not write",
            optional_blocks=[],
            root=alias / "ledgers",
        )
    assert list(victim.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows atomic private staging contract")
def test_windows_small_context_stage_is_private_before_first_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ledgers"
    small_context._ensure_windows_private_directory(root)
    original_create = small_context._windows_create_private_file
    created: list[Path] = []

    def create_and_check(path: Path) -> int:
        descriptor = original_create(path)
        assert small_context._windows_private_dacl(path) is True
        created.append(path)
        return descriptor

    monkeypatch.setattr(small_context, "_windows_create_private_file", create_and_check)
    ledger = write_ledger(
        model="tiny:latest",
        runtime_cap=4096,
        cwd="C:\\project",
        base_message="private context",
        optional_blocks=[],
        root=root,
    )

    assert ledger is not None
    assert len(created) == 1
    assert small_context._windows_private_dacl(ledger.path) is True


def test_full_path_cleanup_removes_stale_crash_temp_alias_without_deleting_ledger(tmp_path: Path) -> None:
    now = time.time()
    ledger = tmp_path / "1234567890-tiny-0123456789abcdef.md"
    staging = tmp_path / ".1234567890-tiny-0123456789abcdef.md.abcdef0123456789.tmp"
    ledger.write_text("private context", encoding="utf-8")
    os.link(ledger, staging)
    os.utime(ledger, (now - 600, now - 600))

    small_context._cleanup_ledgers_by_path(tmp_path, now=now)

    assert ledger.read_text(encoding="utf-8") == "private context"
    assert ledger.stat().st_nlink == 1
    assert not staging.exists()


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
