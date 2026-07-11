"""Tests for H12 — Multi-Model Composite Scoring."""
from __future__ import annotations

from algo_cli.intelligence.multi_model_score import (
    ModelResponse,
    score_response,
    score_panel,
    pick_winner,
)


def test_score_response_basic() -> None:
    resp = ModelResponse(model_name="m1", response="This is a clear, direct answer.")
    scored = score_response(resp)

    assert scored.model_name == "m1"
    assert scored.score > 0.0
    assert "directness" in scored.sub_scores


def test_score_response_error() -> None:
    resp = ModelResponse(model_name="m1", response="", error="timeout")
    scored = score_response(resp)

    assert scored.score == 0.0


def test_score_response_hedging() -> None:
    resp = ModelResponse(
        model_name="m1",
        response="Maybe perhaps possibly I think it might work could be.",
    )
    scored = score_response(resp)

    assert scored.sub_scores["directness"] < 0.7


def test_score_panel_picks_winner() -> None:
    responses = [
        ModelResponse(model_name="good", response="The answer is 42."),
        ModelResponse(model_name="bad", response=""),
    ]
    scored = score_panel(responses)

    assert len(scored) == 2
    winner = pick_winner(scored)
    assert winner is not None
    assert winner.winner is True
    assert winner.model_name == "good"


def test_score_panel_empty() -> None:
    scored = score_panel([])
    assert len(scored) == 0
    assert pick_winner(scored) is None


def test_score_response_with_weights() -> None:
    resp = ModelResponse(model_name="m1", response="Direct answer.")
    scored = score_response(resp, weights={"directness": 2.0, "completeness": 1.0})

    assert scored.score > 0.0
    assert "directness" in scored.sub_scores


def test_score_response_custom_dimensions() -> None:
    resp = ModelResponse(model_name="m1", response="Test response.")
    scored = score_response(resp, dimensions=["directness"])

    assert "directness" in scored.sub_scores
    assert "completeness" not in scored.sub_scores
