from __future__ import annotations

from algo_cli.workspace_resolver import parse_path_arg


def test_parse_path_arg_strips_outer_quotes():
    raw = r'"G:\Projects\Example Site\Permits"'
    assert parse_path_arg(raw) == r"G:\Projects\Example Site\Permits"


def test_parse_path_arg_quoted_path_with_spaces():
    assert parse_path_arg('"Building 2/manifest.json"') == "Building 2/manifest.json"


def test_parse_path_arg_simple_filename():
    assert parse_path_arg("release-plan.md") == "release-plan.md"


def test_parse_path_arg_unquoted_windows_backslashes():
    raw = r"C:\Users\example\algo-cli"
    assert parse_path_arg(raw) == raw


def test_parse_path_arg_quoted_windows_backslashes():
    raw = r"C:\Users\example\algo-cli"
    assert parse_path_arg(f'"{raw}"') == raw
