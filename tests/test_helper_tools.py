"""Tests for find_unique_anchor and batch_edit helper tools.

These are the high-leverage helpers the model should reach for when
edit_file fails or when many edits are needed on the same file.
"""
from __future__ import annotations

from algo_cli import tools


def test_find_unique_anchor_unique_match_reports_context(tmp_path):
    p = tmp_path / "note.py"
    p.write_text(
        "def a():\n"
        "    return 1\n"
        "\n"
        "def b():\n"
        "    return 2\n"
        "\n"
        "def c():\n"
        "    return 3\n",
        encoding="utf-8",
    )
    out = tools.find_unique_anchor(str(p), "def b():\n    return 2")
    assert "Found 1 unique match" in out
    assert "def b" in out
    assert "match at line 4" in out


def test_find_unique_anchor_multiple_matches_shows_each(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("x = 1\nx = 2\nx = 3\n", encoding="utf-8")
    out = tools.find_unique_anchor(str(p), "x = 2")
    assert "Found 1 unique match" in out  # single match — only one
    p.write_text("x = 1\nx = 2\nx = 1\nx = 2\n", encoding="utf-8")
    out = tools.find_unique_anchor(str(p), "x = 2")
    assert "Found 2 matches" in out
    assert "match at line 2" in out
    assert "match at line 4" in out


def test_find_unique_anchor_no_match(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("alpha\nbeta\n", encoding="utf-8")
    out = tools.find_unique_anchor(str(p), "delta")
    assert "No match" in out


def test_find_unique_anchor_empty_needle(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("alpha\n", encoding="utf-8")
    out = tools.find_unique_anchor(str(p), "")
    assert "Error" in out


def test_find_unique_anchor_missing_file(tmp_path):
    out = tools.find_unique_anchor(str(tmp_path / "missing.txt"), "anything")
    assert "file not found" in out


def test_find_unique_anchor_with_context_lines(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("a\nb\nc\nd\ne\nf\ng\nh\n", encoding="utf-8")
    out = tools.find_unique_anchor(str(p), "d", context_before=2, context_after=2)
    assert "b" in out
    assert "c" in out
    assert "e" in out
    assert "f" in out


def test_batch_edit_applies_edits_in_order(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    out = tools.batch_edit(
        str(p),
        [
            {"old_string": "a", "new_string": "A"},
            {"old_string": "c", "new_string": "C"},
        ],
    )
    assert "applied 2 edits" in out
    assert p.read_text(encoding="utf-8") == "A\nb\nC\n"


def test_batch_edit_chained_edits(tmp_path):
    """Edit #2 operates on the post-#1 content."""
    p = tmp_path / "note.py"
    p.write_text("hello world\n", encoding="utf-8")
    out = tools.batch_edit(
        str(p),
        [
            {"old_string": "hello", "new_string": "goodbye"},
            {"old_string": "goodbye world", "new_string": "goodbye planet"},
        ],
    )
    assert "applied 2 edits" in out
    assert p.read_text(encoding="utf-8") == "goodbye planet\n"


def test_batch_edit_empty_list(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("x", encoding="utf-8")
    out = tools.batch_edit(str(p), [])
    assert "Error" in out
    assert p.read_text(encoding="utf-8") == "x"  # unchanged


def test_batch_edit_edit_not_found_aborts(tmp_path):
    p = tmp_path / "note.py"
    original = "a\nb\nc\n"
    p.write_text(original, encoding="utf-8")
    out = tools.batch_edit(
        str(p),
        [
            {"old_string": "a", "new_string": "A"},
            {"old_string": "missing", "new_string": "M"},
        ],
    )
    assert "Error" in out
    assert "not found" in out
    # Atomic: file unchanged
    assert p.read_text(encoding="utf-8") == original


def test_batch_edit_ambiguous_match_aborts(tmp_path):
    p = tmp_path / "note.py"
    original = "x\nx\nx\n"
    p.write_text(original, encoding="utf-8")
    out = tools.batch_edit(str(p), [{"old_string": "x", "new_string": "y"}])
    assert "Error" in out
    assert "matched 3 locations" in out
    assert p.read_text(encoding="utf-8") == original


def test_batch_edit_replace_all_applies_global(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("x\nx\nx\n", encoding="utf-8")
    out = tools.batch_edit(str(p), [{"old_string": "x", "new_string": "y"}], replace_all=True)
    assert "applied 1 edits" in out
    assert p.read_text(encoding="utf-8") == "y\ny\ny\n"


def test_batch_edit_missing_file(tmp_path):
    out = tools.batch_edit(str(tmp_path / "missing.txt"), [{"old_string": "x", "new_string": "y"}])
    assert "file not found" in out


def test_batch_edit_empty_old_string_rejected(tmp_path):
    p = tmp_path / "note.py"
    p.write_text("hello\n", encoding="utf-8")
    out = tools.batch_edit(str(p), [{"old_string": "", "new_string": "x"}])
    assert "Error" in out
    assert "empty old_string" in out
    assert p.read_text(encoding="utf-8") == "hello\n"


def test_batch_edit_no_op_aborts(tmp_path):
    p = tmp_path / "note.py"
    original = "hello\n"
    p.write_text(original, encoding="utf-8")
    out = tools.batch_edit(str(p), [{"old_string": "hello", "new_string": "hello"}])
    assert "Error" in out
    assert "no-op" in out
    assert p.read_text(encoding="utf-8") == original


def test_find_unique_anchor_registered_in_all_tools():
    assert tools.find_unique_anchor in tools.ALL_TOOLS
    assert tools.TOOL_MAP.get("find_unique_anchor") is tools.find_unique_anchor


def test_batch_edit_registered_in_all_tools():
    assert tools.batch_edit in tools.ALL_TOOLS
    assert tools.TOOL_MAP.get("batch_edit") is tools.batch_edit


def test_batch_edit_in_mutating_tools():
    from algo_cli.tool_policy import MUTATING_TOOLS, WRITE_TOOLS

    assert "batch_edit" in MUTATING_TOOLS
    assert "batch_edit" in WRITE_TOOLS
