"""Strict, evidence-gated comparative ratings for local agent harnesses.

The source matrix is preserved as evidence, but reported overall values are
never trusted.  Rankings always use the equal-weight arithmetic mean of the
five declared axes.  A separate ten-gate rubric prevents a corrected ranking
from being misrepresented as proof that a project is the comparative leader.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


SCHEMA_VERSION = 1
SOURCE_DATE = "2026-07-10"
SUBJECT_PROJECT = "Algo CLI"
AXES = (
    "architecture",
    "code_quality",
    "test_coverage",
    "security_safety",
    "local_first_fit",
)
AXIS_LABELS = {
    "architecture": "Architecture",
    "code_quality": "Code quality",
    "test_coverage": "Test coverage",
    "security_safety": "Security/safety",
    "local_first_fit": "Local-first fit",
}

# These are the scores in the attached 2026-07-10 review.  Do not adjust them
# in order to change the result; new scores require a new, reproducible review.
ATTACHED_2026_07_10_ROWS: tuple[dict[str, Any], ...] = (
    {
        "project": "OpenAgentd",
        "axes": {
            "architecture": 9,
            "code_quality": 9,
            "test_coverage": 7,
            "security_safety": 8,
            "local_first_fit": 7,
        },
        "reported_overall": 8.8,
    },
    {
        "project": "QodeX",
        "axes": {
            "architecture": 8,
            "code_quality": 8,
            "test_coverage": 8,
            "security_safety": 9,
            "local_first_fit": 9,
        },
        "reported_overall": 8.4,
    },
    {
        "project": SUBJECT_PROJECT,
        "axes": {
            "architecture": 8,
            "code_quality": 8,
            "test_coverage": 9,
            "security_safety": 7,
            "local_first_fit": 9,
        },
        "reported_overall": 8.2,
    },
    {
        "project": "EvoAgentX",
        "axes": {
            "architecture": 7,
            "code_quality": 7,
            "test_coverage": 5,
            "security_safety": 6,
            "local_first_fit": 7,
        },
        "reported_overall": 6.4,
    },
    {
        "project": "AgentEvolver",
        "axes": {
            "architecture": 6,
            "code_quality": 5,
            "test_coverage": 2,
            "security_safety": 4,
            "local_first_fit": 5,
        },
        "reported_overall": 4.4,
    },
    {
        "project": "GenericAgent",
        "axes": {
            "architecture": 5,
            "code_quality": 4,
            "test_coverage": 1,
            "security_safety": 4,
            "local_first_fit": 6,
        },
        "reported_overall": 4.0,
    },
)

LEADER_GATE_NAMES = (
    "architecture dominance",
    "code quality dominance",
    "test coverage dominance",
    "security/safety dominance",
    "local-first fit dominance",
    "reproducible comparative benchmark",
    "production algorithm effectiveness",
    "cross-harness protocol parity",
    "clean landed release",
    "complete competitor evidence",
)


def _safe_text(value: Any) -> str:
    try:
        return "" if value is None else str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _json_safe(
    value: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= 8:
        return _safe_text(value)
    identities = seen if seen is not None else set()
    identity = id(value)
    if identity in identities:
        return "<recursive>"
    if isinstance(value, Mapping):
        identities.add(identity)
        try:
            return {
                _safe_text(key): _json_safe(item, depth=depth + 1, seen=identities)
                for key, item in value.items()
            }
        finally:
            identities.discard(identity)
    if isinstance(value, (list, tuple)):
        identities.add(identity)
        try:
            return [_json_safe(item, depth=depth + 1, seen=identities) for item in value]
        finally:
            identities.discard(identity)
    if isinstance(value, (set, frozenset)):
        identities.add(identity)
        try:
            safe_items = [_json_safe(item, depth=depth + 1, seen=identities) for item in value]
            return sorted(safe_items, key=_safe_text)
        finally:
            identities.discard(identity)
    return _safe_text(value)


def _decimal_score(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not score.is_finite() or not Decimal("0") <= score <= Decimal("10"):
        return None
    return score


def _one_decimal(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def _materialize_rows(rows: Iterable[Mapping[str, Any]] | None) -> tuple[list[Any], str | None]:
    if rows is None or isinstance(rows, (str, bytes, Mapping)):
        return [], "rows must be an iterable of rating mappings"
    try:
        return list(rows), None
    except Exception as exc:
        return [], f"could not read rating rows: {_safe_text(exc)}"


def recompute_comparative_rating(
    rows: Iterable[Mapping[str, Any]] | None = ATTACHED_2026_07_10_ROWS,
) -> dict[str, Any]:
    """Validate five-axis rows and rank only by their recomputed mean.

    A bad reported overall is explicitly rejected, but an otherwise valid row
    remains in the corrected ranking.  Malformed rows make the comparison
    incomplete and are excluded rather than receiving invented defaults.
    """
    raw_rows, collection_error = _materialize_rows(rows)
    schema_errors = [collection_error] if collection_error else []
    arithmetic_errors: list[str] = []
    normalized: list[dict[str, Any]] = []
    seen_projects: set[str] = set()

    for position, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, Mapping):
            schema_errors.append(f"row {position} is not a mapping")
            continue

        project = _safe_text(raw.get("project")).strip()
        if not project:
            schema_errors.append(f"row {position} is missing project")
            continue
        project_key = project.casefold()
        if project_key in seen_projects:
            schema_errors.append(f"duplicate project row: {project!r}")
            continue
        seen_projects.add(project_key)

        raw_axes = raw.get("axes")
        if not isinstance(raw_axes, Mapping):
            schema_errors.append(f"project {project!r} axes must be a mapping")
            continue
        supplied_axes = {_safe_text(key) for key in raw_axes}
        expected_axes = set(AXES)
        if supplied_axes != expected_axes:
            missing = sorted(expected_axes - supplied_axes)
            extra = sorted(supplied_axes - expected_axes)
            schema_errors.append(
                f"project {project!r} must provide exactly five axes; "
                f"missing={missing}, extra={extra}"
            )
            continue

        decimal_axes: dict[str, Decimal] = {}
        invalid_axes: list[str] = []
        for axis in AXES:
            score = _decimal_score(raw_axes.get(axis))
            if score is None:
                invalid_axes.append(axis)
            else:
                decimal_axes[axis] = score
        if invalid_axes:
            schema_errors.append(
                f"project {project!r} has invalid 0..10 scores for axes {invalid_axes}"
            )
            continue

        reported = _decimal_score(raw.get("reported_overall"))
        if reported is None:
            schema_errors.append(
                f"project {project!r} reported_overall must be a finite 0..10 number"
            )
            continue

        computed = _one_decimal(sum(decimal_axes.values(), Decimal("0")) / Decimal(len(AXES)))
        reported_matches = reported == computed
        if not reported_matches:
            arithmetic_errors.append(
                f"project {project!r} reported overall {float(reported):.1f} "
                f"does not equal the five-axis mean {float(computed):.1f}"
            )

        normalized.append(
            {
                "project": project,
                "axes": {axis: float(decimal_axes[axis]) for axis in AXES},
                "weights": {axis: 0.2 for axis in AXES},
                "reported_overall": float(reported),
                "reported_overall_valid": reported_matches,
                "accepted_reported_overall": float(reported) if reported_matches else None,
                "computed_overall": float(computed),
                "ranking_overall": float(computed),
            }
        )

    normalized.sort(key=lambda row: (-float(row["computed_overall"]), str(row["project"]).casefold()))
    previous_score: float | None = None
    previous_rank = 0
    for position, row in enumerate(normalized, start=1):
        ranking_score = float(row["computed_overall"])
        rank = previous_rank if previous_score == ranking_score else position
        row["rank"] = rank
        previous_score = ranking_score
        previous_rank = rank

    strict_leader = bool(
        normalized
        and (len(normalized) == 1 or normalized[0]["computed_overall"] > normalized[1]["computed_overall"])
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "source_date": SOURCE_DATE,
        "axes": list(AXES),
        "axis_labels": dict(AXIS_LABELS),
        "weighting": "equal arithmetic mean (20% per axis)",
        "row_count_received": len(raw_rows),
        "row_count_ranked": len(normalized),
        "complete": not schema_errors and bool(normalized),
        "reported_arithmetic_valid": not arithmetic_errors,
        "status": "blocked" if schema_errors else ("corrected" if arithmetic_errors else "valid"),
        "ranking_basis": "computed_overall; reported_overall is never used for ranking",
        "strict_leader": normalized[0]["project"] if strict_leader else None,
        "ranking": normalized,
        "arithmetic_errors": arithmetic_errors,
        "schema_errors": schema_errors,
        "validation_errors": [*schema_errors, *arithmetic_errors],
    }


def _project_names(rating: Mapping[str, Any]) -> list[str]:
    ranking = rating.get("ranking")
    if not isinstance(ranking, list):
        return []
    return [
        str(row.get("project"))
        for row in ranking
        if isinstance(row, Mapping) and str(row.get("project") or "").strip()
    ]


def _mapping_evidence(evidence: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = evidence.get(key)
    return value if isinstance(value, Mapping) else None


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_set(value: Any) -> set[str] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    strings = {item.strip() for item in value if isinstance(item, str) and item.strip()}
    return strings if len(strings) == len(value) else None


def _evidence_gate(
    name: str,
    data: Mapping[str, Any] | None,
    *,
    required_fields: tuple[str, ...],
    valid: bool | None = None,
    reason: str = "",
) -> dict[str, Any]:
    if data is None:
        status = "unavailable"
        gate_reason = "required structured evidence was not supplied"
    else:
        missing = [field for field in required_fields if field not in data]
        if missing:
            status = "error"
            gate_reason = f"evidence is missing required fields: {missing}"
        elif data.get("status") != "pass":
            status = "fail"
            gate_reason = reason or "evidence status is not pass"
        elif valid is False:
            status = "fail"
            gate_reason = reason or "evidence failed its acceptance criteria"
        elif valid is None:
            status = "error"
            gate_reason = reason or "evidence could not be validated"
        else:
            status = "pass"
            gate_reason = ""
    return {
        "name": name,
        "status": status,
        "critical": True,
        "points": 1.0 if status == "pass" else 0.0,
        "max_points": 1.0,
        "reason": gate_reason,
        "evidence": _json_safe(data) if data is not None else {},
    }


def _axis_gates(
    rating: Mapping[str, Any],
    subject: str,
    *,
    competitor_evidence_ready: bool,
) -> list[dict[str, Any]]:
    ranking = rating.get("ranking")
    rows = [row for row in ranking if isinstance(row, Mapping)] if isinstance(ranking, list) else []
    subject_row = next((row for row in rows if row.get("project") == subject), None)
    gates: list[dict[str, Any]] = []
    for axis in AXES:
        name = f"{AXIS_LABELS[axis].lower()} dominance"
        if not competitor_evidence_ready:
            status = "unavailable"
            reason = "complete revision-pinned competitor evidence is required before axis comparison"
            metrics: dict[str, Any] = {}
        elif subject_row is None or not isinstance(subject_row.get("axes"), Mapping):
            status = "error"
            reason = f"subject {subject!r} is absent from the corrected rating"
            metrics = {}
        else:
            subject_score = float(subject_row["axes"][axis])
            competitors = [
                (str(row["project"]), float(row["axes"][axis]))
                for row in rows
                if row.get("project") != subject and isinstance(row.get("axes"), Mapping)
            ]
            if not competitors:
                status = "error"
                reason = "at least one competitor is required"
                metrics = {"subject_score": subject_score}
            else:
                best_score = max(score for _project, score in competitors)
                leaders = sorted(project for project, score in competitors if score == best_score)
                status = "pass" if subject_score > best_score else "fail"
                reason = (
                    ""
                    if status == "pass"
                    else "subject must strictly exceed the best competitor; ties do not establish leadership"
                )
                metrics = {
                    "subject": subject,
                    "subject_score": subject_score,
                    "best_competitor_score": best_score,
                    "best_competitors": leaders,
                    "strictly_greater": subject_score > best_score,
                }
        gates.append(
            {
                "name": name,
                "axis": axis,
                "status": status,
                "critical": True,
                "points": 1.0 if status == "pass" else 0.0,
                "max_points": 1.0,
                "reason": reason,
                "evidence": metrics,
            }
        )
    return gates


def evaluate_comparative_leader(
    rating: Mapping[str, Any],
    *,
    subject: str = SUBJECT_PROJECT,
    evidence: Mapping[str, Any] | None = None,
    worktree_clean: bool | None = None,
    competitor_evidence_complete: bool | None = None,
) -> dict[str, Any]:
    """Apply the ten critical gates required for a comparative-leader claim.

    Evidence is deliberately separate from axis scores: it can validate a
    claim, but it cannot add points to an axis.  Unknown flags fail closed.
    """
    supplied = evidence if isinstance(evidence, Mapping) else {}
    projects = _project_names(rating)
    project_set = set(projects)
    competitor_set = project_set - {subject}

    competitor_data = _mapping_evidence(supplied, "competitors")
    competitor_projects = _string_set(competitor_data.get("projects")) if competitor_data else None
    revisions = competitor_data.get("revisions") if competitor_data else None
    artifact_refs = competitor_data.get("artifact_refs") if competitor_data else None
    competitors_valid = bool(
        competitor_data
        and competitor_projects == competitor_set
        and isinstance(revisions, Mapping)
        and competitor_set <= {str(key) for key in revisions}
        and all(_nonempty_text(revisions.get(project)) for project in competitor_set)
        and isinstance(artifact_refs, Mapping)
        and competitor_set <= {str(key) for key in artifact_refs}
        and all(_nonempty_text(artifact_refs.get(project)) for project in competitor_set)
        and _nonempty_text(competitor_data.get("observed_at"))
    )
    if competitor_evidence_complete is not True:
        competitors_valid = False
    competitor_gate = _evidence_gate(
        "complete competitor evidence",
        competitor_data,
        required_fields=("status", "projects", "revisions", "artifact_refs", "observed_at"),
        valid=(competitors_valid if competitor_evidence_complete is not None else None),
        reason=(
            "competitor evidence is explicitly incomplete"
            if competitor_evidence_complete is False
            else "evidence must cover every competitor with a pinned revision and artifact"
        ),
    )
    competitor_ready = competitor_gate["status"] == "pass"

    benchmark_data = _mapping_evidence(supplied, "benchmark")
    benchmark_projects = _string_set(benchmark_data.get("projects")) if benchmark_data else None
    runs_per_project = benchmark_data.get("runs_per_project") if benchmark_data else None
    benchmark_valid = bool(
        benchmark_data
        and _nonempty_text(benchmark_data.get("protocol"))
        and _nonempty_text(benchmark_data.get("artifact_digest"))
        and benchmark_projects == project_set
        and isinstance(runs_per_project, int)
        and not isinstance(runs_per_project, bool)
        and runs_per_project >= 3
    )
    benchmark_gate = _evidence_gate(
        "reproducible comparative benchmark",
        benchmark_data,
        required_fields=(
            "status",
            "protocol",
            "artifact_digest",
            "projects",
            "runs_per_project",
        ),
        valid=benchmark_valid,
        reason="benchmark must cover every project with at least three runs per project",
    )

    algorithm_data = _mapping_evidence(supplied, "algorithms")
    required_checks = algorithm_data.get("required_checks") if algorithm_data else None
    passed_checks = algorithm_data.get("passed_checks") if algorithm_data else None
    algorithms = _string_set(algorithm_data.get("algorithm_ids")) if algorithm_data else None
    receipts = algorithm_data.get("production_receipts") if algorithm_data else None
    algorithm_valid = bool(
        algorithm_data
        and _nonempty_text(algorithm_data.get("probe"))
        and _nonempty_text(algorithm_data.get("artifact_digest"))
        and isinstance(required_checks, int)
        and not isinstance(required_checks, bool)
        and required_checks > 0
        and passed_checks == required_checks
        and algorithms
        and isinstance(receipts, Mapping)
        and algorithms <= {str(key) for key in receipts}
        and all(_nonempty_text(receipts.get(algorithm)) for algorithm in algorithms)
    )
    algorithm_gate = _evidence_gate(
        "production algorithm effectiveness",
        algorithm_data,
        required_fields=(
            "status",
            "probe",
            "artifact_digest",
            "required_checks",
            "passed_checks",
            "algorithm_ids",
            "production_receipts",
        ),
        valid=algorithm_valid,
        reason="every required algorithm check needs a production-path receipt",
    )

    protocol_data = _mapping_evidence(supplied, "protocol_parity")
    protocol_projects = _string_set(protocol_data.get("projects")) if protocol_data else None
    raw_results = protocol_data.get("raw_results") if protocol_data else None
    protocol_valid = bool(
        protocol_data
        and _nonempty_text(protocol_data.get("protocol"))
        and _nonempty_text(protocol_data.get("artifact_digest"))
        and protocol_projects == project_set
        and protocol_data.get("same_workload") is True
        and protocol_data.get("same_hardware") is True
        and protocol_data.get("same_model") is True
        and isinstance(raw_results, Mapping)
        and project_set <= {str(key) for key in raw_results}
        and all(_nonempty_text(raw_results.get(project)) for project in project_set)
    )
    protocol_gate = _evidence_gate(
        "cross-harness protocol parity",
        protocol_data,
        required_fields=(
            "status",
            "protocol",
            "artifact_digest",
            "projects",
            "same_workload",
            "same_hardware",
            "same_model",
            "raw_results",
        ),
        valid=protocol_valid,
        reason="all projects must run the same workload, hardware, and model with raw artifacts",
    )

    release_data = _mapping_evidence(supplied, "release")
    release_valid = bool(
        release_data
        and worktree_clean is True
        and _nonempty_text(release_data.get("commit"))
        and len(str(release_data.get("commit")).strip()) >= 7
        and _nonempty_text(release_data.get("verification_artifact"))
    )
    release_gate = _evidence_gate(
        "clean landed release",
        release_data,
        required_fields=("status", "commit", "verification_artifact"),
        valid=(release_valid if worktree_clean is not None else None),
        reason=(
            "the worktree is dirty; working-tree results cannot establish released leadership"
            if worktree_clean is False
            else "a clean worktree, landed commit, and verification artifact are required"
        ),
    )

    gates = [
        *_axis_gates(rating, subject, competitor_evidence_ready=competitor_ready),
        benchmark_gate,
        algorithm_gate,
        protocol_gate,
        release_gate,
        competitor_gate,
    ]
    score = float(sum(float(gate["points"]) for gate in gates))
    ranking = rating.get("ranking")
    subject_row = next(
        (
            row
            for row in ranking
            if isinstance(row, Mapping) and row.get("project") == subject
        ),
        None,
    ) if isinstance(ranking, list) else None
    corrected_rank = subject_row.get("rank") if isinstance(subject_row, Mapping) else None
    strict_leader = rating.get("strict_leader") == subject
    all_gates_pass = len(gates) == 10 and all(gate["status"] == "pass" for gate in gates)
    claim_allowed = bool(
        rating.get("complete")
        and strict_leader
        and corrected_rank == 1
        and all_gates_pass
        and worktree_clean is True
        and competitor_evidence_complete is True
    )
    blocking_reasons = [
        f"{gate['name']}: {gate['reason'] or gate['status']}"
        for gate in gates
        if gate["status"] != "pass"
    ]
    if corrected_rank != 1:
        blocking_reasons.insert(0, f"corrected comparative rank is {corrected_rank!r}, not 1")
    if not strict_leader:
        blocking_reasons.insert(0, "subject is not the unique corrected-rating leader")
    if not rating.get("complete"):
        blocking_reasons.insert(0, "rating rows are incomplete or malformed")

    return {
        "schema_version": SCHEMA_VERSION,
        "subject": subject,
        "status": "leader" if claim_allowed else "blocked",
        "claim_allowed": claim_allowed,
        "score": score,
        "max_score": 10.0,
        "gate_count": len(gates),
        "all_gates_critical": True,
        "corrected_rank": corrected_rank,
        "strict_corrected_leader": strict_leader,
        "worktree_clean": worktree_clean,
        "competitor_evidence_complete": competitor_evidence_complete,
        "evidence_policy": (
            "Every gate must pass. Unknown evidence, a dirty worktree, a tie, or a non-leading "
            "corrected rank blocks a leader claim. Evidence never changes an axis score."
        ),
        "gates": gates,
        "blocking_reasons": blocking_reasons,
    }


def build_competitive_harness_report(
    rows: Iterable[Mapping[str, Any]] | None = ATTACHED_2026_07_10_ROWS,
    *,
    subject: str = SUBJECT_PROJECT,
    evidence: Mapping[str, Any] | None = None,
    worktree_clean: bool | None = None,
    competitor_evidence_complete: bool | None = None,
) -> dict[str, Any]:
    """Return the corrected rating and fail-closed leader rubric as JSON data."""
    rating = recompute_comparative_rating(rows)
    leader = evaluate_comparative_leader(
        rating,
        subject=subject,
        evidence=evidence,
        worktree_clean=worktree_clean,
        competitor_evidence_complete=competitor_evidence_complete,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "report": "competitive-harness-rating",
        "source_date": SOURCE_DATE,
        "rating": rating,
        "leader_rubric": leader,
    }


__all__ = [
    "ATTACHED_2026_07_10_ROWS",
    "AXES",
    "AXIS_LABELS",
    "LEADER_GATE_NAMES",
    "SCHEMA_VERSION",
    "SOURCE_DATE",
    "SUBJECT_PROJECT",
    "build_competitive_harness_report",
    "evaluate_comparative_leader",
    "recompute_comparative_rating",
]
