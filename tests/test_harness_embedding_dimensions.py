"""Requested dimensions, including model-default mode, bind index readiness."""

import json
import inspect
import os

import pytest

from algo_cli import harness, main, tools
from algo_cli.config import Config


@pytest.fixture
def public_index(monkeypatch):
    monkeypatch.setattr(harness, "all_source_roots", lambda: ())
    records = [
        {
            "id": str(i),
            "harness": "fixture",
            "kind": "wiki",
            "title": f"Public note {i}",
            "path": f"note-{i}.md",
            "search_text": f"public note {i}",
        }
        for i in range(3)
    ]
    index = {"source_policy": harness._source_policy(), "record_count": len(records), "records": records}
    harness.INDEX_PATH.write_text(json.dumps(index))
    harness._set_index_cache(None)
    return records


def _embed(texts, width=2):
    return [[1.0] * width for _ in texts]


@pytest.mark.parametrize("before,after", [(2, 3), (2, None), (None, 2)])
def test_dimension_mode_change_rebuilds_even_when_actual_width_is_equal(public_index, before, after):
    harness.embed_index_records(lambda texts: _embed(texts, before or 2), "fixture", dimensions=before)
    assert harness.embedded_count("fixture", dimensions=after) == (0, 3)
    assert harness.embedding_progress("fixture", dimensions=after)["pending"] == 3
    assert harness.stats(model="fixture", dimensions=after)["embeddings"]["complete"] is False
    seen = []

    def embed(texts):
        seen.extend(texts)
        return _embed(texts, after or 2)

    result = harness.embed_index_records(embed, "fixture", dimensions=after)
    assert result["ready"] is True
    assert result["embedded"] == len(seen) == 3
    harness._set_index_cache(None)
    assert harness.embedded_count("fixture", dimensions=after) == (3, 3)
    assert harness.embedded_count("fixture", dimensions=before) == (0, 3)
    assert all(row["embedding_dimensions"] == after for row in harness.load_index()["records"])
    assert harness.load_index()["embeddings"]["requested_dimensions"] == after


@pytest.mark.parametrize("dimensions", [2, None])
def test_legacy_vectors_are_not_qualified_by_accidental_width_match(public_index, dimensions):
    harness.embed_index_records(_embed, "fixture")
    assert harness.embedded_count("fixture") == (3, 3)
    assert harness.embedded_count("fixture", dimensions=dimensions) == (0, 3)
    assert harness.embed_index_records(_embed, "fixture", dimensions=dimensions)["embedded"] == 3


@pytest.mark.parametrize("after", [3, None])
@pytest.mark.parametrize("stop", ["cap", "failure", "interrupt"])
def test_dimension_migration_resumes_only_current_mode_batches(public_index, after, stop):
    harness.embed_index_records(_embed, "fixture", dimensions=2)
    calls = 0

    def embed(texts):
        nonlocal calls
        calls += 1
        if calls == 2 and stop != "cap":
            if stop == "interrupt":
                raise KeyboardInterrupt
            raise RuntimeError("fixture outage")
        return _embed(texts, after or 4)

    kwargs = {"dimensions": after, "batch_size": 1, "max_records": 1 if stop == "cap" else 0}
    if stop == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            harness.embed_index_records(embed, "fixture", **kwargs)
    else:
        assert harness.embed_index_records(embed, "fixture", **kwargs)["ready"] is False
    harness._set_index_cache(None)
    assert harness.embedded_count("fixture", dimensions=after) == (1, 3)
    result = harness.embed_index_records(lambda texts: _embed(texts, after or 4), "fixture", dimensions=after)
    assert result["embedded"] == 2
    assert result["ready"] is True


def test_wrong_provider_width_cannot_be_saved_as_requested_width(public_index):
    harness.embed_index_records(_embed, "fixture", dimensions=2)
    result = harness.embed_index_records(_embed, "fixture", dimensions=3)
    assert result["ready"] is False
    assert result["reason"] == "embedding_dimension_mismatch"
    assert result["embedded"] == 0
    assert harness.embedded_count("fixture", dimensions=3) == (0, 3)
    assert harness.embedded_count("fixture", dimensions=2) == (3, 3)


@pytest.mark.parametrize("bad", [True, False, 0, -2, 2.0, "2", "default", []])
def test_invalid_dimension_contract_is_rejected_before_network_or_storage(public_index, bad):
    before = harness.INDEX_PATH.read_bytes()
    with pytest.raises(ValueError, match="dimensions"):
        harness.embed_index_records(
            lambda _: pytest.fail("invalid request reached provider"), "fixture", dimensions=bad
        )
    assert harness.INDEX_PATH.read_bytes() == before


@pytest.mark.parametrize("metadata", [True, 2.0, "2", -2])
def test_malformed_persisted_dimension_metadata_does_not_match(public_index, metadata):
    record = {
        **public_index[0],
        "embedding": [1.0, 1.0],
        "embedding_model": "fixture",
        "embedding_dimensions": metadata,
    }
    index = {"records": [record]}
    assert (
        harness.retrieve_for_query(
            "public", lambda _: pytest.fail("ineligible vector"), "fixture", dimensions=2, index=index
        )
        == []
    )


def test_query_cache_separates_default_explicit_and_unbound_modes(public_index):
    base = {**public_index[0], "embedding": [1.0, 1.0], "embedding_model": "fixture"}
    calls = []

    def embed(texts):
        calls.append(texts)
        return _embed(texts)

    for _repeat in range(2):
        for dimensions in [2, None]:
            index = {"records": [{**base, "embedding_dimensions": dimensions}]}
            assert harness.retrieve_for_query("public", embed, "fixture", dimensions=dimensions, index=index)
        assert harness.retrieve_for_query("public", embed, "fixture", index={"records": [base]})
    assert len(calls) == 3


def test_mismatched_query_width_never_enters_cache(public_index):
    harness.embed_index_records(_embed, "fixture", dimensions=2)
    assert harness.retrieve_for_query("public", lambda texts: _embed(texts, 3), "fixture", dimensions=2) == []
    assert harness.retrieve_for_query("public", _embed, "fixture", dimensions=2)


def test_dimension_mismatch_keeps_lexical_fallback_and_honest_coverage(public_index):
    harness.embed_index_records(_embed, "fixture", dimensions=2)
    results = harness.hybrid_search("public", lambda _: pytest.fail("no eligible vectors"), "fixture", dimensions=3)
    assert results
    assert all(row["rank_sources"] == ["keyword"] for row in results)
    assert all(row["rank_provenance"]["embedding_coverage"] == 0 for row in results)


@pytest.mark.parametrize("changed", [False, True])
def test_refresh_preserves_dimensions_only_with_unchanged_input(public_index, monkeypatch, tmp_path, changed):
    root = tmp_path / "wiki"
    root.mkdir()
    path = root / "note.md"
    path.write_text("# Original\nPublic embedding dimension fixture.\n")
    monkeypatch.setattr(
        harness, "all_source_roots", lambda: (harness.SourceRoot("fixture", "wiki", root, ("*.md",), 10),)
    )
    harness.load_index(refresh=True)
    harness.embed_index_records(_embed, "fixture", dimensions=2)
    mtime = path.stat().st_mtime_ns
    if changed:
        path.write_text("# Changed\nThis is a different public document.\n")
    os.utime(path, ns=(mtime + 1_000_000, mtime + 1_000_000))
    harness.load_index(refresh=True)
    harness._set_index_cache(None)
    index = harness.load_index()
    assert index["embeddings"]["active_model"] == "fixture"
    assert index["embeddings"]["requested_dimensions"] == 2
    assert harness.embedded_count("fixture", dimensions=2) == (0 if changed else 1, 1)
    if changed:
        assert "embedding_dimensions" not in index["records"][0]


@pytest.fixture
def local_transport(public_index, monkeypatch):
    from algo_cli import embedding_binding

    monkeypatch.setattr(embedding_binding, "probe_ollama_identity", lambda *_args, **_kwargs: "sha256:" + "a" * 64)
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def embed(self, *, model, input, dimensions=None):
            calls.append((dimensions, len(input)))
            return {"embeddings": _embed(input, dimensions or 4)}

    monkeypatch.setattr(main, "Client", Client)
    monkeypatch.setattr(tools, "gateway_ready", lambda: False)
    monkeypatch.setattr(main, "ollama_server_ready", lambda _: True)
    monkeypatch.setattr(main, "local_model_names", lambda _: ["fixture"])
    monkeypatch.setattr(main, "show_info", lambda _: None)
    return calls


@pytest.mark.parametrize("command", ["automatic", "slash"])
@pytest.mark.parametrize("dimensions", [3, None])
def test_actual_runtime_rebuilds_after_setting_change(local_transport, command, dimensions):
    # Seed the old representation directly so this regression also fails old runtime code.
    index = harness.load_index()
    for row in index["records"]:
        row.update(embedding=[1.0, 1.0], embedding_model="fixture", embedding_dimensions=2)
    harness.INDEX_PATH.write_text(json.dumps(index))
    harness._set_index_cache(None)
    cfg = Config(harness_embed_model="fixture", embed_dimensions=dimensions, embedding_backend="local")
    if command == "automatic":
        assert main.ensure_harness_index(cfg) is True
    else:
        from algo_cli.oliver_slash_dispatch import handle_command

        assert handle_command("/harness embed", cfg, None)[0] is True
    assert local_transport == [(dimensions, 3)]
    assert {len(row["embedding"]) for row in harness.load_index()["records"]} == {dimensions or 4}


def test_runtime_search_status_and_refresh_use_configured_mode(local_transport):
    harness.embed_index_records(_embed, "fixture", dimensions=2)
    cfg = Config(harness_embed_model="fixture", embed_dimensions=3)
    result = tools.harness_search("public", cfg=cfg)
    assert "Retrieval: keyword-only" in result
    assert local_transport == []
    assert json.loads(tools.harness_stats(cfg=cfg))["embeddings"]["pending_count"] == 3
    assert "0/3 ready, 3 pending" in tools.harness_refresh(cfg=cfg)


@pytest.mark.parametrize("name", ["harness_stats", "harness_refresh", "available_actions"])
def test_model_tool_dispatch_injects_dimensions_without_exposing_config(public_index, monkeypatch, name):
    from algo_cli import nathan_runtime

    registered = tools.TOOL_MAP[name]
    assert "cfg" not in inspect.signature(registered).parameters
    cfg = Config(harness_embed_model="fixture", embed_dimensions=3)
    observed = []
    monkeypatch.setitem(nathan_runtime.TOOL_MAP, name, lambda **kwargs: observed.append(kwargs) or "ok")
    assert nathan_runtime.run_tool(name, {}, cfg) == "ok"
    assert observed == [{"cfg": cfg}]


@pytest.mark.parametrize("dimensions", [2, None])
def test_pattern_projection_preserves_mode_without_inheriting_parent_vector(public_index, tmp_path, dimensions):
    path = tmp_path / "ALGO.md"
    path.write_text("### A3. Public Retrieval Pattern\n\n**Status:** proposed\n\nUse bounded public sources.\n")
    parent = {
        "id": "algo-cli:algorithm:ALGO.md",
        "harness": "algo-cli",
        "kind": "algorithm",
        "path": str(path),
        "embedding": [1.0, 1.0],
        "embedding_model": "fixture",
        "embedding_dimensions": dimensions,
    }
    index = {"records": [parent], "embeddings": {"active_model": "fixture", "requested_dimensions": dimensions}}
    first = harness._merge_pattern_records(index)
    pattern = next(row for row in first["records"] if row.get("pattern_id"))
    assert not any(field in pattern for field in harness._EMBEDDING_FIELDS)
    pattern.update(embedding=[1.0, 1.0], embedding_model="fixture", embedding_dimensions=dimensions)
    second = harness._merge_pattern_records(index, previous=first)
    retained = next(row for row in second["records"] if row.get("pattern_id"))
    assert retained["embedding_dimensions"] == dimensions
    assert second["embeddings"]["complete"] is True


@pytest.mark.parametrize("dimensions", [2, None])
def test_default_mode_batch_width_stays_consistent_on_resume(public_index, dimensions):
    harness.embed_index_records(_embed, "fixture", dimensions=dimensions, max_records=1)
    result = harness.embed_index_records(lambda texts: _embed(texts, 3), "fixture", dimensions=dimensions)
    assert result["ready"] is False
    assert result["reason"] == "embedding_dimension_mismatch"
    assert harness.embedded_count("fixture", dimensions=dimensions) == (1, 3)
