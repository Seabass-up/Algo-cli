"""B72. Team Execution: ALL vs RACE Strategies.

ALL: wait for all agents, compare results.
RACE: first-success wins with atomic lock.
Input sharding: split input across agents.
Source: gecko pattern.
"""
from __future__ import annotations

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
    """Execute tasks across a team of agents with ALL or RACE strategy."""

    def __init__(self, members: list[TeamMember]) -> None:
        self._members = {m.agent_id: m for m in members}
        self._lock = threading.Lock()

    def execute_all(
        self,
        task: str,
        run_fn: Callable[[str, str], Any],  # (agent_id, task) → output
        timeout: float = 60.0,
    ) -> TeamResult:
        """ALL strategy: run all agents, wait for all, return all results."""
        result = TeamResult(strategy=TeamStrategy.ALL)
        start = time.time()
        threads: list[threading.Thread] = []

        def worker(agent_id: str) -> None:
            try:
                output = run_fn(agent_id, task)
                with self._lock:
                    result.results[agent_id] = output
            except Exception as e:
                with self._lock:
                    result.errors[agent_id] = str(e)

        for member_id in self._members:
            t = threading.Thread(target=worker, args=(member_id,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=timeout)

        result.duration_s = time.time() - start
        return result

    def execute_race(
        self,
        task: str,
        run_fn: Callable[[str, str], Any],
        timeout: float = 60.0,
    ) -> TeamResult:
        """RACE strategy: first agent to succeed wins."""
        result = TeamResult(strategy=TeamStrategy.RACE)
        start = time.time()
        won = threading.Event()

        def worker(agent_id: str) -> None:
            if won.is_set():
                return
            try:
                output = run_fn(agent_id, task)
                with self._lock:
                    if not won.is_set():
                        result.results[agent_id] = output
                        result.winner = agent_id
                        won.set()
            except Exception as e:
                with self._lock:
                    result.errors[agent_id] = str(e)

        threads: list[threading.Thread] = []
        for member_id in self._members:
            t = threading.Thread(target=worker, args=(member_id,))
            threads.append(t)
            t.start()

        won.wait(timeout=timeout)
        for t in threads:
            t.join(timeout=1.0)

        result.duration_s = time.time() - start
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