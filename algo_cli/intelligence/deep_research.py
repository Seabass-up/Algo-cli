"""B62. Sub-Question Decomposition + Iterative Gap-Filling.

Decompose a complex query into sub-questions, search for each,
identify gaps, and search again until coverage is sufficient.
Source: sibyl pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class GapType(Enum):
    MISSING = auto()       # no answer found
    SHALLOW = auto()       # answer too brief
    STALE = auto()         # answer references old info
    CONTRADICTORY = auto() # conflicting answers


@dataclass
class SubQuestion:
    id: str
    question: str
    search_queries: list[str] = field(default_factory=list)
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.0
    gaps: list[GapType] = field(default_factory=list)


@dataclass
class DecompositionResult:
    original_query: str
    sub_questions: list[SubQuestion] = field(default_factory=list)
    iterations: int = 0
    coverage_score: float = 0.0
    summary: str = ""


class QueryDecomposer:
    """Decompose a complex query into sub-questions."""

    QUESTION_PREFIXES = [
        "What is", "How does", "Why is", "When was", "Where is",
        "Who is", "What are the key features of",
        "What are the limitations of", "How is X different from",
    ]

    def decompose(self, query: str, max_sub_questions: int = 5) -> list[SubQuestion]:
        """Split a complex query into focused sub-questions."""
        # Simple heuristic: split on conjunctions and question marks
        parts = query.replace(" and ", "|").replace(" vs ", "|").split("|")
        sub_qs: list[SubQuestion] = []
        for i, part in enumerate(parts):
            part = part.strip().rstrip("?")
            if not part:
                continue
            sq = SubQuestion(id=f"sq_{i+1}", question=part)
            # Generate search queries
            sq.search_queries = [part, f"{part} overview", f"{part} explained"]
            sub_qs.append(sq)
            if len(sub_qs) >= max_sub_questions:
                break

        if not sub_qs:
            sub_qs.append(SubQuestion(id="sq_1", question=query, search_queries=[query]))

        return sub_qs


class GapFiller:
    """Identify gaps in answers and generate follow-up searches."""

    def identify_gaps(self, sq: SubQuestion) -> list[GapType]:
        gaps: list[GapType] = []
        if not sq.answer:
            gaps.append(GapType.MISSING)
        elif len(sq.answer) < 100:
            gaps.append(GapType.SHALLOW)
        if sq.confidence < 0.5:
            gaps.append(GapType.MISSING)
        return gaps

    def generate_followup(self, sq: SubQuestion) -> list[str]:
        """Generate follow-up search queries for gaps."""
        followups: list[str] = []
        for gap in sq.gaps:
            if gap == GapType.MISSING:
                followups.append(f"{sq.question} detailed explanation")
                followups.append(f"{sq.question} examples")
            elif gap == GapType.SHALLOW:
                followups.append(f"{sq.question} in depth analysis")
            elif gap == GapType.STALE:
                followups.append(f"{sq.question} 2025 2026 latest")
        return followups


class DeepResearchEngine:
    """Orchestrate sub-question decomposition with iterative gap-filling."""

    def __init__(self, search_fn: Callable[[str], list[tuple[str, str]]],
                 max_iterations: int = 3, coverage_threshold: float = 0.8) -> None:
        self._search = search_fn
        self._max_iterations = max_iterations
        self._coverage_threshold = coverage_threshold
        self._decomposer = QueryDecomposer()
        self._gap_filler = GapFiller()

    def run(self, query: str) -> DecompositionResult:
        result = DecompositionResult(original_query=query)
        result.sub_questions = self._decomposer.decompose(query)

        for iteration in range(self._max_iterations):
            result.iterations = iteration + 1

            for sq in result.sub_questions:
                if sq.confidence >= 0.7:
                    continue
                # Search for each query
                for search_q in sq.search_queries:
                    results = self._search(search_q)
                    for title, content in results:
                        sq.sources.append(title)
                        if not sq.answer:
                            sq.answer = content
                        else:
                            sq.answer += "\n\n" + content
                    if results:
                        sq.confidence = min(1.0, sq.confidence + 0.3)

                # Check for gaps
                sq.gaps = self._gap_filler.identify_gaps(sq)
                if sq.gaps:
                    followups = self._gap_filler.generate_followup(sq)
                    sq.search_queries.extend(followups)

            # Compute coverage
            answered = sum(1 for sq in result.sub_questions if sq.confidence >= 0.7)
            result.coverage_score = answered / len(result.sub_questions) if result.sub_questions else 0.0

            if result.coverage_score >= self._coverage_threshold:
                break

        result.summary = self._synthesize(result)
        return result

    def _synthesize(self, result: DecompositionResult) -> str:
        lines = [f"# Research: {result.original_query}", ""]
        for sq in result.sub_questions:
            lines.append(f"## {sq.question}")
            lines.append(sq.answer[:500] if sq.answer else "(no answer found)")
            lines.append("")
        lines.append(f"Coverage: {result.coverage_score:.0%} after {result.iterations} iterations")
        return "\n".join(lines)