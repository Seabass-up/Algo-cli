"""Tests for H18 — Dual-Layer Validation."""
from __future__ import annotations

from algo_cli.intelligence.dual_layer_validate import (
    ValidationResult,
    run_validators,
    run_skeptic,
    dual_layer_validate,
)


def _validator_pass(name: str, claims: dict) -> ValidationResult:
    return ValidationResult(validator_name=name, passed=True, message="ok")


def _validator_fail(name: str, claims: dict) -> ValidationResult:
    return ValidationResult(validator_name=name, passed=False, message="fail")


def test_run_validators_all_pass() -> None:
    validators = [("v1", _validator_pass), ("v2", _validator_pass)]
    results = run_validators("H1", {}, validators)

    assert len(results) == 2
    assert all(r.passed for r in results)


def test_run_validators_some_fail() -> None:
    validators = [("v1", _validator_pass), ("v2", _validator_fail)]
    results = run_validators("H1", {}, validators)

    assert len(results) == 2
    assert results[0].passed is True
    assert results[1].passed is False


def test_run_skeptic_all_pass() -> None:
    validator_results = [
        ValidationResult(validator_name="v1", passed=True),
        ValidationResult(validator_name="v2", passed=True),
    ]
    skeptic = run_skeptic("H1", {"key": "value"}, validator_results)

    assert skeptic.passed is True
    assert skeptic.validator_name == "skeptic"


def test_run_skeptic_finds_failed_validators() -> None:
    validator_results = [
        ValidationResult(validator_name="v1", passed=True),
        ValidationResult(validator_name="v2", passed=False, message="bad"),
    ]
    skeptic = run_skeptic("H1", {}, validator_results)

    assert skeptic.passed is False


def test_run_skeptic_finds_unsupported_claims() -> None:
    validator_results = [ValidationResult(validator_name="v1", passed=True)]
    skeptic = run_skeptic("H1", {"empty_key": "", "none_key": None}, validator_results)

    assert skeptic.passed is False
    assert "unsupported" in skeptic.message.lower()


def test_dual_layer_validate_all_pass() -> None:
    validators = [("v1", _validator_pass), ("v2", _validator_pass)]
    result = dual_layer_validate("H1", {"key": "value"}, validators)

    assert result.final_verdict is True
    assert result.confidence == 1.0


def test_dual_layer_validate_validator_fails() -> None:
    validators = [("v1", _validator_pass), ("v2", _validator_fail)]
    result = dual_layer_validate("H1", {"key": "value"}, validators)

    assert result.final_verdict is False
    assert result.confidence < 1.0


def test_dual_layer_validate_skeptic_fails() -> None:
    validators = [("v1", _validator_pass)]
    result = dual_layer_validate("H1", {"empty": ""}, validators)

    assert result.final_verdict is False