"""Persistent agent-thread storage and handoff tests."""

from __future__ import annotations

import json

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


def test_thread_workspace_preserves_initial_head_and_updates_git_evidence(tmp_path):
    path = tmp_path / "threads.json"
    initial = {
        "available": True,
        "workspace_root": "/workspace/feature",
        "repository_root": "/workspace/repo",
        "branch": "algo/feature",
        "head": "a" * 40,
        "clean": True,
    }
    record = agent_threads.create_thread("Task", workspace=initial, path=path)

    finished = agent_threads.finish_turn(
        record["id"],
        status="complete",
        workspace={
            **initial,
            "head": "b" * 40,
            "clean": False,
            "status": " M algo_cli/main.py",
            "status_digest": "e" * 64,
            "tracked_diff_digest": "c" * 64,
            "untracked_digest": "d" * 64,
        },
        path=path,
    )

    assert finished["workspace"]["initial_head"] == "a" * 40
    assert finished["workspace"]["head"] == "b" * 40
    assert finished["workspace"]["clean"] is False
    assert finished["workspace"]["status_digest"] == "e" * 64
    assert finished["workspace"]["tracked_diff_digest"] == "c" * 64
    handoff = agent_threads.context_handoff(finished)
    assert "algo/feature" in handoff
    assert "Initial HEAD" in handoff
    assert "/workspace/feature" not in handoff


def test_version_one_thread_store_migrates_without_losing_records(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(
        '{"version": 1, "threads": [{"id": "legacy01", "status": "complete", '
        '"task": "old task", "turns": [], "blocks": [], "children": []}]}',
        encoding="utf-8",
    )

    records = agent_threads.load_threads(path)

    assert records[0]["id"] == "legacy01"
    assert records[0]["workspace"] == {}
    assert records[0]["run_contract"] == {}
    assert records[0]["checkpoint"] == {}


def test_version_two_thread_store_migrates_without_losing_records(tmp_path):
    path = tmp_path / "threads.json"
    path.write_text(
        '{"version": 2, "threads": [{"id": "legacy02", "status": "partial", '
        '"task": "old task", "turns": [], "blocks": [], "children": [], '
        '"workspace": {"available": true, "head": "' + ("a" * 40) + '"}}]}',
        encoding="utf-8",
    )

    records = agent_threads.load_threads(path)

    assert records[0]["id"] == "legacy02"
    assert records[0]["workspace"]["head"] == "a" * 40
    assert records[0]["run_contract"] == {}
    assert records[0]["checkpoint"] == {}


def test_run_contract_checkpoint_and_block_context_round_trip_bounded(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread(
        "Sensitive original task",
        run_contract={
            "contract_id": "run-contract-v1:" + ("a" * 64),
            "digest": "a" * 64,
            "run_nonce": "nonce1234",
            "mode": "enforced",
            "approval_mode": "never",
            "journal_file": "nonce1234.jsonl",
            "task": "must not be copied",
        },
        checkpoint={
            "next_block_ordinal": 1,
            "last_verified_sequence": 7,
            "uncertain_mutation_steps": ["b1-r0-t0"],
            "terminal": False,
        },
        path=path,
    )
    context = "x" * (agent_threads.MAX_BLOCK_CONTEXT_CHARS + 100)

    finished = agent_threads.finish_turn(
        record["id"],
        status="partial",
        blocks=[
            {
                "role": "plan",
                "status": "complete",
                "context_output": context,
                "tool_calls": 3,
            }
        ],
        run_contract=record["run_contract"],
        checkpoint={
            "next_block_ordinal": 1,
            "last_verified_sequence": 9,
            "uncertain_mutation_steps": [],
            "terminal": True,
            "terminal_status": "partial",
        },
        path=path,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["version"] == agent_threads.THREADS_SCHEMA_VERSION
    assert finished["run_contract"] == {
        "contract_id": "run-contract-v1:" + ("a" * 64),
        "digest": "a" * 64,
        "run_nonce": "nonce1234",
        "mode": "enforced",
        "approval_mode": "never",
        "journal_file": "nonce1234.jsonl",
    }
    assert "task" not in finished["run_contract"]
    assert finished["checkpoint"]["next_block_ordinal"] == 1
    assert finished["checkpoint"]["last_verified_sequence"] == 9
    assert finished["checkpoint"]["terminal"] is True
    assert len(finished["blocks"][0]["context_output"]) == (
        agent_threads.MAX_BLOCK_CONTEXT_CHARS
    )


def test_missing_clean_evidence_stays_unknown_instead_of_false(tmp_path):
    path = tmp_path / "threads.json"
    record = agent_threads.create_thread(
        "Read-only task",
        workspace={
            "available": True,
            "workspace_root": "/workspace",
            "branch": "feature/read",
            "head": "a" * 40,
        },
        path=path,
    )

    loaded = agent_threads.resolve_thread(record["id"], path=path)

    assert "clean" not in loaded["workspace"]
