"""B75. Cavecrew: Model Specialization per Role.

Match model cost to task complexity.
Investigator (cheap) → Builder (standard) → Reviewer (cheap) → Orchestrator (expensive).
Saves ~60% tokens.  Source: Claudient pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class CrewRole(Enum):
    INVESTIGATOR = auto()   # cheap model — search, read, gather
    BUILDER = auto()        # standard model — write, edit, implement
    REVIEWER = auto()       # cheap model — check, verify, audit
    ORCHESTRATOR = auto()   # expensive model — plan, decide, coordinate


@dataclass
class CrewMember:
    role: CrewRole
    model: str
    system_prompt: str = ""
    max_iterations: int = 5


@dataclass
class CrewTask:
    description: str
    assigned_role: CrewRole
    dependencies: list[str] = field(default_factory=list)
    result: str = ""


DEFAULT_CREW: dict[CrewRole, CrewMember] = {
    CrewRole.INVESTIGATOR: CrewMember(
        role=CrewRole.INVESTIGATOR,
        model="qwen3:4b",
        system_prompt="You are an investigator. Search, read, and gather information. Be concise.",
        max_iterations=3,
    ),
    CrewRole.BUILDER: CrewMember(
        role=CrewRole.BUILDER,
        model="glm-5.2",
        system_prompt="You are a builder. Write and edit code. Follow existing patterns.",
        max_iterations=5,
    ),
    CrewRole.REVIEWER: CrewMember(
        role=CrewRole.REVIEWER,
        model="qwen3:4b",
        system_prompt="You are a reviewer. Check for errors, missing tests, and improvements.",
        max_iterations=3,
    ),
    CrewRole.ORCHESTRATOR: CrewMember(
        role=CrewRole.ORCHESTRATOR,
        model="gpt-5.5",
        system_prompt="You are the orchestrator. Plan, decide, and coordinate. Be strategic.",
        max_iterations=5,
    ),
}


class Cavecrew:
    """Model-specialized crew for cost-efficient multi-agent work."""

    def __init__(self, crew: dict[CrewRole, CrewMember] | None = None) -> None:
        self._crew = crew or dict(DEFAULT_CREW)
        self._tasks: list[CrewTask] = []
        self._results: dict[str, str] = {}

    def assign(self, task: CrewTask) -> None:
        self._tasks.append(task)

    def run(self, run_fn: Callable[[CrewMember, str, str], str]) -> dict[str, str]:
        """Execute tasks in dependency order, routing to appropriate models."""
        completed: set[str] = set()

        for _ in range(len(self._tasks) * 2):  # safety limit
            ready = [
                t for t in self._tasks
                if t.description not in completed
                and all(d in completed for d in t.dependencies)
            ]
            if not ready:
                break

            for task in ready:
                member = self._crew.get(task.assigned_role)
                if not member:
                    continue
                try:
                    result = run_fn(member, member.system_prompt, task.description)
                    task.result = result
                    self._results[task.description] = result
                    completed.add(task.description)
                except Exception as e:
                    task.result = f"ERROR: {e}"
                    self._results[task.description] = task.result
                    completed.add(task.description)

        return dict(self._results)

    def estimate_cost(self) -> dict[CrewRole, int]:
        """Estimate relative token cost per role."""
        costs = {
            CrewRole.INVESTIGATOR: 1,   # cheap
            CrewRole.BUILDER: 3,         # standard
            CrewRole.REVIEWER: 1,        # cheap
            CrewRole.ORCHESTRATOR: 5,    # expensive
        }
        return {role: costs.get(role, 1) * sum(1 for t in self._tasks if t.assigned_role == role)
                for role in CrewRole}

    @property
    def crew(self) -> dict[CrewRole, CrewMember]:
        return dict(self._crew)