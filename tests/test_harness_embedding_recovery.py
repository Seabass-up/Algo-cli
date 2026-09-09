"""Embedding failures must leave a resumable, source-current index."""

import json
import os
from typing import Any

import pytest

from algo_cli import harness


def _index(monkeypatch: pytest.MonkeyPatch, count: int = 3) -> None:
    monkeypatch.setattr(harness, "all_source_roots", lambda: ())
    monkeypatch.setattr(harness, "EMBED_WRITE_INTERVAL_S", 1_000_000)
    payload = {
        "source_policy": harness._source_policy(),
        "record_count": count,
        "records": [
            {"id": str(i), "harness": "test", "kind": "wiki", "search_text": f"record {i}"} for i in range(count)
        ],
    }
    harness.INDEX_PATH.write_text(json.dumps(payload), encoding="utf-8")
    harness._set_index_cache(None)


def _persisted_count() -> int:
    return sum(bool(row.get("embedding")) for row in json.loads(harness.INDEX_PATH.read_text())["records"])


@pytest.mark.parametrize("failure", ["short_batch", "exception", "interrupt"])
def test_failure_checkpoints_completed_batches_and_resume_skips_them(monkeypatch, failure):
    _index(monkeypatch)
    calls = 0

    def embed(texts):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [[1.0, 0.5] for _ in texts]
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "exception":
            raise RuntimeError("temporary backend failure")
        return []

    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            harness.embed_index_records(embed, "test-model", batch_size=2)
    else:
        result = harness.embed_index_records(embed, "test-model", batch_size=2)
        assert result["ready"] is False
        assert result["embedded"] == 2
    assert _persisted_count() == 2

    harness._set_index_cache(None)
    resumed_texts = []

    def resume(texts):
        resumed_texts.extend(texts)
        return [[1.0, 0.5] for _ in texts]

    result = harness.embed_index_records(resume, "test-model")
    assert result["ready"] is True
    assert result["embedded"] == 1
    assert len(resumed_texts) == 1
    assert _persisted_count() == 3


@pytest.mark.parametrize(
    "bad", [None, [], "invalid", [True, 0], ["1.0", 0], [float("nan"), 1], [float("inf"), 1], [0, 0]]
)
def test_invalid_vector_rejects_entire_batch_without_persisting(monkeypatch, bad: Any):
    _index(monkeypatch, 2)
    result = harness.embed_index_records(lambda _: [[1.0, 0.5], bad], "test-model")
    assert result["ready"] is False
    assert result["reason"] == "invalid_embedding_vector"
    assert result["embedded"] == 0
    assert result["pending"] == 2
    assert _persisted_count() == 0


def test_mixed_vector_dimensions_reject_entire_batch(monkeypatch):
    _index(monkeypatch, 2)
    result = harness.embed_index_records(lambda _: [[1.0, 0.5], [1.0]], "test-model")
    assert result["ready"] is False
    assert result["reason"] == "embedding_dimension_mismatch"
    assert result["embedded"] == 0
    assert _persisted_count() == 0


def test_dimension_change_after_a_checkpoint_preserves_prior_batch(monkeypatch):
    _index(monkeypatch)
    harness.embed_index_records(lambda texts: [[1.0, 0.5] for _ in texts], "test-model", max_records=1)
    result = harness.embed_index_records(lambda texts: [[1.0] * 3 for _ in texts], "test-model")
    assert result["ready"] is False
    assert result["reason"] == "embedding_dimension_mismatch"
    assert _persisted_count() == 1


@pytest.mark.parametrize("changed", [False, True])
def test_refresh_reuses_embedding_only_when_actual_embedding_input_is_unchanged(monkeypatch, tmp_path, changed):
    root = tmp_path / "wiki"
    root.mkdir()
    source = root / "note.md"
    source.write_text("# Original\nThe harness checkpoints completed embedding batches.\n")
    roots = (harness.SourceRoot("test", "wiki", root, ("*.md",), 10),)
    monkeypatch.setattr(harness, "all_source_roots", lambda: roots)
    harness.load_index(refresh=True)
    harness.embed_index_records(lambda texts: [[1.0, 0.5] for _ in texts], "test-model")
    old_mtime = source.stat().st_mtime_ns
    if changed:
        source.write_text("# Changed\nThe harness must invalidate changed source content.\n")
    os.utime(source, ns=(old_mtime + 1_000_000, old_mtime + 1_000_000))

    refreshed = harness.load_index(refresh=True)
    record = refreshed["records"][0]
    assert record["file_mtime_ns"] == source.stat().st_mtime_ns
    if changed:
        assert not record.get("embedding")
    else:
        assert record["embedding"] == [1.0, 0.5]
        assert record["embedding_model"] == "test-model"


@pytest.mark.parametrize("count", [3, 35])
def test_source_changes_are_not_hidden_by_embedding_writes(monkeypatch, count):
    _index(monkeypatch, count)
    watermark = 10
    monkeypatch.setattr(harness, "_source_watermark_ns", lambda _index: watermark)

    def embed(texts):
        nonlocal watermark
        watermark += 1
        return [[1.0, 0.5] for _ in texts]

    result = harness.embed_index_records(embed, "test-model")
    assert result["ready"] is False
    assert result["reason"] == "source_changed_during_embedding"
    assert _persisted_count() == 0
    assert harness._INDEX_CACHE is None


@pytest.mark.parametrize("batch_size", [0, -1, True])
def test_invalid_batch_size_does_not_report_success(monkeypatch, batch_size):
    _index(monkeypatch)
    with pytest.raises(ValueError, match="batch_size"):
        harness.embed_index_records(lambda _: [], "test-model", batch_size=batch_size)


def test_empty_index_is_not_reported_ready(monkeypatch):
    _index(monkeypatch, 0)
    result = harness.embed_index_records(lambda _: pytest.fail("nothing to embed"), "test-model")
    assert result["ready"] is False
    assert result["reason"] == "empty_index"


def test_failed_checkpoint_cannot_leave_an_unpersisted_ready_cache(monkeypatch):
    _index(monkeypatch)

    def fail_write(*_args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(harness, "_atomic_write_json", fail_write)
    result = harness.embed_index_records(lambda texts: [[1.0, 0.5] for _ in texts], "test-model")
    assert result["ready"] is False
    assert result["reason"] == "index_write_error"
    assert harness._INDEX_CACHE is None
    assert _persisted_count() == 0


@pytest.mark.parametrize("content_changed", [False, True])
@pytest.mark.parametrize("dimensions", [2, None])
def test_capability_embedding_reuse_depends_on_content_not_package_timestamp(content_changed, dimensions):
    prior = harness._runtime_capability_records()[0]
    prior["embedding"] = [0.25, 0.75]
    prior["embedding_model"] = "test-model"
    prior["embedding_dimensions"] = dimensions
    prior["file_mtime_ns"] -= 1
    prior["file_size"] -= 1
    if content_changed:
        prior["search_text"] = "a different capability contract"
    current = next(row for row in harness._runtime_capability_records({"records": [prior]}) if row["id"] == prior["id"])
    if content_changed:
        assert not current.get("embedding")
        assert "embedding_dimensions" not in current
    else:
        assert current["embedding"] == [0.25, 0.75]
        assert current["embedding_model"] == "test-model"
        assert current["embedding_dimensions"] == dimensions
