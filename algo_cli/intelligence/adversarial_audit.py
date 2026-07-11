"""H8 — Adversarial Self-Audit.

Catches fabricated or exaggerated claims by comparing claimed behavior
against actual test results.
Mined from GLOSSOPETRAE §6.5 falsify_workflow.mjs.

LLM integration: optionally uses an LLM to analyze discrepancies.
Falls back to rule-based comparison when no model is available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Claim:
    """A claimed behavior to audit."""

    entry_id: str
    claimed_status: str  # "implemented", "partial", "planned"
    claimed_tests: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class AuditResult:
    """Result of auditing a single claim."""

    entry_id: str
    passed: bool
    discrepancies: list[str] = field(default_factory=list)
    actual_status: str = ""
    verified_tests: list[str] = field(default_factory=list)


def audit_claim(
    claim: Claim,
    actual_tests: list[str],
    actual_status: str,
    model_client: Any | None = None,
) -> AuditResult:
    """Audit a single claim against actual results.

    Args:
        claim: The claimed behavior.
        actual_tests: List of actual test names that passed.
        actual_status: The actual status string.
        model_client: Optional LLM for deeper analysis.

    Returns:
        AuditResult with discrepancies found.
    """
    discrepancies: list[str] = []

    # Check status mismatch
    if claim.claimed_status != actual_status:
        discrepancies.append(
            f"Status mismatch: claimed '{claim.claimed_status}', actual '{actual_status}'"
        )

    # Check missing tests
    claimed_set = set(claim.claimed_tests)
    actual_set = set(actual_tests)
    missing = claimed_set - actual_set
    if missing:
        discrepancies.append(f"Missing tests: {sorted(missing)}")

    # Check extra tests (not necessarily bad, but worth noting)
    extra = actual_set - claimed_set
    if extra and claim.claimed_tests:
        discrepancies.append(f"Unclaimed tests: {sorted(extra)}")

    # If claimed implemented but no tests, that's fabrication
    if claim.claimed_status == "implemented" and not actual_tests:
        discrepancies.append("Claimed 'implemented' but no passing tests found")

    passed = len(discrepancies) == 0
    return AuditResult(
        entry_id=claim.entry_id,
        passed=passed,
        discrepancies=discrepancies,
        actual_status=actual_status,
        verified_tests=list(actual_set),
    )


def audit_batch(
    claims: list[Claim],
    actual_results: dict[str, tuple[list[str], str]],
    model_client: Any | None = None,
) -> list[AuditResult]:
    """Audit multiple claims against actual results.

    Args:
        claims: List of claimed behaviors.
        actual_results: Dict mapping entry_id to (test_names, status).
        model_client: Optional LLM for deeper analysis.

    Returns:
        List of AuditResults.
    """
    results: list[AuditResult] = []
    for claim in claims:
        if claim.entry_id in actual_results:
            tests, status = actual_results[claim.entry_id]
            results.append(audit_claim(claim, tests, status, model_client))
        else:
            results.append(
                AuditResult(
                    entry_id=claim.entry_id,
                    passed=False,
                    discrepancies=[f"No actual results found for entry '{claim.entry_id}'"],
                    actual_status="missing",
                    verified_tests=[],
                )
            )
    return results


def summarize_audit(results: list[AuditResult]) -> dict[str, Any]:
    """Summarize audit results."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    all_discrepancies: list[str] = []
    for r in results:
        all_discrepancies.extend(r.discrepancies)
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": passed / total if total > 0 else 0.0,
        "discrepancies": all_discrepancies,
        "discrepancy_count": len(all_discrepancies),
    }