"""H25 — Consortium Synthesis.

Synthesize ground truth from all model responses (vs H12's pick-winner).
Mined from G0DM0D3 api/lib/consortium.ts.

Unlike ULTRAPLINIAN (H12) which picks the BEST single response, CONSORTIUM
collects ALL responses and feeds them to an orchestrator model that
synthesizes ground truth. Key principles: ground truth over popularity,
specificity wins, no hedging, attribution-free.

LLM integration: requires an orchestrator model for synthesis. Falls back
to rule-based merge when no model is available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConsortiumResponse:
    """A single model's response in the consortium."""

    model_name: str
    response: str
    confidence: float = 0.0


@dataclass
class SynthesisResult:
    """Result of consortium synthesis."""

    synthesized: str
    source_models: list[str] = field(default_factory=list)
    agreement_score: float = 0.0
    method: str = "rule-based"  # or "llm-orchestrated"
    dissenting_models: list[str] = field(default_factory=list)


def _extract_claims(text: str) -> set[str]:
    """Extract claim-like sentences from text (rule-based)."""
    import re
    # Split on sentence boundaries
    sentences = re.split(r"[.!?]\s+", text)
    # Filter to substantive sentences (not too short, not questions)
    claims = set()
    for s in sentences:
        s = s.strip()
        if len(s) > 10 and not s.endswith("?"):
            claims.add(s.lower())
    return claims


def _rule_based_synthesize(responses: list[ConsortiumResponse]) -> SynthesisResult:
    """Rule-based synthesis: find common claims across models."""
    if not responses:
        return SynthesisResult(synthesized="", method="rule-based")

    # Extract claims from each response
    all_claims: dict[str, set[str]] = {}
    for resp in responses:
        all_claims[resp.model_name] = _extract_claims(resp.response)

    # Find claims that appear in multiple responses (agreement)
    claim_counts: dict[str, int] = {}
    for model_claims in all_claims.values():
        for claim in model_claims:
            claim_counts[claim] = claim_counts.get(claim, 0) + 1

    # Synthesize: claims that appear in majority of responses
    threshold = max(2, len(responses) // 2 + 1)
    consensus_claims = [c for c, count in claim_counts.items() if count >= threshold]

    # Sort by frequency (most agreed first)
    consensus_claims.sort(key=lambda c: -claim_counts[c])

    synthesized = ". ".join(consensus_claims[:10]) if consensus_claims else responses[0].response

    # Agreement score: fraction of claims that reached consensus
    total_claims = len(claim_counts)
    consensus_count = len(consensus_claims)
    agreement = consensus_count / total_claims if total_claims > 0 else 0.0

    # Find dissenting models (low overlap with consensus)
    consensus_set = set(consensus_claims)
    dissenting = []
    for model_name, model_claims in all_claims.items():
        overlap = len(model_claims & consensus_set)
        if overlap < len(consensus_set) * 0.3:
            dissenting.append(model_name)

    return SynthesisResult(
        synthesized=synthesized,
        source_models=[r.model_name for r in responses],
        agreement_score=agreement,
        method="rule-based",
        dissenting_models=dissenting,
    )


def synthesize(
    responses: list[ConsortiumResponse],
    orchestrator_client: Any | None = None,
) -> SynthesisResult:
    """Synthesize ground truth from multiple model responses.

    Args:
        responses: List of model responses.
        orchestrator_client: Optional orchestrator model that synthesizes
                            from all responses. Falls back to rule-based merge.

    Returns:
        SynthesisResult with synthesized ground truth.
    """
    if not responses:
        return SynthesisResult(synthesized="", method="empty")

    # Try LLM orchestrator if available
    if orchestrator_client is not None:
        try:
            prompt_parts = ["Synthesize ground truth from these model responses:"]
            for resp in responses:
                prompt_parts.append(f"\n[{resp.model_name}]: {resp.response}")
            prompt_parts.append("\n\nPrinciples: ground truth over popularity, specificity wins, no hedging, attribution-free.")
            prompt = "\n".join(prompt_parts)

            result = orchestrator_client.generate(prompt)
            if result:
                return SynthesisResult(
                    synthesized=result,
                    source_models=[r.model_name for r in responses],
                    agreement_score=1.0,
                    method="llm-orchestrated",
                )
        except Exception:
            pass

    # Fall back to rule-based synthesis
    return _rule_based_synthesize(responses)