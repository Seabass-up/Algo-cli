"""Identity layer: scaffold, mtime cache, drift, RAG retrieval."""

from __future__ import annotations

import json
import time

import pytest

from algo_cli import identity
from conftest import make_fake_embed


def test_scaffold_creates_four_files():
    created = identity.scaffold_if_needed()
    assert len(created) == 4
    for path in identity.ALL_PATHS:
        assert path.exists()
    # second call is a no-op
    assert identity.scaffold_if_needed() == []


def test_scaffold_refreshes_only_the_stock_legacy_identity():
    identity.scaffold_if_needed()
    identity.IDENTITY_PATH.write_text(identity.LEGACY_DEFAULT_IDENTITY, encoding="utf-8")

    assert identity.scaffold_if_needed() == []
    assert identity.IDENTITY_PATH.read_text(encoding="utf-8") == identity.DEFAULT_IDENTITY


def test_scaffold_preserves_custom_identity():
    identity.scaffold_if_needed()
    custom = "# Algo CLI - Identity\n\nMy customized runtime identity.\n"
    identity.IDENTITY_PATH.write_text(custom, encoding="utf-8")

    assert identity.scaffold_if_needed() == []
    assert identity.IDENTITY_PATH.read_text(encoding="utf-8") == custom


def test_read_cached_hits_cache():
    identity.scaffold_if_needed()
    first = identity.read_cached(identity.SOUL_PATH)
    second = identity.read_cached(identity.SOUL_PATH)
    assert first is second  # same object -> cache hit, no re-read
    assert "Soul" in first


def test_identity_mtime_key_changes_when_user_file_touched():
    identity.scaffold_if_needed()
    key_a = identity.identity_mtime_key()
    time.sleep(0.02)
    identity.USER_PATH.write_text("# About the User\n\nTouched.\n", encoding="utf-8")
    key_b = identity.identity_mtime_key()
    assert key_a != key_b


def test_detect_changes():
    identity.scaffold_if_needed()
    identity.build_identity_block()  # populates cache
    assert identity.detect_changes() == []

    time.sleep(0.02)
    identity.USER_PATH.write_text("# About the User\n\nName: Tester\n", encoding="utf-8")
    changed = identity.detect_changes()
    assert identity.USER_PATH in changed


def test_build_identity_block_sections():
    identity.scaffold_if_needed()
    block = identity.build_identity_block()
    assert "## Identity" in block
    assert "## Soul" in block
    assert "## About the User" in block


def test_build_identity_block_with_retrieved_lessons():
    identity.scaffold_if_needed()
    block = identity.build_identity_block(retrieved_lessons=["lesson one", "lesson two"])
    assert "## Relevant Lessons" in block
    assert "lesson one" in block
    # full inline lessons section should not appear when retrieval is used
    assert "## Lessons Learned" not in block


def test_append_lesson_and_cache_invalidation():
    identity.scaffold_if_needed()
    identity.read_cached(identity.LESSONS_PATH)
    identity.append_lesson("Always quote Windows paths with spaces.")
    refreshed = identity.read_cached(identity.LESSONS_PATH)
    assert "Always quote Windows paths" in refreshed


def test_write_user_profile_overwrites():
    identity.scaffold_if_needed()
    identity.write_user_profile("# About the User\n\nName: Example User\n")
    assert "Example User" in identity.read_cached(identity.USER_PATH)


def test_chunk_lessons():
    text = (
        "# Lessons Learned\n\n"
        "## 2026-05-14 09:00\nFirst lesson body with enough characters to count.\n\n"
        "## 2026-05-14 10:00\nSecond lesson body, also long enough to be retained.\n"
    )
    chunks = identity._chunk_lessons(text)
    assert len(chunks) == 2
    assert all(c.startswith("## ") for c in chunks)


def test_cosine_properties():
    assert identity._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert identity._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert identity._cosine([], [1.0]) == 0.0


def test_rebuild_and_retrieve_lessons():
    identity.scaffold_if_needed()
    identity.LESSONS_PATH.write_text(
        "# Lessons Learned\n\n"
        "## L1\nThe footer toolbar needs noreverse to avoid white inversion.\n\n"
        "## L2\nThe rust indexer is used only for cold-start builds.\n",
        encoding="utf-8",
    )
    embed = make_fake_embed()
    result = identity.rebuild_lessons_index(embed, "fake-model")
    assert result["ready"] is True
    assert result["chunk_count"] == 2
    assert result["dimensions"] == 16

    persisted = json.loads(identity.LESSONS_INDEX_PATH.read_text(encoding="utf-8"))
    assert persisted["embedding_model"] == "fake-model"
    assert persisted["vector_dimensions"] == 16

    hits = identity.retrieve_lessons("footer toolbar question", embed, "fake-model", k=1)
    assert len(hits) == 1
    assert "footer" in hits[0].lower()


def test_lessons_index_stale_detection():
    identity.scaffold_if_needed()
    identity.LESSONS_PATH.write_text(
        "# Lessons Learned\n\n## L1\nA lesson long enough to pass the minimum length.\n",
        encoding="utf-8",
    )
    embed = make_fake_embed()
    identity.rebuild_lessons_index(embed, "fake-model")
    assert identity.lessons_index_stale() is False

    time.sleep(0.02)
    identity.append_lesson("Another lesson appended after the index was built.")
    assert identity.lessons_index_stale() is True


@pytest.mark.parametrize("query_dimensions", [2, 3])
def test_retrieve_rejects_different_model_with_same_or_different_dimensions(query_dimensions):
    identity.scaffold_if_needed()
    identity.LESSONS_PATH.write_text(
        "# Lessons Learned\n\n## L1\nA sufficiently long model identity safety lesson.\n",
        encoding="utf-8",
    )
    identity.rebuild_lessons_index(lambda _texts: [[1.0, 0.0]], "model-a")
    query_calls = 0

    def query_embed(texts):
        nonlocal query_calls
        query_calls += 1
        return [[1.0] + [0.0] * (query_dimensions - 1) for _ in texts]

    assert identity.lessons_index_stale("model-b") is True
    assert identity.retrieve_lessons("safety", query_embed, "model-b") == []
    # Reject before embedding: equal vector width does not make two models compatible.
    assert query_calls == 0


def test_retrieve_rejects_query_vector_dimension_mismatch_without_matmul_error():
    identity.scaffold_if_needed()
    identity.LESSONS_PATH.write_text(
        "# Lessons Learned\n\n## L1\nA sufficiently long vector dimension safety lesson.\n",
        encoding="utf-8",
    )
    identity.rebuild_lessons_index(lambda _texts: [[1.0, 0.0]], "model-a")

    assert identity.retrieve_lessons(
        "safety",
        lambda _texts: [[1.0, 0.0, 0.0]],
        "model-a",
    ) == []


def test_rebuild_rejects_mixed_vector_dimensions_without_writing_invalid_index():
    identity.scaffold_if_needed()
    identity.LESSONS_PATH.write_text(
        "# Lessons Learned\n\n"
        "## L1\nThe first sufficiently long dimension validation lesson.\n\n"
        "## L2\nThe second sufficiently long dimension validation lesson.\n",
        encoding="utf-8",
    )

    result = identity.rebuild_lessons_index(
        lambda _texts: [[1.0, 0.0], [1.0, 0.0, 0.0]],
        "model-a",
    )

    assert result == {
        "chunk_count": 0,
        "ready": False,
        "reason": "embed_dimension_mismatch",
    }
    assert not identity.LESSONS_INDEX_PATH.exists()
