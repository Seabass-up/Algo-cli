"""Adaptive deliberation policy for non-interactive runs."""

from __future__ import annotations

import re


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

_EXACT_RESPONSE_TASK = re.compile(
    r"\A\s*(?:reply|respond|return|output|say)\s+"
    r"(?:with\s+)?exactly\s*:?\s*"
    r"[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}"
    r"[.!]?\s*\Z",
    re.IGNORECASE,
)


def needs_deliberation(prompt: str) -> bool:
    """Enable model reasoning only when a one-shot task signals depth."""

    lowered = (prompt or "").casefold()
    return any(cue in lowered for cue in _DEEP_CUES)


def is_exact_response_task(prompt: str) -> bool:
    """Recognize a closed-form response that cannot benefit from tools or recall."""

    return _EXACT_RESPONSE_TASK.fullmatch(prompt or "") is not None


__all__ = ["is_exact_response_task", "needs_deliberation"]
