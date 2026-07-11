"""Advisory Agent Block resource budgeting derived from routed tasks."""

from __future__ import annotations

from dataclasses import dataclass

from .task_router import TaskRoute


@dataclass(frozen=True)
class SpawnBudget:
    max_blocks: int
    max_iterations_per_block: int
    parallelism: int
    reasons: tuple[str, ...]


_INDEPENDENT_RESEARCH_TERMS = (
    "compare",
    "versus",
    " vs ",
    "alternatives",
    "options",
    "across",
)


def compute_budget(route: TaskRoute, prompt: str = "") -> SpawnBudget:
    """Recommend an execution budget without changing pipeline behavior."""
    lowered = f" {prompt.lower().strip()} "

    if route.risk == "high":
        return SpawnBudget(
            max_blocks=0,
            max_iterations_per_block=0,
            parallelism=0,
            reasons=("High-risk work should remain user-directed; no automatic expansion recommended.",),
        )

    if route.recommended_mode != "agent":
        return SpawnBudget(
            max_blocks=0,
            max_iterations_per_block=0,
            parallelism=0,
            reasons=("Direct chat is sufficient for this task.",),
        )

    if route.task_type == "review":
        return SpawnBudget(
            max_blocks=2,
            max_iterations_per_block=8,
            parallelism=0,
            reasons=("A reviewer and finalizer provide a bounded review pass.",),
        )

    if route.task_type == "research":
        independent = any(term in lowered for term in _INDEPENDENT_RESEARCH_TERMS)
        reasons = ["A planner, researcher, and finalizer cover a focused research task."]
        if independent:
            reasons.append("Independent comparisons detected; parallel research may be useful later.")
        return SpawnBudget(
            max_blocks=3,
            max_iterations_per_block=8,
            parallelism=1 if independent else 0,
            reasons=tuple(reasons),
        )

    if route.task_type == "coding":
        return SpawnBudget(
            max_blocks=4,
            max_iterations_per_block=8,
            parallelism=0,
            reasons=("Coding work benefits from planning, implementation, review, and finalization.",),
        )

    return SpawnBudget(
        max_blocks=0,
        max_iterations_per_block=0,
        parallelism=0,
        reasons=("No bounded Agent Blocks budget is recommended for this route.",),
    )


def parallelism_label(value: int) -> str:
    if value >= 2:
        return "recommended"
    if value == 1:
        return "optional"
    return "none"
