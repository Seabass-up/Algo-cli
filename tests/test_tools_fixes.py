"""Regressions for tool failure classification, partial search errors, fallback globs and shell output."""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

import pytest

from algo_cli import nathan_runtime, search_execution, tools
from algo_cli.config import Config


@pytest.mark.parametrize(
    "result",
    [
        "Error writing /x: [Errno 13] Permission denied",
        "Error reading /x: [Errno 5] Input/output error",
        "Error running command: [Errno 2] No such file or directory",
        "Error searching: rg: /x: Permission denied (os error 13)",
        "Error fetching URL: timed out after 30 seconds.",
        "Error searching web: 401 Unauthorized",
        "Error listing /x: [Errno 13] Permission denied",
        "Error extracting PDF text from /x.pdf: broken xref",
        "Error running git diff: [Errno 2] git",
    ],
)
def test_classify_tool_status_marks_verb_prefixed_tool_errors_failed(result):
    assert nathan_runtime.classify_tool_status(result) == "failed"


@pytest.mark.parametrize(
    "result",
    ["Error handling notes\nsee below", "error rates dropped this week", "src/a.py:1:Error reading config"],
)
def test_classify_tool_status_keeps_ordinary_error_text_worked(result):
    assert nathan_runtime.classify_tool_status(result) == "worked"


def test_search_files_keeps_rg_matches_when_rg_reports_partial_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(
        search_execution, "run_search_process",
        lambda *args, **kwargs: search_execution.SearchProcessResult(
            f"{tmp_path}/ok/a.txt:1:needle\n".encode(),
            f"rg: {tmp_path}/locked: Permission denied (os error 13)\n".encode(),
            2, False, False, False,
        ),
    )

    out = tools.search_files("needle", path=str(tmp_path))

    assert not out.startswith("Error")
    assert "a.txt:1:needle" in out
    assert "[partial: search reported 1 error(s); first: rg:" in out
    # Report section 4: a partial search is not a complete success, but the matches stay in the result.
    assert nathan_runtime.classify_tool_status(out) == "failed"
    assert nathan_runtime.classify_tool_status(out, name="search_files") == "failed"


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0 or shutil.which("rg") is None,
    reason="needs ripgrep and POSIX permissions as a non-root user",
)
def test_search_files_real_rg_with_unreadable_subfolder_returns_matches(tmp_path):
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.txt").write_text("needle\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        out = tools.search_files("needle", path=str(tmp_path))
    finally:
        locked.chmod(0o700)

    assert "a.txt:1:needle" in out
    assert "[partial:" in out


def test_run_search_process_drains_large_stderr_without_ending_search():
    child = (
        "import sys\n"
        "for i in range(3000): sys.stderr.write(f'rg: /deny/{i}: Permission denied\\n')\n"
        "sys.stderr.flush()\n"
        "print('late-match', flush=True)\n"
    )
    result = search_execution.run_search_process(
        [sys.executable, "-I", "-S", "-c", child],
        limit=10,
        max_bytes=10_000,
        timeout=10,
        process_kwargs=tools._isolated_process_group_kwargs(),
        terminate=tools._terminate_process_tree,
    )

    assert result.stdout.strip() == b"late-match"
    assert result.returncode == 0 and not result.timed_out
    assert result.stderr_truncated and len(result.stderr) <= 4096


@pytest.mark.parametrize(
    ("glob", "expected", "absent"),
    [
        ("**/*.py", "pkg/mod.py", "notes.md"),
        ("pkg/*.py", "pkg/mod.py", "top.py"),
        ("!*.md", "pkg/mod.py", "notes.md"),
        ("*.{py,txt}", "top.py", "notes.md"),
    ],
)
def test_search_files_python_fallback_honors_rg_style_globs(tmp_path, monkeypatch, glob, expected, absent):
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "top.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("needle\n", encoding="utf-8")

    out = tools.search_files("needle", path=str(tmp_path), glob=glob)

    assert expected.replace("/", os.sep) in out
    assert absent not in out


def test_glob_matcher_matches_ripgrep_semantics():
    assert search_execution.glob_matcher("**/*.py")("a/b/c.py")
    assert search_execution.glob_matcher("**/*.py")("c.py")
    assert not search_execution.glob_matcher("src/*.py")("src/deep/c.py")
    assert search_execution.glob_matcher("*.py")("deep/c.py")
    assert not search_execution.glob_matcher("!*.md")("docs/readme.md")
    assert search_execution.glob_matcher("test_[a-c]*.py")("tests/test_b1.py")


@pytest.mark.skipif(os.name == "nt", reason="printf is a POSIX shell builtin")
def test_run_shell_preserves_leading_whitespace_of_first_line():
    out = tools.run_shell("printf ' M modified.py\\n?? new.py\\n'")

    assert out.startswith(" M modified.py\n?? new.py")
    assert out.endswith("[exit code: 0]")


@pytest.mark.parametrize(
    ("result", "name"),
    [
        ("Error reading sensor 3: timeout\nnext line", "read_file"),
        ("Error reading sensor 3: timeout", ""),
        ("Error running migrations: none pending\n[exit code: 0]", "run_shell"),
    ],
)
def test_classify_tool_status_keeps_raw_error_like_output_worked(result, name):
    assert nathan_runtime.classify_tool_status(result, name=name) == "worked"


def test_classify_tool_status_uses_exit_code_for_error_like_shell_output():
    assert nathan_runtime.classify_tool_status("Error running migrations: boom\n[exit code: 1]", name="run_shell") == "failed"
    assert nathan_runtime.classify_tool_status("Error reading C:\\x.txt: [Errno 13] denied", name="read_file") == "failed"


def test_glob_matcher_case_sensitivity_follows_platform(monkeypatch):
    monkeypatch.setattr(search_execution.os, "name", "nt")
    assert search_execution.glob_matcher("*.py")("SETUP.PY")
    assert search_execution.glob_matcher("docs/*.md")("DOCS/README.MD")
    monkeypatch.setattr(search_execution.os, "name", "posix")
    assert not search_execution.glob_matcher("*.py")("SETUP.PY")
    assert search_execution.glob_matcher("*.py")("setup.py")


# Report section 3: read_file bodies are never classified by their first line.
@pytest.mark.parametrize(
    "body",
    [
        "Error reading /var/log/app.log: connection reset\nnext line",
        "Error reading /var/log/app.log: connection reset\n",
        "Error: disk full\nretrying\n",
        "Tool error for read_file: quoted in a runbook\n",
        "tests failed\n[exit code: 1]\n",
    ],
)
def test_classify_tool_status_keeps_read_file_bodies_worked(body):
    assert nathan_runtime.classify_tool_status(body, name="read_file") == "worked"


def test_read_file_of_error_like_log_is_worked(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("Error reading /var/log/app.log: connection reset\nsecond line\n", encoding="utf-8")

    out = tools.read_file(str(log))

    assert out.startswith("Error reading /var/log/app.log")
    assert nathan_runtime.classify_tool_status(out, name="read_file") == "worked"


def test_read_file_real_errors_stay_failed(tmp_path, monkeypatch):
    assert nathan_runtime.classify_tool_status("Error reading /tmp/x: denied", name="read_file") == "failed"
    for out in (
        tools.read_file(str(tmp_path / "missing.txt")),
        tools.read_file(str(tmp_path)),
    ):
        assert out.startswith("Error")
        assert nathan_runtime.classify_tool_status(out, name="read_file") == "failed"
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "notes.md").write_text("x\n", encoding="utf-8")
    suggested = tools.read_file("notes.md", cwd=str(tmp_path))
    assert "\n" in suggested
    assert nathan_runtime.classify_tool_status(suggested, name="read_file") == "failed"
    hint = "Tool argument error for read_file: unexpected keyword\nCorrect signature — read_file(path)"
    assert nathan_runtime.classify_tool_status(hint, name="read_file") == "failed"

    def broken_open(*args, **kwargs):
        raise OSError(5, "Input/output error")

    target = tmp_path / "io.txt"
    target.write_text("data\n", encoding="utf-8")
    monkeypatch.setattr(type(target), "open", broken_open)
    out = tools.read_file(str(target))
    assert out.startswith(f"Error reading {target}:")
    assert nathan_runtime.classify_tool_status(out, name="read_file") == "failed"


# Report section 4: the partial note survives truncation and only search results are sniffed for it.
def test_partial_search_note_survives_truncation_and_classifies_failed(tmp_path, monkeypatch):
    stdout = "".join(f"{tmp_path}/a.txt:{i}:needle\n" for i in range(1, 400)).encode()
    monkeypatch.setattr(tools.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(tools, "MAX_TOOL_RESULT", 2_000)
    monkeypatch.setattr(
        search_execution, "run_search_process",
        lambda *args, **kwargs: search_execution.SearchProcessResult(
            stdout, b"rg: /locked: Permission denied (os error 13)\n", 2, True, False, False,
        ),
    )

    out = tools.search_files("needle", path=str(tmp_path), limit=1000)

    assert len(out.encode()) <= 2_000
    assert "a.txt:1:needle" in out and "...[truncated:" in out
    assert "\n[partial: search reported 1 error(s); first: rg: /locked: Permission denied (os error 13)]\n...[truncated:" in out
    assert nathan_runtime.classify_tool_status(out, name="search_files") == "failed"


def test_complete_search_and_file_bodies_with_partial_text_stay_worked():
    matches = "/w/a.py:1:needle\n/w/b.py:2:needle"
    assert nathan_runtime.classify_tool_status(matches, name="search_files") == "worked"
    body = "notes\n[partial: search reported 1 error(s); first: rg: x]"
    assert nathan_runtime.classify_tool_status(body, name="read_file") == "worked"


# Report "Team Ctrl+C can return while a tool is still running".
def test_write_file_refuses_after_team_cancellation(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    target = tmp_path / "out.txt"

    out = tools.write_file(str(target), "data", cancel_event=cancelled)

    assert out == tools.TEAM_CANCELLED_WRITE
    assert not target.exists()
    assert nathan_runtime.classify_tool_status(out, name="write_file") == "failed"
    assert tools.write_file(str(target), "data", cancel_event=threading.Event()).startswith("Wrote ")
    assert target.read_text(encoding="utf-8") == "data"


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell commands")
def test_run_shell_does_not_start_after_team_cancellation(tmp_path):
    cancelled = threading.Event()
    cancelled.set()

    out = tools.run_shell("touch marker", cwd=str(tmp_path), cancel_event=cancelled)

    assert "agent team was cancelled" in out
    assert not (tmp_path / "marker").exists()
    assert nathan_runtime.classify_tool_status(out, name="run_shell") == "failed"


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell commands")
def test_run_shell_terminates_running_command_on_team_cancellation(tmp_path):
    cancelled = threading.Event()
    timer = threading.Timer(0.3, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        out = tools.run_shell("sleep 3 && touch marker", cwd=str(tmp_path), timeout=30, cancel_event=cancelled)
    finally:
        timer.cancel()

    assert out == tools.TEAM_CANCELLED_SHELL
    assert time.monotonic() - started < 2.5
    time.sleep(3.2)
    assert not (tmp_path / "marker").exists()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell commands")
def test_run_shell_without_team_cancellation_is_unchanged(tmp_path):
    assert tools.run_shell("printf ok", cwd=str(tmp_path)) == "ok\n[exit code: 0]"
    assert tools.run_shell("printf ok", cwd=str(tmp_path), cancel_event=threading.Event()) == "ok\n[exit code: 0]"
    assert tools.run_shell("sleep 2", cwd=str(tmp_path), timeout=0.2, cancel_event=threading.Event()).startswith(
        "Error: command timed out"
    )


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell commands")
def test_run_tool_passes_only_the_team_cancellation_event(tmp_path):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=False)
    cfg.safe_mode = False
    cancelled = threading.Event()
    cancelled.set()

    # A model-supplied value never reaches the tool, and non-team sessions run normally.
    assert nathan_runtime.run_tool("run_shell", {"command": "printf ok", "cancel_event": cancelled}, cfg) == (
        "ok\n[exit code: 0]"
    )
    assert nathan_runtime.run_tool("write_file", {"path": "a.txt", "content": "x"}, cfg).startswith("Wrote ")

    setattr(cfg, "_algo_team_cancellation", cancelled)
    assert nathan_runtime.run_tool("write_file", {"path": "b.txt", "content": "x"}, cfg) == tools.TEAM_CANCELLED_WRITE
    assert "agent team was cancelled" in nathan_runtime.run_tool("run_shell", {"command": "touch marker"}, cfg)
    assert not (tmp_path / "b.txt").exists() and not (tmp_path / "marker").exists()


def test_team_cancellation_parameter_is_hidden_from_tool_schemas():
    import inspect

    assert "cancel_event" not in inspect.signature(tools.TOOL_MAP["run_shell"]).parameters
    assert "cancel_event" not in inspect.signature(tools.TOOL_MAP["write_file"]).parameters


def test_nameless_status_callers_pass_tool_name_so_file_bodies_stay_successful():
    from types import SimpleNamespace

    from algo_cli import agent_pipeline, nathan_program_runtime, oliver_oneshot

    body = "Error reading /var/log/app.log: connection reset\nnext line"
    no_outcome = SimpleNamespace(outcome=None)
    assert agent_pipeline._pipeline_outcome_status(no_outcome, body, name="read_file") == "succeeded"
    assert oliver_oneshot._tool_status_from_result(body, name="read_file") == "ok"
    assert nathan_program_runtime._action_result_status(body, name="read_file") == "worked"
    real_error = "Error reading /tmp/x: [Errno 13] Permission denied"
    assert agent_pipeline._pipeline_outcome_status(no_outcome, real_error, name="read_file") == "failed"
    assert oliver_oneshot._tool_status_from_result(real_error, name="read_file") == "failed"
