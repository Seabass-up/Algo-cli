"""H20 — Falsification Suite.

Multiple independent attack vectors on a claim.
Mined from GLOSSOPETRAE experiments/falsify/ (S1-S4 probes).

Each probe attacks the claim from a different angle. If any probe
succeeds in falsifying the claim, the claim is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class FalsificationProbe:
    """A single falsification probe."""

    name: str
    description: str
    attack_fn: Callable[[dict[str, Any]], bool]  # Returns True if falsified


@dataclass
class ProbeResult:
    """Result of a single probe."""

    probe_name: str
    falsified: bool
    evidence: str = ""


@dataclass
class FalsificationResult:
    """Combined result of all probes."""

    entry_id: str
    probe_results: list[ProbeResult] = field(default_factory=list)
    any_falsified: bool = False
    falsified_by: list[str] = field(default_factory=list)
    survived_count: int = 0
    total_count: int = 0


# Built-in probes
def probe_missing_evidence(entry_id: str, claims: dict[str, Any]) -> ProbeResult:
    """S1: Check if claims lack evidence."""
    falsified = False
    evidence_parts: list[str] = []

    for key, value in claims.items():
        if value is None:
            falsified = True
            evidence_parts.append(f"{key} is None")
        elif isinstance(value, (list, dict)) and len(value) == 0:
            falsified = True
            evidence_parts.append(f"{key} is empty")

    return ProbeResult(
        probe_name="missing-evidence",
        falsified=falsified,
        evidence="; ".join(evidence_parts) if evidence_parts else "All claims have values",
    )


def probe_contradictory_claims(entry_id: str, claims: dict[str, Any]) -> ProbeResult:
    """S2: Check for internal contradictions."""
    falsified = False
    evidence_parts: list[str] = []

    # Check status vs tests
    status = claims.get("status", "")
    tests = claims.get("tests", [])
    if status == "implemented" and not tests:
        falsified = True
        evidence_parts.append("Status 'implemented' but no tests")

    # Check score vs status
    score = claims.get("score", 0.0)
    if status == "implemented" and isinstance(score, (int, float)) and score < 0.5:
        falsified = True
        evidence_parts.append(f"Status 'implemented' but score={score}")

    return ProbeResult(
        probe_name="contradictory-claims",
        falsified=falsified,
        evidence="; ".join(evidence_parts) if evidence_parts else "No contradictions found",
    )


def probe_boundary_values(entry_id: str, claims: dict[str, Any]) -> ProbeResult:
    """S3: Check for impossible boundary values."""
    falsified = False
    evidence_parts: list[str] = []

    for key, value in claims.items():
        if isinstance(value, (int, float)):
            if value < 0.0 and "count" in key.lower():
                falsified = True
                evidence_parts.append(f"{key}={value} is negative")
            if value > 1.0 and "rate" in key.lower():
                falsified = True
                evidence_parts.append(f"{key}={value} exceeds 1.0")

    return ProbeResult(
        probe_name="boundary-values",
        falsified=falsified,
        evidence="; ".join(evidence_parts) if evidence_parts else "All values in bounds",
    )


def probe_unverifiable_sources(entry_id: str, claims: dict[str, Any]) -> ProbeResult:
    """S4: Check if sources are unverifiable."""
    falsified = False
    evidence_parts: list[str] = []

    source = claims.get("source", "")
    if source is None:
        falsified = True
        evidence_parts.append("Source is None")
    elif not isinstance(source, str):
        falsified = True
        evidence_parts.append("Source is not a string")
    elif not source.strip():
        falsified = True
        evidence_parts.append("Source is empty string")

    return ProbeResult(
        probe_name="unverifiable-sources",
        falsified=falsified,
        evidence="; ".join(evidence_parts) if evidence_parts else "Sources verifiable",
    )


# Default probe set
DEFAULT_PROBES: list[tuple[str, Callable[[str, dict], ProbeResult]]] = [
    ("missing-evidence", probe_missing_evidence),
    ("contradictory-claims", probe_contradictory_claims),
    ("boundary-values", probe_boundary_values),
    ("unverifiable-sources", probe_unverifiable_sources),
]


def run_falsification(
    entry_id: str,
    claims: dict[str, Any],
    probes: list[tuple[str, Callable[[str, dict], ProbeResult]]] | None = None,
    model_client: Any | None = None,
) -> FalsificationResult:
    """Run a falsification suite against a claim set.

    Args:
        entry_id: The entry being tested.
        claims: The claims to falsify.
        probes: List of (name, probe_fn) tuples. Defaults to built-in probes.
        model_client: Optional LLM for deeper probes.

    Returns:
        FalsificationResult with all probe results.
    """
    probe_list = probes or DEFAULT_PROBES
    results: list[ProbeResult] = []

    for name, fn in probe_list:
        result = fn(entry_id, claims)
        results.append(result)

    falsified_by = [r.probe_name for r in results if r.falsified]
    survived = sum(1 for r in results if not r.falsified)

    return FalsificationResult(
        entry_id=entry_id,
        probe_results=results,
        any_falsified=len(falsified_by) > 0,
        falsified_by=falsified_by,
        survived_count=survived,
        total_count=len(results),
    )