"""Tests for working-directory code RAG."""

from algo_cli import code_rag


def _make_embed():
    """Deterministic keyword-biased embedder: vector dim = len(vocab)."""
    vocab = ["alpha", "beta", "gamma", "delta", "widget", "gadget", "parser", "loop"]

    def embed(texts):
        out = []
        for t in texts:
            low = t.lower()
            out.append([float(low.count(w)) + 0.01 for w in vocab])
        return out

    return embed


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_build_index_chunks_source(tmp_path):
    _write(tmp_path, "a.py", "\n".join(f"line {i} alpha" for i in range(120)))
    _write(tmp_path, "pkg/b.py", "def widget():\n    return 1\n")
    index = code_rag.build_or_update_index(str(tmp_path))
    rels = {c["relative_path"] for c in index["chunks"]}
    assert "a.py" in rels
    assert "pkg/b.py" in rels
    # 120 lines with 60-line chunks / 50-line step -> at least 2 chunks for a.py
    assert sum(1 for c in index["chunks"] if c["relative_path"] == "a.py") >= 2


def test_skips_non_code_and_skip_dirs(tmp_path):
    _write(tmp_path, "keep.py", "widget = 1\n")
    _write(tmp_path, "image.png", "not text")
    _write(tmp_path, "node_modules/dep.js", "stuff")
    _write(tmp_path, "__pycache__/x.py", "cached")
    index = code_rag.build_or_update_index(str(tmp_path))
    rels = {c["relative_path"] for c in index["chunks"]}
    assert "keep.py" in rels
    assert "image.png" not in rels
    assert not any("node_modules" in r for r in rels)
    assert not any("__pycache__" in r for r in rels)


def test_incremental_reuse_unchanged(tmp_path):
    _write(tmp_path, "a.py", "alpha = 1\n")
    embed = _make_embed()
    idx1 = code_rag.ensure_embeddings(str(tmp_path), embed, "fake-model")
    chunk = next(c for c in idx1["chunks"] if c["relative_path"] == "a.py")
    assert chunk.get("embedding_model") == "fake-model"
    # Re-run without changing the file: embedding is reused (still present).
    idx2 = code_rag.build_or_update_index(str(tmp_path))
    chunk2 = next(c for c in idx2["chunks"] if c["relative_path"] == "a.py")
    assert chunk2.get("embedding") == chunk.get("embedding")


def test_retrieve_ranks_relevant_chunk(tmp_path):
    _write(tmp_path, "parser.py", "def parser():\n    return 'parser parser parser'\n")
    _write(tmp_path, "widget.py", "def widget():\n    return 'widget widget widget'\n")
    embed = _make_embed()
    hits = code_rag.retrieve(str(tmp_path), "fix the parser", embed, "fake-model", k=2)
    assert hits
    assert hits[0]["relative_path"] == "parser.py"


def test_format_code_context_includes_location(tmp_path):
    results = [{"relative_path": "x.py", "start_line": 3, "end_line": 9, "text": "x.py:3\ncode", "score": 0.9}]
    block = code_rag.format_code_context(results)
    assert "x.py:3-9" in block
    assert "```" in block


def test_looks_like_code_project(tmp_path):
    assert code_rag.looks_like_code_project(str(tmp_path)) is False
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert code_rag.looks_like_code_project(str(tmp_path)) is True


def test_secret_files_never_indexed(tmp_path):
    _write(tmp_path, "app.py", "widget = 1\n")
    _write(tmp_path, "secrets.yaml", "api: hunter2\n")
    _write(tmp_path, "api_key.json", '{"k": "x"}\n')
    _write(tmp_path, "auth-config.toml", "token = 'y'\n")
    index = code_rag.build_or_update_index(str(tmp_path))
    rels = {c["relative_path"] for c in index["chunks"]}
    assert rels == {"app.py"}


def test_secret_directories_never_indexed(tmp_path):
    _write(tmp_path, "app.py", "widget = 1\n")
    _write(tmp_path, "secrets/settings.py", "api_key = 'hunter2'\n")
    _write(tmp_path, "credentials/client.py", "token = 'secret'\n")
    index = code_rag.build_or_update_index(str(tmp_path), force=True)
    rels = {c["relative_path"] for c in index["chunks"]}
    assert rels == {"app.py"}


def test_symlink_inside_project_is_indexed(tmp_path):
    _write(tmp_path, "real.py", "def widget():\n    return 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    index = code_rag.build_or_update_index(str(tmp_path), force=True)
    rels = {c["relative_path"] for c in index["chunks"]}
    assert "link.py" in rels


def test_symlink_escape_outside_project_is_not_indexed(tmp_path):
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("leaked = True\n", encoding="utf-8")
    (tmp_path / "innocent.py").symlink_to(outside)
    _write(tmp_path, "app.py", "widget = 1\n")
    index = code_rag.build_or_update_index(str(tmp_path), force=True)
    rels = {c["relative_path"] for c in index["chunks"]}
    assert rels == {"app.py"}


def test_symlink_to_secret_path_inside_project_is_not_indexed(tmp_path):
    _write(tmp_path, "app.py", "widget = 1\n")
    _write(tmp_path, "secrets/real.py", "api_key = 'hunter2'\n")
    (tmp_path / "linked_secret.py").symlink_to(tmp_path / "secrets" / "real.py")
    index = code_rag.build_or_update_index(str(tmp_path), force=True)
    rels = {c["relative_path"] for c in index["chunks"]}
    assert rels == {"app.py"}


def test_embed_text_front_loads_symbols():
    body_lines = ["# comment filler"] * 40 + ["def find_the_widget(x):", "    return x"]
    chunk = {"text": "pkg/mod.py:1\n" + "\n".join(body_lines)}
    embed_text = code_rag.embed_text_for(chunk)
    assert len(embed_text) <= code_rag.EMBED_TEXT_CHARS + 20
    assert "def find_the_widget" in embed_text  # symbol line survives truncation
    assert embed_text.startswith("pkg/mod.py:1")


def test_scan_throttle_reuses_memory_index(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "alpha = 1\n")
    code_rag.invalidate_cache()
    walks = {"n": 0}
    real_iter = code_rag._iter_source_files

    def counting_iter(root):
        walks["n"] += 1
        return real_iter(root)

    monkeypatch.setattr(code_rag, "_iter_source_files", counting_iter)
    code_rag.build_or_update_index(str(tmp_path))
    code_rag.build_or_update_index(str(tmp_path))  # within TTL -> cached
    assert walks["n"] == 1
    code_rag.build_or_update_index(str(tmp_path), force=True)
    assert walks["n"] == 2


def test_purge_persisted_indexes_removes_files_and_memory_cache(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    (index_dir / "one.json").write_text("{}", encoding="utf-8")
    (index_dir / "two.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    code_rag._INDEX_MEM["fixture"] = {"chunks": []}
    code_rag._LAST_SCAN["fixture"] = 1.0

    assert code_rag.persisted_index_count() == 2
    assert code_rag.purge_persisted_indexes() == 2
    assert code_rag.persisted_index_count() == 0
    assert not index_dir.exists()
    assert code_rag._INDEX_MEM == {}
    assert code_rag._LAST_SCAN == {}


def test_purge_persisted_indexes_does_not_follow_directory_symlink(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = outside / "keep.json"
    retained.write_text("{}", encoding="utf-8")
    index_link = tmp_path / "code_index"
    index_link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_link)

    assert code_rag.purge_persisted_indexes() == 1
    assert not index_link.exists()
    assert retained.exists()


def test_numpy_path_is_true_cosine(tmp_path):
    """An un-normalized embedder must rank by direction, not magnitude."""
    _write(tmp_path, "big.py", "unrelated = 'beta beta beta beta beta beta'\n")
    _write(tmp_path, "small.py", "match = 'alpha'\n")
    code_rag.invalidate_cache()

    def embed(texts):
        out = []
        for t in texts:
            low = t.lower()
            # big magnitudes on the beta axis, small on alpha
            out.append([float(low.count("alpha")), float(low.count("beta")) * 10.0, 0.01])
        return out

    hits = code_rag.retrieve(str(tmp_path), "alpha", embed, "fake-model", k=2)
    assert hits
    assert hits[0]["relative_path"] == "small.py"  # direction wins over magnitude
