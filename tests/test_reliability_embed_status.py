from __future__ import annotations

import json

from algo_cli import harness

_IDENTITY_A = "sha256:" + "a" * 64
_IDENTITY_B = "sha256:" + "b" * 64


def _write_pending_index(count: int = 3) -> None:
    records = [
        {
            "id": f"algo-cli:wiki:page-{i}",
            "harness": "algo-cli",
            "kind": "wiki",
            "title": f"page {i}",
            "path": f"__pytest_harness__/page-{i}.md",
            "search_text": f"algo wiki page {i}",
        }
        for i in range(count)
    ]
    harness.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    harness.INDEX_PATH.write_text(json.dumps({"record_count": len(records), "records": records}), encoding="utf-8")
    harness._INDEX_CACHE = None
    harness._ID_LOOKUP = None


class _BoundEmbedder:
    def __init__(self, identity: str, dims: int = 2) -> None:
        self.embedding_identity = identity
        self._dims = dims

    def validate_embedding_identity(self) -> bool:
        return True

    def __call__(self, texts):
        return [[1.0] * self._dims for _ in texts]


def _pending_recommendation(stats: dict) -> str:
    return next(item for item in stats["quality"]["recommendations"] if "/harness embed" in item)


def test_embed_pass_persists_its_embedding_contract() -> None:
    _write_pending_index()

    harness.embed_index_records(_BoundEmbedder(_IDENTITY_A), "m-old", dimensions=2, max_records=1)

    assert harness.last_embed_pass()["contract"] == {"model": "m-old", "dimensions": 2, "identity": _IDENTITY_A}


def test_pass_for_current_contract_is_reported_as_current() -> None:
    _write_pending_index()
    harness.embed_index_records(_BoundEmbedder(_IDENTITY_A), "m", dimensions=2, max_records=1)

    stats = harness.stats(model="m", dimensions=2, embedding_identity=_IDENTITY_A)

    last_pass = stats["embeddings"]["last_pass"]
    assert last_pass["outcome"] == "partial"
    assert last_pass["contract_status"] == "current"
    assert stats["quality"]["last_embed_pass"] == last_pass
    assert "last embed pass embedded 1, per-turn cap reached" in _pending_recommendation(stats)


def test_model_switch_labels_previous_contract_pass_stale() -> None:
    _write_pending_index()
    harness.embed_index_records(_BoundEmbedder(_IDENTITY_A), "m-old", dimensions=2, max_records=1)

    stats = harness.stats(model="m-new", dimensions=2, embedding_identity=_IDENTITY_A)

    assert stats["embeddings"]["last_pass"] is None
    assert stats["quality"]["last_embed_pass"] is None
    stale = stats["embeddings"]["stale_last_pass"]
    assert stale["contract_status"] == "different"
    assert stale["contract"]["model"] == "m-old"
    recommendation = _pending_recommendation(stats)
    assert "per-turn cap reached" not in recommendation
    assert "different embedding contract (model m-old, " in recommendation


def test_dimension_switch_labels_previous_contract_pass_stale() -> None:
    _write_pending_index()
    harness.embed_index_records(_BoundEmbedder(_IDENTITY_A), "m", dimensions=2, max_records=1)

    stats = harness.stats(model="m", dimensions=3, embedding_identity=_IDENTITY_A)

    assert stats["embeddings"]["last_pass"] is None
    assert stats["embeddings"]["stale_last_pass"]["contract"]["dimensions"] == 2
    assert "different embedding contract" in _pending_recommendation(stats)


def test_identity_switch_labels_previous_contract_pass_stale() -> None:
    _write_pending_index()
    harness.embed_index_records(_BoundEmbedder(_IDENTITY_A), "m", dimensions=2, max_records=1)

    stats = harness.stats(model="m", dimensions=2, embedding_identity=_IDENTITY_B)

    assert stats["embeddings"]["last_pass"] is None
    assert stats["embeddings"]["stale_last_pass"]["contract"]["identity"] == _IDENTITY_A


def test_pass_without_recorded_contract_is_labeled_unrecorded() -> None:
    _write_pending_index()
    harness.record_embed_pass("skipped", "non_local_host", pending=3)

    stats = harness.stats(model="m")

    last_pass = stats["embeddings"]["last_pass"]
    assert last_pass["reason"] == "non_local_host"
    assert last_pass["contract_status"] == "unrecorded"
    assert "stale_last_pass" not in stats["embeddings"]


def test_unavailable_active_identity_keeps_current_pass_outcome_visible() -> None:
    _write_pending_index()
    harness.record_embed_pass(
        "failed", "embedding_dimension_mismatch", model="m", dimensions=2, embedding_identity=_IDENTITY_A
    )

    stats = harness.stats(model="m", dimensions=2, embedding_identity=None)

    last_pass = stats["embeddings"]["last_pass"]
    assert last_pass is not None
    assert last_pass["reason"] == "embedding_dimension_mismatch"
    assert last_pass["contract_status"] == "unverified"
    assert "stale_last_pass" not in stats["embeddings"]
    assert stats["quality"]["last_embed_pass"] == last_pass
    recommendation = _pending_recommendation(stats)
    assert "different embedding contract" not in recommendation
    assert "last embed pass failed: " in recommendation
    assert "embedding identity unverified" in recommendation


def test_pass_recorded_without_identity_is_unverified_not_different() -> None:
    _write_pending_index()
    harness.record_embed_pass("skipped", "non_local_host", model="m", dimensions=2, embedding_identity=None)

    stats = harness.stats(model="m", dimensions=2, embedding_identity=_IDENTITY_A)

    assert stats["embeddings"]["last_pass"]["contract_status"] == "unverified"
    assert "stale_last_pass" not in stats["embeddings"]


def test_unavailable_identity_still_reports_model_switch_as_different() -> None:
    _write_pending_index()
    harness.record_embed_pass("failed", "x", model="m-old", dimensions=2, embedding_identity=_IDENTITY_A)

    stats = harness.stats(model="m-new", dimensions=2, embedding_identity=None)

    assert stats["embeddings"]["last_pass"] is None
    assert stats["embeddings"]["stale_last_pass"]["contract_status"] == "different"
