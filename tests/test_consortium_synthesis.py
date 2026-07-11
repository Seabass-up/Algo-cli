"""Tests for H25 — Consortium Synthesis."""
from __future__ import annotations

from algo_cli.intelligence.consortium_synthesis import (
    ConsortiumResponse,
    synthesize,
)


def test_synthesize_empty() -> None:
    result = synthesize([])
    assert result.synthesized == ""
    assert result.method == "empty"


def test_synthesize_single_response() -> None:
    responses = [ConsortiumResponse(model_name="m1", response="The answer is 42.")]
    result = synthesize(responses)

    assert "42" in result.synthesized
    assert "m1" in result.source_models


def test_synthesize_consensus() -> None:
    responses = [
        ConsortiumResponse(model_name="m1", response="The sky is blue. Water is wet."),
        ConsortiumResponse(model_name="m2", response="The sky is blue. Grass is green."),
        ConsortiumResponse(model_name="m3", response="The sky is blue. Fire is hot."),
    ]
    result = synthesize(responses)

    assert result.method == "rule-based"
    assert "the sky is blue" in result.synthesized.lower()
    assert result.agreement_score > 0.0


def test_synthesize_dissenting_models() -> None:
    responses = [
        ConsortiumResponse(model_name="m1", response="The answer is 42. The sky is blue."),
        ConsortiumResponse(model_name="m2", response="The answer is 42. The sky is blue."),
        ConsortiumResponse(model_name="m3", response="Something completely different about cats and dogs."),
    ]
    result = synthesize(responses)

    assert len(result.source_models) == 3
    # m3 should be dissenting (low overlap with consensus)
    assert "m3" in result.dissenting_models


class _MockOrchestrator:
    def __init__(self, response: str):
        self._response = response

    def generate(self, prompt: str) -> str:
        return self._response


def test_synthesize_with_orchestrator() -> None:
    responses = [
        ConsortiumResponse(model_name="m1", response="Answer A"),
        ConsortiumResponse(model_name="m2", response="Answer B"),
    ]
    orchestrator = _MockOrchestrator("Synthesized ground truth: Answer A and B agree on X.")
    result = synthesize(responses, orchestrator_client=orchestrator)

    assert result.method == "llm-orchestrated"
    assert "Synthesized" in result.synthesized


def test_synthesize_orchestrator_failure_falls_back() -> None:
    responses = [
        ConsortiumResponse(model_name="m1", response="The sky is blue."),
        ConsortiumResponse(model_name="m2", response="The sky is blue."),
    ]

    class _FailingOrchestrator:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("orchestrator failed")

    result = synthesize(responses, orchestrator_client=_FailingOrchestrator())

    # Should fall back to rule-based
    assert result.method == "rule-based"
