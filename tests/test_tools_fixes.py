"""Regressions for tool failure classification, partial search errors, fallback globs and shell output."""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from algo_cli import nathan_runtime, search_execution, tools


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
    assert nathan_runtime.classify_tool_status(out) == "worked"


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
