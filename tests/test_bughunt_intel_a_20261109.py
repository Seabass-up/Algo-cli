"""Regression tests for gatherer, hash_dedup, incremental_index and dag_orchestration defects."""
from __future__ import annotations

from pathlib import Path

import pytest

from algo_cli.intelligence import dag_orchestration, gatherer, hash_dedup, incremental_index
from algo_cli.intelligence.dag_orchestration import DAGNode, DependencyGraph, NodeStatus
from algo_cli.intelligence.gatherer import GathererEntry, GathererQueue
from algo_cli.intelligence.hash_dedup import ChangeKind, HashDeduplicator
from algo_cli.intelligence.incremental_index import ChangeType, IncrementalIndex


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


# --- D1: gatherer -----------------------------------------------------------


def test_gatherer_committed_entries_not_selected_again() -> None:
    queue = GathererQueue()
    queue.enqueue_many(["a.py", "b.py"])
    for entry in queue.next_batch():
        queue.mark_in_progress(entry)
        queue.mark_success(entry)
    assert queue.next_batch() == []


def test_gatherer_process_handles_more_than_one_batch() -> None:
    queue = GathererQueue()
    paths = [f"f{i}.py" for i in range(60)]
    queue.enqueue_many(paths)
    calls: list[str] = []
    stats = queue.process(lambda e: calls.append(e.path), batch_size=50)
    assert sorted(calls) == sorted(paths)
    assert len(calls) == 60
    assert stats["committed"] == 60
    assert stats["pending"] == 0


def test_gatherer_failed_entry_waits_for_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(gatherer, "time", clock)
    queue = GathererQueue()
    queue.enqueue(GathererEntry(path="a.py"))
    entry = queue.next_batch()[0]
    queue.mark_in_progress(entry)
    queue.mark_failure(entry)  # attempts=1 -> delay 2s
    assert queue.next_batch() == []
    clock.now += 1.0
    assert queue.next_batch() == []
    clock.now += 1.0
    assert [e.path for e in queue.next_batch()] == ["a.py"]


def test_gatherer_reenqueue_keeps_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(gatherer, "time", clock)
    queue = GathererQueue()
    queue.enqueue(GathererEntry(path="a.py"))
    entry = queue.next_batch()[0]
    queue.mark_in_progress(entry)
    queue.mark_failure(entry)
    queue.enqueue(GathererEntry(path="a.py"))  # e.g. a re-scan re-enqueues the path
    assert queue.next_batch() == []


# --- D2: hash_dedup diff ----------------------------------------------------


def _snap(files: dict[str, bytes]) -> HashDeduplicator:
    d = HashDeduplicator()
    d.snapshot((p, c, 0.0) for p, c in files.items())
    return d


def test_hash_copy_to_new_path_reports_new() -> None:
    old = _snap({"A": b"x"})
    new = _snap({"A": b"x", "B": b"x"})
    changes = new.diff(old)
    assert [(c.kind, c.path) for c in changes] == [(ChangeKind.NEW, "B")]


def test_hash_delete_with_surviving_duplicate_reports_deleted() -> None:
    old = _snap({"A": b"x", "B": b"x"})
    new = _snap({"B": b"x"})
    changes = new.diff(old)
    assert [(c.kind, c.path) for c in changes] == [(ChangeKind.DELETED, "A")]


def test_hash_rename_reports_moved() -> None:
    old = _snap({"A": b"x"})
    new = _snap({"B": b"x"})
    changes = new.diff(old)
    assert [(c.kind, c.path, c.old_path) for c in changes] == [(ChangeKind.MOVED, "B", "A")]


def test_hash_rename_with_extra_copy_reports_moved_and_new() -> None:
    old = _snap({"A": b"x"})
    new = _snap({"B": b"x", "C": b"x"})
    kinds = sorted((c.kind.value, c.path) for c in new.diff(old))
    assert kinds == [("moved", "B"), ("new", "C")]


# --- D3: incremental_index rename detection ---------------------------------


def test_incremental_scan_rename_reports_single_moved(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    idx = IncrementalIndex()
    idx.scan(tmp_path, glob="*.py")
    a.rename(b)
    changes = idx.scan(tmp_path, glob="*.py")
    assert [(c.change_type, c.path, c.old_path) for c in changes] == [(ChangeType.MOVED, str(b), str(a))]


def test_incremental_scan_paths_rename_reports_single_moved(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("x = 1\n", encoding="utf-8")
    idx = IncrementalIndex()
    idx.scan_paths([a])
    a.rename(b)
    changes = idx.scan_paths([b])
    assert [(c.change_type, c.path, c.old_path) for c in changes] == [(ChangeType.MOVED, str(b), str(a))]


def test_incremental_scan_real_delete_still_reports_deleted(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    a.write_text("x = 1\n", encoding="utf-8")
    idx = IncrementalIndex()
    idx.scan(tmp_path, glob="*.py")
    a.unlink()
    changes = idx.scan(tmp_path, glob="*.py")
    assert [(c.change_type, c.path) for c in changes] == [(ChangeType.DELETED, str(a))]


# --- D4: dag_orchestration --------------------------------------------------


def test_dag_caller_inputs_win_and_dict_not_mutated() -> None:
    seen: list[object] = []
    graph = DependencyGraph()
    graph.add_node(DAGNode(id="read", execute=lambda ctx: seen.append(ctx.get("x")) or ctx.get("x")))
    graph.run({"x": "old"})
    inputs = {"x": "new"}
    result = graph.run(inputs)
    assert seen[-1] == "new"
    assert result["read"] == "new"
    assert inputs == {"x": "new"}


def test_dag_second_run_executes_nodes_again() -> None:
    calls: list[int] = []
    graph = DependencyGraph()
    graph.add_node(DAGNode(id="a", execute=lambda ctx: calls.append(1) or "ok"))
    graph.add_node(DAGNode(id="b", execute=lambda ctx: calls.append(2) or "ok", depends_on=["a"]))
    graph.add_edge("a", "b")
    graph.run()
    graph.run()
    assert calls == [1, 2, 1, 2]


def test_dag_dependents_of_failed_node_are_skipped_with_error() -> None:
    def boom(ctx: dict) -> None:
        raise RuntimeError("boom")

    graph = DependencyGraph()
    graph.add_node(DAGNode(id="a", execute=boom))
    graph.add_node(DAGNode(id="b", execute=lambda ctx: "b"))
    graph.add_node(DAGNode(id="c", execute=lambda ctx: "c"))
    graph.add_node(DAGNode(id="d", execute=lambda ctx: "d"))
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.run()
    assert graph._nodes["a"].status == NodeStatus.FAILED
    assert graph._nodes["b"].status == NodeStatus.SKIPPED
    assert "'a'" in (graph._nodes["b"].error or "")
    assert graph._nodes["c"].status == NodeStatus.SKIPPED
    assert "'b'" in (graph._nodes["c"].error or "")
    assert graph._nodes["d"].status == NodeStatus.DONE


def test_dag_edge_cycle_is_reported() -> None:
    calls: list[str] = []
    graph = DependencyGraph()
    graph.add_node(DAGNode(id="a", execute=lambda ctx: calls.append("a")))
    graph.add_node(DAGNode(id="b", execute=lambda ctx: calls.append("b")))
    graph.add_edge("a", "b")
    graph.add_edge("b", "a")
    with pytest.raises(ValueError, match="Cycle detected"):
        graph.run()
    assert calls == []


def test_dag_module_is_importable() -> None:
    assert dag_orchestration.NodeStatus.SKIPPED is NodeStatus.SKIPPED
    assert hash_dedup.ChangeKind.NEW is ChangeKind.NEW
    assert incremental_index.ChangeType.MOVED is ChangeType.MOVED
