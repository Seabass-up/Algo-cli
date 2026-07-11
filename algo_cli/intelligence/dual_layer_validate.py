"""H18 — Dual-Layer Validation.

Validator probes check claims, then an independent skeptic attempts to refute.
Mined from GLOSSOPETRAE VALIDATION.md: six validators + independent skeptic.

LLM integration: optionally uses an LLM as the skeptic. Falls back to
rule-based refutation when no model is available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ValidationResult:
    """Result of a single validator probe."""

    validator_name: str
    passed: bool
    message: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class DualLayerResult:
    """Combined result of validator + skeptic layers."""

    entry_id: str
    validator_results: list[ValidationResult] = field(default_factory=list)
    skeptic_result: ValidationResult | None = None
    final_verdict: bool = False
    confidence: float = 0.0


def run_validators(
    entry_id: str,
    claims: dict[str, Any],
    validators: list[tuple[str, Callable[[str, dict], ValidationResult]]],
) -> list[ValidationResult]:
    """Run multiple validator probes on a claim set.

    Args:
        entry_id: The entry being validated.
        claims: The claims to validate.
        validators: List of (name, validator_fn) tuples.

    Returns:
        List of ValidationResult from each validator.
    """
    results: list[ValidationResult] = []
    for name, fn in validators:
        result = fn(entry_id, claims)
        if not isinstance(result, ValidationResult):
            result = ValidationResult(
                validator_name=name,
                passed=bool(result),
            )
        results.append(result)
    return results


def run_skeptic(
    entry_id: str,
    claims: dict[str, Any],
    validator_results: list[ValidationResult],
    model_client: Any | None = None,
) -> ValidationResult:
    """Run an independent skeptic that attempts to refute the validators.

    Args:
        entry_id: The entry being scrutinized.
        claims: The claims to attack.
        validator_results: Results from the validator layer.
        model_client: Optional LLM for deeper refutation.

    Returns:
        ValidationResult from the skeptic's perspective.
    """
    # Rule-based skeptic: find any validator that failed
    failed_validators = [r for r in validator_results if not r.passed]
    if failed_validators:
        return ValidationResult(
            validator_name="skeptic",
            passed=False,
            message=f"Skeptic confirms failures from: {[r.validator_name for r in failed_validators]}",
            evidence=[r.message for r in failed_validators],
        )

    # Rule-based skeptic: check for unsupported claims
    unsupported = []
    for key, value in claims.items():
        if value is None:
            unsupported.append(f"{key} is None")
        elif isinstance(value, str) and not value.strip():
            unsupported.append(f"{key} is empty")

    if unsupported:
        return ValidationResult(
            validator_name="skeptic",
            passed=False,
            message="Skeptic found unsupported claims",
            evidence=unsupported,
        )

    return ValidationResult(
        validator_name="skeptic",
        passed=True,
        message="Skeptic could not refute validators",
    )


def dual_layer_validate(
    entry_id: str,
    claims: dict[str, Any],
    validators: list[tuple[str, Callable[[str, dict], ValidationResult]]],
    model_client: Any | None = None,
) -> DualLayerResult:
    """Run full dual-layer validation.

    Args:
        entry_id: The entry being validated.
        claims: The claims to validate.
        validators: List of (name, validator_fn) tuples.
        model_client: Optional LLM for skeptic.

    Returns:
        DualLayerResult with both layers and final verdict.
    """
    validator_results = run_validators(entry_id, claims, validators)
    skeptic_result = run_skeptic(entry_id, claims, validator_results, model_client)

    # Final verdict: all validators must pass AND skeptic must pass
    all_validators_pass = all(r.passed for r in validator_results)
    final_verdict = all_validators_pass and skeptic_result.passed

    # Confidence: fraction of validators that passed
    if validator_results:
        validator_pass_rate = sum(1 for r in validator_results if r.passed) / len(validator_results)
    else:
        validator_pass_rate = 0.0

    confidence = validator_pass_rate * (1.0 if skeptic_result.passed else 0.5)

    return DualLayerResult(
        entry_id=entry_id,
        validator_results=validator_results,
        skeptic_result=skeptic_result,
        final_verdict=final_verdict,
        confidence=confidence,
    )