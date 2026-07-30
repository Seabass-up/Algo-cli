"""Runtime QoS and named log helpers derived from curated action authority.

Inspired by macOS launchd ``POSIXSpawnType`` and ``StandardErrorPath``:
classify tools by expected runtime posture and give each tool a stable log path.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Generic, TypeVar

from .config import CONFIG_DIR
from .marcus_authority import DataClass, policy_for_action


class SpawnClass(str, Enum):
    ADAPTIVE = "adaptive"
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


@dataclass(frozen=True)
class RuntimeHint:
    tool_name: str
    spawn_class: SpawnClass
    log_path: str
    log_suppression: bool
    reason: str
    estimated_cost: float

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "spawn_class": self.spawn_class.value,
            "log_path": self.log_path,
            "log_suppression": self.log_suppression,
            "reason": self.reason,
            "estimated_cost": self.estimated_cost,
        }


T = TypeVar("T")


@dataclass(frozen=True)
class ScheduledJob(Generic[T]):
    key: str
    payload: T
    spawn_class: SpawnClass
    estimated_cost: float
    enqueued_at: float
    virtual_finish: float
    sequence: int


class WeightedFairQueue(Generic[T]):
    """Small deterministic weighted-fair queue with starvation-safe aging."""

    DEFAULT_WEIGHTS = {
        SpawnClass.INTERACTIVE: 4.0,
        SpawnClass.ADAPTIVE: 2.0,
        SpawnClass.BACKGROUND: 1.0,
    }

    def __init__(
        self,
        *,
        weights: dict[SpawnClass, float] | None = None,
        aging_rate: float = 0.05,
    ) -> None:
        configured = dict(self.DEFAULT_WEIGHTS)
        configured.update(weights or {})
        self.weights = {key: max(0.01, float(value)) for key, value in configured.items()}
        self.aging_rate = max(0.0, float(aging_rate))
        self._class_finish = {spawn: 0.0 for spawn in SpawnClass}
        self._jobs: list[ScheduledJob[T]] = []
        self._sequence = 0
        self._lock = threading.RLock()

    def enqueue(
        self,
        key: str,
        payload: T,
        spawn_class: SpawnClass,
        *,
        estimated_cost: float = 1.0,
        now: float | None = None,
    ) -> ScheduledJob[T]:
        with self._lock:
            if not self._jobs:
                self._class_finish = {spawn: 0.0 for spawn in SpawnClass}
            cost = max(0.01, float(estimated_cost))
            finish = self._class_finish[spawn_class] + cost / self.weights[spawn_class]
            self._class_finish[spawn_class] = finish
            job = ScheduledJob(
                key=str(key),
                payload=payload,
                spawn_class=spawn_class,
                estimated_cost=cost,
                enqueued_at=time.monotonic() if now is None else float(now),
                virtual_finish=finish,
                sequence=self._sequence,
            )
            self._sequence += 1
            self._jobs.append(job)
            return job

    def pop(self, *, now: float | None = None) -> ScheduledJob[T] | None:
        with self._lock:
            if not self._jobs:
                return None
            current = time.monotonic() if now is None else float(now)

            def priority(job: ScheduledJob[T]) -> tuple[float, int]:
                waited = max(0.0, current - job.enqueued_at)
                return job.virtual_finish - self.aging_rate * waited, job.sequence

            selected = min(self._jobs, key=priority)
            self._jobs.remove(selected)
            return selected

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)


def _safe_tool_name(tool_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", tool_name or "tool").strip("_") or "tool"


def named_tool_log_path(tool_name: str, *, log_root: Path | str | None = None, suppress: bool = False) -> Path:
    """Return a stable named log path for a tool, or /dev/null when explicitly suppressed."""
    if suppress:
        return Path("/dev/null")
    root = Path(log_root).expanduser() if log_root is not None else CONFIG_DIR / "logs" / "tools"
    safe = _safe_tool_name(tool_name)
    stamp = time.strftime("%Y%m%d")
    return root / safe / f"{stamp}.log"


def classify_tool_runtime(tool_name: str, args: dict[str, Any] | None = None) -> RuntimeHint:
    """Classify from ActionSpec authority plus bounded argument-aware overrides."""

    name = (tool_name or "").strip()
    args = args or {}
    policy = policy_for_action(name)
    suppress = policy.suppress_logs or any(
        data_class in {DataClass.CREDENTIAL, DataClass.AUTHENTICATION}
        for data_class in policy.data_classes
    )
    spawn = SpawnClass(policy.runtime_class.value)
    estimated_cost = policy.estimated_cost
    reason = "curated ActionSpec runtime posture"
    if name == "run_shell":
        command = str(args.get("command", "")).lower()
        if any(token in command for token in ("pytest", "ruff", "mypy", "npm test", "cargo test")):
            spawn = SpawnClass.ADAPTIVE
            reason = "curated shell posture adjusted for a bounded verification command"
            estimated_cost = 4.0
    log_path = named_tool_log_path(name, suppress=suppress)
    return RuntimeHint(name, spawn, str(log_path), suppress, reason, estimated_cost)


def order_tool_batch_by_qos(calls: list[tuple[str, dict[str, Any]]]) -> list[int]:
    """Return call indexes in weighted-fair dispatch order."""
    scheduler: WeightedFairQueue[int] = WeightedFairQueue()
    enqueued_at = time.monotonic()
    for index, (name, args) in enumerate(calls):
        hint = classify_tool_runtime(name, args)
        scheduler.enqueue(
            str(index),
            index,
            hint.spawn_class,
            estimated_cost=hint.estimated_cost,
            now=enqueued_at,
        )
    ordered: list[int] = []
    while len(scheduler):
        job = scheduler.pop(now=enqueued_at)
        if job is not None:
            ordered.append(job.payload)
    return ordered


__all__ = [
    "RuntimeHint",
    "ScheduledJob",
    "SpawnClass",
    "WeightedFairQueue",
    "classify_tool_runtime",
    "named_tool_log_path",
    "order_tool_batch_by_qos",
]
