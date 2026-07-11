from __future__ import annotations

import json
import os
import subprocess
import sys
import math
from concurrent.futures import ThreadPoolExecutor

from algo_cli.intuition_engine import IntuitionEngine


def fake_embed(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        low = text.lower()
        if "python" in low:
            vectors.append([1.0, 0.0])
        elif "rust" in low:
            vectors.append([0.0, 1.0])
        else:
            vectors.append([0.5, 0.5])
    return vectors


def test_recall_disabled_by_default(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    engine.capture_block("note", "Stable project context.", source="test", embed_fn=fake_embed)

    assert engine.recall("project context", embed_fn=fake_embed) == []
    assert engine.run("project context", embed_fn=fake_embed) is None
    assert engine.last_verdict()["reason"] == "recall_disabled"


def test_run_formats_recalled_blocks_when_enabled(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    block_id = engine.capture_block("note", "Python project context.", source="test", embed_fn=fake_embed)

    result = engine.run("python context", enabled=True, embed_fn=fake_embed)

    assert result is not None
    assert result.startswith("python context\n\n## Relevant Context")
    assert f"- [NOTE] `{block_id}` (1.00): Python project context." in result


def test_format_for_injection_is_public_and_block_based(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))

    text = engine.format_for_injection(
        [{"id": "pattern:20260522:abcd1234", "type": "pattern", "score": 0.87, "content": "Prefer list comprehensions."}]
    )

    assert text == (
        "## Relevant Context (from memory)\n"
        "- [PATTERN] `pattern:20260522:abcd1234` (0.87): Prefer list comprehensions."
    )


def test_capture_deduplicates_by_type_and_content_hash(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))

    first = engine.capture_block("memory", "Python is preferred.", source="test", embed_fn=fake_embed)
    second = engine.capture_block("memory", "Python is preferred.", source="test", embed_fn=fake_embed)

    assert first == second
    assert engine.status()["block_count"] == 1
    assert engine.status()["embedded"] == 1


def test_concurrent_intuition_saves_do_not_share_temp_file(tmp_path):
    index_path = str(tmp_path / "intuition.json")

    def capture(i: int) -> str:
        engine = IntuitionEngine(index_path=index_path)
        return engine.capture_block("memory", f"Python fact {i}.", source="test", embed_fn=fake_embed)

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(capture, range(25)))

    assert len(ids) == 25
    persisted = IntuitionEngine(index_path=index_path)
    assert set(persisted.blocks) == set(ids)
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_intuition_process_writes_are_preserved(tmp_path):
    index_path = tmp_path / "intuition.json"
    script = (
        "import sys; "
        "from algo_cli.intuition_engine import IntuitionEngine; "
        "engine = IntuitionEngine(index_path=sys.argv[1]); "
        "engine.capture_block('memory', sys.argv[2], source='process')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    contents = [f"Process intuition fact {i}." for i in range(12)]

    processes = [subprocess.Popen([sys.executable, "-c", script, str(index_path), content], env=env) for content in contents]
    failures = [proc.wait(timeout=15) for proc in processes]

    assert failures == [0] * len(contents)
    data = json.loads(index_path.read_text(encoding="utf-8"))
    persisted = {block["content"] for block in data["blocks"].values()}
    assert persisted == set(contents)
    assert not list(tmp_path.glob("*.tmp"))


def test_recall_uses_embedding_similarity(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    python_id = engine.capture_block("memory", "Python is preferred.", source="test", embed_fn=fake_embed)
    engine.capture_block("memory", "Rust is useful for systems.", source="test", embed_fn=fake_embed)

    results = engine.recall("python tools", enabled=True, embed_fn=fake_embed, min_score=0.1)

    assert results[0]["id"] == python_id
    assert results[0]["score"] == 1.0


def test_recall_threshold_matches_displayed_rounded_score(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    borderline = 0.6496

    def borderline_embed(texts: list[str]) -> list[list[float]]:
        return [
            [1.0, 0.0] if text == "query" else [borderline, math.sqrt(1 - borderline**2)]
            for text in texts
        ]

    block_id = engine.capture_block("pattern", "candidate", source="test", embed_fn=borderline_embed)

    results = engine.recall("query", enabled=True, embed_fn=borderline_embed, min_score=0.65)

    assert len(results) == 1
    assert results[0]["id"] == block_id
    assert results[0]["score"] == 0.65


def test_list_forget_and_reindex_blocks(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    first = engine.capture_block("memory", "Python is preferred.", source="test")
    second = engine.capture_block("memory", "Rust is useful.", source="test")

    status = engine.status()
    assert status["block_count"] == 2
    assert status["pending"] == 2
    assert {block["id"] for block in engine.list_blocks()} == {first, second}

    result = engine.reindex(fake_embed, embedding_model="fake-embed")
    assert result["ok"] is True
    assert result["updated"] == 2
    assert engine.status()["embedded"] == 2

    removed = engine.forget_block(first)
    assert removed is not None
    assert engine.status()["block_count"] == 1
    assert engine.forget_block(first) is None


def test_reindex_batches_embedding_requests(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    engine.index_blocks([
        {"type": "memory", "content": "Python one."},
        {"type": "memory", "content": "Python two."},
        {"type": "memory", "content": "Rust three."},
    ])
    calls: list[list[str]] = []

    def recording_embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return fake_embed(texts)

    result = engine.reindex(recording_embed, embedding_model="fake-embed", batch_size=2)

    assert result["ok"] is True
    assert result["updated"] == 3
    assert [len(batch) for batch in calls] == [2, 1]


def test_reindex_isolates_malformed_vector_in_batch(tmp_path):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    engine.index_blocks([
        {"type": "memory", "content": "Good vector."},
        {"type": "memory", "content": "Bad vector."},
    ])

    result = engine.reindex(
        lambda _texts: [[1.0, 0.0], ["not-a-number"]],
        embedding_model="fake-embed",
    )

    assert result["ok"] is False
    assert result["updated"] == 1
    assert result["failed"] == 1
    assert engine.status()["embedded"] == 1


def test_sync_from_squeezer_persists_one_batch(tmp_path, monkeypatch):
    engine = IntuitionEngine(index_path=str(tmp_path / "intuition.json"))
    saves = 0
    original_save = engine._save_index

    def counting_save():
        nonlocal saves
        saves += 1
        original_save()

    class _Squeezer:
        def get_new_blocks(self):
            return [
                {"type": "memory", "content": "One."},
                {"type": "memory", "content": "Two."},
                {"type": "memory", "content": "Three."},
            ]

    monkeypatch.setattr(engine, "_save_index", counting_save)

    assert engine.sync_from_squeezer(_Squeezer()) == 3
    assert saves == 1
    assert engine.status()["block_count"] == 3


def test_capture_rechecks_stale_duplicate_after_external_forget(tmp_path):
    index_path = str(tmp_path / "intuition.json")
    engine = IntuitionEngine(index_path=index_path)
    block_id = engine.capture_block("memory", "Python is preferred.", source="test", embed_fn=fake_embed)
    other = IntuitionEngine(index_path=index_path)
    assert other.forget_block(block_id) is not None
    embed_calls = 0

    def counting_embed(texts: list[str]) -> list[list[float]]:
        nonlocal embed_calls
        embed_calls += 1
        return fake_embed(texts)

    restored_id = engine.capture_block(
        "memory",
        "Python is preferred.",
        source="test",
        embed_fn=counting_embed,
    )

    assert embed_calls == 1
    assert restored_id in IntuitionEngine(index_path=index_path).blocks
