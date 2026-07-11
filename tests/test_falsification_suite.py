"""Tests for H20 — Falsification Suite."""
from __future__ import annotations

from algo_cli.intelligence.falsification_suite import (
    FalsificationResult,
    ProbeResult,
    run_falsification,
    probe_missing_evidence,
    probe_contradictory_claims,
    probe_boundary_values,
    probe_unverifiable_sources,
)


def test_probe_missing_evidence_clean() -> None:
    result = probe_missing_evidence("H1", {"key": "value", "list": [1]})
    assert result.falsified is False


def test_probe_missing_evidence_none() -> None:
    result = probe_missing_evidence("H1", {"key": None})
    assert result.falsified is True


def test_probe_missing_evidence_empty() -> None:
    result = probe_missing_evidence("H1", {"key": []})
    assert result.falsified is True


def test_probe_contradictory_claims_clean() -> None:
    result = probe_contradictory_claims("H1", {"status": "implemented", "tests": ["t1"], "score": 0.9})
    assert result.falsified is False


def test_probe_contradictory_claims_no_tests() -> None:
    result = probe_contradictory_claims("H1", {"status": "implemented", "tests": [], "score": 0.9})
    assert result.falsified is True


def test_probe_contradictory_claims_low_score() -> None:
    result = probe_contradictory_claims("H1", {"status": "implemented", "tests": ["t1"], "score": 0.3})
    assert result.falsified is True


def test_probe_boundary_values_clean() -> None:
    result = probe_boundary_values("H1", {"pass_rate": 0.95, "count": 10})
    assert result.falsified is False


def test_probe_boundary_values_negative_count() -> None:
    result = probe_boundary_values("H1", {"count": -1})
    assert result.falsified is True


def test_probe_boundary_values_rate_over_1() -> None:
    result = probe_boundary_values("H1", {"pass_rate": 1.5})
    assert result.falsified is True


def test_probe_unverifiable_sources_clean() -> None:
    result = probe_unverifiable_sources("H1", {"source": "WHITEPAPER.md §5"})
    assert result.falsified is False


def test_probe_unverifiable_sources_empty() -> None:
    result = probe_unverifiable_sources("H1", {"source": ""})
    assert result.falsified is True


def test_run_falsification_all_survive() -> None:
    claims = {"status": "implemented", "tests": ["t1"], "score": 0.9, "source": "WHITEPAPER.md"}
    result = run_falsification("H1", claims)

    assert isinstance(result, FalsificationResult)
    assert result.any_falsified is False
    assert result.survived_count == 4
    assert result.total_count == 4


def test_run_falsification_some_falsified() -> None:
    claims = {"status": "implemented", "tests": [], "score": 0.3, "source": ""}
    result = run_falsification("H1", claims)

    assert result.any_falsified is True
    assert len(result.falsified_by) >= 2


def test_run_falsification_custom_probes() -> None:
    def custom_probe(entry_id: str, claims: dict) -> ProbeResult:
        return ProbeResult(probe_name="custom", falsified=True, evidence="always fails")

    result = run_falsification("H1", {"key": "value"}, probes=[("custom", custom_probe)])

    assert result.any_falsified is True
    assert "custom" in result.falsified_by