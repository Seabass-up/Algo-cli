from __future__ import annotations

import os
import json

import pytest

from algo_cli import harness, main, tools, index_compute_lab, oliver_slash_dispatch
from algo_cli.config import Config


def test_retired_tools_are_not_model_discoverable():
    assert {"embed_text", "query_knowledge_graph", "reindex_knowledge_graph", "write_knowledge_graph_note"}.isdisjoint(tools.TOOL_MAP)
    commands = {name for name, _ in oliver_slash_dispatch.SLASH_COMMANDS}
    assert {"/embed", "/icl", "/intuition", "/code-rag", "/harness embed", "/harness benchmark-embed"}.isdisjoint(commands)
    groups = json.loads(tools.available_actions("slash"))["focused"]["commands"]
    offered = [item for group in groups.values() for item in group]
    for retired in ("/embed", "/icl", "/intuition", "/code-rag", "/harness embed"):
        assert not any(item == retired or item.startswith(retired + " ") for item in offered)


@pytest.mark.parametrize("command", ["/embed hello", "/icl on", "/harness embed", "/harness benchmark-embed", "/intuition on", "/code-rag on", "/lessons reindex"])
def test_retired_commands_return_without_model_or_lab_calls(monkeypatch, command):
    messages = []
    monkeypatch.setattr(main, "show_error", messages.append)
    monkeypatch.setattr(main, "make_embed_fn", lambda *a, **kw: pytest.fail("No embedding calls"))
    handled, _ = oliver_slash_dispatch.handle_command(command, Config(), object())
    assert handled and messages and "retired" in messages[0]


def test_embedding_factories_cannot_contact_a_provider():
    with pytest.raises(RuntimeError, match="retired"):
        main.make_local_embed_fn(Config(), "qwen3-embedding:latest")
    with pytest.raises(RuntimeError, match="retired"):
        main.make_embed_fn(Config(), "qwen3-embedding:latest")
    assert "retired" in tools.embed_text("private")
    assert tools.gateway_embed("private", "unused", True, None)["error"] == "cli_embeddings_retired"


def test_lexical_harness_search_does_not_probe_embedding_identity(monkeypatch):
    index = {"records": [{"id": "algo-cli:skill:example", "title": "Native reference", "path": "/synthetic/reference.md",
                          "harness": "algo-cli", "kind": "skill", "search_text": "native reference", "description": "Reference only"}]}
    monkeypatch.setattr(harness, "retrieval_index", lambda **kw: index)
    monkeypatch.setattr(main, "make_local_embed_fn", lambda *a, **kw: pytest.fail("No embedding calls"))
    result = tools.harness_search("native reference", cfg=Config(continuum_enabled=True))
    assert "algo-cli:skill:example" in result and "keyword-only" in result


def test_lab_bridge_never_executes_or_persists(monkeypatch):
    assert index_compute_lab.lab_available() is False
    assert index_compute_lab.atoms_dir() is None
    assert index_compute_lab.context_for_query("private") == ""
    assert "retired" in index_compute_lab.run_ask("private")
    assert "retired" in index_compute_lab.run_pipeline()
    assert "retired" in index_compute_lab.write_graph_note("private", "private")


def test_exact_catalog_root_is_not_invalidated_by_generated_state(monkeypatch, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    catalog = root / "ALGO.md"
    catalog.write_text("old\n")
    index_path = root / "harness_index.json"
    index_path.write_text("{}")
    os.utime(catalog, ns=(1_000_000_000, 1_000_000_000))
    os.utime(index_path, ns=(2_000_000_000, 2_000_000_000))
    record = {"path": str(catalog), "file_size": catalog.stat().st_size,
              "file_mtime_ns": catalog.stat().st_mtime_ns}
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (harness.SourceRoot("algo-cli", "algorithm", root, ("ALGO.md",), 1),))
    (root / "unrelated_state.json").write_text("generated state")
    assert harness._source_watermark_ns({"records": [record]}) == 0
    catalog.write_text("changed\n")
    os.utime(catalog, ns=(3_000_000_000, 3_000_000_000))
    assert harness._source_watermark_ns({"records": [record]}) == catalog.stat().st_mtime_ns


def test_normalization_drops_malformed_cache_records():
    index = harness._normalize_index_records({"records": [None, "bad", 42], "record_count": 3})
    assert index["records"] == [] and index["record_count"] == 0


def test_catalog_size_change_with_restored_timestamp_invalidates_index(monkeypatch, tmp_path):
    catalog = tmp_path / "ALGO.md"
    catalog.write_text("old\n")
    index_path = tmp_path / "harness_index.json"
    index_path.write_text("{}")
    os.utime(catalog, ns=(1_000_000_000, 1_000_000_000))
    os.utime(index_path, ns=(2_000_000_000, 2_000_000_000))
    observed = catalog.stat()
    record = {"path": str(catalog), "file_size": observed.st_size, "file_mtime_ns": observed.st_mtime_ns}
    monkeypatch.setattr(harness, "INDEX_PATH", index_path)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (
        harness.SourceRoot("algo-cli", "algorithm", tmp_path, ("ALGO.md",), 1),))
    catalog.write_text("changed size\n")
    os.utime(catalog, ns=(observed.st_mtime_ns, observed.st_mtime_ns))
    assert harness._source_watermark_ns({"records": [record]}) > index_path.stat().st_mtime_ns
