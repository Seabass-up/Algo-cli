"""H11 — Checkpoint/Resume.

Resumable long catalog operations via state serialization.
Mined from GLOSSOPETRAE experiment harnesses.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Checkpoint:
    """A single checkpoint capturing operation state."""

    operation_id: str
    step: int
    total_steps: int
    state: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "step": self.step,
            "total_steps": self.total_steps,
            "state": dict(self.state),
            "created_at": self.created_at,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(
            operation_id=data["operation_id"],
            step=data["step"],
            total_steps=data["total_steps"],
            state=dict(data.get("state", {})),
            created_at=data.get("created_at", time.time()),
            completed=data.get("completed", False),
        )


class CheckpointManager:
    """Save and load checkpoints for resumable operations."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, Checkpoint] = {}

    def save(self, checkpoint: Checkpoint) -> None:
        self._checkpoints[checkpoint.operation_id] = checkpoint

    def load(self, operation_id: str) -> Checkpoint | None:
        return self._checkpoints.get(operation_id)

    def complete(self, operation_id: str) -> Checkpoint | None:
        cp = self._checkpoints.get(operation_id)
        if cp is not None:
            cp.completed = True
        return cp

    def is_complete(self, operation_id: str) -> bool:
        cp = self._checkpoints.get(operation_id)
        return cp is not None and cp.completed

    def resume_step(self, operation_id: str) -> int:
        """Return the step to resume from (0 if no checkpoint)."""
        cp = self._checkpoints.get(operation_id)
        if cp is None or cp.completed:
            return 0
        return cp.step

    def serialize(self, operation_id: str) -> str:
        cp = self._checkpoints.get(operation_id)
        if cp is None:
            raise KeyError(f"No checkpoint for {operation_id!r}")
        return json.dumps(cp.to_dict())

    def deserialize(self, data: str) -> Checkpoint:
        return Checkpoint.from_dict(json.loads(data))

    def all(self) -> list[Checkpoint]:
        return list(self._checkpoints.values())

    def count(self) -> int:
        return len(self._checkpoints)

    def clear(self) -> None:
        self._checkpoints.clear()