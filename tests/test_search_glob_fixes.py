from __future__ import annotations

import re
import warnings

import pytest

from algo_cli import search_execution

ODD_GLOBS = ["[", "[]", "[!]", "*[^]", "[a-", "\\", "[[:alpha:]]", "**/[z-a]*", "[^]]", "[a--]", "[&&~~||]"]


@pytest.mark.parametrize("glob", ODD_GLOBS)
def test_odd_globs_never_raise(glob):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        re.compile(search_execution._glob_regex(glob))
        matches = search_execution.glob_matcher(glob)
        for target in ["a", "src/app.py", "[", "[^]", "x[^]", "[z-a]b", "\\", "]"]:
            matches(target)


def test_unterminated_negated_class_matches_literally():
    matches = search_execution.glob_matcher("*[^]")
    assert matches("notes[^]")
    assert not matches("notes.py")


def test_reversed_range_matches_literally():
    matches = search_execution.glob_matcher("**/[z-a]*")
    assert matches("src/[z-a]file")
    assert not matches("src/main.py")


def test_unterminated_and_empty_classes_match_literally():
    assert search_execution.glob_matcher("[a-")("[a-")
    assert search_execution.glob_matcher("[]")("[]")
    assert search_execution.glob_matcher("[!]")("[!]")
    assert search_execution.glob_matcher("\\")("\\")


def test_valid_classes_keep_their_meaning():
    assert search_execution.glob_matcher("test_[a-c]*.py")("tests/test_b1.py")
    assert not search_execution.glob_matcher("test_[a-c]*.py")("tests/test_d1.py")
    assert search_execution.glob_matcher("[!a]*.py")("b.py")
    assert not search_execution.glob_matcher("[!a]*.py")("a.py")
    assert search_execution.glob_matcher("[^]]x")("ax")
    assert not search_execution.glob_matcher("[^]]x")("]x")
    assert search_execution.glob_matcher("[]a]x")("]x")
    assert search_execution.glob_matcher("[a-]x")("-x")
    assert search_execution.glob_matcher("[\\]x")("\\x")


def test_character_class_is_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr(search_execution.os, "name", "nt")
    assert search_execution.glob_matcher("[a-c]*.PY")("B.py")
    monkeypatch.setattr(search_execution.os, "name", "posix")
    assert not search_execution.glob_matcher("[a-c]*.PY")("B.py")


def test_python_search_child_survives_odd_glob(tmp_path, capsys):
    (tmp_path / "notes[^]").write_text("needle\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("needle\n", encoding="utf-8")
    request = {
        "path": str(tmp_path),
        "pattern": "needle",
        "glob": "*[^]",
        "max_files": 100,
        "max_file_bytes": 1024,
        "skip_dirs": [],
        "limit": 10,
    }
    assert search_execution._python_search(request) == 0
    out = capsys.readouterr().out
    assert "notes[^]" in out
    assert "other.txt" not in out
