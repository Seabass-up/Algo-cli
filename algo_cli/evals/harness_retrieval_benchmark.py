"""Bounded, offline effectiveness benchmark for harness retrieval.

The benchmark reads a snapshot of the persisted harness index and owns every
BM25 object it creates. It deliberately does not call ``harness.search_index``
or clear/populate any process-global retrieval cache.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .. import harness
from ..retrieval_algorithms import (
    FULL_SORT_THRESHOLD,
    BM25Index,
    lexical_tokens,
    stable_top_k,
)

BENCHMARK_VERSION = "harness-retrieval-v1"
CANARY_QUERIES: tuple[str, ...] = (
    "rate your harness",
    "harness context",
    "memory recall",
    "verification before completion",
    "index-compute-lab",
)
CANONICAL_ALGO_ID = "algo-cli:algorithm:ALGO.md"
STABILITY_PASSES = 3
CANARY_LIMIT = 5
COLD_SAMPLE_TARGET = 5
REUSABLE_WARMUPS = 3
REUSABLE_SAMPLE_TARGET = 9
MIN_REUSABLE_SPEEDUP = 1.5
MAX_WARM_MAD_RATIO = 0.25
MAX_BENCHMARK_RECORDS = 2_048
MAX_BENCHMARK_TEXT_CHARS = 40_000

SearchFn = Callable[[str, int], Sequence[Any]]
ClockFn = Callable[[], int]


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_persisted_index() -> tuple[dict[str, Any], str | None]:
    """Read the live index file without invoking the global index cache."""

    try:
        payload = json.loads(harness.INDEX_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"records": []}, f"harness index not found: {harness.INDEX_PATH}"
    except (OSError, json.JSONDecodeError) as exc:
        return {"records": []}, f"could not read harness index: {exc}"
    if not isinstance(payload, dict):
        return {"records": []}, "harness index root is not an object"
    return payload, None


def _eligible_records(index: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_records = index.get("records")
    if not isinstance(raw_records, list):
        return []
    return [
        record
        for record in raw_records
        if isinstance(record, dict) and not harness.is_excluded_from_retrieval(record)
    ]


def _bounded_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(records) <= MAX_BENCHMARK_RECORDS:
        return list(records)
    # Keep canonical/project-local evidence in the bounded corpus, then retain
    # source order so repeated runs over an unchanged index remain identical.
    prioritized = sorted(
        enumerate(records),
        key=lambda pair: (
            str(pair[1].get("id") or "") != CANONICAL_ALGO_ID,
            str(pair[1].get("harness") or "") != "algo-cli",
            pair[0],
        ),
    )
    return [record for _position, record in prioritized[:MAX_BENCHMARK_RECORDS]]


def _search_text(record: Mapping[str, Any]) -> str:
    text = str(record.get("search_text") or "")
    if text:
        return text[:MAX_BENCHMARK_TEXT_CHARS]
    return " ".join(
        str(record.get(field) or "")
        for field in (
            "id",
            "harness",
            "kind",
            "title",
            "description",
            "tags",
            "relative_path",
            "summary",
        )
    ).lower()[:MAX_BENCHMARK_TEXT_CHARS]


def _local_search(
    records: Sequence[dict[str, Any]],
    bm25: BM25Index,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    terms = lexical_tokens(query)
    if not terms:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for lexical_score, record in zip(bm25.scores(terms), records):
        score = lexical_score + float(harness.score_record(record, terms))
        if score > 0.0:
            scored.append((score, record))
    return [
        record
        for _score, record in stable_top_k(
            scored,
            limit,
            score=lambda pair: pair[0],
        )
    ]


def _result_id(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("id") or "")
    return str(result or "")


def _stable_top_k_parity() -> tuple[bool, str]:
    """Exercise the heap branch above its adaptive crossover threshold."""

    values = [
        (index, (index * 2_654_435_761) % 97)
        for index in range(FULL_SORT_THRESHOLD + 257)
    ]
    expected = sorted(values, key=lambda item: item[1], reverse=True)[:17]
    actual = stable_top_k(values, 17, score=lambda item: item[1])
    return actual == expected, _digest(actual)


def _measure_ns(operation: Callable[[], Any], clock_ns: ClockFn) -> int:
    started = int(clock_ns())
    operation()
    return max(0, int(clock_ns()) - started)


def _median_absolute_deviation(values: Sequence[int], median: float) -> float:
    if not values:
        return 0.0
    return float(statistics.median(abs(float(value) - median) for value in values))


def _milliseconds(value_ns: float) -> float:
    return round(float(value_ns) / 1_000_000.0, 6)


def run_harness_retrieval_benchmark(
    index: Mapping[str, Any] | None = None,
    search_fn: SearchFn | None = None,
    *,
    clock_ns: ClockFn | None = None,
) -> dict[str, Any]:
    """Run the bounded retrieval benchmark and return JSON-serializable evidence.

    Args:
        index: Optional index payload. When omitted, read the persisted live index
            directly without touching the harness index cache.
        search_fn: Optional ``(query, limit) -> results`` function for canary
            checks. Timing always uses local BM25 instances.
        clock_ns: Optional monotonic nanosecond clock for deterministic tests.
    """

    load_error: str | None = None
    if index is None:
        index_payload, load_error = _load_persisted_index()
    else:
        index_payload = dict(index)
    raw_records = index_payload.get("records")
    index_record_count = len(raw_records) if isinstance(raw_records, list) else 0
    eligible_records = _eligible_records(index_payload)
    records = _bounded_records(eligible_records)
    documents = [_search_text(record) for record in records]
    reusable_index = BM25Index(documents)
    active_search: SearchFn
    if search_fn is None:
        def active_search(query: str, limit: int) -> list[dict[str, Any]]:
            return _local_search(records, reusable_index, query, limit)
    else:
        active_search = search_fn

    ranking_passes: list[list[list[str]]] = []
    search_errors: list[str] = []
    for _pass_index in range(STABILITY_PASSES):
        pass_rankings: list[list[str]] = []
        for query in CANARY_QUERIES:
            try:
                results = active_search(query, CANARY_LIMIT)
                ids = [_result_id(result) for result in results]
                pass_rankings.append([record_id for record_id in ids if record_id])
            except Exception as exc:
                search_errors.append(f"{query}: {type(exc).__name__}: {exc}")
                pass_rankings.append([])
        ranking_passes.append(pass_rankings)

    observation_count = len(CANARY_QUERIES) * STABILITY_PASSES
    nonempty_observations = sum(
        1
        for pass_rankings in ranking_passes
        for ranking in pass_rankings
        if ranking
    )
    stable_rankings = bool(ranking_passes) and all(
        pass_rankings == ranking_passes[0]
        for pass_rankings in ranking_passes[1:]
    )
    canonical_algo_top1 = bool(ranking_passes) and all(
        pass_rankings
        and pass_rankings[0]
        and pass_rankings[0][0] == CANONICAL_ALGO_ID
        for pass_rankings in ranking_passes
    )
    top_k_parity, top_k_digest = _stable_top_k_parity()

    clock = clock_ns or time.perf_counter_ns
    query_terms = [lexical_tokens(query) for query in CANARY_QUERIES]

    def score_all_queries(bm25: BM25Index) -> list[list[float]]:
        return [bm25.scores(terms) for terms in query_terms]

    cold_samples_ns: list[int] = []
    for _sample in range(COLD_SAMPLE_TARGET):
        cold_samples_ns.append(
            _measure_ns(
                lambda: score_all_queries(BM25Index(documents)),
                clock,
            )
        )

    for _warmup in range(REUSABLE_WARMUPS):
        score_all_queries(reusable_index)
    warm_samples_ns = [
        _measure_ns(lambda: score_all_queries(reusable_index), clock)
        for _sample in range(REUSABLE_SAMPLE_TARGET)
    ]

    cold_median_ns = float(statistics.median(cold_samples_ns)) if cold_samples_ns else 0.0
    warm_median_ns = float(statistics.median(warm_samples_ns)) if warm_samples_ns else 0.0
    cold_mad_ns = _median_absolute_deviation(cold_samples_ns, cold_median_ns)
    warm_mad_ns = _median_absolute_deviation(warm_samples_ns, warm_median_ns)
    warm_mad_ratio = warm_mad_ns / warm_median_ns if warm_median_ns > 0.0 else None
    speedup = cold_median_ns / warm_median_ns if warm_median_ns > 0.0 else None
    sufficient_samples = (
        len(cold_samples_ns) >= COLD_SAMPLE_TARGET
        and len(warm_samples_ns) >= REUSABLE_SAMPLE_TARGET
        and cold_median_ns > 0.0
        and warm_median_ns > 0.0
    )

    correctness_failures: list[str] = []
    if load_error:
        correctness_failures.append(load_error)
    if not records:
        correctness_failures.append("no eligible harness records")
    if search_errors:
        correctness_failures.append("canary search raised an exception")
    if nonempty_observations != observation_count:
        correctness_failures.append(
            f"nonempty canary observations {nonempty_observations}/{observation_count}"
        )
    if not stable_rankings:
        correctness_failures.append("canary rankings changed across stability passes")
    if not canonical_algo_top1:
        correctness_failures.append("canonical ALGO record was not top-1")
    if not top_k_parity:
        correctness_failures.append("stable_top_k diverged from a full stable sort")
    correctness_passed = not correctness_failures

    performance_warnings: list[str] = []
    if not sufficient_samples:
        performance_warnings.append("timing samples were insufficient or below clock resolution")
    if speedup is None or speedup < MIN_REUSABLE_SPEEDUP:
        performance_warnings.append(
            f"reusable BM25 speedup was below {MIN_REUSABLE_SPEEDUP:.1f}x"
        )
    if warm_mad_ratio is None or warm_mad_ratio > MAX_WARM_MAD_RATIO:
        performance_warnings.append(
            f"warm MAD ratio exceeded {MAX_WARM_MAD_RATIO:.2f}"
        )

    if not correctness_passed:
        status = "fail"
        reason = "retrieval correctness failed: " + "; ".join(correctness_failures)
    elif performance_warnings:
        status = "warn"
        reason = "retrieval correctness passed; " + "; ".join(performance_warnings)
    else:
        assert speedup is not None and warm_mad_ratio is not None
        status = "pass"
        reason = (
            "retrieval correctness passed; reusable BM25 speedup "
            f"{speedup:.2f}x with warm MAD ratio {warm_mad_ratio:.3f}"
        )

    index_fingerprint = [
        {
            "id": str(record.get("id") or ""),
            "harness": str(record.get("harness") or ""),
            "kind": str(record.get("kind") or ""),
            "relative_path": str(record.get("relative_path") or ""),
            "search_text": _search_text(record),
        }
        for record in records
    ]
    cold_samples_ms = [_milliseconds(value) for value in cold_samples_ns]
    warm_samples_ms = [_milliseconds(value) for value in warm_samples_ns]
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "status": status,
        "reason": reason,
        "correctness": {
            "passed": correctness_passed,
            "failures": correctness_failures,
            "search_errors": search_errors,
            "canary_query_count": len(CANARY_QUERIES),
            "stability_passes": STABILITY_PASSES,
            "observation_count": observation_count,
            "nonempty_observations": nonempty_observations,
            "stable_rankings": stable_rankings,
            "canonical_algo_top1": canonical_algo_top1,
            "stable_top_k_parity": top_k_parity,
            "first_pass_rankings": {
                query: ranking_passes[0][index]
                for index, query in enumerate(CANARY_QUERIES)
            },
        },
        "performance": {
            "operation": "bm25_build_plus_all_queries_vs_reusable_all_queries",
            "cold_sample_count": len(cold_samples_ns),
            "warmup_count": REUSABLE_WARMUPS,
            "warm_sample_count": len(warm_samples_ns),
            "cold_samples_ms": cold_samples_ms,
            "warm_samples_ms": warm_samples_ms,
            "cold_median_ms": _milliseconds(cold_median_ns),
            "warm_median_ms": _milliseconds(warm_median_ns),
            "cold_mad_ms": _milliseconds(cold_mad_ns),
            "warm_mad_ms": _milliseconds(warm_mad_ns),
            "warm_mad_ratio": (
                round(warm_mad_ratio, 6) if warm_mad_ratio is not None else None
            ),
            "speedup": round(speedup, 6) if speedup is not None else None,
            "sufficient_samples": sufficient_samples,
            "minimum_speedup": MIN_REUSABLE_SPEEDUP,
            "maximum_warm_mad_ratio": MAX_WARM_MAD_RATIO,
        },
        "evidence": {
            "index_record_count": index_record_count,
            "eligible_record_count": len(eligible_records),
            "benchmark_record_count": len(records),
            "maximum_benchmark_records": MAX_BENCHMARK_RECORDS,
            "maximum_text_chars": MAX_BENCHMARK_TEXT_CHARS,
            "index_digest": _digest(index_fingerprint),
            "canary_digest": _digest(CANARY_QUERIES),
            "ranking_digest": _digest(ranking_passes),
            "stable_top_k_digest": top_k_digest,
            "timing_digest": _digest(
                {
                    "cold_samples_ns": cold_samples_ns,
                    "warm_samples_ns": warm_samples_ns,
                }
            ),
        },
    }


__all__ = [
    "BENCHMARK_VERSION",
    "CANARY_QUERIES",
    "CANONICAL_ALGO_ID",
    "MAX_BENCHMARK_RECORDS",
    "run_harness_retrieval_benchmark",
]
