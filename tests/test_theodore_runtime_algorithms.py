"""Focused tests for the Track L runtime algorithms."""

from __future__ import annotations

from collections import OrderedDict

from algo_cli.cache_admission import WindowTinyLFUCache
from algo_cli.evals.performance_regression import RegressionState, detect_cusum
from algo_cli.dorothy_perf_telemetry import runtime_performance_snapshot
from algo_cli.theodore_runtime_qos import (
    SpawnClass,
    WeightedFairQueue,
    order_tool_batch_by_qos,
)


def test_window_tinylfu_keeps_hot_key_during_one_off_scan() -> None:
    cache: WindowTinyLFUCache[str, str] = WindowTinyLFUCache(4, window_fraction=0.25)
    cache.put("hot", "value")
    for _ in range(12):
        assert cache.get("hot") == "value"

    for index in range(12):
        cache.put(f"scan-{index}", str(index))

    assert "hot" in cache
    assert cache.get("hot") == "value"
    assert len(cache) <= 4
    assert cache.snapshot()["rejections"] > 0


def test_window_tinylfu_resize_and_clear_remain_bounded() -> None:
    cache: WindowTinyLFUCache[str, int] = WindowTinyLFUCache(8)
    for index in range(20):
        cache.put(str(index), index)

    cache.resize(2)

    assert len(cache) <= 2
    assert cache.snapshot()["capacity"] == 2
    cache.clear()
    assert len(cache) == 0
    assert cache.snapshot()["hits"] == 0


def test_window_tinylfu_counts_one_frequency_event_per_miss_fill() -> None:
    cache: WindowTinyLFUCache[str, str] = WindowTinyLFUCache(4)

    assert cache.get("new") is None
    before_fill = cache._sketch.estimate("new")
    cache.put("new", "value")

    assert before_fill == 1
    assert cache._sketch.estimate("new") == 1


def test_window_tinylfu_hot_set_hit_rate_beats_plain_lru_under_scan_pollution() -> None:
    capacity = 32
    hot_keys = [f"hot-{index}" for index in range(8)]
    workload: list[str] = []
    for cycle in range(12):
        workload.extend(hot_keys)
        workload.extend(f"scan-{cycle}-{index}" for index in range(40))

    tinylfu: WindowTinyLFUCache[str, str] = WindowTinyLFUCache(capacity)
    lru: OrderedDict[str, str] = OrderedDict()
    tinylfu_hot_hits = 0
    lru_hot_hits = 0
    for key in workload:
        if tinylfu.get(key) is not None:
            tinylfu_hot_hits += int(key.startswith("hot-"))
        else:
            tinylfu.put(key, key)
        if key in lru:
            lru_hot_hits += int(key.startswith("hot-"))
            lru.move_to_end(key)
        else:
            if len(lru) >= capacity:
                lru.popitem(last=False)
            lru[key] = key

    assert tinylfu_hot_hits > lru_hot_hits
    assert tinylfu_hot_hits >= len(hot_keys) * 10


def test_cusum_ignores_single_spike_but_detects_sustained_regression() -> None:
    isolated = detect_cusum([100, 100, 100, 100, 100, 300, 100])
    sustained = detect_cusum([100, 101, 99, 100, 100, 125, 130, 132])

    assert isolated.state == RegressionState.STABLE
    assert sustained.state == RegressionState.REGRESSING
    assert sustained.change_index == 5


def test_cusum_does_not_carry_an_old_spike_into_a_later_small_run() -> None:
    samples = [100, 100, 100, 100, 100, 300, *([100] * 12), 102, 102]

    result = detect_cusum(samples)

    assert result.state == RegressionState.STABLE


def test_cusum_filters_invalid_samples_and_requires_warmup() -> None:
    result = detect_cusum([100, float("nan"), 101, 99, 100])

    assert result.state == RegressionState.INSUFFICIENT_DATA
    assert result.sample_count == 4


def test_runtime_performance_snapshot_uses_comparable_tool_series() -> None:
    history = [
        {"event": "tool", "tool": "read_file", "status": "worked", "duration_ms": value}
        for value in (100, 100, 101, 99, 100, 125, 130)
    ]
    history.append({"event": "qos", "tool": "read_file"})

    snapshot = runtime_performance_snapshot(history)

    assert snapshot["series"] == "tool:read_file"
    assert snapshot["state"] == "regressing"
    assert snapshot["sample_count"] == 7


def test_runtime_performance_snapshot_reports_worst_eligible_series() -> None:
    history = [
        {"event": "tool", "tool": "slow_tool", "status": "worked", "duration_ms": value}
        for value in (100, 100, 101, 99, 100, 125, 130)
    ]
    history.extend(
        {"event": "tool", "tool": "latest_tool", "status": "worked", "duration_ms": value}
        for value in (50, 50, 51, 49, 50, 50, 51)
    )

    snapshot = runtime_performance_snapshot(history)

    assert snapshot["series"] == "tool:slow_tool"
    assert snapshot["state"] == "regressing"


def test_weighted_fair_queue_ages_background_work() -> None:
    queue: WeightedFairQueue[str] = WeightedFairQueue(aging_rate=0.05)
    queue.enqueue("background", "background", SpawnClass.BACKGROUND, estimated_cost=8.0, now=0.0)
    for index in range(40):
        queue.enqueue(
            f"interactive-{index}",
            f"interactive-{index}",
            SpawnClass.INTERACTIVE,
            estimated_cost=1.0,
            now=100.0,
        )

    ordered: list[str] = []
    while len(queue):
        job = queue.pop(now=100.0)
        assert job is not None
        ordered.append(job.key)

    assert ordered.index("background") < 32


def test_tool_batch_qos_orders_interactive_then_adaptive_then_background() -> None:
    calls = [
        ("harness_refresh", {}),
        ("read_file", {"path": "README.md"}),
        ("available_actions", {}),
    ]

    assert order_tool_batch_by_qos(calls) == [2, 1, 0]
