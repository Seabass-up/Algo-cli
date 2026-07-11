from __future__ import annotations

import pytest

from algo_cli import context_budget, harness, session_commands
from algo_cli.config import Config


def test_mercury_compact_for_read_only_research_task():
    text = "Policy review. Read-only. Deliver sections 1-5 from the rollout plan."
    resolved = harness.resolve_mercury_stop_conditions(
        user_message=text,
        session_mode="explore",
        include_external=True,
    )
    full = harness.load_mercury_stop_conditions()
    assert resolved == harness.MERCURY_STOP_CONDITIONS_COMPACT
    assert resolved != full or not full


def test_mercury_full_for_high_risk_external_task():
    text = "Send this proposal email to the client and post the invoice payment."
    resolved = harness.resolve_mercury_stop_conditions(
        user_message=text,
        session_mode="explore",
        include_external=True,
    )
    full = harness.load_mercury_stop_conditions()
    if not full:
        return
    assert resolved == full


@pytest.mark.parametrize("enabled", [False, True])
def test_mercury_external_guidance_respects_chat_prompt_opt_in(
    monkeypatch,
    tmp_path,
    enabled,
):
    sentinel = "PRIVATE-MERCURY-SENTINEL"
    stop_conditions = tmp_path / "stop-conditions.md"
    stop_conditions.write_text(sentinel, encoding="utf-8")
    reads: list[object] = []

    def tracked_read(path, max_chars):
        reads.append(path)
        return sentinel[:max_chars]

    monkeypatch.setattr(harness, "MERCURY_STOP_CONDITIONS_PATH", stop_conditions)
    monkeypatch.setattr(harness, "read_text", tracked_read)
    cfg = Config(
        external_harness_sources_enabled=enabled,
        session_mode="publish",
    )

    prompt = context_budget.build_system_prompt(
        cfg,
        user_message="Prepare the external release announcement",
    )

    assert (sentinel in prompt) is enabled
    assert bool(reads) is enabled
    if not enabled:
        assert harness.MERCURY_STOP_CONDITIONS_COMPACT in prompt


def test_session_slash_read_file():
    from pathlib import Path

    cfg = Config()
    cfg.cwd = str(Path(__file__).resolve().parents[1])
    out = session_commands.execute("/read README.md", cfg, max_read_chars=200)
    assert not out.lower().startswith("error")
