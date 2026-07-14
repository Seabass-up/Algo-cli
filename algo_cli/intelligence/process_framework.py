"""B46. Process Framework for Business Workflows (Semantic Kernel Pattern).

Models real-world business processes (bid → permit → install → invoice)
as state machines with typed steps, conditional transitions, retries,
and failure fallbacks.  Processes are serializable for pause/resume.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ProcessState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ProcessStep:
    name: str
    action: Callable[[dict], Any]
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    state: ProcessState = ProcessState.PENDING
    condition: Callable[[dict], bool] | None = None
    on_success: str | None = None
    on_failure: str | None = None
    retries: int = 0
    max_retries: int = 3

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "retries": self.retries,
            "outputs": self.outputs,
        }


class Process:
    """State-machine workflow with conditional transitions."""

    def __init__(self, name: str):
        self.name = name
        self.steps: dict[str, ProcessStep] = {}
        self.start_step: str | None = None
        self.context: dict[str, Any] = {}
        self.completed: bool = False

    def add_step(self, step: ProcessStep, is_start: bool = False) -> None:
        self.steps[step.name] = step
        if is_start:
            self.start_step = step.name

    def run(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute the process from start to end (or failure)."""
        if context:
            self.context.update(context)
        current = self.start_step
        while current:
            step = self.steps.get(current)
            if not step:
                break
            # Condition gate
            if step.condition and not step.condition(self.context):
                step.state = ProcessState.SKIPPED
                current = step.on_success
                continue
            step.state = ProcessState.RUNNING
            try:
                result = step.action(self.context)
                step.outputs = result if isinstance(result, dict) else {"result": result}
                self.context.update(step.outputs)
                step.state = ProcessState.COMPLETED
                current = step.on_success
            except Exception as e:
                step.retries += 1
                if step.retries < step.max_retries:
                    step.state = ProcessState.WAITING
                    continue  # retry same step
                step.state = ProcessState.FAILED
                self.context[f"{step.name}_error"] = str(e)
                current = step.on_failure
        self.completed = True
        return self.context

    def status(self) -> dict[str, Any]:
        """Return current process status."""
        return {
            "name": self.name,
            "completed": self.completed,
            "steps": {name: step.to_dict() for name, step in self.steps.items()},
            "context_keys": list(self.context.keys()),
        }

    def serialize(self) -> str:
        """Serialize process state for persistence."""
        return json.dumps({
            "name": self.name,
            "start_step": self.start_step,
            "completed": self.completed,
            "context": {k: v for k, v in self.context.items() if isinstance(v, (str, int, float, bool, list, dict))},
            "steps": {name: step.to_dict() for name, step in self.steps.items()},
        }, indent=2)

    def resume_from(self, step_name: str) -> dict[str, Any]:
        """Resume execution from a specific step."""
        current: str | None = step_name
        while current:
            step = self.steps.get(current)
            if not step:
                break
            if step.state == ProcessState.COMPLETED:
                current = step.on_success
                continue
            if step.condition and not step.condition(self.context):
                step.state = ProcessState.SKIPPED
                current = step.on_success
                continue
            step.state = ProcessState.RUNNING
            try:
                result = step.action(self.context)
                step.outputs = result if isinstance(result, dict) else {"result": result}
                self.context.update(step.outputs)
                step.state = ProcessState.COMPLETED
                current = step.on_success
            except Exception as e:
                step.retries += 1
                if step.retries < step.max_retries:
                    step.state = ProcessState.WAITING
                    continue
                step.state = ProcessState.FAILED
                self.context[f"{step.name}_error"] = str(e)
                current = step.on_failure
        self.completed = True
        return self.context


# ── factories for reusable business workflows ────────────────────────


def make_bid_process() -> Process:
    """Create a standard bid workflow."""
    p = Process("bid_process")

    def site_visit(ctx: dict) -> dict:
        return {"site_visited": True, "notes": ctx.get("notes", "")}

    def estimate(ctx: dict) -> dict:
        return {"estimate_total": ctx.get("estimate_total", 0)}

    def send_bid(ctx: dict) -> dict:
        return {"bid_sent": True, "bid_amount": ctx.get("estimate_total", 0)}

    def follow_up(ctx: dict) -> dict:
        return {"follow_up_sent": True}

    def won(ctx: dict) -> dict:
        return {"status": "won"}

    def lost(ctx: dict) -> dict:
        return {"status": "lost"}

    p.add_step(ProcessStep(name="site_visit", action=site_visit, on_success="estimate"), is_start=True)
    p.add_step(ProcessStep(name="estimate", action=estimate, on_success="send_bid"))
    p.add_step(ProcessStep(name="send_bid", action=send_bid, on_success="follow_up"))
    p.add_step(ProcessStep(name="follow_up", action=follow_up, on_success="check_result"))
    p.add_step(ProcessStep(
        name="check_result",
        action=lambda ctx: {"won": ctx.get("won", False)},
        condition=lambda ctx: ctx.get("won", False),
        on_success="won",
        on_failure="lost",
    ))
    p.add_step(ProcessStep(name="won", action=won))
    p.add_step(ProcessStep(name="lost", action=lost))
    return p


def make_permit_process() -> Process:
    """Create a standard permit workflow."""
    p = Process("permit_process")

    def apply(ctx: dict) -> dict:
        return {"permit_submitted": True}

    def review(ctx: dict) -> dict:
        return {"reviewed": True, "approved": ctx.get("approved", False)}

    def corrections(ctx: dict) -> dict:
        return {"corrections_made": True}

    def approval(ctx: dict) -> dict:
        return {"permit_approved": True}

    def schedule_inspection(ctx: dict) -> dict:
        return {"inspection_scheduled": True}

    p.add_step(ProcessStep(name="apply", action=apply, on_success="review"), is_start=True)
    p.add_step(ProcessStep(
        name="review",
        action=review,
        condition=lambda ctx: ctx.get("approved", False),
        on_success="approval",
        on_failure="corrections",
    ))
    p.add_step(ProcessStep(name="corrections", action=corrections, on_success="review"))
    p.add_step(ProcessStep(name="approval", action=approval, on_success="schedule_inspection"))
    p.add_step(ProcessStep(name="schedule_inspection", action=schedule_inspection))
    return p
