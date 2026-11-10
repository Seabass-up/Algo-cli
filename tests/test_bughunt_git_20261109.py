from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from algo_cli import git_evidence, git_publish, worktree_runtime


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Algo Tests")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "Initial fixture")
    return root


def test_untracked_non_ascii_name_edit_is_detected_as_state_change(repository: Path) -> None:
    target = repository / "café.md"
    target.write_text("first draft\n", encoding="utf-8")
    before = git_evidence.capture_git_snapshot(str(repository))

    target.write_text("second draft, edited in place\n", encoding="utf-8")
    after = git_evidence.capture_git_snapshot(str(repository))

    assert before.available and after.available
    assert before.untracked_digest != after.untracked_digest
    assert git_evidence.has_observed_delta(before, after)


def test_fresh_repository_without_commits_yields_available_snapshot_and_detects_added_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    before = git_evidence.capture_git_snapshot(str(root))

    assert before.available, before.error
    assert before.head is None

    (root / "new.txt").write_text("hello\n", encoding="utf-8")
    after = git_evidence.capture_git_snapshot(str(root))

    assert after.available, after.error
    assert git_evidence.has_verified_delta(before, after)


def test_run_git_names_missing_working_directory(tmp_path: Path) -> None:
    from algo_cli.worktree_runtime import WorktreeError

    missing = tmp_path / "does-not-exist"

    with pytest.raises(WorktreeError) as excinfo:
        worktree_runtime._run_git(["status"], cwd=missing)

    assert "Working directory does not exist" in str(excinfo.value)
    assert str(missing.resolve()) in str(excinfo.value)
    assert "not installed" not in str(excinfo.value)


def test_run_program_names_missing_working_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    rc, output = git_publish._run_program("git", ["status"], cwd=missing)

    assert rc != 0
    assert "Working directory does not exist" in output
    assert str(missing.resolve()) in output
    assert "not installed" not in output


def test_push_bound_destination_names_missing_working_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    with pytest.raises(git_publish.PublishError) as excinfo:
        git_publish._push_bound_destination(
            cwd=missing,
            push_url="file:///nonexistent-remote",
            refspec="HEAD:refs/heads/main",
        )

    assert "Working directory does not exist" in str(excinfo.value)
    assert "not installed" not in str(excinfo.value)


def test_run_program_sets_terminal_prompt_off_and_detaches_stdin(monkeypatch, repository: Path) -> None:
    calls: list[dict] = []

    def fake_run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, stdout=b"ok\n", stderr=b"")

    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    monkeypatch.setattr(git_publish.subprocess, "run", fake_run)

    rc, output = git_publish._run_program("git", ["status"], cwd=repository)

    assert (rc, output) == (0, "ok")
    assert len(calls) == 1
    assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["capture_output"] is True
    assert calls[0]["timeout"] == 120
    assert "GIT_TERMINAL_PROMPT" not in os.environ


def test_run_program_keeps_stdin_piped_input_when_input_is_supplied(monkeypatch, repository: Path) -> None:
    calls: list[dict] = []

    def fake_run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(git_publish.subprocess, "run", fake_run)

    git_publish._run_program("git", ["update-ref", "--stdin"], cwd=repository, input_text="x\n")

    assert calls[0]["input"] == b"x\n"
    assert calls[0].get("stdin") is None
    assert calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
