"""Alternating retrieval slices must reuse bounded, source-scoped derivations."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from algo_cli import harness


def _record(name, kind="wiki"):
    return {
        "id": name,
        "harness": "fixture",
        "kind": kind,
        "title": name,
        "path": name,
        "search_text": "public fixture " + name,
        "embedding_model": "fixture",
        "embedding": [1.0, 0.5],
    }


def _lexical(records, kind=None):
    return harness._candidate_bm25_index(records, harness_names={"fixture"}, kind=kind, excluded_kinds=frozenset())


def _matrix(records, kind=None):
    return harness._normalized_candidate_matrix(
        records, model="fixture", dimensions=2, harness_names={"fixture"}, kind=kind, excluded_kinds=frozenset()
    )


def test_lexical_statistics_survive_alternating_filters():
    a, b = _record("alpha"), _record("beta", "skill")
    full = _lexical([a, b])
    wiki = _lexical([a], "wiki")
    skill = _lexical([b], "skill")
    assert _lexical([a, b]) is full
    assert _lexical([a], "wiki") is wiki
    assert _lexical([b], "skill") is skill


@pytest.mark.skipif(not harness._NUMPY, reason="NumPy is not installed")
def test_normalized_matrices_survive_alternating_filters():
    a, b = _record("alpha"), _record("beta", "skill")
    full = _matrix([a, b])[1]
    wiki = _matrix([a], "wiki")[1]
    skill = _matrix([b], "skill")[1]
    assert _matrix([a, b])[1] is full
    assert _matrix([a], "wiki")[1] is wiki
    assert _matrix([b], "skill")[1] is skill


@pytest.mark.parametrize("dimension", ["max_entries", "max_rows", "max_weight"])
def test_slice_cache_evicts_least_recent_within_each_budget(dimension):
    options = {"max_entries": 8, "max_rows": 100, "max_weight": 100}
    options[dimension] = 2
    cache = harness._RetrievalSliceCache(**options)
    for name in ("hot", "first"):
        rows = [_record(name)]
        generation, _ = cache.lookup((name,))
        cache.remember(generation, (name,), rows, rows, name, weight=1)
    generation, hot = cache.lookup(("hot",))
    rows = [_record("third")]
    cache.remember(generation, ("third",), rows, rows, "third", weight=1)
    assert cache.lookup(("first",))[1] is None
    assert cache.lookup(("hot",))[1] is hot
    assert cache.snapshot()["slices"] == 2
    assert cache.snapshot()["input_rows"] == 2
    assert cache.snapshot()["weight"] == 2


@pytest.mark.parametrize("weight,count", [(4, 1), (1, 4), (0, 1), (-1, 1)])
def test_oversized_or_invalid_entry_does_not_evict_reusable_slices(weight, count):
    cache = harness._RetrievalSliceCache(3, max_rows=3)
    rows = [_record("hot")]
    generation, _ = cache.lookup(("hot",))
    cache.remember(generation, ("hot",), rows, rows, "hot", weight=1)
    before = cache.snapshot()
    large = [_record(str(i)) for i in range(count)]
    cache.remember(generation, ("oversized",), large, large, "oversized", weight=weight)
    assert cache.snapshot() == before
    assert cache.lookup(("oversized",))[1] is None
    assert cache.lookup(("hot",))[1] is not None


def test_replacement_adjusts_budgets_without_duplicate_entries():
    cache = harness._RetrievalSliceCache(10)
    generation, _ = cache.lookup(("one",))
    rows = [_record("one")]
    cache.remember(generation, ("one",), rows, rows, "first", weight=7)
    cache.remember(generation, ("one",), rows, rows, "replacement", weight=2)
    assert cache.snapshot()["weight"] == 2
    assert cache.snapshot()["input_rows"] == cache.snapshot()["slices"] == 1
    assert cache.lookup(("one",))[1][2] == "replacement"


def test_clear_rejects_a_concurrent_older_generation_build():
    cache = harness._RetrievalSliceCache(10)
    started, resume = Event(), Event()

    def build():
        generation, _ = cache.lookup(("stale",))
        started.set()
        assert resume.wait(5)
        rows = [_record("stale")]
        cache.remember(generation, ("stale",), rows, rows, "stale", weight=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(build)
        assert started.wait(5)
        cache.clear()
        resume.set()
        future.result(timeout=5)
    assert cache.last is None
    assert cache.snapshot()["slices"] == cache.snapshot()["weight"] == 0
    assert cache.lookup(("stale",))[1] is None


@pytest.mark.parametrize("builder", [_lexical, _matrix])
def test_index_invalidation_discards_every_slice(builder):
    if builder is _matrix and not harness._NUMPY:
        pytest.skip("NumPy is not installed")
    a, b = _record("alpha"), _record("beta")
    first, second = builder([a]), builder([b])
    harness._set_index_cache(None)
    again = builder([a])
    assert (again[1] is not first[1]) if builder is _matrix else (again is not first)
    again = builder([b])
    assert (again[1] is not second[1]) if builder is _matrix else (again is not second)


@pytest.mark.parametrize("builder", [_lexical, _matrix])
def test_replacement_records_and_reordered_rows_do_not_borrow_a_slice(builder):
    if builder is _matrix and not harness._NUMPY:
        pytest.skip("NumPy is not installed")
    a, b = _record("alpha"), _record("beta")
    original = builder([a, b])
    replacement = builder([dict(a), b])
    reversed_rows = builder([b, a])
    if builder is _matrix:
        assert replacement[1] is not original[1] and reversed_rows[1] is not original[1]
        assert reversed_rows[0] == [b, a]
    else:
        assert replacement is not original and reversed_rows is not original


@pytest.mark.skipif(not harness._NUMPY, reason="NumPy is not installed")
def test_filtered_invalid_input_rows_are_retained_until_matrix_eviction():
    import gc
    import weakref

    class Row(dict):
        pass

    good = _record("good")
    zero = Row(_record("zero"), embedding=[0.0, 0.0])
    reference = weakref.ref(zero)
    rows, matrix = _matrix([good, zero])
    assert rows == [good] and matrix.shape == (1, 2)
    del zero
    gc.collect()
    assert reference() is not None
    harness._VECTOR_MATRIX_CACHE.clear()
    gc.collect()
    assert reference() is None


@pytest.mark.skipif(not harness._NUMPY, reason="NumPy is not installed")
def test_matrix_payload_budget_bypasses_oversized_entry_without_changing_output(monkeypatch):
    monkeypatch.setattr(harness, "_VECTOR_MATRIX_CACHE", harness._RetrievalSliceCache(8))
    a, b = _record("alpha"), _record("beta")
    small = _matrix([a])[1]
    rows, large = _matrix([a, b])
    assert rows == [a, b] and large.nbytes == 16
    assert harness._VECTOR_MATRIX_CACHE.snapshot()["weight"] == 8
    assert _matrix([a])[1] is small


def test_lexical_budget_bypasses_oversized_entry_without_changing_scores(monkeypatch):
    monkeypatch.setattr(harness, "_BM25_INDEX_CACHE", harness._RetrievalSliceCache(1, max_rows=1))
    a, b = _record("alpha"), _record("beta")
    small = _lexical([a])
    large = _lexical([a, b])
    assert large.bm25.scores(["alpha"]) == harness.BM25Index([a["search_text"], b["search_text"]]).scores(["alpha"])
    assert harness._BM25_INDEX_CACHE.snapshot()["weight"] == 1
    assert _lexical([a]) is small


@pytest.mark.parametrize(
    "builder,cache_name,budget",
    [
        (_lexical, "_BM25_INDEX_CACHE", 1),
        (_matrix, "_VECTOR_MATRIX_CACHE", 8),
    ],
)
def test_uncached_derivation_does_not_report_previous_slice_as_reused(monkeypatch, builder, cache_name, budget):
    if builder is _matrix and not harness._NUMPY:
        pytest.skip("NumPy is not installed")
    cache = harness._RetrievalSliceCache(budget)
    monkeypatch.setattr(harness, cache_name, cache)
    a, b = _record("alpha"), _record("beta")
    builder([a])
    assert cache.last is not None
    builder([a, b])
    assert cache.last is None
    assert cache.snapshot()["slices"] == 1
