"""Search matcher inputs are data, and both backends have bounded output."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from algo_cli import search_execution, tools


@pytest.mark.parametrize("child", ["import sys; sys.stdin.buffer.read(); print('done')", "print('done')"])
def test_bounded_stdin_writer_handles_consumption_and_early_exit(child: str) -> None:
    result = search_execution.run_search_process(
        [sys.executable, "-I", "-S", "-c", child],
        limit=2,
        max_bytes=100,
        timeout=5,
        process_kwargs=tools._isolated_process_group_kwargs(),
        terminate=tools._terminate_process_tree,
        input_bytes=b"x" * 1_000_000,
    )
    assert result.stdout.strip() == b"done"
    assert result.returncode == 0 and not result.timed_out


def test_stalled_stdin_writer_is_reaped_on_timeout() -> None:
    result = search_execution.run_search_process(
        [sys.executable, "-I", "-S", "-c", "import time; time.sleep(30)"],
        limit=2,
        max_bytes=100,
        timeout=0.2,
        process_kwargs=tools._isolated_process_group_kwargs(),
        terminate=tools._terminate_process_tree,
        input_bytes=b"x" * 1_000_000,
    )
    assert result.timed_out and result.returncode != 0


@pytest.mark.parametrize("pattern", ["--files", "--version", "--help", "--pre=not-a-real-command"])
def test_rg_treats_option_shaped_pattern_as_data(tmp_path: Path, pattern: str) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    target = tmp_path / "example.txt"
    target.write_text(f"literal {pattern}\n", encoding="utf-8")

    result = tools.search_files(pattern, path=str(tmp_path))

    assert result == f"{target}:1:literal {pattern}"


def test_rg_ignores_inherited_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "example.txt"
    target.write_text("needle\n", encoding="utf-8")
    rc = tmp_path / "rg-options"
    rc.write_text("--files\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(rc))

    assert tools.search_files("needle", path=str(root)) == f"{target}:1:needle"


@pytest.mark.parametrize("glob", ["--files", "--pre=not-a-real-command"])
def test_rg_treats_option_shaped_glob_as_data(tmp_path: Path, glob: str) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    target = tmp_path / glob
    target.write_text("needle\n", encoding="utf-8")

    assert tools.search_files("needle", path=str(tmp_path), glob=glob) == f"{target}:1:needle"


@pytest.mark.parametrize("backend", ["rg", "fallback", "single-file"])
def test_search_caps_one_large_unicode_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    target = tmp_path / "example.txt"
    target.write_text("needle " + "\u2603" * 100_000 + "\n", encoding="utf-8")

    result = tools.search_files("needle", path=str(target if backend == "single-file" else tmp_path))

    assert "needle" in result
    assert "truncated" in result
    assert len(result.encode("utf-8")) <= tools.MAX_TOOL_RESULT


@pytest.mark.parametrize("backend", ["rg", "fallback", "single-file"])
def test_search_preserves_line_numbers_and_reports_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    target = tmp_path / "example.txt"
    target.write_text("unmatched\nneedle first\nneedle second\nneedle third\n", encoding="utf-8")

    result = tools.search_files("needle", path=str(target if backend == "single-file" else tmp_path), limit=2)

    assert f"{target}:2:needle first\n{target}:3:needle second" in result
    assert "needle third" not in result
    assert "truncated" in result


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "2"])
def test_search_rejects_invalid_limits(tmp_path: Path, limit: object) -> None:
    (tmp_path / "example.txt").write_text("needle\n", encoding="utf-8")

    result = tools.search_files("needle", path=str(tmp_path), limit=limit)  # type: ignore[arg-type]

    assert result.startswith("Error:")
    assert "limit" in result


def test_search_bounds_pattern_before_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "example.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which", lambda _name: pytest.fail("oversized pattern reached launch"))

    result = tools.search_files("a" * 5000, path=str(tmp_path))

    assert result.startswith("Error:")
    assert "pattern" in result


@pytest.mark.parametrize("backend", ["fallback", "single-file"])
def test_python_regex_timeout_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    target = tmp_path / "example.txt"
    target.write_text("a" * 200 + "!\n", encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(tools, "SEARCH_TIMEOUT_SECONDS", 0.3)
    children: list[subprocess.Popen] = []
    original = subprocess.Popen

    def tracked(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked)
    started = time.monotonic()
    result = tools.search_files("(a+)+$", path=str(target if backend == "single-file" else tmp_path))

    assert result.startswith("Error: search timed out")
    assert time.monotonic() - started < 12
    assert children and all(child.poll() is not None for child in children)
    assert all(child.stdout is None or child.stdout.closed for child in children)
    assert all(child.stderr is None or child.stderr.closed for child in children)


def test_search_cancellation_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    children: list[subprocess.Popen] = []
    original = subprocess.Popen

    def tracked(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", tracked)
    monkeypatch.setattr(search_execution.queue.Queue, "get", interrupt)
    with pytest.raises(KeyboardInterrupt):
        search_execution.run_search_process(
            [sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"],
            limit=10,
            max_bytes=1000,
            timeout=10,
            process_kwargs=tools._isolated_process_group_kwargs(),
            terminate=tools._terminate_process_tree,
        )

    assert children and all(child.poll() is not None for child in children)
    assert all(child.stdout is None or child.stdout.closed for child in children)
    assert all(child.stderr is None or child.stderr.closed for child in children)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_process_capture_stops_an_unbounded_writer(stream: str) -> None:
    result = search_execution.run_search_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            f"import os;\nwhile True: os.write({1 if stream == 'stdout' else 2}, b'x' * 4096)",
        ],
        limit=10,
        max_bytes=5000,
        timeout=5,
        process_kwargs=tools._isolated_process_group_kwargs(),
        terminate=tools._terminate_process_tree,
    )

    assert not result.timed_out
    assert len(result.stdout) <= 5000
    assert len(result.stderr) <= 4096
    assert result.truncated if stream == "stdout" else result.stderr_truncated


@pytest.mark.parametrize("chunk_size", [1, 2, 5, 4096])
@pytest.mark.parametrize(
    "payload",
    [b"one\ntwo\n", b"one\ntwo\nthree", b"one\ntwo\nthree\n", b"0123456789" * 100],
    ids=["exact-limit", "unterminated-extra", "extra-line", "byte-flood"],
)
def test_capture_limits_hold_across_chunk_boundaries(chunk_size: int, payload: bytes) -> None:
    capture = search_execution._Capture(12, 2)
    for offset in range(0, len(payload), chunk_size):
        capture.append(payload[offset : offset + chunk_size])
        if capture.truncated:
            break
    expected = b"".join(payload.splitlines(keepends=True)[:2])[:12]
    assert capture.data == expected
    assert capture.truncated == (expected != payload)


@pytest.mark.parametrize("backend", ["rg", "fallback", "single-file"])
@pytest.mark.parametrize("glob", [None, "*.py", "*.txt"])
def test_legitimate_search_globs_and_no_match_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, glob: str | None
) -> None:
    if backend == "rg" and shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    if backend == "fallback":
        monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    target = tmp_path / "example.py"
    target.write_text("one\nneedle two\n", encoding="utf-8")
    path = str(target if backend == "single-file" else tmp_path)

    result = tools.search_files("needle", path=path, glob=glob, limit=1)

    assert result == ("No matches." if glob == "*.txt" else f"{target}:2:needle two")
    assert tools.search_files("absent", path=path, glob=glob) == "No matches."


@pytest.mark.parametrize("glob", ["*.{py,js}", "**/src/**/*.py", "!*.txt"])
def test_rg_preserves_extended_glob_syntax(tmp_path: Path, glob: str) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    source = tmp_path / "src"
    source.mkdir()
    target = source / "example.py"
    target.write_text("needle\n", encoding="utf-8")
    (source / "example.txt").write_text("needle\n", encoding="utf-8")

    assert tools.search_files("needle", path=str(tmp_path), glob=glob) == f"{target}:1:needle"


def test_output_limit_does_not_hide_a_reported_search_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_execution,
        "run_search_process",
        lambda *args, **kwargs: search_execution.SearchProcessResult(
            b"some matches\n",
            b"permission denied",
            2,
            True,
            False,
            False,
        ),
    )

    result = tools.search_files("needle", path=str(tmp_path))

    assert result.startswith("Error searching:")
    assert "permission denied" in result


@pytest.mark.parametrize("field", ["pattern", "glob"])
def test_invalid_unicode_returns_bounded_error(tmp_path: Path, field: str) -> None:
    args = {"pattern": "needle", "path": str(tmp_path), field: "\ud800"}

    result = tools.search_files(**args)

    assert result.startswith("Error:")
    assert "UTF-8" in result


def test_process_that_closes_output_but_stays_alive_is_reaped() -> None:
    result = search_execution.run_search_process(
        [sys.executable, "-I", "-S", "-c", "import os,time; os.close(1); os.close(2); time.sleep(60)"],
        limit=10,
        max_bytes=5000,
        timeout=0.3,
        process_kwargs=tools._isolated_process_group_kwargs(),
        terminate=tools._terminate_process_tree,
    )

    assert result.timed_out
    assert result.returncode != 0


def test_rg_normalizes_crlf_result_lines(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    target = tmp_path / "example.txt"
    target.write_bytes(b"unmatched\r\nneedle first\r\nneedle second\r\n")

    result = tools.search_files("needle", path=str(tmp_path))

    assert result == f"{target}:2:needle first\n{target}:3:needle second"


def test_unicode_line_separator_cannot_bypass_result_line_limit(tmp_path: Path) -> None:
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    target = tmp_path / "example.txt"
    target.write_text("needle first\u2028needle second\u2028needle third\n", encoding="utf-8")

    result = tools.search_files("needle", path=str(tmp_path), limit=1)

    assert result.startswith(f"{target}:1:needle first\n")
    assert "needle second" not in result
    assert "truncated" in result
