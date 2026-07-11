"""Tests for model->tool boundary hardening."""

from algo_cli import tool_contract as tc
from algo_cli.tools import read_file, run_shell


def test_signature_hint_hides_runtime_params():
    hint = tc.signature_hint(run_shell)
    assert "command" in hint
    assert "cwd" not in hint          # runtime-injected
    assert "safe_mode" not in hint    # runtime-injected


def test_correct_tool_error_includes_signature():
    msg = tc.correct_tool_error(
        "read_file", {"pat": "x"},
        TypeError("read_file() got an unexpected keyword argument 'pat'"),
        read_file,
    )
    assert "Correct signature" in msg
    assert "path" in msg
    assert "You sent: pat" in msg


def test_correct_tool_error_flags_raw_json():
    msg = tc.correct_tool_error(
        "read_file", {"raw": '{"path"'},
        TypeError("unexpected keyword 'raw'"),
        read_file,
    )
    assert "not valid JSON" in msg


def test_shell_hint_for_head():
    hint = tc.shell_mistake_hint(
        "pytest test.py | head",
        "'head' is not recognized as an internal or external command",
    )
    assert "head" in hint
    assert "cmd.exe" in hint


def test_shell_hint_generic_for_unknown_recognized_error():
    hint = tc.shell_mistake_hint(
        "frobnicate --x",
        "'frobnicate' is not recognized as an internal or external command",
    )
    assert "cmd.exe" in hint


def test_shell_hint_empty_on_clean_output():
    assert tc.shell_mistake_hint("echo hi", "hi\n[exit code: 0]") == ""


def test_run_tool_returns_corrective_message():
    from algo_cli.config import Config
    from algo_cli.tool_runtime import run_tool

    # 'pattern' is required for search_files; omit it to force a TypeError.
    result = run_tool("read_file", {"bogus_param": "y"}, Config())
    assert "Correct signature" in result
    assert "read_file" in result


def test_run_tool_unknown_tool_lists_alternatives():
    from algo_cli.config import Config
    from algo_cli.tool_runtime import run_tool

    result = run_tool("definitely_not_a_tool", {}, Config())
    assert "Unknown tool" in result
    assert "Available tools include" in result
