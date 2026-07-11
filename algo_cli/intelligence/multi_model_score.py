"""H12 — Multi-Model Composite Scoring.

Score algorithm candidates across a panel of models.
Mined from G0DM0D3 ULTRAPLINIAN: query N models, score each response,
pick the winner.

LLM integration: requires model clients to query. Falls back to
rule-based scoring when no models are available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelResponse:
    """A single model's response to a prompt."""

    model_name: str
    response: str
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class ScoredResponse:
    """A response with its composite score."""

    model_name: str
    response: str
    score: float
    sub_scores: dict[str, float] = field(default_factory=dict)
    winner: bool = False


# Scoring dimensions
SCORE_DIMENSIONS = [
    "directness",      # How direct and clear is the response?
    "completeness",    # Does it address all parts of the prompt?
    "accuracy",        # Is the information correct?
    "conciseness",     # Is it appropriately concise?
]


def _score_dimension(response: str, dimension: str) -> float:
    """Score a single dimension of a response (rule-based fallback)."""
    if not response:
        return 0.0

    text = response.lower()

    if dimension == "directness":
        # Penalize hedging language
        hedge_words = ["maybe", "perhaps", "might", "could", "possibly", "i think"]
        hedge_count = sum(text.count(w) for w in hedge_words)
        return max(0.0, 1.0 - hedge_count * 0.1)

    if dimension == "completeness":
        # Reward longer responses (up to a point)
        length = len(response)
        if length < 50:
            return length / 50.0
        if length > 500:
            return min(1.0, 500 / length + 0.5)
        return 1.0

    if dimension == "accuracy":
        # Rule-based: penalize obvious errors (empty, repeated chars)
        if not response.strip():
            return 0.0
        if response.strip() == response[0] * len(response.strip()):
            return 0.1
        return 0.8  # Default neutral-positive

    if dimension == "conciseness":
        # Reward shorter responses
        length = len(response)
        if length < 100:
            return 1.0
        if length > 2000:
            return 0.3
        return max(0.3, 1.0 - (length - 100) / 1900)

    return 0.5


def score_response(
    response: ModelResponse,
    dimensions: list[str] | None = None,
    weights: dict[str, float] | None = None,
    model_client: Any | None = None,
) -> ScoredResponse:
    """Score a single model response across dimensions.

    Args:
        response: The model response to score.
        dimensions: Which dimensions to score. Defaults to all.
        weights: Optional weights per dimension. Defaults to equal.
        model_client: Optional LLM for richer scoring.

    Returns:
        ScoredResponse with composite score.
    """
    dims = dimensions or SCORE_DIMENSIONS
    w = weights or {d: 1.0 / len(dims) for d in dims}

    if response.error:
        return ScoredResponse(
            model_name=response.model_name,
            response=response.response,
            score=0.0,
            sub_scores={d: 0.0 for d in dims},
        )

    sub_scores: dict[str, float] = {}
    for dim in dims:
        sub_scores[dim] = _score_dimension(response.response, dim)

    total_weight = sum(w.get(d, 0.0) for d in dims)
    if total_weight == 0:
        composite = 0.0
    else:
        composite = sum(sub_scores[d] * w.get(d, 0.0) for d in dims) / total_weight

    return ScoredResponse(
        model_name=response.model_name,
        response=response.response,
        score=composite,
        sub_scores=sub_scores,
    )


def score_panel(
    responses: list[ModelResponse],
    dimensions: list[str] | None = None,
    weights: dict[str, float] | None = None,
    model_client: Any | None = None,
) -> list[ScoredResponse]:
    """Score a panel of model responses and pick the winner.

    Args:
        responses: List of model responses.
        dimensions: Which dimensions to score.
        weights: Optional weights per dimension.
        model_client: Optional LLM for richer scoring.

    Returns:
        List of ScoredResponses, with the winner marked.
    """
    scored = [
        score_response(r, dimensions, weights, model_client)
        for r in responses
    ]
    if scored:
        best_idx = max(range(len(scored)), key=lambda i: scored[i].score)
        scored[best_idx] = ScoredResponse(
            model_name=scored[best_idx].model_name,
            response=scored[best_idx].response,
            score=scored[best_idx].score,
            sub_scores=scored[best_idx].sub_scores,
            winner=True,
        )
    return scored


def pick_winner(scored: list[ScoredResponse]) -> ScoredResponse | None:
    """Get the winning response from a scored panel."""
    for s in scored:
        if s.winner:
            return s
    if not scored:
        return None
    return max(scored, key=lambda s: s.score)