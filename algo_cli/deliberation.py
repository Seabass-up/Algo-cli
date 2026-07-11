"""Adaptive deliberation policy for non-interactive runs."""

from __future__ import annotations


_DEEP_CUES = (
    "deep analysis",
    "architecture decision",
    "architectural tradeoff",
    "security audit",
    "threat model",
    "formal proof",
    "prove that",
    "root cause analysis",
    "compare strategies",
    "evaluate alternatives",
    "multi-system",
    "ambiguous requirements",
)


def needs_deliberation(prompt: str) -> bool:
    """Enable model reasoning only when a one-shot task signals depth."""

    lowered = (prompt or "").casefold()
    return any(cue in lowered for cue in _DEEP_CUES)


__all__ = ["needs_deliberation"]
