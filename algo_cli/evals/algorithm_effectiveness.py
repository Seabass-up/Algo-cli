"""Bounded live evidence probe for core harness and memory algorithms.

The probe deliberately exercises production functions against the current
harness index without calling an embedding model.  The canonical ALGO record's
persisted embedding is used as the deterministic query vector, so an exact
vector hit and lexical/provenance path can be checked together.
"""

from __future__ import annotations

import math
import json
import tempfile
from pathlib import Path
from typing import Any

from .. import harness, memory_candidates
from ..retrieval_algorithms import FULL_SORT_THRESHOLD, stable_top_k


PROBE_SCHEMA_VERSION = 1
PROBE_NAME = "harness-algorithm-effectiveness-v2"
CANONICAL_ALGO_ID = "algo-cli:algorithm:ALGO.md"
PROBE_RESULT_LIMIT = 12
TOP_K_PARITY_COUNT = FULL_SORT_THRESHOLD + 257
TOP_K_PARITY_LIMIT = 31
EXACT_VECTOR_MIN_SCORE = 0.999

REQUIRED_CHECKS = (
    "bm25_lexical",
    "exact_vector",
    "rrf_fusion",
    "stable_top_k",
    "window_tinylfu",
    "embedding_priority",
    "memory_admission",
)


def _check(passed: bool, evidence: dict[str, Any], reason: str = "") -> dict[str, Any]:
    return {
        "status": "pass" if passed else "fail",
        "required": True,
        "reason": "" if passed else reason,
        "evidence": evidence,
    }


def _terminal_result(status: str, reason: str) -> dict[str, Any]:
    checks = {
        name: {
            "status": status,
            "required": True,
            "reason": reason,
            "evidence": {},
        }
        for name in REQUIRED_CHECKS
    }
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "probe": PROBE_NAME,
        "status": status,
        "reason": reason,
        "required_checks": list(REQUIRED_CHECKS),
        "summary": {
            "required": len(REQUIRED_CHECKS),
            "passed": 0,
            status: len(REQUIRED_CHECKS),
        },
        "checks": checks,
    }


def _canonical_result(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (result for result in results if str(result.get("id") or "") == CANONICAL_ALGO_ID),
        None,
    )


def _cache_rows(cache: Any) -> int:
    if not isinstance(cache, tuple) or len(cache) < 2 or not isinstance(cache[1], list):
        return 0
    return len(cache[1])


def _matrix_dimensions(cache: Any) -> tuple[int, int]:
    if not isinstance(cache, tuple) or len(cache) < 3:
        return 0, 0
    shape = getattr(cache[2], "shape", ())
    if not isinstance(shape, tuple) or len(shape) != 2:
        return 0, 0
    return int(shape[0]), int(shape[1])


def _top_k_parity_check() -> dict[str, Any]:
    # The modulo score creates many ties, so equality also verifies stable
    # original-order tie breaking in the heap branch above the crossover.
    candidates = list(range(TOP_K_PARITY_COUNT))

    def score(item: int) -> float:
        return float((item * 37) % 101)

    expected = sorted(candidates, key=score, reverse=True)[:TOP_K_PARITY_LIMIT]
    actual = stable_top_k(candidates, TOP_K_PARITY_LIMIT, score=score)
    parity = actual == expected
    return _check(
        TOP_K_PARITY_COUNT > FULL_SORT_THRESHOLD and parity,
        {
            "candidate_count": TOP_K_PARITY_COUNT,
            "full_sort_threshold": FULL_SORT_THRESHOLD,
            "limit": TOP_K_PARITY_LIMIT,
            "heap_branch_exercised": TOP_K_PARITY_COUNT > FULL_SORT_THRESHOLD,
            "matches_stable_full_sort": parity,
            "expected_prefix": expected[:5],
            "actual_prefix": actual[:5],
        },
        "stable_top_k diverged from stable full-sort parity above the heap threshold",
    )


def _int_tier_map(value: Any) -> dict[str, int]:
    mapping = value if isinstance(value, dict) else {}
    return {tier: int(mapping.get(tier, 0)) for tier in harness.EMBED_PRIORITY_TIERS}


def _embedding_priority_check(model: str, record_count: int) -> dict[str, Any]:
    progress = harness.embedding_progress(model)
    totals = _int_tier_map(progress.get("total_by_priority"))
    embedded = _int_tier_map(progress.get("embedded_by_priority"))
    pending = _int_tier_map(progress.get("pending_by_priority"))
    high_value_tiers = harness.EMBED_PRIORITY_TIERS[:2]
    expected_high_value_total = sum(totals[tier] for tier in high_value_tiers)
    expected_high_value_pending = sum(pending[tier] for tier in high_value_tiers)
    tier_arithmetic_valid = all(
        embedded[tier] + pending[tier] == totals[tier]
        for tier in harness.EMBED_PRIORITY_TIERS
    )
    passed = (
        progress.get("policy") == harness.EMBED_PRIORITY_POLICY
        and sum(totals.values()) == record_count
        and tier_arithmetic_valid
        and int(progress.get("high_value_total", -1)) == expected_high_value_total
        and int(progress.get("high_value_pending", -1)) == expected_high_value_pending
    )
    return _check(
        passed,
        {
            "policy": progress.get("policy"),
            "expected_policy": harness.EMBED_PRIORITY_POLICY,
            "total_by_priority": totals,
            "embedded_by_priority": embedded,
            "pending_by_priority": pending,
            "high_value_total": int(progress.get("high_value_total", 0)),
            "high_value_embedded": int(progress.get("high_value_embedded", 0)),
            "high_value_pending": int(progress.get("high_value_pending", 0)),
            "next_priority": progress.get("next_priority"),
            "tier_total": sum(totals.values()),
            "record_count": record_count,
            "tier_arithmetic_valid": tier_arithmetic_valid,
        },
        "value-aware embedding tier totals or high-value progress are inconsistent",
    )


def _query_cache_check(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    hit_delta = int(after.get("hits", 0)) - int(before.get("hits", 0))
    miss_delta = int(after.get("misses", 0)) - int(before.get("misses", 0))
    size = int(after.get("size", 0))
    capacity = int(after.get("capacity", 0))
    window_size = int(after.get("window_size", 0))
    main_size = int(after.get("main_size", 0))
    bounds_valid = (
        capacity >= 1
        and 0 <= size <= capacity
        and window_size >= 0
        and main_size >= 0
        and window_size + main_size == size
    )
    return _check(
        hit_delta >= 1 and bounds_valid,
        {
            "hits_before": int(before.get("hits", 0)),
            "hits_after": int(after.get("hits", 0)),
            "hit_delta": hit_delta,
            "miss_delta": miss_delta,
            "size": size,
            "capacity": capacity,
            "window_size": window_size,
            "main_size": main_size,
            "bounds_valid": bounds_valid,
        },
        "the repeated production query did not produce a cache hit or cache bounds are invalid",
    )


def _memory_admission_check() -> dict[str, Any]:
    persisted: list[str] = []

    def persist(text: str) -> bool:
        persisted.append(text)
        return True

    with tempfile.TemporaryDirectory(prefix="algo-cli-memory-probe-") as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        first = memory_candidates.process_memory_candidates(
            "Remember that our standard shell is zsh.",
            [],
            state_path,
            True,
            persist,
            daily_limit=2,
            entry_limit=4,
            char_limit=256,
        )
        duplicate = memory_candidates.process_memory_candidates(
            "Remember that our standard shell is zsh.",
            persisted,
            state_path,
            True,
            persist,
            daily_limit=2,
            entry_limit=4,
            char_limit=256,
        )
        privacy = memory_candidates.process_memory_candidates(
            "Remember that " + "sk-" + "abcdefghijklmnop123456 is the API key.",
            persisted,
            state_path,
            True,
            persist,
            daily_limit=2,
            entry_limit=4,
            char_limit=256,
        )
        state_text = state_path.read_text(encoding="utf-8")
        state_payload = json.loads(state_text)

    first_stored = int(first.get("counts", {}).get("stored") or 0)
    duplicate_stored = int(duplicate.get("counts", {}).get("stored") or 0)
    privacy_stored = int(privacy.get("counts", {}).get("stored") or 0)
    duplicate_reasons = duplicate.get("reason_counts", {})
    privacy_reasons = privacy.get("reason_counts", {})
    accepted = state_payload.get("accepted", []) if isinstance(state_payload, dict) else []
    metadata_only_state = (
        "standard shell" not in state_text
        and "abcdefghijklmnop" not in state_text
        and isinstance(accepted, list)
        and len(accepted) == 1
    )
    passed = (
        first.get("status") == "stored"
        and first_stored == 1
        and duplicate_stored == 0
        and int(duplicate_reasons.get("duplicate_fingerprint") or 0) == 1
        and privacy_stored == 0
        and int(privacy_reasons.get("secret") or 0) == 1
        and len(persisted) == 1
        and metadata_only_state
    )
    return _check(
        passed,
        {
            "first_status": first.get("status"),
            "first_stored": first_stored,
            "duplicate_stored": duplicate_stored,
            "duplicate_reason_counts": duplicate_reasons,
            "privacy_stored": privacy_stored,
            "privacy_reason_counts": privacy_reasons,
            "persist_callback_count": len(persisted),
            "state_version": state_payload.get("version") if isinstance(state_payload, dict) else None,
            "state_entry_count": len(accepted) if isinstance(accepted, list) else 0,
            "metadata_only_state": metadata_only_state,
        },
        "memory admission did not store once, reject duplicate/secret input, or keep metadata-only state",
    )
def _build_probe_query(index: dict[str, Any], canonical: dict[str, Any], dimensions: int) -> str:
    # Include index identity to avoid colliding with a user query cached under a
    # different vector while keeping both calls in this probe identical.
    generated = str(index.get("generated") or "unknown").replace(" ", "-")
    source_version = str(canonical.get("file_mtime_ns") or canonical.get("updated") or "unknown")
    return (
        "rate your harness algorithm catalog self-evaluation "
        f"{CANONICAL_ALGO_ID} effectiveness-probe-{generated}-{source_version}-{dimensions}"
    )


def _vector_values(canonical: dict[str, Any]) -> tuple[list[float] | None, str]:
    raw = canonical.get("embedding")
    model = str(canonical.get("embedding_model") or "").strip()
    if not isinstance(raw, list) or not raw or not model:
        return None, model
    try:
        vector = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None, model
    if not all(math.isfinite(value) for value in vector):
        return None, model
    if math.sqrt(sum(value * value for value in vector)) <= 0.0:
        return None, model
    return vector, model


def run_algorithm_effectiveness_probe() -> dict[str, Any]:
    """Run the bounded live probe and return JSON-serializable evidence.

    Missing index/vector/NumPy prerequisites are reported as ``unavailable``;
    unexpected runtime exceptions are reported as ``error``.  A normal run is
    ``pass`` only when every required check passes.
    """
    try:
        index = harness.load_index()
        records = [
            record
            for record in (index.get("records", []) or [])
            if isinstance(record, dict)
        ]
    except Exception as exc:
        return _terminal_result("error", f"harness index load failed: {type(exc).__name__}: {exc}")

    if not records:
        return _terminal_result("unavailable", "harness index has no records")
    if not bool(getattr(harness, "_NUMPY", False)):
        return _terminal_result("unavailable", "NumPy exact-matrix retrieval is unavailable")

    canonical = next(
        (record for record in records if str(record.get("id") or "") == CANONICAL_ALGO_ID),
        None,
    )
    if canonical is None:
        return _terminal_result("unavailable", f"canonical record is missing: {CANONICAL_ALGO_ID}")
    query_vector, model = _vector_values(canonical)
    if query_vector is None:
        return _terminal_result(
            "unavailable",
            "canonical ALGO record has no valid persisted embedding/model",
        )

    checks: dict[str, dict[str, Any]] = {}
    embed_calls = 0

    def deterministic_embed(texts: list[str]) -> list[list[float]]:
        nonlocal embed_calls
        embed_calls += 1
        return [list(query_vector) for _text in texts]

    try:
        query = _build_probe_query(index, canonical, len(query_vector))
        query_cache_before = harness._QUERY_VEC_CACHE.snapshot()

        first_results = harness.hybrid_search(
            query,
            deterministic_embed,
            model,
            k=PROBE_RESULT_LIMIT,
        )
        bm25_cache_first = harness._BM25_INDEX_CACHE
        vector_cache_first = harness._VECTOR_MATRIX_CACHE

        second_results = harness.hybrid_search(
            query,
            deterministic_embed,
            model,
            k=PROBE_RESULT_LIMIT,
        )
        bm25_cache_second = harness._BM25_INDEX_CACHE
        vector_cache_second = harness._VECTOR_MATRIX_CACHE
        query_cache_after = harness._QUERY_VEC_CACHE.snapshot()

        first_canonical = _canonical_result(first_results)
        second_canonical = _canonical_result(second_results)
        first_provenance = (
            first_canonical.get("rank_provenance", {})
            if isinstance(first_canonical, dict)
            and isinstance(first_canonical.get("rank_provenance"), dict)
            else {}
        )
        second_provenance = (
            second_canonical.get("rank_provenance", {})
            if isinstance(second_canonical, dict)
            and isinstance(second_canonical.get("rank_provenance"), dict)
            else {}
        )

        lexical_score = float(first_provenance.get("lexical_score") or 0.0)
        keyword_rank = int(first_provenance.get("keyword_rank") or 0)
        bm25_cache_reused = bm25_cache_first is not None and bm25_cache_first is bm25_cache_second
        lexical_consistent = (
            first_provenance.get("keyword_rank") == second_provenance.get("keyword_rank")
            and first_provenance.get("lexical_score") == second_provenance.get("lexical_score")
        )
        checks["bm25_lexical"] = _check(
            first_canonical is not None
            and second_canonical is not None
            and keyword_rank >= 1
            and lexical_score > 0.0
            and bm25_cache_reused
            and lexical_consistent,
            {
                "canonical_id": CANONICAL_ALGO_ID,
                "keyword_rank": keyword_rank,
                "lexical_score": lexical_score,
                "provenance_present": bool(first_provenance),
                "cache_reused": bm25_cache_reused,
                "cache_rows": _cache_rows(bm25_cache_second),
                "repeat_provenance_consistent": lexical_consistent,
            },
            "canonical lexical provenance was absent/non-positive or the BM25 corpus cache was rebuilt",
        )

        vector_score = float(first_provenance.get("vector_score") or 0.0)
        matrix_cache_reused = (
            vector_cache_first is not None and vector_cache_first is vector_cache_second
        )
        matrix_rows, matrix_dimensions = _matrix_dimensions(vector_cache_second)
        vector_consistent = (
            first_provenance.get("vector_rank") == second_provenance.get("vector_rank")
            and first_provenance.get("vector_score") == second_provenance.get("vector_score")
        )
        checks["exact_vector"] = _check(
            first_canonical is not None
            and second_canonical is not None
            and vector_score >= EXACT_VECTOR_MIN_SCORE
            and matrix_cache_reused
            and matrix_rows > 0
            and matrix_dimensions == len(query_vector)
            and vector_consistent,
            {
                "query_vector_source": CANONICAL_ALGO_ID,
                "model": model,
                "dimensions": len(query_vector),
                "vector_rank": int(first_provenance.get("vector_rank") or 0),
                "vector_score": vector_score,
                "minimum_exact_score": EXACT_VECTOR_MIN_SCORE,
                "matrix_cache_reused": matrix_cache_reused,
                "matrix_rows": matrix_rows,
                "matrix_dimensions": matrix_dimensions,
                "repeat_provenance_consistent": vector_consistent,
                "local_embed_calls": embed_calls,
            },
            "the canonical self-vector was not exact or the normalized matrix cache was rebuilt",
        )

        embedded, eligible = harness._retrieval_embedding_coverage(
            model,
            harness=None,
            kind=None,
        )
        expected_mode = "rrf" if eligible > 0 and embedded == eligible else "coverage-neutral-rrf"
        expected_coverage = round(embedded / eligible, 6) if eligible else 0.0
        provenances = [
            result.get("rank_provenance", {})
            for result in [*first_results, *second_results]
            if isinstance(result, dict) and isinstance(result.get("rank_provenance"), dict)
        ]
        observed_modes = sorted({str(item.get("fusion_mode") or "") for item in provenances})
        observed_coverages = sorted(
            {round(float(item.get("embedding_coverage") or 0.0), 6) for item in provenances}
        )
        first_sources = list(first_canonical.get("rank_sources", [])) if first_canonical else []
        second_sources = list(second_canonical.get("rank_sources", [])) if second_canonical else []
        dual_source = set(first_sources) == {"keyword", "vector"} and set(second_sources) == {
            "keyword",
            "vector",
        }
        raw_rrf = float(first_provenance.get("rrf_raw_score") or 0.0)
        fused_rrf = float(first_provenance.get("rrf_score") or 0.0)
        expected_fused = raw_rrf if expected_mode == "rrf" else raw_rrf / 2.0
        rrf_arithmetic_valid = math.isclose(fused_rrf, expected_fused, abs_tol=1.5e-6)
        result_order_consistent = [item.get("id") for item in first_results] == [
            item.get("id") for item in second_results
        ]
        checks["rrf_fusion"] = _check(
            first_canonical is not None
            and second_canonical is not None
            and eligible > 0
            and observed_modes == [expected_mode]
            and observed_coverages == [expected_coverage]
            and dual_source
            and rrf_arithmetic_valid
            and result_order_consistent,
            {
                "embedded": embedded,
                "eligible": eligible,
                "expected_mode": expected_mode,
                "observed_modes": observed_modes,
                "expected_coverage": expected_coverage,
                "observed_coverages": observed_coverages,
                "canonical_rank_sources": first_sources,
                "dual_source_provenance": dual_source,
                "rrf_raw_score": raw_rrf,
                "rrf_score": fused_rrf,
                "rrf_arithmetic_valid": rrf_arithmetic_valid,
                "repeat_result_order_consistent": result_order_consistent,
                "result_count": len(first_results),
            },
            "RRF mode/coverage, dual-source provenance, or repeat ordering was inconsistent",
        )

        checks["stable_top_k"] = _top_k_parity_check()
        checks["window_tinylfu"] = _query_cache_check(query_cache_before, query_cache_after)
        checks["embedding_priority"] = _embedding_priority_check(model, len(records))
        checks["memory_admission"] = _memory_admission_check()
    except Exception as exc:
        reason = f"production-path probe failed: {type(exc).__name__}: {exc}"
        for name in REQUIRED_CHECKS:
            checks.setdefault(
                name,
                {
                    "status": "error",
                    "required": True,
                    "reason": reason,
                    "evidence": {},
                },
            )
        return {
            "schema_version": PROBE_SCHEMA_VERSION,
            "probe": PROBE_NAME,
            "status": "error",
            "reason": reason,
            "required_checks": list(REQUIRED_CHECKS),
            "summary": {
                "required": len(REQUIRED_CHECKS),
                "passed": sum(item.get("status") == "pass" for item in checks.values()),
                "error": sum(item.get("status") == "error" for item in checks.values()),
            },
            "checks": checks,
        }

    passed = sum(check.get("status") == "pass" for check in checks.values())
    overall_status = "pass" if passed == len(REQUIRED_CHECKS) else "fail"
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "probe": PROBE_NAME,
        "status": overall_status,
        "reason": "" if overall_status == "pass" else "one or more required algorithm checks failed",
        "index": {
            "generated": str(index.get("generated") or ""),
            "record_count": len(records),
            "canonical_id": CANONICAL_ALGO_ID,
            "embedding_model": model,
            "embedding_dimensions": len(query_vector),
        },
        "required_checks": list(REQUIRED_CHECKS),
        "summary": {
            "required": len(REQUIRED_CHECKS),
            "passed": passed,
            "failed": len(REQUIRED_CHECKS) - passed,
        },
        "checks": checks,
    }


__all__ = [
    "CANONICAL_ALGO_ID",
    "PROBE_NAME",
    "PROBE_SCHEMA_VERSION",
    "REQUIRED_CHECKS",
    "run_algorithm_effectiveness_probe",
]
