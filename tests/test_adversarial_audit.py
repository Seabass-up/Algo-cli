"""Tests for H8 — Adversarial Self-Audit."""
from __future__ import annotations

from algo_cli.intelligence.adversarial_audit import (
    Claim,
    AuditResult,
    audit_claim,
    audit_batch,
    summarize_audit,
)


def test_audit_claim_passes() -> None:
    claim = Claim(
        entry_id="H1",
        claimed_status="implemented",
        claimed_tests=["test_h1_basic"],
    )
    result = audit_claim(claim, actual_tests=["test_h1_basic"], actual_status="implemented")

    assert result.passed is True
    assert len(result.discrepancies) == 0


def test_audit_claim_status_mismatch() -> None:
    claim = Claim(
        entry_id="H2",
        claimed_status="implemented",
        claimed_tests=["test_h2"],
    )
    result = audit_claim(claim, actual_tests=["test_h2"], actual_status="partial")

    assert result.passed is False
    assert any("Status mismatch" in d for d in result.discrepancies)


def test_audit_claim_missing_tests() -> None:
    claim = Claim(
        entry_id="H3",
        claimed_status="implemented",
        claimed_tests=["test_a", "test_b"],
    )
    result = audit_claim(claim, actual_tests=["test_a"], actual_status="implemented")

    assert result.passed is False
    assert any("Missing tests" in d for d in result.discrepancies)


def test_audit_claim_fabricated_implementation() -> None:
    claim = Claim(
        entry_id="H4",
        claimed_status="implemented",
        claimed_tests=[],
    )
    result = audit_claim(claim, actual_tests=[], actual_status="implemented")

    assert result.passed is False
    assert any("no passing tests" in d for d in result.discrepancies)


def test_audit_batch() -> None:
    claims = [
        Claim(entry_id="H1", claimed_status="implemented", claimed_tests=["t1"]),
        Claim(entry_id="H2", claimed_status="implemented", claimed_tests=["t2"]),
    ]
    actual = {
        "H1": (["t1"], "implemented"),
        "H2": ([], "planned"),
    }
    results = audit_batch(claims, actual)

    assert len(results) == 2
    assert results[0].passed is True
    assert results[1].passed is False


def test_audit_batch_missing_entry() -> None:
    claims = [Claim(entry_id="H99", claimed_status="implemented", claimed_tests=["t1"])]
    actual: dict[str, tuple[list[str], str]] = {}
    results = audit_batch(claims, actual)

    assert results[0].passed is False
    assert "No actual results" in results[0].discrepancies[0]


def test_summarize_audit() -> None:
    results = [
        AuditResult(entry_id="H1", passed=True, discrepancies=[]),
        AuditResult(entry_id="H2", passed=False, discrepancies=["err1", "err2"]),
    ]
    summary = summarize_audit(results)

    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["discrepancy_count"] == 2