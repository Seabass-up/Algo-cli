"""B74. Saga Pattern for Multi-Agent Compensation.

Each step defines a compensating action.  On failure, execute
compensations in reverse order.
Source: Claudient pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Any


class SagaStepStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()
    COMPENSATED = auto()


@dataclass
class SagaStep:
    name: str
    execute: Callable[[], Any]
    compensate: Callable[[], Any] | None = None
    status: SagaStepStatus = SagaStepStatus.PENDING
    result: Any = None
    error: str | None = None


@dataclass
class SagaResult:
    success: bool
    completed_steps: list[str] = field(default_factory=list)
    compensated_steps: list[str] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None


class SagaOrchestrator:
    """Execute multi-step operations with automatic rollback."""

    def __init__(self) -> None:
        self._steps: list[SagaStep] = []

    def add_step(self, step: SagaStep) -> None:
        self._steps.append(step)

    def execute(self) -> SagaResult:
        """Execute all steps.  On failure, compensate in reverse."""
        result = SagaResult(success=True)

        for step in self._steps:
            step.status = SagaStepStatus.RUNNING
            try:
                step.result = step.execute()
                step.status = SagaStepStatus.DONE
                result.completed_steps.append(step.name)
            except Exception as e:
                step.error = str(e)
                step.status = SagaStepStatus.FAILED
                result.success = False
                result.failed_step = step.name
                result.error = str(e)

                # Compensate in reverse
                self._compensate(result)
                break

        return result

    def _compensate(self, result: SagaResult) -> None:
        """Run compensations in reverse order."""
        for step in reversed(self._steps):
            if step.status != SagaStepStatus.DONE:
                continue
            if step.compensate:
                try:
                    step.compensate()
                    step.status = SagaStepStatus.COMPENSATED
                    result.compensated_steps.append(step.name)
                except Exception:
                    pass  # compensation failed — log but continue

    @property
    def steps(self) -> list[SagaStep]:
        return list(self._steps)