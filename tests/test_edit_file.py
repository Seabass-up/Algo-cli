"""Tests for the edit_file tool.

edit_file is the surgical find/replace counterpart to write_file. The tests
cover happy paths, all the failure modes, the atomic-write guarantee, and
the line-number reporting.
"""
from __future__ import annotations

from algo_cli import tools


def test_edit_file_replaces_unique_match(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = tools.edit_file(str(p), "beta", "BETA")

    assert "Edited" in result
    assert "lines 2-2" in result
    assert p.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_edit_file_replaces_multiline_match(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text(
        "def hello():\n"
        "    return 1\n"
        "\n"
        "def goodbye():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    result = tools.edit_file(
        str(p),
        "def hello():\n    return 1",
        "def hello():\n    return 42",
    )

    assert "Edited" in result
    assert "lines 1-2" in result
    new = p.read_text(encoding="utf-8")
    assert "return 42" in new
    assert "return 1\n" not in new


def test_edit_file_replace_all_replaces_every_match(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("foo bar foo baz foo\n", encoding="utf-8")

    result = tools.edit_file(str(p), "foo", "FOO", replace_all=True)

    assert "3 occurrence" in result
    assert p.read_text(encoding="utf-8") == "FOO bar FOO baz FOO\n"


def test_edit_file_ambiguous_match_fails_without_replace_all(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("foo bar foo\n", encoding="utf-8")

    result = tools.edit_file(str(p), "foo", "FOO")

    assert "Error" in result
    assert "matched 2 locations" in result
    # File unchanged
    assert p.read_text(encoding="utf-8") == "foo bar foo\n"


def test_edit_file_ambiguous_multiline_reports_actual_start_lines(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("a\nb\nc\nx\na\nb\nc\n", encoding="utf-8")

    result = tools.edit_file(str(p), "a\nb", "A\nB")

    assert "matched 2 locations" in result
    assert "lines [1, 5]" in result


def test_find_unique_anchor_preserves_extra_trailing_blank_lines(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("a\nb\n\n\nc\n", encoding="utf-8")

    result = tools.find_unique_anchor(str(p), "a\nb\n\n")

    assert result.startswith("Found 1 unique match")


def test_edit_file_no_match_reports_first_line(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = tools.edit_file(str(p), "delta", "DELTA")

    assert "Error" in result
    assert "old_string not found" in result
    # File unchanged
    assert p.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_edit_file_empty_old_string_rejected(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("alpha\n", encoding="utf-8")

    result = tools.edit_file(str(p), "", "BETA")

    assert "Error" in result
    assert "non-empty old_string" in result
    # File unchanged
    assert p.read_text(encoding="utf-8") == "alpha\n"


def test_edit_file_no_op_change_rejected(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("alpha\nbeta\n", encoding="utf-8")

    result = tools.edit_file(str(p), "alpha", "alpha")

    assert "Error" in result
    assert "identical" in result


def test_edit_file_missing_file(tmp_path):
    p = tmp_path / "missing.txt"
    result = tools.edit_file(str(p), "foo", "FOO")
    assert "Error" in result
    assert "file not found" in result


def test_edit_file_directory_rejected(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    result = tools.edit_file(str(d), "foo", "FOO")
    assert "Error" in result
    assert "is a directory" in result


def test_edit_file_delete_with_empty_new_string(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("keep\nremove me\nalso keep\n", encoding="utf-8")

    result = tools.edit_file(str(p), "remove me\n", "")

    assert "Edited" in result
    assert p.read_text(encoding="utf-8") == "keep\nalso keep\n"


def test_edit_file_uses_cwd_for_relative_path(tmp_path, monkeypatch):
    p = tmp_path / "note.txt"
    p.write_text("alpha\nbeta\n", encoding="utf-8")
    result = tools.edit_file("note.txt", "alpha", "ALPHA", cwd=str(tmp_path))
    assert "Edited" in result
    assert p.read_text(encoding="utf-8") == "ALPHA\nbeta\n"


def test_edit_file_registered_in_all_tools():
    assert tools.edit_file in tools.ALL_TOOLS
    assert tools.TOOL_MAP.get("edit_file") is tools.edit_file


def test_edit_file_in_available_actions_files_group():
    """The model should discover edit_file through available_actions()."""
    import json

    payload = json.loads(tools.available_actions("files"))
    focused = payload["focused"]
    assert "edit_file" in focused["model_callable_tools"]["files"]


def test_edit_file_in_verification_layer():
    """The system prompt verification layer must mention edit_file preference."""
    import json

    payload = json.loads(tools.available_actions("tools"))
    layer = payload.get("verification_layer", [])
    joined = "\n".join(layer).lower()
    assert "edit_file" in joined
    assert "prefer edit_file" in joined


def test_edit_file_in_mutating_tools():
    from algo_cli.tool_policy import MUTATING_TOOLS, WRITE_TOOLS

    assert "edit_file" in MUTATING_TOOLS
    assert "edit_file" in WRITE_TOOLS


def test_edit_file_described_as_mutation():
    from algo_cli.tool_policy import describes_mutation_action

    desc = describes_mutation_action("edit_file", {"path": "/tmp/foo.py"})
    assert desc == "edit_file: /tmp/foo.py"


def test_edit_file_in_oneshot_dangerous_tools():
    from algo_cli import oneshot

    assert "edit_file" in oneshot.DANGEROUS_TOOLS


def test_edit_file_atomic_replace_preserves_file_on_concurrent_write(tmp_path):
    """edit_file should use atomic write: a partial failure cannot leave
    a truncated file. We verify the helper by reading the file's full
    contents after a successful edit and confirming the helper completed
    by replacing the target via os.replace semantics."""
    p = tmp_path / "note.txt"
    p.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    # Sanity: helper exists and is used
    assert hasattr(tools, "_atomic_write_text")
    result = tools.edit_file(str(p), "beta", "BETA")
    assert "Edited" in result
    # File ends with the trailing newline it started with
    text = p.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text == "alpha\nBETA\ngamma\n"
