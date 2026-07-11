"""Tests for the strict ten-gate harness scorecard finalizer."""

from __future__ import annotations

import json

import pytest

from algo_cli.evals.scorecard_grading import finalize_scorecard


def _gate(
    name: str,
    *,
    status: str = "pass",
    critical: bool = False,
    evidence: str = "measured evidence",
    metrics: dict[str, object] | None = None,
) -> dict[str, object]:
    gate: dict[str, object] = {
        "name": name,
        "status": status,
        "critical": critical,
        "evidence": evidence,
        "recommendation": "",
    }
    if metrics is not None:
        gate["metrics"] = metrics
    return gate


def _all_pass() -> list[dict[str, object]]:
    return [_gate(f"gate-{index}") for index in range(10)]


def test_all_pass_is_ready_and_score_is_sum_of_gate_points() -> None:
    checks = _all_pass()
    checks[0]["metrics"] = {
        "sample_count": 7,
        "algorithms": ["bm25", "cosine", "rrf"],
    }

    payload = finalize_scorecard(checks)

    assert payload["schema_version"] == 2
    assert payload["max_score"] == 10.0
    assert payload["score"] == 10.0
    assert payload["score"] == sum(check["points"] for check in payload["checks"])
    assert payload["overall_status"] == "ready"
    assert payload["validation_errors"] == []
    assert payload["checks"][0]["metrics"] == checks[0]["metrics"]
    json.dumps(payload, allow_nan=False)


def test_status_points_are_explicit_and_noncritical_shortfalls_degrade() -> None:
    statuses = ["pass"] * 6 + ["warn", "unavailable", "fail", "error"]
    checks = [_gate(f"gate-{index}", status=status) for index, status in enumerate(statuses)]

    payload = finalize_scorecard(checks)

    assert [check["points"] for check in payload["checks"]] == [
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        0.5,
        0.0,
        0.0,
        0.0,
    ]
    assert payload["score"] == 6.5
    assert payload["overall_status"] == "degraded"


def test_wiring_only_without_benchmark_or_algorithm_evidence_cannot_score_ten() -> None:
    checks = [_gate(f"wiring-{index}") for index in range(8)]
    checks.extend(
        [
            _gate(
                "performance benchmark",
                status="unavailable",
                evidence="no compatible benchmark artifact",
            ),
            _gate(
                "algorithm use",
                status="unavailable",
                evidence="no production retrieval receipt",
            ),
        ]
    )

    payload = finalize_scorecard(checks)

    assert payload["score"] == 8.0
    assert payload["score"] < payload["max_score"]
    assert payload["overall_status"] == "degraded"


def test_pass_without_human_or_structured_evidence_fails_closed() -> None:
    checks = _all_pass()
    checks[4]["evidence"] = ""

    payload = finalize_scorecard(checks)

    assert payload["score"] == 9.0
    assert payload["checks"][4]["status"] == "error"
    assert payload["checks"][4]["critical"] is True
    assert payload["overall_status"] == "blocked"
    assert "without evidence" in " ".join(payload["validation_errors"])


def test_structured_metrics_are_sufficient_machine_verifiable_evidence() -> None:
    checks = _all_pass()
    checks[6]["evidence"] = ""
    checks[6]["metrics"] = {
        "sample_count": 5,
        "median_ratio": 1.04,
        "fusion_mode": "rrf",
    }

    payload = finalize_scorecard(checks)

    assert payload["overall_status"] == "ready"
    assert payload["checks"][6]["metrics"] == checks[6]["metrics"]


@pytest.mark.parametrize("status", ["fail", "error"])
def test_critical_fail_or_error_blocks(status: str) -> None:
    checks = _all_pass()
    checks[-1] = _gate("critical gate", status=status, critical=True)

    payload = finalize_scorecard(checks)

    assert payload["score"] == 9.0
    assert payload["overall_status"] == "blocked"


@pytest.mark.parametrize("status", ["warn", "unavailable", "fail", "error"])
def test_noncritical_nonpass_status_degrades(status: str) -> None:
    checks = _all_pass()
    checks[-1] = _gate("noncritical gate", status=status, critical=False)

    payload = finalize_scorecard(checks)

    assert payload["overall_status"] == "degraded"


@pytest.mark.parametrize(
    "checks",
    [
        [_gate(f"gate-{index}") for index in range(9)],
        [_gate(f"gate-{index}") for index in range(11)],
        [*_all_pass()[:-1], _gate("gate-0")],
        None,
    ],
)
def test_invalid_gate_schema_returns_blocked_payload_without_awarding_ten(
    checks: list[dict[str, object]] | None,
) -> None:
    payload = finalize_scorecard(checks)

    assert payload["overall_status"] == "blocked"
    assert payload["score"] < 10.0
    assert payload["validation_errors"]
    json.dumps(payload, allow_nan=False)


def test_optional_capabilities_are_normalized_but_unscored() -> None:
    capabilities = [
        {
            "name": "google workspace",
            "status": "blocked",
            "evidence": "credentials not configured",
            "metrics": {"commands_wired": 8},
        }
    ]

    payload = finalize_scorecard(_all_pass(), capabilities)

    assert payload["score"] == 10.0
    assert payload["overall_status"] == "ready"
    assert payload["capabilities"] == [
        {
            "name": "google workspace",
            "status": "fail",
            "evidence": "credentials not configured",
            "recommendation": "",
            "scored": False,
            "points": 0.0,
            "max_points": 0.0,
            "metrics": {"commands_wired": 8},
        }
    ]


@pytest.mark.parametrize("status", ["warn", "unavailable", "fail", "error"])
def test_ten_of_ten_invariant_requires_every_scored_gate_to_pass(status: str) -> None:
    checks = _all_pass()
    checks[3] = _gate("gate-3", status=status)

    payload = finalize_scorecard(checks)

    assert payload["score"] < 10.0
    assert payload["overall_status"] != "ready"
    assert not all(check["status"] == "pass" for check in payload["checks"])
