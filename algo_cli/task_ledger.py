"""Durable task ledger for /goal and long autonomous runs.

`/goal` and agent pipelines previously held all state in RAM: a crash, an
exit, or hitting the round cap lost the plan entirely. This module persists a
single active goal's progress to ``CONFIG_DIR/task_ledger.json`` so a run can
be inspected with ``/goal status`` and continued with ``/goal resume`` across
process restarts.

The ledger holds at most one active goal at a time (the common case for a
terminal session). Completing, blocking, or starting a new goal overwrites it.
Writes are atomic (tmp + os.replace) via config._atomic_write_text.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import CONFIG_DIR, _atomic_write_text, _load_json_file

LEDGER_PATH = CONFIG_DIR / "task_ledger.json"
LEDGER_SCHEMA_VERSION = 1

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_BLOCKED = "blocked"
STATUS_STOPPED = "stopped"  # user-interrupted or round cap reached


@dataclass
class GoalRecord:
    goal: str
    status: str = STATUS_RUNNING
    rounds_done: int = 0
    max_rounds: int = 10
    reason: str = ""                      # blocked/stopped explanation
    cwd: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)  # per-round notes

    def add_round(self, summary: str) -> None:
        self.rounds_done += 1
        self.updated_at = time.time()
        self.history.append(
            {"round": self.rounds_done, "at": self.updated_at, "summary": summary[:500]}
        )

    @property
    def is_open(self) -> bool:
        return self.status in {STATUS_RUNNING, STATUS_STOPPED}


def save_goal(record: GoalRecord) -> None:
    record.updated_at = time.time()
    payload = {"schema_version": LEDGER_SCHEMA_VERSION, "goal": asdict(record)}
    _atomic_write_text(LEDGER_PATH, json.dumps(payload, indent=2))


def load_goal() -> GoalRecord | None:
    data = _load_json_file(LEDGER_PATH, None)
    if not isinstance(data, dict):
        return None
    goal_data = data.get("goal")
    if not isinstance(goal_data, dict) or not goal_data.get("goal"):
        return None
    known = GoalRecord.__dataclass_fields__.keys()
    filtered = {k: v for k, v in goal_data.items() if k in known}
    try:
        return GoalRecord(**filtered)
    except TypeError:
        return None


def clear_goal() -> bool:
    if not LEDGER_PATH.exists():
        return False
    try:
        LEDGER_PATH.unlink()
        return True
    except OSError:
        return False
