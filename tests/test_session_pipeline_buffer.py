"""/diff and /changes — surface evidence captured by the most recent agent pipeline."""

from __future__ import annotations

from algo_cli import agent_blocks, main
from algo_cli.config import Config


def _block(**kwargs) -> agent_blocks.AgentBlock:
    defaults = dict(role="implement", prompt="p", status="complete", output="ok", duration_ms=1500.0, tool_calls=2)
    defaults.update(kwargs)
    return agent_blocks.AgentBlock(**defaults)


def _capture_console(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(main, "show_info", lambda message: lines.append(f"INFO: {message}"))
    monkeypatch.setattr(main, "show_error", lambda message: lines.append(f"ERROR: {message}"))
    monkeypatch.setattr(main.console, "print", lambda text="": lines.append(str(text)))
    return lines


def test_session_pipeline_blocks_starts_empty():
    main.clear_session_pipeline_blocks()
    assert main.session_pipeline_blocks() == []


def test_clear_command_clears_session_pipeline_buffer(monkeypatch):
    cfg = Config()
    main._session_pipeline_blocks[:] = [_block()]
    monkeypatch.setattr(main, "show_info", lambda _msg: None)

    main.handle_command("/clear", cfg, object())  # type: ignore[arg-type]

    assert main.session_pipeline_blocks() == []


def test_diff_command_reports_empty_when_no_pipeline_run(monkeypatch):
    main.clear_session_pipeline_blocks()
    lines = _capture_console(monkeypatch)

    main.handle_diff_command()

    assert any("No pipeline activity" in line for line in lines)


def test_diff_command_reports_no_evidence_when_no_requires_change_block_recorded_git(monkeypatch):
    main._session_pipeline_blocks[:] = [_block(role="plan", requires_change=False, git_evidence="")]
    lines = _capture_console(monkeypatch)

    main.handle_diff_command()

    assert any("No verified diff captured" in line for line in lines)


def test_diff_command_prints_most_recent_requires_change_block_with_git_evidence(monkeypatch):
    older = _block(
        role="implement",
        requires_change=True,
        git_evidence="OLDER DIFF\n diff --git a/x.py b/x.py",
        status="complete",
        successful_writes=["x.py"],
    )
    newer = _block(
        role="implement",
        requires_change=True,
        git_evidence="NEWER DIFF\n diff --git a/y.py b/y.py",
        status="partial",
        status_reason="Required change not verified: ...",
        verification_warning="Git verification was unavailable.",
        successful_writes=["y.py", "z.py"],
    )
    main._session_pipeline_blocks[:] = [older, newer]
    lines = _capture_console(monkeypatch)

    main.handle_diff_command()

    combined = "\n".join(lines)
    assert "NEWER DIFF" in combined
    assert "OLDER DIFF" not in combined
    assert "partial" in combined
    assert "Required change not verified" in combined
    assert "Git verification was unavailable" in combined
    assert "y.py" in combined and "z.py" in combined


def test_changes_command_reports_empty_when_no_pipeline_run(monkeypatch):
    main.clear_session_pipeline_blocks()
    lines = _capture_console(monkeypatch)

    main.handle_changes_command()

    assert any("No pipeline activity" in line for line in lines)


def test_changes_command_summarizes_each_block_status_and_evidence(monkeypatch):
    main._session_pipeline_blocks[:] = [
        _block(role="plan", status="complete", duration_ms=300.0, tool_calls=0),
        _block(
            role="implement",
            status="partial",
            duration_ms=12400.0,
            tool_calls=5,
            status_reason="Required change not verified: no attributable Git delta.",
            successful_writes=["ollama_cli/main.py"],
        ),
        _block(
            role="review",
            status="complete",
            duration_ms=4200.0,
            tool_calls=2,
            mutation_actions=["run_shell: git add main.py"],
        ),
    ]
    lines = _capture_console(monkeypatch)

    main.handle_changes_command()

    combined = "\n".join(lines)
    assert "3 blocks" in combined
    assert "[plan]" in combined and "complete" in combined
    assert "[implement]" in combined and "partial" in combined
    assert "Required change not verified" in combined
    assert "ollama_cli/main.py" in combined
    assert "[review]" in combined
    assert "git add main.py" in combined


def test_diff_and_changes_registered_as_slash_commands():
    names = {name for name, _description in main.SLASH_COMMANDS}
    assert "/diff" in names
    assert "/changes" in names
