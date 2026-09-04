"""Tests for working-directory code RAG."""

import os
from pathlib import Path
import subprocess

import pytest

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
    _write(tmp_path, "benchmark-results/run/generated.py", "generated = True")
    _write(tmp_path, "__pycache__/x.py", "cached")
    index = code_rag.build_or_update_index(str(tmp_path))
    rels = {c["relative_path"] for c in index["chunks"]}
    assert "keep.py" in rels
    assert "image.png" not in rels
    assert not any("node_modules" in r for r in rels)
    assert not any("benchmark-results" in r for r in rels)
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


def test_symlink_inside_project_is_not_indexed(tmp_path):
    _write(tmp_path, "real.py", "def widget():\n    return 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
    index = code_rag.build_or_update_index(str(tmp_path), force=True)
    rels = {c["relative_path"] for c in index["chunks"]}
    assert rels == {"real.py"}


def test_symlink_escape_outside_project_is_not_indexed(tmp_path):
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("leaked = True\n", encoding="utf-8")
    (tmp_path / "innocent.py").symlink_to(outside)
    _write(tmp_path, "app.py", "widget = 1\n")
    index = code_rag.build_or_update_index(str(tmp_path), force=True)
    rels = {c["relative_path"] for c in index["chunks"]}
    assert rels == {"app.py"}


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_junctioned_source_directory_outside_project_is_not_indexed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write(workspace, "app.py", "widget = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside, "leak.py", "JUNCTION_SECRET_CANARY = True\n")
    junction = workspace / "vendor"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")

    index = code_rag.build_or_update_index(str(workspace), force=True)
    rels = {chunk["relative_path"] for chunk in index["chunks"]}
    assert rels == {"app.py"}
    assert "JUNCTION_SECRET_CANARY" not in repr(index)


def test_symlink_to_secret_path_inside_project_is_not_indexed(tmp_path):
    _write(tmp_path, "app.py", "widget = 1\n")
    _write(tmp_path, "secrets/real.py", "api_key = 'hunter2'\n")
    (tmp_path / "linked_secret.py").symlink_to(tmp_path / "secrets" / "real.py")
    index = code_rag.build_or_update_index(str(tmp_path), force=True)
    rels = {c["relative_path"] for c in index["chunks"]}
    assert rels == {"app.py"}


def test_hardlinked_source_is_not_read_embedded_or_persisted(tmp_path, monkeypatch):
    canary = "ECHO_HARDLINK_CANARY_92eb60"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = tmp_path / "legacy-user.md"
    legacy.write_text(canary + "\n", encoding="utf-8")
    os.link(legacy, workspace / "profile.md")
    index_dir = tmp_path / "code_index"
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)

    embedded: list[str] = []

    def embed(texts):
        embedded.extend(texts)
        return [[1.0] for _ in texts]

    index = code_rag.ensure_embeddings(str(workspace), embed, "fake-model")

    assert index["files"] == {}
    assert index["chunks"] == []
    assert embedded == []
    persisted = b"".join(path.read_bytes() for path in index_dir.rglob("*") if path.is_file() and not path.is_symlink())
    assert canary.encode() not in persisted


def test_source_hardlinked_during_scan_is_rejected_before_index_publish(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("def widget():\n    return 1\n", encoding="utf-8")
    alias = tmp_path.parent / f"{tmp_path.name}-late-hardlink.py"
    index_dir = tmp_path / "code_index"
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    real_build_project_graph = code_rag.build_project_graph

    def build_then_link(*args, **kwargs):
        graph = real_build_project_graph(*args, **kwargs)
        os.link(source, alias)
        return graph

    monkeypatch.setattr(code_rag, "build_project_graph", build_then_link)
    try:
        index = code_rag.build_or_update_index(str(tmp_path), force=True)
    finally:
        alias.unlink(missing_ok=True)

    assert index["files"] == {}
    assert index["chunks"] == []
    assert not code_rag._index_path_for(str(tmp_path)).exists()


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


def test_purge_persisted_indexes_removes_preexisting_plaintext_canary(tmp_path, monkeypatch):
    canary = "ECHO_PREEXISTING_INDEX_CANARY_5d09f1"
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    index_path = index_dir / "legacy.json"
    index_path.write_text('{"chunks":[{"text":"' + canary + '"}]}', encoding="utf-8")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    code_rag._INDEX_MEM["legacy"] = {"chunks": [{"text": canary}]}

    assert code_rag.purge_persisted_indexes() == 1

    assert not index_dir.exists()
    assert code_rag._INDEX_MEM == {}
    assert canary not in repr(code_rag._INDEX_MEM)


def test_purge_persisted_indexes_full_path_fallback_preserves_identity_checks(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    (index_dir / "one.json").write_text("one", encoding="utf-8")
    (index_dir / "two.json").write_text("two", encoding="utf-8")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    monkeypatch.setattr(code_rag, "_windows_path_purge_required", lambda: True)

    assert code_rag.purge_persisted_indexes() == 2
    assert not index_dir.exists()


def test_full_path_purge_accepts_safe_nonprivate_legacy_index_root(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    (index_dir / "legacy.json").write_text("legacy", encoding="utf-8")
    checked: list[Path] = []
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    monkeypatch.setattr(code_rag, "_windows_path_purge_required", lambda: True)
    monkeypatch.setattr(
        code_rag,
        "_windows_safe_creation_dacl",
        lambda path: checked.append(path) or True,
    )

    assert code_rag.purge_persisted_indexes() == 1
    assert checked == [index_dir.parent, index_dir]
    assert not index_dir.exists()


def test_full_path_purge_requires_safe_parent_before_accepting_absence(tmp_path, monkeypatch):
    index_dir = tmp_path / "missing-code-index"
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    monkeypatch.setattr(code_rag, "_windows_path_purge_required", lambda: True)
    monkeypatch.setattr(code_rag, "_windows_safe_creation_dacl", lambda _path: False)

    with pytest.raises(OSError, match="parent ACL is unsafe"):
        code_rag.purge_persisted_indexes()

    monkeypatch.setattr(code_rag, "_windows_safe_creation_dacl", lambda _path: True)
    assert code_rag.purge_persisted_indexes() == 0


def test_full_path_purge_does_not_hide_missing_entry_after_partial_deletion(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    first = index_dir / "one.json"
    second = index_dir / "two.json"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    monkeypatch.setattr(code_rag, "_windows_path_purge_required", lambda: True)
    if os.name == "nt":
        from algo_cli import tools

        real_delete = tools._windows_delete_path_by_identity

        def fail_second(
            path: Path,
            expected: os.stat_result,
            *,
            allow_directory: bool,
        ) -> None:
            if path == second:
                raise FileNotFoundError(path)
            real_delete(path, expected, allow_directory=allow_directory)

        monkeypatch.setattr(tools, "_windows_delete_path_by_identity", fail_second)
    else:
        real_unlink = Path.unlink

        def fail_second_portable(path: Path, *args, **kwargs):
            if path == second:
                raise FileNotFoundError(path)
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_second_portable)

    with pytest.raises(FileNotFoundError):
        code_rag.purge_persisted_indexes()

    assert not first.exists()
    assert second.read_text(encoding="utf-8") == "two"
    assert index_dir.exists()


def test_full_path_purge_absolutizes_relative_store_before_deletion(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    index_dir = Path("code_index")
    index_dir.mkdir()
    (index_dir / "legacy.json").write_text("legacy", encoding="utf-8")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    monkeypatch.setattr(code_rag, "_windows_path_purge_required", lambda: True)

    assert code_rag.purge_persisted_indexes() == 1
    assert not index_dir.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_windows_purge_removes_leaf_junction_without_following(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = outside / "keep.json"
    retained.write_text("protected", encoding="utf-8")
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    junction = index_dir / "linked"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)

    assert code_rag.purge_persisted_indexes() == 1
    assert retained.read_text(encoding="utf-8") == "protected"
    assert not index_dir.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_windows_purge_rejects_junctioned_ancestor_before_deletion(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    index_dir = outside / "code_index"
    index_dir.mkdir(parents=True)
    retained = index_dir / "keep.json"
    retained.write_text("protected", encoding="utf-8")
    alias = tmp_path / "alias"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(outside)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", alias / "code_index")

    with pytest.raises(OSError, match="ancestry is unsafe"):
        code_rag.purge_persisted_indexes()
    assert retained.read_text(encoding="utf-8") == "protected"


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


def test_purge_persisted_indexes_unlinks_leaf_symlink_without_following(tmp_path, monkeypatch):
    outside = tmp_path / "outside.json"
    outside.write_text("ECHO_OUTSIDE_CANARY", encoding="utf-8")
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    (index_dir / "linked.json").symlink_to(outside)
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)

    assert code_rag.persisted_index_count() == 1
    assert code_rag.purge_persisted_indexes() == 1
    assert outside.read_text(encoding="utf-8") == "ECHO_OUTSIDE_CANARY"
    assert not index_dir.exists()


def test_purge_persisted_indexes_rejects_external_hardlink(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    index_path = index_dir / "legacy.json"
    index_path.write_text("protected", encoding="utf-8")
    alias = tmp_path / "outside-alias.json"
    os.link(index_path, alias)
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)

    expected_message = "unexpected nested or special" if os.name == "nt" else "external hardlink"
    with pytest.raises(OSError, match=expected_message):
        code_rag.purge_persisted_indexes()
    assert index_path.read_text(encoding="utf-8") == "protected"
    assert alias.read_text(encoding="utf-8") == "protected"


def test_purge_persisted_indexes_fails_closed_on_nested_canary(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    nested = index_dir / "legacy-layout" / "deeper"
    nested.mkdir(parents=True)
    canary = nested / "old.json"
    canary.write_text("ECHO_NESTED_INDEX_CANARY", encoding="utf-8")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)

    assert code_rag.persisted_index_count() == 1
    with pytest.raises(OSError, match="unexpected nested or special"):
        code_rag.purge_persisted_indexes()

    assert canary.read_text(encoding="utf-8") == "ECHO_NESTED_INDEX_CANARY"
    assert code_rag.persisted_index_count() == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support is unavailable")
def test_purge_persisted_indexes_fails_closed_on_special_entry(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    retained = index_dir / "retained.json"
    retained.write_text("retained", encoding="utf-8")
    fifo = index_dir / "unexpected.fifo"
    os.mkfifo(fifo)
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)

    with pytest.raises(OSError, match="unexpected nested or special"):
        code_rag.purge_persisted_indexes()

    assert retained.read_text(encoding="utf-8") == "retained"
    assert fifo.exists()


def test_purge_persisted_indexes_surfaces_partial_delete_failure(tmp_path, monkeypatch):
    index_dir = tmp_path / "code_index"
    index_dir.mkdir()
    (index_dir / "one.json").write_text("one", encoding="utf-8")
    (index_dir / "two.json").write_text("two", encoding="utf-8")
    monkeypatch.setattr(code_rag, "CODE_INDEX_DIR", index_dir)
    if os.name == "nt":
        from algo_cli import tools

        real_delete = tools._windows_delete_path_by_identity

        def fail_second(
            path: Path,
            expected: os.stat_result,
            *,
            allow_directory: bool,
        ) -> None:
            if path.name == "two.json":
                raise OSError("injected identity-delete failure")
            real_delete(path, expected, allow_directory=allow_directory)

        monkeypatch.setattr(tools, "_windows_delete_path_by_identity", fail_second)
    else:
        real_unlink = os.unlink

        def fail_second_portable(path, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == "two.json":
                raise OSError("injected identity-delete failure")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(code_rag.os, "unlink", fail_second_portable)

    with pytest.raises(OSError, match="injected identity-delete failure"):
        code_rag.purge_persisted_indexes()

    assert (index_dir / "two.json").read_text(encoding="utf-8") == "two"
    assert code_rag.persisted_index_count() == 1


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
