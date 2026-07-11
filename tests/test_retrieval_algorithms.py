"""Tests and deterministic benchmarks for active harness ranking algorithms."""

from __future__ import annotations

import json
import os
import random
import warnings

import pytest

from algo_cli import code_rag, harness
from algo_cli.retrieval_algorithms import BM25Index, bm25_scores, stable_top_k


def test_bm25_rewards_rare_exact_terms_and_saturates_frequency() -> None:
    documents = [
        "runtime runtime runtime runtime common",
        "runtime kernel common",
        "common harness",
    ]

    scores = bm25_scores(documents, ["runtime", "kernel"])

    assert scores[1] > scores[0] > scores[2]


def test_reusable_bm25_index_matches_one_shot_scores() -> None:
    documents = ["runtime runtime common", "runtime kernel common", "common harness"]
    query = ["runtime", "kernel"]

    assert BM25Index(documents).scores(query) == bm25_scores(documents, query)


def test_harness_reuses_bm25_corpus_statistics(monkeypatch) -> None:
    records = [
        {"id": "a", "harness": "h", "kind": "wiki", "search_text": "runtime kernel"},
        {"id": "b", "harness": "h", "kind": "wiki", "search_text": "general helper"},
    ]
    monkeypatch.setattr(harness, "load_index", lambda refresh=False: {"records": records})
    harness._BM25_INDEX_CACHE = None

    harness.search_index("runtime")
    first_index = harness._BM25_INDEX_CACHE[2]
    harness.search_index("kernel")

    assert harness._BM25_INDEX_CACHE[2] is first_index
    harness._set_index_cache({"records": records})
    assert harness._BM25_INDEX_CACHE is None


def test_stable_top_k_matches_full_stable_sort_with_ties() -> None:
    rng = random.Random(7)
    values = [(index, rng.randrange(8)) for index in range(9_000)]
    expected = sorted(values, key=lambda item: item[1], reverse=True)[:17]

    selected = stable_top_k(values, 17, score=lambda item: item[1])

    assert selected == expected


def test_hybrid_search_exposes_bm25_vector_and_rrf_provenance() -> None:
    index = {
        "generated": "2026-07-09T00:00:00",
        "record_count": 2,
        "roots": [],
        "records": [
            {
                "id": "algo-cli:wiki:runtime.md",
                "harness": "algo-cli",
                "kind": "wiki",
                "title": "Runtime kernel",
                "path": "__pytest__/runtime.md",
                "relative_path": "runtime.md",
                "summary": "Runtime kernel scheduling",
                "search_text": "runtime kernel scheduling",
                "embedding": [1.0, 0.0],
                "embedding_model": "m",
            },
            {
                "id": "codex:skill:generic.md",
                "harness": "codex",
                "kind": "skill",
                "title": "Generic helper",
                "path": "__pytest__/generic.md",
                "relative_path": "generic.md",
                "summary": "General purpose helper",
                "search_text": "general purpose helper",
                "embedding": [0.0, 1.0],
                "embedding_model": "m",
            },
        ],
    }
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._INDEX_CACHE = None

    results = harness.hybrid_search("runtime kernel", lambda _texts: [[1.0, 0.0]], "m", k=2)

    assert results[0]["id"] == "algo-cli:wiki:runtime.md"
    assert results[0]["rank_sources"] == ["keyword", "vector"]
    assert results[0]["rank_provenance"]["lexical_score"] > 0
    assert results[0]["rank_provenance"]["vector_score"] == 1.0
    assert results[0]["rank_provenance"]["rrf_score"] == results[0]["score"]


def test_partial_embedding_coverage_does_not_hide_exact_lexical_hit() -> None:
    exact = {
        "id": "exact",
        "harness": "test",
        "kind": "wiki",
        "title": "uniqueneedle",
        "path": "__pytest__/exact.md",
        "relative_path": "exact.md",
        "summary": "uniqueneedle exact answer",
        "search_text": "uniqueneedle exact answer",
    }
    weak = [
        {
            "id": f"weak-{index:02d}",
            "harness": "test",
            "kind": "wiki",
            "title": f"generic {index}",
            "path": f"__pytest__/weak-{index:02d}.md",
            "relative_path": f"weak-{index:02d}.md",
            "summary": "uniqueneedle weak background",
            "search_text": "uniqueneedle weak background",
            "embedding": [1.0, 0.0],
            "embedding_model": "m",
        }
        for index in range(18)
    ]
    index = {"record_count": 19, "roots": [], "records": [exact, *weak]}
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._set_index_cache(None)

    results = harness.hybrid_search("uniqueneedle", lambda _texts: [[1.0, 0.0]], "m", k=6)

    assert results[0]["id"] == "exact"
    assert results[0]["rank_provenance"]["fusion_mode"] == "coverage-neutral-rrf"
    assert results[0]["rank_provenance"]["embedding_coverage"] < 1.0


def test_make_record_indexes_bounded_content_beyond_display_summary(tmp_path) -> None:
    path = tmp_path / "late.md"
    path.write_text("# Late\n" + ("prefix " * 110) + "lateuniqueterm\n", encoding="utf-8")
    root = harness.SourceRoot("test", "wiki", tmp_path, ("*.md",), 10)

    record = harness.make_record(root, path)

    assert "lateuniqueterm" not in record["summary"]
    assert "lateuniqueterm" in record["index_text"]
    assert "lateuniqueterm" in record["search_text"]


def test_reviewed_algo_catalog_indexes_late_headings_without_loading_full_body(tmp_path, monkeypatch) -> None:
    path = tmp_path / "ALGO.md"
    path.write_text(
        "# Catalog\n" + ("body text\n" * 700) + "### Z99. Late Rendezvous Retention Pattern\n",
        encoding="utf-8",
    )
    root = harness.SourceRoot("algo-cli", "algorithm", tmp_path, ("ALGO.md",), 1)
    record = harness.make_record(root, path)
    monkeypatch.setattr(harness, "load_index", lambda refresh=False: {"records": [record]})

    hits = harness.search_index("late rendezvous retention", limit=1)

    assert "Late Rendezvous Retention Pattern" in record["heading_text"]
    assert hits[0]["id"] == "algo-cli:algorithm:ALGO.md"


def test_deleted_nested_source_invalidates_and_is_removed(tmp_path, monkeypatch) -> None:
    root_dir = tmp_path / "root"
    nested = root_dir / "nested"
    nested.mkdir(parents=True)
    source = nested / "gone.md"
    source.write_text("# Gone\n", encoding="utf-8")
    source_root = harness.SourceRoot("test", "wiki", root_dir, ("*.md",), 10)
    monkeypatch.setattr(harness, "all_source_roots", lambda: (source_root,))
    index = harness.build_index()
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    index_time = harness.INDEX_PATH.stat().st_mtime
    os.utime(root_dir, (index_time - 10, index_time - 10))
    os.utime(nested, (index_time - 10, index_time - 10))
    source.unlink()
    # Hide directory-mtime evidence: deletion must be found from the path-set gap.
    os.utime(root_dir, (index_time - 10, index_time - 10))
    os.utime(nested, (index_time - 10, index_time - 10))
    harness._set_index_cache(None)

    assert harness.index_is_stale() is True
    refreshed = harness.load_index()
    assert refreshed["records"] == []
    assert refreshed["refresh_stats"]["removed_records"] == 1


@pytest.mark.skipif(not harness._NUMPY, reason="NumPy is not installed")
def test_vector_retrieval_reuses_normalized_matrix_and_preserves_scalar_ranking(monkeypatch) -> None:
    index = {
        "records": [
            {
                "id": "a", "harness": "h", "kind": "wiki", "title": "a",
                "path": "a", "relative_path": "a", "embedding_model": "m",
                "embedding": [100.0, 0.0],
            },
            {
                "id": "b", "harness": "h", "kind": "wiki", "title": "b",
                "path": "b", "relative_path": "b", "embedding_model": "m",
                "embedding": [1.0, 1.0],
            },
        ]
    }
    monkeypatch.setattr(harness, "load_index", lambda refresh=False: index)
    harness._VECTOR_MATRIX_CACHE = None
    def embed(_texts):
        return [[1.0, 1.0]]

    numpy_hits = harness.retrieve_for_query("matrix-cache-query", embed, "m", k=2)
    first_matrix = harness._VECTOR_MATRIX_CACHE[2]
    repeated_hits = harness.retrieve_for_query("matrix-cache-query", embed, "m", k=2)

    assert harness._VECTOR_MATRIX_CACHE[2] is first_matrix
    assert repeated_hits == numpy_hits

    monkeypatch.setattr(harness, "_NUMPY", False)
    scalar_hits = harness.retrieve_for_query("matrix-cache-query", embed, "m", k=2)
    assert [hit["id"] for hit in scalar_hits] == [hit["id"] for hit in numpy_hits]
    assert [hit["score"] for hit in scalar_hits] == pytest.approx(
        [hit["score"] for hit in numpy_hits], abs=1e-4
    )

    harness._set_index_cache(index)
    assert harness._VECTOR_MATRIX_CACHE is None


@pytest.mark.skipif(not harness._NUMPY, reason="NumPy is not installed")
def test_vector_retrieval_filters_nonfinite_rows_without_runtime_warnings(monkeypatch) -> None:
    index = {
        "records": [
            {
                "id": "finite", "harness": "h", "kind": "wiki", "title": "finite",
                "path": "finite", "relative_path": "finite", "embedding_model": "m",
                "embedding": [1.0, 1.0],
            },
            {
                "id": "infinite", "harness": "h", "kind": "wiki", "title": "infinite",
                "path": "infinite", "relative_path": "infinite", "embedding_model": "m",
                "embedding": [float("inf"), 1.0],
            },
        ]
    }
    monkeypatch.setattr(harness, "load_index", lambda refresh=False: index)
    harness._VECTOR_MATRIX_CACHE = None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hits = harness.retrieve_for_query("finite-query", lambda _texts: [[1.0, 1.0]], "m", k=2)

    assert [hit["id"] for hit in hits] == ["finite"]
    assert caught == []


def test_code_rag_reuses_content_identical_embeddings_after_append(tmp_path) -> None:
    path = tmp_path / "module.py"
    path.write_text("\n".join(f"line_{index} = {index}" for index in range(120)), encoding="utf-8")

    def embed(texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]

    first = code_rag.ensure_embeddings(str(tmp_path), embed, "m", cap=100)
    embedded_before = sum(1 for chunk in first["chunks"] if chunk.get("embedding_model") == "m")
    path.write_text(
        path.read_text(encoding="utf-8") + "\n" + "\n".join(f"tail_{index} = {index}" for index in range(10)),
        encoding="utf-8",
    )

    refreshed = code_rag.build_or_update_index(str(tmp_path), force=True)

    assert embedded_before >= 3
    assert refreshed["refresh_stats"]["content_reused_embeddings"] >= 2
    assert sum(1 for chunk in refreshed["chunks"] if chunk.get("embedding_model") == "m") >= 2
