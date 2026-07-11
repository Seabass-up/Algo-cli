"""Focused tests for the bounded live algorithm-effectiveness probe."""

from __future__ import annotations

import json
from typing import Any

from algo_cli import harness
from algo_cli.evals import algorithm_effectiveness


MODEL = "probe-embedding-model"


def _record(
    record_id: str,
    harness_name: str,
    kind: str,
    text: str,
    *,
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    relative_path = record_id.rsplit(":", 1)[-1]
    record: dict[str, Any] = {
        "id": record_id,
        "harness": harness_name,
        "kind": kind,
        "title": text.title(),
        "path": f"__pytest_harness__/{relative_path}",
        "relative_path": relative_path,
        "description": text,
        "tags": [],
        "summary": text,
        "index_text": text,
        "heading_text": "",
        "search_text": text.lower(),
        "file_size": len(text),
        "file_mtime_ns": 1,
    }
    if embedding is not None:
        record["embedding"] = embedding
        record["embedding_model"] = MODEL
    return record


def _write_probe_index(*, complete_coverage: bool = False) -> None:
    canonical = _record(
        algorithm_effectiveness.CANONICAL_ALGO_ID,
        "algo-cli",
        "algorithm",
        "rate your harness algorithm catalog self evaluation canonical reviewed patterns",
        embedding=[1.0, 0.0, 0.0],
    )
    canonical = harness._normalize_reviewed_algo_record(canonical)
    canonical["embedding"] = [1.0, 0.0, 0.0]
    canonical["embedding_model"] = MODEL

    records = [
        _record(
            "codex:plugin:gamma/plugin.json",
            "codex",
            "plugin",
            "codex plugin metadata gamma",
        ),
        _record(
            "algo-cli:wiki:runtime.md",
            "algo-cli",
            "wiki",
            "runtime harness maintenance wiki",
            embedding=[0.0, 1.0, 0.0],
        ),
        _record(
            "algo-cli:skill:retrieval.md",
            "algo-cli",
            "skill",
            "retrieval skill BM25 vector fusion",
            embedding=[0.0, 0.0, 1.0] if complete_coverage else None,
        ),
        _record(
            "codex:connector:apps.json",
            "codex",
            "connector",
            "connector runtime metadata",
            embedding=[0.0, 1.0, 1.0],
        ),
        _record(
            "index-compute-lab:memory:atom.md",
            "index-compute-lab",
            "memory",
            "durable graph memory atom",
        ),
        canonical,
    ]
    if complete_coverage:
        for position, record in enumerate(records):
            if record.get("embedding"):
                continue
            record["embedding"] = [0.0, 1.0, float(position % 2)]
            record["embedding_model"] = MODEL
    index = {
        "generated": "2026-07-10T16:00:00",
        "record_count": len(records),
        "roots": [],
        "indexer": "python",
        "records": records,
        "embeddings": harness._embeddings_summary(records, active_model=MODEL),
    }
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._INDEX_CACHE_SIGNATURE = None
    harness._STALE_CHECK_CACHE = None
    harness._BM25_INDEX_CACHE = None
    harness._VECTOR_MATRIX_CACHE = None
    harness._QUERY_VEC_CACHE.clear()


def test_live_probe_passes_all_required_checks_with_partial_coverage() -> None:
    _write_probe_index(complete_coverage=False)

    result = algorithm_effectiveness.run_algorithm_effectiveness_probe()

    assert result["status"] == "pass"
    assert result["summary"] == {
        "required": len(algorithm_effectiveness.REQUIRED_CHECKS),
        "passed": len(algorithm_effectiveness.REQUIRED_CHECKS),
        "failed": 0,
    }
    assert all(check["status"] == "pass" for check in result["checks"].values())
    assert result["checks"]["bm25_lexical"]["evidence"]["cache_reused"] is True
    assert result["checks"]["exact_vector"]["evidence"]["vector_score"] >= 0.999
    assert result["checks"]["exact_vector"]["evidence"]["matrix_cache_reused"] is True
    assert result["checks"]["rrf_fusion"]["evidence"]["expected_mode"] == "coverage-neutral-rrf"
    assert result["checks"]["rrf_fusion"]["evidence"]["dual_source_provenance"] is True
    assert result["checks"]["stable_top_k"]["evidence"]["heap_branch_exercised"] is True
    assert result["checks"]["window_tinylfu"]["evidence"]["hit_delta"] >= 1
    assert result["checks"]["embedding_priority"]["evidence"]["policy"] == harness.EMBED_PRIORITY_POLICY
    assert result["checks"]["embedding_priority"]["evidence"]["high_value_pending"] == 2
    assert result["checks"]["memory_admission"]["evidence"]["first_stored"] == 1
    assert result["checks"]["memory_admission"]["evidence"]["privacy_stored"] == 0
    assert result["checks"]["memory_admission"]["evidence"]["metadata_only_state"] is True
    json.dumps(result)


def test_live_probe_accepts_complete_coverage_rrf_mode() -> None:
    _write_probe_index(complete_coverage=True)

    result = algorithm_effectiveness.run_algorithm_effectiveness_probe()

    assert result["status"] == "pass"
    fusion = result["checks"]["rrf_fusion"]["evidence"]
    assert fusion["expected_mode"] == "rrf"
    assert fusion["observed_modes"] == ["rrf"]
    assert fusion["expected_coverage"] == 1.0


def test_live_probe_reports_unavailable_without_canonical_record() -> None:
    records = [
        _record(
            "algo-cli:wiki:runtime.md",
            "algo-cli",
            "wiki",
            "runtime harness maintenance wiki",
            embedding=[1.0, 0.0, 0.0],
        )
    ]
    harness.INDEX_PATH.write_text(
        json.dumps({"record_count": 1, "records": records}),
        encoding="utf-8",
    )
    harness._INDEX_CACHE = None
    harness._INDEX_CACHE_SIGNATURE = None

    result = algorithm_effectiveness.run_algorithm_effectiveness_probe()

    assert result["status"] == "unavailable"
    assert "canonical record is missing" in result["reason"]
    assert all(check["status"] == "unavailable" for check in result["checks"].values())


def test_live_probe_reports_error_instead_of_raising(monkeypatch) -> None:
    _write_probe_index()

    def explode(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("synthetic hybrid failure")

    monkeypatch.setattr(harness, "hybrid_search", explode)

    result = algorithm_effectiveness.run_algorithm_effectiveness_probe()

    assert result["status"] == "error"
    assert "synthetic hybrid failure" in result["reason"]
    assert all(check["status"] == "error" for check in result["checks"].values())


def test_live_probe_fails_when_any_required_check_fails(monkeypatch) -> None:
    _write_probe_index()
    monkeypatch.setattr(
        algorithm_effectiveness,
        "stable_top_k",
        lambda _items, _k, score: [],
    )

    result = algorithm_effectiveness.run_algorithm_effectiveness_probe()

    assert result["status"] == "fail"
    assert result["checks"]["stable_top_k"]["status"] == "fail"
    assert result["summary"]["passed"] == len(algorithm_effectiveness.REQUIRED_CHECKS) - 1
