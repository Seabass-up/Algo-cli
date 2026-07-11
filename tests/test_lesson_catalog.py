"""Tests for H5 — Lesson-to-Catalog Proposal Pipeline."""
from __future__ import annotations

from algo_cli.intelligence.lesson_catalog import (
    CatalogProposal,
    propose_from_lesson,
    propose_batch,
    filter_high_confidence,
)


def test_propose_from_lesson_extracts_keywords() -> None:
    lesson = "When verifying algorithm patterns, always check the pipeline stages and validate the metric scores."
    proposal = propose_from_lesson(lesson)

    assert isinstance(proposal, CatalogProposal)
    assert len(proposal.keywords) > 0
    assert "verify" in proposal.keywords or "validate" in proposal.keywords
    assert proposal.confidence > 0.0


def test_propose_from_lesson_no_keywords() -> None:
    lesson = "The weather is nice today."
    proposal = propose_from_lesson(lesson)

    assert proposal.confidence == 0.0
    assert len(proposal.keywords) == 0


def test_propose_from_lesson_with_heading() -> None:
    lesson = "## Always Check Before Push\nUse a guard to prevent bad pushes."
    proposal = propose_from_lesson(lesson)

    assert "Always Check Before Push" in proposal.title


def test_propose_batch() -> None:
    lessons = [
        "Verify the algorithm pipeline stages.",
        "The weather is nice today.",
        "Add a guard to prevent metric overflow.",
    ]
    proposals = propose_batch(lessons)

    assert len(proposals) == 3


def test_filter_high_confidence() -> None:
    proposals = [
        CatalogProposal(title="A", use_for="x", pseudocode="", source_lesson="", confidence=0.8),
        CatalogProposal(title="B", use_for="x", pseudocode="", source_lesson="", confidence=0.1),
        CatalogProposal(title="C", use_for="x", pseudocode="", source_lesson="", confidence=0.5),
    ]
    filtered = filter_high_confidence(proposals, threshold=0.3)

    assert len(filtered) == 2
    assert all(p.confidence >= 0.3 for p in filtered)


def test_proposal_has_pseudocode() -> None:
    lesson = "Use a verification algorithm to validate the pipeline."
    proposal = propose_from_lesson(lesson)

    assert "def" in proposal.pseudocode


def test_proposal_source_lesson_truncated() -> None:
    lesson = "x" * 500
    proposal = propose_from_lesson(lesson)

    assert len(proposal.source_lesson) <= 200