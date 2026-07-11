"""Advisory task routing for chat, tools, and Agent Blocks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskRoute:
    task_type: str
    complexity: str
    recommended_mode: str
    suggested_pipeline: str
    allowed_tool_groups: tuple[str, ...]
    risk: str
    reason: str


_HIGH_RISK_TERMS = (
    "delete",
    "rm -rf",
    "reset --hard",
    "drop table",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "api key",
    "invoice",
    "payment",
    "send email",
    "post this",
    "publish",
)

_REVIEW_TERMS = (
    "review",
    "audit",
    "inspect",
    "critique",
    "check the code",
    "security review",
    "find issues",
    "look for bugs",
)

_RESEARCH_TERMS = (
    "research",
    "investigate",
    "compare",
    "look up",
    "find current",
    "latest",
    "summarize docs",
    "read the docs",
)

_READ_ONLY_TERMS = (
    "read-only",
    "read only",
    "read_file only",
    "do not submit",
    "do not write",
    "no writes",
    "reproduce sections",
    "analyze permit",
    "permit pilot",
    "deliver:",
)

_CODING_TERMS = (
    "fix",
    "implement",
    "build",
    "add",
    "refactor",
    "failing test",
    "bug",
    "stack trace",
    "feature",
    "wire up",
    "remove unused",
    "unused import",
)

_QUESTION_PREFIXES = (
    "how ",
    "what ",
    "why ",
    "where ",
    "when ",
    "explain ",
    "tell me ",
)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def route_task(prompt: str) -> TaskRoute:
    """Classify a prompt with transparent first-match rules."""
    text = (prompt or "").strip()
    lowered = text.lower()
    high_risk = _has_any(lowered, _HIGH_RISK_TERMS)

    if not text:
        return TaskRoute(
            task_type="empty",
            complexity="low",
            recommended_mode="chat",
            suggested_pipeline="default",
            allowed_tool_groups=(),
            risk="low",
            reason="No task text was provided.",
        )

    if _has_any(lowered, _REVIEW_TERMS):
        return TaskRoute(
            task_type="review",
            complexity="medium",
            recommended_mode="agent",
            suggested_pipeline="review",
            allowed_tool_groups=("read", "shell"),
            risk="high" if high_risk else "medium",
            reason="Review and audit tasks benefit from a bounded reviewer/finalizer pipeline.",
        )

    if _has_any(lowered, _RESEARCH_TERMS):
        return TaskRoute(
            task_type="research",
            complexity="medium",
            recommended_mode="agent",
            suggested_pipeline="research",
            allowed_tool_groups=("read", "web"),
            risk="high" if high_risk else "low",
            reason="Research tasks benefit from a planner, researcher, and finalizer.",
        )

    if _has_any(lowered, _READ_ONLY_TERMS):
        return TaskRoute(
            task_type="research",
            complexity="medium",
            recommended_mode="agent",
            suggested_pipeline="research",
            allowed_tool_groups=("read", "web"),
            risk="high" if high_risk else "low",
            reason="Read-only analysis tasks should use the research pipeline, not code-change.",
        )

    if _has_any(lowered, _CODING_TERMS):
        return TaskRoute(
            task_type="coding",
            complexity="high",
            recommended_mode="agent",
            suggested_pipeline="code-change",
            allowed_tool_groups=("read", "write", "shell"),
            risk="high" if high_risk else "medium",
            reason="Coding tasks usually need planning, implementation, review, and verification.",
        )

    if high_risk:
        return TaskRoute(
            task_type="sensitive",
            complexity="medium",
            recommended_mode="chat",
            suggested_pipeline="default",
            allowed_tool_groups=("read",),
            risk="high",
            reason="The task mentions destructive, credential, financial, or publishing actions.",
        )

    if lowered.startswith(_QUESTION_PREFIXES):
        return TaskRoute(
            task_type="question",
            complexity="low",
            recommended_mode="chat",
            suggested_pipeline="default",
            allowed_tool_groups=(),
            risk="low",
            reason="Direct questions are usually best handled in chat mode.",
        )

    return TaskRoute(
        task_type="general",
        complexity="low",
        recommended_mode="chat",
        suggested_pipeline="default",
        allowed_tool_groups=(),
        risk="low",
        reason="No agent-specific routing rule matched.",
    )


def should_suggest(route: TaskRoute) -> bool:
    return route.recommended_mode == "agent" or route.risk == "high"
