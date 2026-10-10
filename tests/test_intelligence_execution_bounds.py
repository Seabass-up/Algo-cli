from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace

import pytest

from algo_cli.intelligence import rate_limiter as rate, team_execution as team
from algo_cli.intelligence.flow_dag import FlowDefinition, FlowEval, FlowExecutor, FlowNode


@pytest.mark.parametrize("kind", ["tool", "python", "invalid-kind"])
def test_failed_flow_node_cannot_report_success_even_when_eval_passes(kind):
    def fail(*_):
        raise ValueError("failed node")

    flow = FlowDefinition("failure", nodes=[FlowNode("n", kind, source_code="raise ValueError('failed node')")],
                          evals=[FlowEval("check", "custom", check_fn=lambda _: True)])
    result = FlowExecutor(tool_handler=fail).run(flow)
    assert result.eval_results == {"check": True}
    assert result.traces[0].error is not None
    assert result.success is False


def test_successful_flow_without_evals_remains_successful():
    result = FlowExecutor(tool_handler=lambda *_: "ok").run(
        FlowDefinition("success", nodes=[FlowNode("n", "tool")], outputs={"answer": "${n.text}"}))
    assert result.success and result.outputs == {"answer": "ok"}


@pytest.mark.parametrize("strategy", ["execute_all", "execute_race"])
def test_team_uses_one_deadline_and_reports_unfinished_members(monkeypatch, strategy):
    clock = [100.0]
    joins = []

    class Thread:
        def __init__(self, *, target, args, daemon=False):
            assert daemon is True

        def start(self):
            pass

        def join(self, timeout=None):
            joins.append(timeout)
            clock[0] += timeout or 0

    class Event:
        def wait(self, timeout=None):
            clock[0] += timeout or 0

        def set(self):
            pass

        def is_set(self):
            return False

    monkeypatch.setattr(team, "threading", SimpleNamespace(Thread=Thread, Event=Event, Lock=threading.Lock))
    monkeypatch.setattr(team, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    executor = team.TeamExecutor([team.TeamMember(str(i)) for i in range(3)])
    result = getattr(executor, strategy)("task", lambda *_: None, timeout=.25)
    assert clock[0] == 100.25
    assert result.duration_s == .25
    assert set(result.errors) == {"0", "1", "2"}
    assert all("timed out" in error.lower() for error in result.errors.values())
    assert sum(joins) <= .25


@pytest.mark.parametrize("strategy", ["execute_all", "execute_race"])
def test_team_result_is_not_mutated_by_late_callback(monkeypatch, strategy):
    release = threading.Event()
    completed = threading.Event()

    def run(*_):
        try:
            assert release.wait(2)
            raise ValueError("late failure")
        finally:
            completed.set()

    executor = team.TeamExecutor([team.TeamMember("slow")])
    try:
        result = getattr(executor, strategy)("task", run, timeout=.01)
        before = copy.deepcopy(result)
    finally:
        release.set()
        assert completed.wait(2)
    time.sleep(.01)
    assert result == before
    assert "slow" in result.errors


def test_race_returns_on_winner_without_waiting_for_loser():
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def run(member, _):
        if member == "slow":
            started.set()
            release.wait(2)
            completed.set()
            raise ValueError("late loser")
        assert started.wait(2)
        return "winner"

    executor = team.TeamExecutor([team.TeamMember("slow"), team.TeamMember("fast")])
    start = time.monotonic()
    try:
        result = executor.execute_race("task", run, timeout=.1)
        assert time.monotonic() - start < .5
        before = copy.deepcopy(result)
    finally:
        release.set()
        assert completed.wait(2)
    time.sleep(.01)
    assert result.winner == "fast"
    assert result.results == {"fast": "winner"}
    assert result == before


def test_race_finishes_when_every_member_fails():
    def run(*_):
        raise ValueError("failed")

    result = team.TeamExecutor([team.TeamMember("a"), team.TeamMember("b")]).execute_race("task", run)
    assert result.winner is None
    assert result.errors == {"a": "failed", "b": "failed"}


@pytest.mark.parametrize("strategy", ["execute_all", "execute_race"])
def test_empty_team_returns_without_waiting(strategy):
    result = getattr(team.TeamExecutor([]), strategy)("task", lambda *_: None)
    assert result.duration_s < .5
    assert not result.results and not result.errors


@pytest.mark.parametrize("method", ["try_acquire", "wait_time", "acquire"])
@pytest.mark.parametrize("tokens", [-1, 0, 2, float("nan"), float("inf")])
def test_bucket_rejects_invalid_requests_before_sleep(monkeypatch, method, tokens):
    bucket = rate.TokenBucket(1, 1)
    monkeypatch.setattr(rate, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda *_: pytest.fail("invalid request slept")))
    with pytest.raises(ValueError):
        getattr(bucket, method)(tokens)
    assert bucket.available_tokens == 1


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_bucket_rejects_invalid_capacity_or_refill(bad):
    with pytest.raises(ValueError):
        rate.TokenBucket(bad, 1)
    with pytest.raises(ValueError):
        rate.TokenBucket(1, bad)


def test_bucket_blocking_acquire_uses_atomic_debit_and_monotonic_deadline(monkeypatch):
    clock = [0.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(rate, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    bucket = rate.TokenBucket(1, 2)
    assert bucket.acquire()
    assert bucket.acquire(timeout=.5)
    assert sleeps == [.5]
    assert bucket.available_tokens == 0
    assert bucket.acquire(timeout=.1) is False
    assert clock[0] == .6


def test_bucket_concurrent_consumers_do_not_overspend():
    bucket = rate.TokenBucket(1, .000001)
    barrier = threading.Barrier(12)
    accepted = []

    def run():
        barrier.wait(timeout=2)
        accepted.append(bucket.try_acquire())

    workers = [threading.Thread(target=run) for _ in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert sum(accepted) == 1
    assert 0 <= bucket.available_tokens <= 1
