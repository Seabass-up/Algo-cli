"""Persistent agent-thread storage and handoff tests."""

from __future__ import annotations

import pytest

from algo_cli import agent_threads


def test_thread_lifecycle_and_prefix_resolution(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread(
        "Inspect the runtime",
        role="reviewer",
        pipeline="review",
        model="qwen3",
        path=path,
    )

    agent_threads.begin_turn(record["id"], "Inspect the runtime", path=path)
    finished = agent_threads.finish_turn(
        record["id"],
        status="complete",
        output="Verified output",
        blocks=[{"role": "review", "status": "complete"}],
        path=path,
    )

    assert finished["status"] == "complete"
    assert finished["turns"][-1]["output"] == "Verified output"
    assert agent_threads.resolve_thread(record["id"][:5], path=path)["id"] == record["id"]
    assert "Verified output" in agent_threads.context_handoff(finished)


def test_child_thread_is_linked_to_parent(tmp_path):
    path = tmp_path / "threads.json"
    parent = agent_threads.create_thread("Parent task", path=path)
    child = agent_threads.create_thread(
        "Child task",
        role="critic",
        pipeline="specialist",
        parent_id=parent["id"],
        path=path,
    )

    reloaded_parent = agent_threads.resolve_thread(parent["id"], path=path)

    assert child["parent_id"] == parent["id"]
    assert child["id"] in reloaded_parent["children"]


def test_unknown_and_ambiguous_thread_references_are_rejected(tmp_path, monkeypatch):
    path = tmp_path / "threads.json"
    values = iter(["abc11111", "abc22222"])
    monkeypatch.setattr(agent_threads, "_new_id", lambda _existing: next(values))
    agent_threads.create_thread("One", path=path)
    agent_threads.create_thread("Two", path=path)

    with pytest.raises(KeyError, match="ambiguous"):
        agent_threads.resolve_thread("abc", path=path)
    with pytest.raises(KeyError, match="Unknown"):
        agent_threads.resolve_thread("missing", path=path)


def test_thread_output_and_history_are_bounded(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread("Task", path=path)
    for index in range(agent_threads.MAX_THREAD_TURNS + 3):
        agent_threads.begin_turn(record["id"], f"turn {index}", path=path)
        agent_threads.finish_turn(
            record["id"],
            status="complete",
            output="x" * (agent_threads.MAX_THREAD_OUTPUT_CHARS + 100),
            path=path,
        )

    loaded = agent_threads.resolve_thread(record["id"], path=path)

    assert loaded["task"] == "Task"
    assert len(loaded["turns"]) == agent_threads.MAX_THREAD_TURNS
    assert len(loaded["output"]) == agent_threads.MAX_THREAD_OUTPUT_CHARS
