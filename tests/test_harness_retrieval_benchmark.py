from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from algo_cli import harness
from algo_cli.evals import harness_retrieval_benchmark as benchmark


def _index() -> dict[str, Any]:
    records = [
        (
            benchmark.CANONICAL_ALGO_ID,
            "ALGO reviewed algorithm pattern catalog",
            "rate assess audit score your harness self evaluation action registry",
            "algorithm",
        ),
        (
            "algo-cli:wiki:harness-context.md",
            "Harness context",
            "harness context retrieval prompt injection local knowledge",
            "wiki",
        ),
        (
            "algo-cli:memory:recall.md",
            "Memory recall",
            "memory recall durable facts remember retrieval",
            "memory",
        ),
        (
            "algo-cli:skill:verification.md",
            "Verification before completion",
            "verification before completion tests evidence claims",
            "skill",
        ),
        (
            "algo-cli:wiki:index-compute-lab.md",
            "Index Compute Lab",
            "index-compute-lab graph algorithms kernel",
            "wiki",
        ),
    ]
    return {
        "record_count": len(records),
        "records": [
            {
                "id": record_id,
                "harness": "algo-cli",
                "kind": kind,
                "title": title,
                "path": f"__benchmark__/{record_id}.md",
                "relative_path": "ALGO.md" if kind == "algorithm" else f"{title}.md",
                "summary": search_text,
                "search_text": search_text,
            }
            for record_id, title, search_text, kind in records
        ],
    }


def _clock(durations_ms: list[float]) -> Iterator[int]:
    now = 0
    yield now
    for duration_ms in durations_ms:
        now += int(duration_ms * 1_000_000)
        yield now
        yield now


def _clock_fn(durations_ms: list[float]):
    points = _clock(durations_ms)
    return lambda: next(points)


def test_benchmark_passes_stable_canaries_and_reusable_bm25_gate() -> None:
    durations = [3.0] * benchmark.COLD_SAMPLE_TARGET + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET

    result = benchmark.run_harness_retrieval_benchmark(
        _index(),
        clock_ns=_clock_fn(durations),
    )

    assert result["status"] == "pass"
    assert result["correctness"]["passed"] is True
    assert result["correctness"]["canonical_algo_top1"] is True
    assert result["correctness"]["nonempty_observations"] == 15
    assert result["correctness"]["stable_rankings"] is True
    assert result["correctness"]["stable_top_k_parity"] is True
    assert result["performance"]["cold_sample_count"] == 5
    assert result["performance"]["warmup_count"] == 3
    assert result["performance"]["warm_sample_count"] == 9
    assert result["performance"]["cold_median_ms"] == 3.0
    assert result["performance"]["warm_median_ms"] == 1.0
    assert result["performance"]["warm_mad_ms"] == 0.0
    assert result["performance"]["warm_mad_ratio"] == 0.0
    assert result["performance"]["speedup"] == 3.0
    assert result["performance"]["sufficient_samples"] is True
    assert len(result["evidence"]["index_digest"]) == 64
    assert len(result["evidence"]["ranking_digest"]) == 64
    assert len(result["evidence"]["timing_digest"]) == 64
    assert result["evidence"]["benchmark_record_count"] == 5
    assert result["evidence"]["maximum_benchmark_records"] == 2_048
    json.dumps(result, allow_nan=False)


def test_benchmark_uses_injected_search_for_three_stability_passes() -> None:
    index = _index()
    records_by_query = {
        query: [index["records"][position]]
        for position, query in enumerate(benchmark.CANARY_QUERIES)
    }
    calls: list[tuple[str, int]] = []

    def search(query: str, limit: int):
        calls.append((query, limit))
        return records_by_query[query]

    durations = [4.0] * benchmark.COLD_SAMPLE_TARGET + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET
    result = benchmark.run_harness_retrieval_benchmark(
        index,
        search,
        clock_ns=_clock_fn(durations),
    )

    assert result["status"] == "pass"
    assert calls == [
        (query, benchmark.CANARY_LIMIT)
        for _pass in range(benchmark.STABILITY_PASSES)
        for query in benchmark.CANARY_QUERIES
    ]


def test_correctness_failure_is_fail_even_when_timing_is_fast() -> None:
    index = _index()

    def broken_search(query: str, _limit: int):
        if query == benchmark.CANARY_QUERIES[0]:
            return [index["records"][1]]
        if query == benchmark.CANARY_QUERIES[2]:
            return []
        return [index["records"][3]]

    durations = [5.0] * benchmark.COLD_SAMPLE_TARGET + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET
    result = benchmark.run_harness_retrieval_benchmark(
        index,
        broken_search,
        clock_ns=_clock_fn(durations),
    )

    assert result["status"] == "fail"
    assert result["correctness"]["passed"] is False
    assert result["correctness"]["canonical_algo_top1"] is False
    assert result["correctness"]["nonempty_observations"] < 15
    assert "canonical ALGO record was not top-1" in result["reason"]
    assert "nonempty canary observations" in result["reason"]


def test_ranking_instability_is_a_correctness_failure() -> None:
    index = _index()
    call_count = 0

    def unstable_search(query: str, _limit: int):
        nonlocal call_count
        pass_index = call_count // len(benchmark.CANARY_QUERIES)
        call_count += 1
        if query == benchmark.CANARY_QUERIES[1] and pass_index == 1:
            return [index["records"][2]]
        position = benchmark.CANARY_QUERIES.index(query)
        return [index["records"][position]]

    durations = [3.0] * benchmark.COLD_SAMPLE_TARGET + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET
    result = benchmark.run_harness_retrieval_benchmark(
        index,
        unstable_search,
        clock_ns=_clock_fn(durations),
    )

    assert result["status"] == "fail"
    assert result["correctness"]["stable_rankings"] is False
    assert "rankings changed" in result["reason"]


def test_correctness_pass_with_slow_or_noisy_reuse_is_warn() -> None:
    slow = benchmark.run_harness_retrieval_benchmark(
        _index(),
        clock_ns=_clock_fn(
            [1.2] * benchmark.COLD_SAMPLE_TARGET
            + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET
        ),
    )
    noisy = benchmark.run_harness_retrieval_benchmark(
        _index(),
        clock_ns=_clock_fn(
            [4.0] * benchmark.COLD_SAMPLE_TARGET
            + [0.5, 1.5, 0.5, 1.5, 1.0, 0.5, 1.5, 1.0, 1.0]
        ),
    )

    assert slow["status"] == "warn"
    assert slow["correctness"]["passed"] is True
    assert slow["performance"]["speedup"] == 1.2
    assert "speedup was below" in slow["reason"]
    assert noisy["status"] == "warn"
    assert noisy["correctness"]["passed"] is True
    assert noisy["performance"]["warm_mad_ratio"] > 0.25
    assert "warm MAD ratio exceeded" in noisy["reason"]


def test_benchmark_does_not_mutate_process_global_retrieval_caches() -> None:
    bm25_cache = harness._BM25_INDEX_CACHE
    vector_cache = harness._VECTOR_MATRIX_CACHE
    query_cache_before = harness._QUERY_VEC_CACHE.snapshot()
    durations = [3.0] * benchmark.COLD_SAMPLE_TARGET + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET

    benchmark.run_harness_retrieval_benchmark(
        _index(),
        clock_ns=_clock_fn(durations),
    )

    assert harness._BM25_INDEX_CACHE is bm25_cache
    assert harness._VECTOR_MATRIX_CACHE is vector_cache
    assert harness._QUERY_VEC_CACHE.snapshot() == query_cache_before


def test_benchmark_corpus_bound_preserves_canonical_project_record(monkeypatch) -> None:
    records = list(reversed(_index()["records"]))
    monkeypatch.setattr(benchmark, "MAX_BENCHMARK_RECORDS", 3)

    bounded = benchmark._bounded_records(records)

    assert len(bounded) == 3
    assert bounded[0]["id"] == benchmark.CANONICAL_ALGO_ID
    assert all(record["harness"] == "algo-cli" for record in bounded)


def test_missing_persisted_index_returns_json_safe_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(harness, "INDEX_PATH", tmp_path / "missing-index.json")
    durations = [3.0] * benchmark.COLD_SAMPLE_TARGET + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET

    result = benchmark.run_harness_retrieval_benchmark(
        clock_ns=_clock_fn(durations),
    )

    assert result["status"] == "fail"
    assert "harness index not found" in result["reason"]
    assert result["evidence"]["eligible_record_count"] == 0
    json.dumps(result, allow_nan=False)


def test_malformed_record_collection_returns_json_safe_failure() -> None:
    durations = [3.0] * benchmark.COLD_SAMPLE_TARGET + [1.0] * benchmark.REUSABLE_SAMPLE_TARGET

    result = benchmark.run_harness_retrieval_benchmark(
        {"records": "not-a-list"},
        clock_ns=_clock_fn(durations),
    )

    assert result["status"] == "fail"
    assert "no eligible harness records" in result["reason"]
    assert result["evidence"]["index_record_count"] == 0
    json.dumps(result, allow_nan=False)
