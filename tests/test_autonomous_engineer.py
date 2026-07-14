from __future__ import annotations

from pathlib import Path

import pytest

from algo_cli.intelligence.autonomous_engineer import (
    Config,
    MemoryEngine,
    PerformanceWorker,
)


def test_memory_engine_insert_methods_return_concrete_ids() -> None:
    memory = MemoryEngine(":memory:")
    try:
        session_id = memory.start_session("test")
        task_id = memory.create_task(session_id, "benchmark")
        attempt_id = memory.log_attempt(
            task_id,
            "performance",
            {
                "code": "def total(xs): return sum(xs)",
                "success": True,
                "time": 0.001,
                "score_vector": {"median_s": 0.001},
                "reflection": "correct",
            },
        )
        benchmark_id = memory.log_benchmark(
            attempt_id,
            {"median": 0.001, "stdev": 0.0, "repeats": 2, "confidence": 1.0},
        )

        assert all(
            isinstance(value, int)
            for value in (session_id, task_id, attempt_id, benchmark_id)
        )
    finally:
        memory.close()


def test_memory_batch_rolls_back_on_error() -> None:
    memory = MemoryEngine(":memory:")
    try:
        with pytest.raises(RuntimeError, match="stop"):
            with memory.batch():
                memory.start_session("rolled back")
                raise RuntimeError("stop")

        count = memory.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == 0
    finally:
        memory.close()


def test_performance_worker_rejects_incomplete_harness_without_executing(tmp_path: Path) -> None:
    memory = MemoryEngine(":memory:")
    config = Config(workspace=str(tmp_path), benchmark_warmup=0, benchmark_repeats=1)
    worker = PerformanceWorker(memory, config)
    try:
        session_id = memory.start_session("test")
        task_id = memory.create_task(session_id, "invalid")

        result = worker.run({"task_id": task_id, "code": "value = 1"})

        assert result["success"] is False
        assert "no 'call' expression" in result["reflection"]
    finally:
        memory.close()


def test_performance_worker_runs_correctness_gated_benchmark(tmp_path: Path) -> None:
    memory = MemoryEngine(":memory:")
    config = Config(
        workspace=str(tmp_path),
        benchmark_warmup=0,
        benchmark_repeats=2,
        benchmark_target_sample_time=0.0,
        benchmark_max_loops=1,
    )
    worker = PerformanceWorker(memory, config)
    try:
        session_id = memory.start_session("test")
        task_id = memory.create_task(session_id, "valid")
        result = worker.run(
            {
                "task_id": task_id,
                "code": "def total(xs):\n    return sum(xs)",
                "call": "total([1, 2, 3])",
                "test": "assert total([1, 2, 3]) == 6",
            }
        )

        assert result["success"] is True
        assert result["score_vector"]["correct"] is True
        assert result["score_vector"]["repeats"] == 2
        assert memory.best_attempt(task_id) is not None
    finally:
        memory.close()
