"""Discovery and reads share root-relative exclusions on every platform."""

import os
from pathlib import Path

import pytest

from algo_cli import harness


def _source(monkeypatch, directory: Path):
    directory.mkdir(parents=True)
    root = harness.SourceRoot("fixture", "wiki", directory, ("*.md",), 50)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (root,))
    return root


def _read(monkeypatch, root, path, **overrides):
    record = harness.make_record(root, path)
    record.update(overrides)
    monkeypatch.setattr(harness, "get_record", lambda _identity: record)
    return harness.read_record(record["id"])


@pytest.mark.parametrize("ancestor", sorted(harness._SKIP_DIRS))
def test_discovered_source_remains_readable_under_skipped_ancestor(monkeypatch, tmp_path, ancestor):
    root = _source(monkeypatch, tmp_path / ancestor / "public")
    path = root.root / "guide.md"
    path.write_text("# Guide\nPublic body.\n", encoding="utf-8")
    assert harness.iter_files(root) == [path]
    assert "Public body." in _read(monkeypatch, root, path)


@pytest.mark.parametrize("directory", sorted(harness._SKIP_DIRS) + ["credentials", "secrets", "tokens"])
def test_nested_excluded_path_cannot_be_read_from_a_stale_record(monkeypatch, tmp_path, directory):
    root = _source(monkeypatch, tmp_path / "public")
    path = root.root / directory / "guide.md"
    path.parent.mkdir()
    path.write_text("PRIVATE_SOURCE_CANARY", encoding="utf-8")
    assert harness.iter_files(root) == []
    result = _read(monkeypatch, root, path, relative_path="guide.md")
    assert result.startswith("Error: record points to a skipped/sensitive path.")
    assert "PRIVATE_SOURCE_CANARY" not in result


@pytest.mark.parametrize("filename", ["auth.json", "credentials.md", "secrets.md", "tokens.md"])
def test_filename_exclusions_survive_root_relative_reads(monkeypatch, tmp_path, filename):
    root = _source(monkeypatch, tmp_path / "public")
    path = root.root / filename
    path.write_text("PRIVATE_SOURCE_CANARY", encoding="utf-8")
    assert _read(monkeypatch, root, path).startswith("Error:")


def test_cached_record_does_not_authorize_a_removed_source_root(monkeypatch, tmp_path):
    root = _source(monkeypatch, tmp_path / "tmp" / "public")
    path = root.root / "guide.md"
    path.write_text("Public body.", encoding="utf-8")
    assert "Public body." in _read(monkeypatch, root, path)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", ())
    assert harness.read_record("fixture:wiki:guide.md").startswith("Error:")


def test_dotdot_path_cannot_erase_an_excluded_component(monkeypatch, tmp_path):
    root = _source(monkeypatch, tmp_path / "public")
    (root.root / "logs").mkdir()
    (root.root / "guide.md").write_text("Public body.", encoding="utf-8")
    assert _read(monkeypatch, root, root.root / "logs" / ".." / "guide.md").startswith("Error:")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_resolving_a_link_cannot_erase_an_excluded_component(monkeypatch, tmp_path):
    root = _source(monkeypatch, tmp_path / "public")
    target = root.root / "guide.md"
    target.write_text("Public body.", encoding="utf-8")
    link = root.root / "logs" / "guide.md"
    link.parent.mkdir()
    link.symlink_to(target)
    assert _read(monkeypatch, root, link).startswith("Error:")


def test_most_specific_configured_root_defines_the_relative_path(monkeypatch, tmp_path):
    root = _source(monkeypatch, tmp_path / "tmp" / "public")
    parent = harness.SourceRoot("fixture", "wiki", tmp_path, ("*.md",), 50)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (parent, root))
    path = root.root / "guide.md"
    path.write_text("Public body.", encoding="utf-8")
    assert "Public body." in _read(monkeypatch, root, path)
