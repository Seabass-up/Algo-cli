"""Screenshot-as-verification helpers (I6).

These helpers do not call a vision model themselves. They validate the textual
result returned by ``vision_describe`` against expected/forbidden UI evidence,
so screenshots become structured test artifacts instead of informal notes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenshotVerification:
    """Structured result for screenshot description verification."""

    passed: bool
    expected_terms: tuple[str, ...]
    missing_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...]
    forbidden_hits: tuple[str, ...]
    coverage: float
    summary: str

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "expected_terms": list(self.expected_terms),
            "missing_terms": list(self.missing_terms),
            "forbidden_terms": list(self.forbidden_terms),
            "forbidden_hits": list(self.forbidden_hits),
            "coverage": round(self.coverage, 3),
            "summary": self.summary,
        }


def _contains_term(text: str, term: str) -> bool:
    return term.strip().lower() in text.lower()


def verify_screenshot_description(
    description: str,
    expected_terms: list[str] | tuple[str, ...] = (),
    forbidden_terms: list[str] | tuple[str, ...] = (),
) -> ScreenshotVerification:
    """Verify a screenshot description against expected and forbidden evidence.

    Args:
        description: Text returned by a vision/OCR model.
        expected_terms: Terms that should appear in the description.
        forbidden_terms: Terms that must not appear (for example, "error").

    Returns:
        A structured verification result suitable for tests or reports.
    """
    expected = tuple(term.strip() for term in expected_terms if term and term.strip())
    forbidden = tuple(term.strip() for term in forbidden_terms if term and term.strip())
    missing = tuple(term for term in expected if not _contains_term(description or "", term))
    hits = tuple(term for term in forbidden if _contains_term(description or "", term))
    coverage = 1.0 if not expected else (len(expected) - len(missing)) / len(expected)
    passed = not missing and not hits
    summary = (
        f"screenshot verification {'passed' if passed else 'failed'}: "
        f"coverage={coverage:.2f}, missing={len(missing)}, forbidden_hits={len(hits)}"
    )
    return ScreenshotVerification(
        passed=passed,
        expected_terms=expected,
        missing_terms=missing,
        forbidden_terms=forbidden,
        forbidden_hits=hits,
        coverage=coverage,
        summary=summary,
    )


__all__ = ["ScreenshotVerification", "verify_screenshot_description"]
