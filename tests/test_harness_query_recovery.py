"""Transient embedding failures must not poison later retrieval."""

from typing import Any

import pytest

from algo_cli import harness


@pytest.fixture
def corpus(monkeypatch):
    index = {
        "records": [
            {
                "id": "query:recovery",
                "harness": "fixture",
                "kind": "wiki",
                "title": "Recovery",
                "search_text": "query recovery",
                "embedding": [1.0, 0.5],
                "embedding_model": "query-test",
            }
        ]
    }
    monkeypatch.setattr(harness, "load_index", lambda: index)
    harness._VECTOR_MATRIX_CACHE.clear()
    return index


@pytest.mark.parametrize("numpy", [False, True])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        [None],
        [[]],
        ["12"],
        [[True, 1]],
        [["1", 0]],
        [[float("nan"), 1]],
        [[float("inf"), 1]],
        [[0, 0]],
        [[10**1000, 1]],
        [[1, 0, 0]],
        [[1, 0], [0, 1]],
    ],
)
def test_failed_query_falls_back_then_recovers_without_cache_poison(corpus, monkeypatch, numpy, bad: Any):
    if numpy and not harness._NUMPY:
        pytest.skip("NumPy unavailable")
    monkeypatch.setattr(harness, "_NUMPY", numpy)
    calls = []

    def embed(texts):
        calls.append(texts)
        return bad if len(calls) == 1 else [[1.0, 0.5]]

    failed = harness.hybrid_search("query recovery", embed, "query-test", k=1)
    assert failed[0]["id"] == "query:recovery"
    assert failed[0]["rank_sources"] == ["keyword"]
    assert len(harness._QUERY_VEC_CACHE) == 0
    recovered = harness.hybrid_search("query recovery", embed, "query-test", k=1)
    assert recovered[0]["rank_sources"] == ["keyword", "vector"]
    assert len(calls) == 2


def test_query_cache_does_not_retain_backend_owned_list(corpus):
    vector = [1.0, 0.5]
    first = harness.retrieve_for_query("query recovery", lambda _: [vector], "query-test")
    vector[:] = [float("nan"), 0]
    again = harness.retrieve_for_query("query recovery", lambda _: pytest.fail("cached"), "query-test")
    assert again == first
    assert again


def test_index_replacement_invalidates_same_model_query_dimensions(corpus):
    assert harness.retrieve_for_query("query recovery", lambda _: [[1.0, 0.5]], "query-test")
    corpus["records"][0]["embedding"] = [1.0, 0.5, 0.0]
    harness._set_index_cache(corpus)
    calls = []

    def embed(texts):
        calls.append(texts)
        return [[1.0, 0.5, 0.0]]

    assert harness.retrieve_for_query("query recovery", embed, "query-test")
    assert len(calls) == 1


def test_unembedded_or_empty_slice_does_not_call_provider(corpus):
    assert harness.retrieve_for_query("query", lambda _: pytest.fail("no eligible vectors"), "other-model") == []
    assert harness.retrieve_for_query("query", lambda _: pytest.fail("empty slice"), "query-test", kind="tool") == []


def test_query_cancellation_propagates_without_caching(corpus):
    def cancel(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        harness.retrieve_for_query("query", cancel, "query-test")
    assert len(harness._QUERY_VEC_CACHE) == 0


@pytest.mark.parametrize("scale", [1e300, 1e-300])
@pytest.mark.parametrize("numpy", [False, True])
def test_finite_extreme_query_values_normalize_without_warnings(corpus, monkeypatch, scale, numpy):
    import warnings

    if numpy and not harness._NUMPY:
        pytest.skip("NumPy unavailable")
    monkeypatch.setattr(harness, "_NUMPY", numpy)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hits = harness.retrieve_for_query("query", lambda _: [[scale, scale / 2]], "query-test")
    assert hits[0]["score"] == 1
    assert not caught
