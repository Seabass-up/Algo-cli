"""Regression tests for code-RAG and harness-index freshness fixes."""

from __future__ import annotations

import json
import os
import time

from algo_cli import code_rag, harness


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _width_embed(width):
    vocab = ["alpha", "beta", "gamma", "delta", "widget", "gadget", "parser", "loop"][:width]

    def embed(texts):
        return [[float(text.lower().count(word)) + 0.01 for word in vocab] for text in texts]

    return embed


def test_code_rag_reembeds_after_embedding_width_change(tmp_path):
    _write(tmp_path, "parser.py", "def parser():\n    return 'parser alpha'\n")
    _write(tmp_path, "widget.py", "def widget():\n    return 'widget beta'\n")

    assert code_rag.retrieve(str(tmp_path), "alpha parser", _width_embed(8), "fake-model", k=2)

    hits = code_rag.retrieve(str(tmp_path), "alpha", _width_embed(4), "fake-model", k=2)

    assert hits
    index = code_rag.build_or_update_index(str(tmp_path))
    assert {len(chunk["embedding"]) for chunk in index["chunks"]} == {4}
    assert {chunk["embedding_dimensions"] for chunk in index["chunks"]} == {4}


def test_code_rag_reembeds_when_embedding_identity_changes(tmp_path):
    _write(tmp_path, "parser.py", "def parser():\n    return 'parser alpha'\n")
    calls: list[int] = []

    def bound(identity):
        base = _width_embed(8)

        def embed(texts):
            calls.append(len(texts))
            return base(texts)

        embed.embedding_identity = identity
        embed.validate_embedding_identity = lambda: True
        return embed

    first = bound("sha256:" + "a" * 64)
    code_rag.retrieve(str(tmp_path), "alpha", first, "fake-model")
    index = code_rag.build_or_update_index(str(tmp_path))
    assert {chunk["embedding_identity"] for chunk in index["chunks"]} == {"sha256:" + "a" * 64}

    second = bound("sha256:" + "b" * 64)
    code_rag.retrieve(str(tmp_path), "alpha", second, "fake-model")
    index = code_rag.build_or_update_index(str(tmp_path))
    assert {chunk["embedding_identity"] for chunk in index["chunks"]} == {"sha256:" + "b" * 64}


def test_code_rag_keeps_legacy_unbound_chunks_retrievable_during_identity_migration(tmp_path, monkeypatch):
    for i in range(10):
        _write(tmp_path, f"mod{i}.py", f"def parser{i}():\n    return 'parser alpha {i}'\n")
    code_rag.retrieve(str(tmp_path), "alpha", _width_embed(8), "fake-model", k=20)
    index = code_rag.build_or_update_index(str(tmp_path))
    assert len(index["chunks"]) == 10
    assert not any("embedding_identity" in chunk for chunk in index["chunks"])

    monkeypatch.setattr(code_rag, "EMBED_PER_TURN_CAP", 3)
    bound = _width_embed(8)
    bound.embedding_identity = "sha256:" + "a" * 64
    bound.validate_embedding_identity = lambda: True

    hits = code_rag.retrieve(str(tmp_path), "alpha parser", bound, "fake-model", k=20)

    assert len(hits) == 10
    index = code_rag.build_or_update_index(str(tmp_path))
    assert sum(1 for chunk in index["chunks"] if chunk.get("embedding_identity")) == 3

    # A chunk tagged with a different identity is never a candidate.
    for chunk in index["chunks"]:
        chunk["embedding_identity"] = "sha256:" + "c" * 64
    assert code_rag._save_index(str(tmp_path), index)
    monkeypatch.setattr(code_rag, "EMBED_PER_TURN_CAP", 0)
    assert code_rag.retrieve(str(tmp_path), "alpha parser", bound, "fake-model", k=20) == []


def test_code_rag_repo_map_drops_deleted_file(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname = 'demo'\n")
    _write(tmp_path, "a.py", "def alpha():\n    return 1\n")
    _write(tmp_path, "b.py", "def beta():\n    return 2\n")
    first = code_rag.build_or_update_index(str(tmp_path), force=True)
    assert "b.py" in json.dumps(first["structural"])

    (tmp_path / "b.py").unlink()
    index = code_rag.build_or_update_index(str(tmp_path), force=True)

    assert "b.py" not in index["files"]
    assert "b.py" not in json.dumps(index["structural"])
    assert "b.py" not in code_rag.render_repo_map(index["structural"], "beta")


def test_future_dated_indexed_source_does_not_keep_index_stale(monkeypatch, tmp_path):
    root_dir = tmp_path / "skills"
    root_dir.mkdir()
    source = root_dir / "SKILL.md"
    source.write_text("# future skill\n", encoding="utf-8")
    future = time.time() + 5 * 365 * 24 * 3600
    os.utime(source, (future, future))
    source_stat = source.stat()
    index_path = tmp_path / "harness_index.json"
    index_path.write_text(
        json.dumps(
            {
                "source_policy": harness._source_policy(),
                "records": [
                    {
                        "id": "test:skills:SKILL.md",
                        "path": str(source),
                        "file_mtime_ns": source_stat.st_mtime_ns,
                        "file_size": source_stat.st_size,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    monkeypatch.setattr(harness, "SOURCE_ROOTS", (harness.SourceRoot("test", "skills", root_dir, ("*.md",), 10),))
    monkeypatch.setattr(harness, "load_extra_source_roots", lambda: [])

    assert harness.index_is_stale() is False

    source.write_text("# future skill, edited\n", encoding="utf-8")
    os.utime(source, (future, future))

    assert harness.index_is_stale() is True
