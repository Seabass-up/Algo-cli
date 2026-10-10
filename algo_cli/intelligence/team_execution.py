"""B72. Team Execution: ALL vs RACE Strategies.

ALL: wait for all agents, compare results.
RACE: first-success wins with atomic lock.
Input sharding: split input across agents.
Source: gecko pattern.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable


class TeamStrategy(Enum):
    ALL = auto()   # wait for all, compare
    RACE = auto()  # first success wins


@dataclass
class TeamMember:
    agent_id: str
    model: str = ""
    role: str = "worker"


@dataclass
class TeamResult:
    strategy: TeamStrategy
    results: dict[str, Any] = field(default_factory=dict)  # agent_id → output
    winner: str | None = None
    errors: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0


class TeamExecutor:
    """Bound waiting and result publication for trusted local callbacks.

    Timeout does not cancel callbacks or undo their external side effects.
    Callers requiring cancellation must make run_fn cooperative or isolate it
    in an owned process with an explicit termination protocol.
    """

    def __init__(self, members: list[TeamMember]) -> None:
        self._members = {m.agent_id: m for m in members}

    def execute_all(
        self,
        task: str,
        run_fn: Callable[[str, str], Any],  # (agent_id, task) → output
        timeout: float = 60.0,
    ) -> TeamResult:
        """ALL strategy: one total deadline, with explicit unfinished results."""
        return self._execute(TeamStrategy.ALL, task, run_fn, timeout)

    def execute_race(
        self,
        task: str,
        run_fn: Callable[[str, str], Any],
        timeout: float = 60.0,
    ) -> TeamResult:
        """RACE strategy: first agent to succeed wins."""
        return self._execute(TeamStrategy.RACE, task, run_fn, timeout)

    def _execute(
        self,
        strategy: TeamStrategy,
        task: str,
        run_fn: Callable[[str, str], Any],
        timeout: float,
    ) -> TeamResult:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and non-negative")
        start = time.monotonic()
        deadline = start + timeout
        result = TeamResult(strategy=strategy)
        pending = set(self._members)
        lock = threading.Lock()
        done = threading.Event()
        closed = False

        def worker(agent_id: str) -> None:
            with lock:
                if closed or (strategy is TeamStrategy.RACE and result.winner is not None):
                    return
            error = None
            output = None
            try:
                output = run_fn(agent_id, task)
            except Exception as exc:
                error = str(exc)
            with lock:
                if closed or time.monotonic() > deadline:
                    return
                pending.discard(agent_id)
                if error is not None:
                    result.errors[agent_id] = error
                elif strategy is TeamStrategy.ALL or result.winner is None:
                    result.results[agent_id] = output
                    if strategy is TeamStrategy.RACE:
                        result.winner = agent_id
                if not pending or result.winner is not None:
                    done.set()

        threads: list[threading.Thread] = []
        for member_id in self._members:
            if time.monotonic() >= deadline:
                break
            t = threading.Thread(target=worker, args=(member_id,), daemon=True)
            threads.append(t)
            t.start()

        if strategy is TeamStrategy.ALL:
            for thread in threads:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        elif pending:
            done.wait(timeout=max(0.0, deadline - time.monotonic()))

        # Close publication under the same lock used by workers. A late callback
        # may continue running, but cannot change these result containers.
        with lock:
            closed = True
            for agent_id in pending:
                result.errors[agent_id] = (
                    "Race finished before this callback completed; callback not cancelled."
                    if result.winner is not None else
                    "Timed out before this callback completed; callback not cancelled."
                )
            result.duration_s = time.monotonic() - start
        return result

    def shard_input(self, items: list[Any], num_shards: int | None = None) -> list[list[Any]]:
        """Split input items across team members."""
        n = num_shards or len(self._members)
        if n <= 0:
            return [items]
        shards: list[list[Any]] = [[] for _ in range(n)]
        for i, item in enumerate(items):
            shards[i % n].append(item)
        return [s for s in shards if s]
