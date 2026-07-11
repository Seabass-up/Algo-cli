"""Tests for H11 — Checkpoint/Resume."""
from __future__ import annotations

from algo_cli.intelligence.checkpoint_resume import Checkpoint, CheckpointManager


def test_save_and_load() -> None:
    mgr = CheckpointManager()
    cp = Checkpoint(operation_id="op1", step=5, total_steps=10, state={"items": [1, 2]})
    mgr.save(cp)
    loaded = mgr.load("op1")
    assert loaded is not None
    assert loaded.step == 5
    assert loaded.state == {"items": [1, 2]}


def test_load_missing_returns_none() -> None:
    mgr = CheckpointManager()
    assert mgr.load("nope") is None


def test_complete() -> None:
    mgr = CheckpointManager()
    mgr.save(Checkpoint("op1", 5, 10))
    mgr.complete("op1")
    assert mgr.is_complete("op1") is True


def test_is_complete_missing() -> None:
    mgr = CheckpointManager()
    assert mgr.is_complete("nope") is False


def test_resume_step() -> None:
    mgr = CheckpointManager()
    mgr.save(Checkpoint("op1", 7, 10))
    assert mgr.resume_step("op1") == 7


def test_resume_step_no_checkpoint() -> None:
    mgr = CheckpointManager()
    assert mgr.resume_step("nope") == 0


def test_resume_step_completed() -> None:
    mgr = CheckpointManager()
    mgr.save(Checkpoint("op1", 10, 10))
    mgr.complete("op1")
    assert mgr.resume_step("op1") == 0


def test_serialize_and_deserialize() -> None:
    mgr = CheckpointManager()
    cp = Checkpoint("op1", 3, 10, state={"k": "v"})
    mgr.save(cp)
    s = mgr.serialize("op1")
    restored = mgr.deserialize(s)
    assert restored.operation_id == "op1"
    assert restored.step == 3
    assert restored.state == {"k": "v"}


def test_serialize_missing_raises() -> None:
    mgr = CheckpointManager()
    try:
        mgr.serialize("nope")
        assert False
    except KeyError:
        pass


def test_count() -> None:
    mgr = CheckpointManager()
    assert mgr.count() == 0
    mgr.save(Checkpoint("op1", 1, 10))
    assert mgr.count() == 1


def test_clear() -> None:
    mgr = CheckpointManager()
    mgr.save(Checkpoint("op1", 1, 10))
    mgr.clear()
    assert mgr.count() == 0