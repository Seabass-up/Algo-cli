"""Focused tests for corrected comparative ratings and leader claims."""

from __future__ import annotations

import copy
import json

from algo_cli.evals.competitive_harness_rating import (
    ATTACHED_2026_07_10_ROWS,
    AXES,
    LEADER_GATE_NAMES,
    build_competitive_harness_report,
    recompute_comparative_rating,
)


def test_runtime_report_runs_local_probes_but_does_not_mislabel_them_cross_harness(
    monkeypatch,
) -> None:
    from algo_cli import git_evidence, tools
    from algo_cli.evals import algorithm_effectiveness, harness_retrieval_benchmark

    monkeypatch.setattr(
        harness_retrieval_benchmark,
        "run_harness_retrieval_benchmark",
        lambda: {
            "benchmark_version": "retrieval-v1",
            "status": "pass",
            "performance": {"cold_sample_count": 5, "warm_sample_count": 9},
            "evidence": {"index_digest": "abc"},
        },
    )
    monkeypatch.setattr(
        algorithm_effectiveness,
        "run_algorithm_effectiveness_probe",
        lambda: {
            "probe": "algorithm-v1",
            "status": "pass",
            "required_checks": ["bm25", "rrf"],
            "summary": {"passed": 2},
            "checks": {
                "bm25": {"status": "pass", "evidence": {"path": "production"}},
                "rrf": {"status": "pass", "evidence": {"path": "production"}},
            },
        },
    )
    monkeypatch.setattr(
        git_evidence,
        "capture_git_snapshot",
        lambda: git_evidence.GitSnapshot(
            True,
            None,
            "abcdef123456",
            "## main\n M file.py",
            "+change",
            (),
            "dirty",
            git_evidence._digest(""),
        ),
    )

    report = json.loads(tools.harness_competitive_rating())
    gates = {gate["name"]: gate for gate in report["leader_rubric"]["gates"]}

    assert report["rating"]["ranking"][0]["project"] == "QodeX"
    assert report["leader_rubric"]["claim_allowed"] is False
    assert gates["production algorithm effectiveness"]["status"] == "pass"
    assert gates["reproducible comparative benchmark"]["status"] == "fail"
    assert gates["clean landed release"]["status"] == "fail"
    assert "not cross-harness evidence" in report["local_probe_artifacts"]["warning"]


def _row(project: str, score: float) -> dict[str, object]:
    return {
        "project": project,
        "axes": {axis: score for axis in AXES},
        "reported_overall": score,
    }


def _complete_evidence(projects: list[str], subject: str = "Algo CLI") -> dict[str, object]:
    competitors = [project for project in projects if project != subject]
    return {
        "benchmark": {
            "status": "pass",
            "protocol": "comparative-v1",
            "artifact_digest": "sha256:benchmark",
            "projects": projects,
            "runs_per_project": 5,
        },
        "algorithms": {
            "status": "pass",
            "probe": "algorithm-effectiveness-v1",
            "artifact_digest": "sha256:algorithms",
            "required_checks": 2,
            "passed_checks": 2,
            "algorithm_ids": ["bm25", "rrf"],
            "production_receipts": {
                "bm25": "artifact://bm25",
                "rrf": "artifact://rrf",
            },
        },
        "protocol_parity": {
            "status": "pass",
            "protocol": "same-workload-v1",
            "artifact_digest": "sha256:protocol",
            "projects": projects,
            "same_workload": True,
            "same_hardware": True,
            "same_model": True,
            "raw_results": {project: f"artifact://raw/{project}" for project in projects},
        },
        "release": {
            "status": "pass",
            "commit": "abcdef1234567890",
            "verification_artifact": "artifact://release-verification",
        },
        "competitors": {
            "status": "pass",
            "projects": competitors,
            "revisions": {project: f"{project}-revision" for project in competitors},
            "artifact_refs": {project: f"artifact://source/{project}" for project in competitors},
            "observed_at": "2026-07-10T12:00:00Z",
        },
    }


def test_attached_matrix_is_recomputed_and_openagentd_overall_is_rejected() -> None:
    report = build_competitive_harness_report()
    rating = report["rating"]
    rows = {row["project"]: row for row in rating["ranking"]}

    assert [(row["project"], row["computed_overall"]) for row in rating["ranking"]] == [
        ("QodeX", 8.4),
        ("Algo CLI", 8.2),
        ("OpenAgentd", 8.0),
        ("EvoAgentX", 6.4),
        ("AgentEvolver", 4.4),
        ("GenericAgent", 4.0),
    ]
    assert rows["Algo CLI"]["rank"] == 2
    assert rows["OpenAgentd"]["reported_overall"] == 8.8
    assert rows["OpenAgentd"]["computed_overall"] == 8.0
    assert rows["OpenAgentd"]["reported_overall_valid"] is False
    assert rows["OpenAgentd"]["accepted_reported_overall"] is None
    assert rating["status"] == "corrected"
    assert len(rating["arithmetic_errors"]) == 1
    assert "OpenAgentd" in rating["arithmetic_errors"][0]
    json.dumps(report, allow_nan=False)


def test_rating_does_not_mutate_or_reassign_source_axis_points() -> None:
    source = copy.deepcopy(ATTACHED_2026_07_10_ROWS)

    rating = recompute_comparative_rating(source)

    assert source == ATTACHED_2026_07_10_ROWS
    algo = next(row for row in rating["ranking"] if row["project"] == "Algo CLI")
    assert algo["axes"] == {
        "architecture": 8.0,
        "code_quality": 8.0,
        "test_coverage": 9.0,
        "security_safety": 7.0,
        "local_first_fit": 9.0,
    }


def test_malformed_five_axis_row_fails_closed_instead_of_receiving_defaults() -> None:
    malformed = _row("Algo CLI", 9)
    malformed["axes"].pop("security_safety")  # type: ignore[union-attr]

    rating = recompute_comparative_rating([malformed, _row("Competitor", 5)])

    assert rating["complete"] is False
    assert rating["status"] == "blocked"
    assert [row["project"] for row in rating["ranking"]] == ["Competitor"]
    assert rating["schema_errors"]


def test_current_subject_is_blocked_even_if_non_axis_evidence_is_complete() -> None:
    projects = [row["project"] for row in recompute_comparative_rating()["ranking"]]
    report = build_competitive_harness_report(
        evidence=_complete_evidence(projects),
        worktree_clean=True,
        competitor_evidence_complete=True,
    )
    rubric = report["leader_rubric"]
    statuses = {gate["name"]: gate["status"] for gate in rubric["gates"]}

    assert rubric["corrected_rank"] == 2
    assert rubric["status"] == "blocked"
    assert rubric["claim_allowed"] is False
    assert statuses["test coverage dominance"] == "pass"
    assert statuses["architecture dominance"] == "fail"
    assert statuses["security/safety dominance"] == "fail"
    assert statuses["local-first fit dominance"] == "fail"  # a tie is not a lead


def test_all_ten_evidence_gates_are_required_for_a_leader_claim() -> None:
    rows = [_row("Algo CLI", 10), _row("Competitor", 5)]
    projects = ["Algo CLI", "Competitor"]

    report = build_competitive_harness_report(
        rows,
        evidence=_complete_evidence(projects),
        worktree_clean=True,
        competitor_evidence_complete=True,
    )
    rubric = report["leader_rubric"]

    assert tuple(gate["name"] for gate in rubric["gates"]) == LEADER_GATE_NAMES
    assert rubric["gate_count"] == 10
    assert rubric["score"] == 10.0
    assert rubric["all_gates_critical"] is True
    assert rubric["status"] == "leader"
    assert rubric["claim_allowed"] is True
    assert rubric["blocking_reasons"] == []


def test_dirty_worktree_can_never_yield_leader() -> None:
    rows = [_row("Algo CLI", 10), _row("Competitor", 5)]
    evidence = _complete_evidence(["Algo CLI", "Competitor"])

    rubric = build_competitive_harness_report(
        rows,
        evidence=evidence,
        worktree_clean=False,
        competitor_evidence_complete=True,
    )["leader_rubric"]
    release = next(gate for gate in rubric["gates"] if gate["name"] == "clean landed release")

    assert release["status"] == "fail"
    assert rubric["score"] == 9.0
    assert rubric["status"] == "blocked"
    assert rubric["claim_allowed"] is False
    assert "dirty" in release["reason"]


def test_unknown_competitor_evidence_can_never_yield_leader_or_axis_points() -> None:
    rows = [_row("Algo CLI", 10), _row("Competitor", 5)]
    evidence = _complete_evidence(["Algo CLI", "Competitor"])

    rubric = build_competitive_harness_report(
        rows,
        evidence=evidence,
        worktree_clean=True,
        competitor_evidence_complete=None,
    )["leader_rubric"]

    assert rubric["status"] == "blocked"
    assert rubric["claim_allowed"] is False
    assert all(gate["status"] == "unavailable" for gate in rubric["gates"][:5])
    assert rubric["gates"][-1]["status"] == "error"


def test_benchmark_and_algorithm_claims_require_complete_machine_evidence() -> None:
    rows = [_row("Algo CLI", 10), _row("Competitor", 5)]
    evidence = _complete_evidence(["Algo CLI", "Competitor"])
    evidence["benchmark"]["runs_per_project"] = 1  # type: ignore[index]
    evidence["algorithms"]["passed_checks"] = 1  # type: ignore[index]

    rubric = build_competitive_harness_report(
        rows,
        evidence=evidence,
        worktree_clean=True,
        competitor_evidence_complete=True,
    )["leader_rubric"]
    statuses = {gate["name"]: gate["status"] for gate in rubric["gates"]}

    assert statuses["reproducible comparative benchmark"] == "fail"
    assert statuses["production algorithm effectiveness"] == "fail"
    assert rubric["score"] == 8.0
    assert rubric["status"] == "blocked"
