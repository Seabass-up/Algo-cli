from __future__ import annotations

import os
import shlex
import signal
import threading
import time

import pytest

from algo_cli.intelligence.parallel_fanout import ParallelFanOut
from algo_cli.intelligence.shell_session import ShellSession
from algo_cli.intelligence.subagent_spawner import AgentState, SubAgentConfig, SubAgentSpawner


# D1: fan_out must not raise on timeout; finished results survive and unfinished tasks are marked.

def test_d1_fan_out_keeps_finished_results_when_timeout_is_hit():
    release = threading.Event()

    def blocking(query: str) -> list[tuple[str, str]]:
        release.wait(5)  # safety bound only; the test releases it in finally
        return []

    try:
        fan = ParallelFanOut(max_workers=4, timeout=0.2)
        fan.register_source("fast", lambda q: [("title", q)])
        fan.register_source("block", blocking)
        results = fan.fan_out(["q1"], ["fast", "block"])
    finally:
        release.set()

    assert len(results) == 2
    by_source = {r.source: r for r in results}
    assert by_source["fast"].success is True
    assert by_source["fast"].results == [("title", "q1")]
    assert by_source["block"].success is False
    assert "timed out" in by_source["block"].error


def test_d1_pending_tasks_are_cancelled_and_marked_timed_out():
    release = threading.Event()
    started: list[str] = []

    def blocking(query: str) -> list[tuple[str, str]]:
        started.append("block")
        release.wait(5)
        return []

    def later(query: str) -> list[tuple[str, str]]:
        started.append("later")
        return [("title", query)]

    try:
        fan = ParallelFanOut(max_workers=1, timeout=0.2)
        fan.register_source("block", blocking)
        fan.register_source("later", later)
        results = fan.fan_out(["q1"], ["block", "later"])
    finally:
        release.set()

    assert len(results) == 2
    by_source = {r.source: r for r in results}
    assert by_source["later"].success is False
    assert "timed out" in by_source["later"].error
    assert started == ["block"]  # the queued task never ran


# D2: a cancelled agent must not be reported as DONE or FAILED when its run function returns.

def test_d2_completion_after_cancel_keeps_cancelled_state():
    spawner = SubAgentSpawner()
    spawner.register(SubAgentConfig(id="a", name="A"))
    cancel_results: list[bool] = []

    def run(config: SubAgentConfig, task: str) -> str:
        cancel_results.append(spawner.cancel("a"))
        return "late output"

    result = spawner.spawn("a", "task", run_fn=run)

    assert cancel_results == [True]
    assert result.state is AgentState.CANCELLED
    assert spawner.get_result("a").state is AgentState.CANCELLED
    assert result.output == ""


def test_d2_failure_after_cancel_keeps_cancelled_state():
    spawner = SubAgentSpawner()
    spawner.register(SubAgentConfig(id="a", name="A"))

    def run(config: SubAgentConfig, task: str) -> str:
        spawner.cancel("a")
        raise RuntimeError("boom")

    result = spawner.spawn("a", "task", run_fn=run)

    assert result.state is AgentState.CANCELLED
    assert spawner.get_result("a").state is AgentState.CANCELLED


# D3: on POSIX a timeout must kill the whole process group, not only the shell.

@pytest.mark.skipif(os.name == "nt", reason="process-group kill is POSIX-only")
def test_d3_timeout_kills_background_children(tmp_path):
    pidfile = tmp_path / "child.pid"
    command = f"echo partial; sleep 30 & echo $! > {shlex.quote(str(pidfile))}; sleep 30"
    session = ShellSession(cwd=str(tmp_path))

    rec = session.execute(command, timeout=1)

    assert rec.timeout_hit is True
    assert rec.exit_code == -1
    assert "partial" in rec.stdout
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 2
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)  # do not leak the child if the test fails
            pytest.fail(f"background child {pid} still alive after command timeout")
        time.sleep(0.05)
