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
    mutation_intent: bool = False
    read_only: bool = False
    external_side_effect: bool = False
    signals: tuple[str, ...] = ()


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
)

_EXTERNAL_SIDE_EFFECT_TERMS = (
    "send email",
    "send the email",
    "post this",
    "publish",
    "deploy",
    "release",
)

_NEGATED_EXTERNAL_TERMS = (
    "do not send",
    "don't send",
    "never send",
    "do not post",
    "don't post",
    "never post",
    "do not publish",
    "don't publish",
    "never publish",
    "do not deploy",
    "don't deploy",
    "never deploy",
    "do not release",
    "don't release",
    "never release",
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

_STRONG_MUTATION_TERMS = (
    "fix",
    "implement ",
    "build",
    "add ",
    "refactor",
    "wire up",
    "remove unused",
    "update ",
    "change ",
    "patch ",
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
    """Classify a prompt with explicit precedence and auditable signals."""
    text = (prompt or "").strip()
    lowered = text.lower()
    read_only = _has_any(lowered, _READ_ONLY_TERMS)
    review = _has_any(lowered, _REVIEW_TERMS)
    research = _has_any(lowered, _RESEARCH_TERMS)
    coding = _has_any(lowered, _CODING_TERMS)
    mutation_intent = (
        _has_any(lowered, _STRONG_MUTATION_TERMS)
        and not read_only
    )
    external_side_effect = (
        _has_any(lowered, _EXTERNAL_SIDE_EFFECT_TERMS)
        and not _has_any(lowered, _NEGATED_EXTERNAL_TERMS)
    )
    high_risk = (
        _has_any(lowered, _HIGH_RISK_TERMS)
        or external_side_effect
    )
    signals = tuple(
        name
        for name, present in (
            ("read_only", read_only),
            ("review", review),
            ("research", research),
            ("coding", coding),
            ("mutation", mutation_intent),
            ("external_side_effect", external_side_effect),
            ("high_risk", high_risk),
        )
        if present
    )

    if not text:
        return TaskRoute(
            task_type="empty",
            complexity="low",
            recommended_mode="chat",
            suggested_pipeline="default",
            allowed_tool_groups=(),
            risk="low",
            reason="No task text was provided.",
            signals=(),
        )

    if read_only:
        task_type = "review" if review and not research else "research"
        return TaskRoute(
            task_type=task_type,
            complexity="medium",
            recommended_mode="agent",
            suggested_pipeline=(
                "review" if task_type == "review" else "research"
            ),
            allowed_tool_groups=(
                ("read", "shell")
                if task_type == "review"
                else ("read", "web")
            ),
            risk="high" if high_risk else "medium",
            reason=(
                "Explicit read-only language prevents mutation routing and "
                "uses a bounded evidence pipeline."
            ),
            mutation_intent=False,
            read_only=True,
            external_side_effect=external_side_effect,
            signals=signals,
        )

    if mutation_intent:
        return TaskRoute(
            task_type="coding",
            complexity="high",
            recommended_mode="agent",
            suggested_pipeline="code-change",
            allowed_tool_groups=("read", "write", "shell"),
            risk="high" if high_risk else "medium",
            reason=(
                "Explicit implementation language requires a bounded "
                "plan, implementation, review, and verification pipeline."
            ),
            mutation_intent=True,
            external_side_effect=external_side_effect,
            signals=signals,
        )

    if review:
        return TaskRoute(
            task_type="review",
            complexity="medium",
            recommended_mode="agent",
            suggested_pipeline="review",
            allowed_tool_groups=("read", "shell"),
            risk="high" if high_risk else "medium",
            reason="Review and audit tasks benefit from a bounded reviewer/finalizer pipeline.",
            external_side_effect=external_side_effect,
            signals=signals,
        )

    if research:
        return TaskRoute(
            task_type="research",
            complexity="medium",
            recommended_mode="agent",
            suggested_pipeline="research",
            allowed_tool_groups=("read", "web"),
            risk="high" if high_risk else "low",
            reason="Research tasks benefit from a planner, researcher, and finalizer.",
            external_side_effect=external_side_effect,
            signals=signals,
        )

    if coding:
        return TaskRoute(
            task_type="coding",
            complexity="high",
            recommended_mode="agent",
            suggested_pipeline="code-change",
            allowed_tool_groups=("read", "write", "shell"),
            risk="high" if high_risk else "medium",
            reason="Coding tasks usually need planning, implementation, review, and verification.",
            mutation_intent=True,
            external_side_effect=external_side_effect,
            signals=signals,
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
            external_side_effect=external_side_effect,
            signals=signals,
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
            signals=signals,
        )

    return TaskRoute(
        task_type="general",
        complexity="low",
        recommended_mode="chat",
        suggested_pipeline="default",
        allowed_tool_groups=(),
        risk="low",
        reason="No agent-specific routing rule matched.",
        signals=signals,
    )


def should_suggest(route: TaskRoute) -> bool:
    return route.recommended_mode == "agent" or route.risk == "high"
