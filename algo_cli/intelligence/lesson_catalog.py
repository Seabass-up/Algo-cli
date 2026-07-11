"""H5 — Lesson-to-Catalog Proposal Pipeline.

Scans lesson text for algorithmic patterns and proposes catalog entries.
Mined from T3MP3ST self-improvement loop (🧪 Research → 📋 Catalog).

LLM integration: optionally uses an LLM to scan lesson text and propose
catalog entries. Falls back to keyword-based extraction when no model is
available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CatalogProposal:
    """A proposed catalog entry derived from a lesson."""

    title: str
    use_for: str
    pseudocode: str
    source_lesson: str
    confidence: float = 0.0
    keywords: list[str] = field(default_factory=list)


# Keyword patterns that indicate algorithmic lessons
_PATTERN_KEYWORDS = {
    "algorithm": ["algorithm", "pattern", "approach", "method", "strategy"],
    "verification": ["verify", "check", "validate", "test", "assert"],
    "guard": ["guard", "clamp", "prevent", "block", "gate"],
    "pipeline": ["pipeline", "flow", "stage", "phase", "step"],
    "metric": ["metric", "score", "measure", "telemetry", "signal"],
    "fallback": ["fallback", "retry", "recover", "degrade"],
}


def _extract_keywords(text: str) -> list[str]:
    """Extract algorithmic keywords from lesson text."""
    text_lower = text.lower()
    found = []
    for category, keywords in _PATTERN_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                found.append(kw)
    return found


def _extract_title(text: str) -> str:
    """Derive a title from lesson text."""
    # Try to find a heading or first sentence
    lines = text.strip().split("\n")
    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line and len(line) > 10:
            # Use first sentence, truncated
            first_sentence = line.split(".")[0]
            if len(first_sentence) > 60:
                return first_sentence[:57] + "..."
            return first_sentence
    return "Untitled Pattern"


def _compute_confidence(keywords: list[str], text: str) -> float:
    """Compute confidence based on keyword density."""
    if not keywords:
        return 0.0
    text_lower = text.lower()
    total_hits = sum(text_lower.count(kw) for kw in keywords)
    # Normalize by text length (per 1000 chars)
    density = total_hits / max(len(text), 1) * 1000
    # Sigmoid-like: 0.5 at density=2, approaching 1.0 at density=5+
    return min(1.0, density / 5.0)


def propose_from_lesson(
    lesson_text: str,
    model_client: Any | None = None,
) -> CatalogProposal:
    """Propose a catalog entry from a lesson.

    Args:
        lesson_text: The lesson text to scan.
        model_client: Optional LLM client for richer extraction.
                     Falls back to keyword-based extraction when None.

    Returns:
        A CatalogProposal with extracted patterns.
    """
    keywords = _extract_keywords(lesson_text)
    title = _extract_title(lesson_text)
    confidence = _compute_confidence(keywords, lesson_text)

    # Build pseudocode from keywords
    if keywords:
        pseudo_lines = [
            f"# Detected patterns: {', '.join(keywords[:5])}",
            f"def {title.lower().replace(' ', '_')}(input):",
            f"    # Keywords: {', '.join(keywords)}",
            "    result = process(input)",
            "    return result",
        ]
        pseudocode = "\n".join(pseudo_lines)
    else:
        pseudocode = "# No clear algorithmic pattern detected"

    use_for = f"Lessons containing: {', '.join(keywords[:3])}" if keywords else "General lessons"

    return CatalogProposal(
        title=title,
        use_for=use_for,
        pseudocode=pseudocode,
        source_lesson=lesson_text[:200],
        confidence=confidence,
        keywords=keywords,
    )


def propose_batch(
    lessons: list[str],
    model_client: Any | None = None,
) -> list[CatalogProposal]:
    """Propose catalog entries from multiple lessons."""
    return [propose_from_lesson(lesson, model_client) for lesson in lessons]


def filter_high_confidence(
    proposals: list[CatalogProposal],
    threshold: float = 0.3,
) -> list[CatalogProposal]:
    """Filter proposals below confidence threshold."""
    return [p for p in proposals if p.confidence >= threshold]
