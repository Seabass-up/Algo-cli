"""Regression tests for public-release privacy scanning."""

from scripts import check_public_release


def test_machine_path_scan_catches_literal_and_source_escaped_windows_paths():
    literal = "C:" + "\\".join(("", "Users", "private-user", "workspace"))
    source_escaped = "C:" + "\\\\".join(("", "Users", "private-user", "workspace"))

    assert check_public_release._scan_text("fixture.py", literal)
    assert check_public_release._scan_text("fixture.py", source_escaped)


def test_machine_path_scan_allows_neutral_windows_fixture():
    assert check_public_release._scan_text("fixture.py", r"C:\\Users\\example\\workspace") == []
