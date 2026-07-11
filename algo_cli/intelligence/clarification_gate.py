"""B67. Human-in-the-Loop Clarification.

Ask clarifying questions before expensive research.
Source: DeepResearch-Agent pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class ClarificationType(Enum):
    SCOPE = auto()        # "Do you mean X or Y?"
    DEPTH = auto()        # "Brief overview or deep dive?"
    AUDIENCE = auto()     # "For beginners or experts?"
    FORMAT = auto()       # "Report, bullet points, or code?"
    TIMEFRAME = auto()   # "Recent only or historical?"


@dataclass
class Clarification:
    id: str
    type: ClarificationType
    question: str
    options: list[str] = field(default_factory=list)
    default: str = ""
    answer: str = ""


@dataclass
class ClarificationResult:
    clarifications: list[Clarification] = field(default_factory=list)
    refined_query: str = ""
    skipped: bool = False


class ClarificationGate:
    """Generate and collect clarifying questions before research."""

    AMBIGUOUS_WORDS = {"best", "good", "fast", "simple", "advanced", "modern", "latest"}

    def generate(self, query: str) -> list[Clarification]:
        """Generate clarifying questions based on query analysis."""
        clarifications: list[Clarification] = []
        words = set(query.lower().split())

        # Check for ambiguous terms
        ambiguous = words & self.AMBIGUOUS_WORDS
        if ambiguous:
            clarifications.append(Clarification(
                id="clar_1",
                type=ClarificationType.SCOPE,
                question=f"You mentioned '{', '.join(ambiguous)}'. Can you be more specific?",
                default="general overview",
            ))

        # Check for depth signals
        if not any(w in query.lower() for w in ["deep", "detailed", "overview", "brief", "summary"]):
            clarifications.append(Clarification(
                id="clar_2",
                type=ClarificationType.DEPTH,
                question="How deep should the research go?",
                options=["Brief overview", "Standard depth", "Deep dive"],
                default="Standard depth",
            ))

        # Check for format
        if not any(w in query.lower() for w in ["report", "bullet", "code", "table", "list"]):
            clarifications.append(Clarification(
                id="clar_3",
                type=ClarificationType.FORMAT,
                question="What format would you prefer?",
                options=["Markdown report", "Bullet points", "Code examples"],
                default="Markdown report",
            ))

        # Check for timeframe
        if not any(w in query.lower() for w in ["2024", "2025", "2026", "recent", "latest", "historical"]):
            clarifications.append(Clarification(
                id="clar_4",
                type=ClarificationType.TIMEFRAME,
                question="What timeframe should I focus on?",
                options=["Recent (2025-2026)", "Last 3 years", "Historical"],
                default="Recent (2025-2026)",
            ))

        return clarifications

    def refine_query(self, query: str, answers: dict[str, str]) -> str:
        """Refine the original query based on clarification answers."""
        refined = query
        for clar_id, answer in answers.items():
            if answer and answer != "general overview":
                refined += f" ({answer})"
        return refined

    def should_ask(self, query: str, auto_skip: bool = False) -> bool:
        """Determine if clarification is needed."""
        if auto_skip:
            return False
        return len(self.generate(query)) > 0