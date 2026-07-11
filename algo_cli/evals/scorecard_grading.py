"""Pure, fail-closed grading for the Algo CLI harness scorecard."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any


SCHEMA_VERSION = 2
SCORED_GATE_COUNT = 10
MAX_SCORE = float(SCORED_GATE_COUNT)
VALID_STATUSES = frozenset({"pass", "warn", "unavailable", "fail", "error"})
STATUS_POINTS = {
    "pass": 1.0,
    "warn": 0.5,
    "unavailable": 0.0,
    "fail": 0.0,
    "error": 0.0,
}


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def _json_safe(value: Any, *, _seen: set[int] | None = None, _depth: int = 0) -> Any:
    """Return a JSON-compatible copy without trusting arbitrary evidence objects."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if _depth >= 8:
        return _safe_text(value)

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "<recursive>"

    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            return {
                _safe_text(key): _json_safe(item, _seen=seen, _depth=_depth + 1)
                for key, item in value.items()
            }
        except Exception:
            return _safe_text(value)
        finally:
            seen.discard(identity)
    if isinstance(value, (list, tuple)):
        seen.add(identity)
        try:
            return [
                _json_safe(item, _seen=seen, _depth=_depth + 1)
                for item in value
            ]
        except Exception:
            return _safe_text(value)
        finally:
            seen.discard(identity)
    if isinstance(value, (set, frozenset)):
        seen.add(identity)
        try:
            items = [_json_safe(item, _seen=seen, _depth=_depth + 1) for item in value]
            return sorted(items, key=_safe_text)
        except Exception:
            return _safe_text(value)
        finally:
            seen.discard(identity)
    return _safe_text(value)


def _evidence_text(value: Any) -> str:
    safe = _json_safe(value)
    if isinstance(safe, (dict, list)):
        try:
            return json.dumps(safe, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return _safe_text(safe)
    return _safe_text(safe)


def _has_structured_evidence(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes, Mapping, list, tuple, set, frozenset)):
        return bool(value)
    return True


def _materialize(values: Iterable[Any] | None, *, label: str) -> tuple[list[Any], str | None]:
    if values is None:
        return [], f"{label} must be an iterable, not null"
    if isinstance(values, (str, bytes, Mapping)):
        return [], f"{label} must be an iterable of mappings"
    try:
        return list(values), None
    except Exception as exc:
        return [], f"could not read {label}: {_safe_text(exc)}"


def _normalize_check(raw: Any, position: int) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        return (
            {
                "name": f"invalid-gate-{position + 1}",
                "status": "error",
                "critical": True,
                "evidence": f"gate definition is {type(raw).__name__}, not a mapping",
                "recommendation": "Provide a mapping with name, status, and evidence.",
            },
            [f"gate {position + 1} is not a mapping"],
        )

    name = _safe_text(raw.get("name")).strip()
    if not name:
        name = f"unnamed-gate-{position + 1}"
        errors.append(f"gate {position + 1} is missing a name")

    raw_status = raw.get("status")
    status = _safe_text(raw_status).strip().lower()
    if status not in VALID_STATUSES:
        errors.append(f"gate {name!r} has invalid status {status or '<missing>'!r}")
        status = "error"

    raw_critical = raw.get("critical", False)
    if isinstance(raw_critical, bool):
        critical = raw_critical
    else:
        errors.append(f"gate {name!r} critical must be a boolean")
        critical = True
        status = "error"

    evidence = _evidence_text(raw.get("evidence", "")).strip()
    recommendation = _evidence_text(raw.get("recommendation", "")).strip()
    metrics_present = "metrics" in raw
    metrics = _json_safe(raw.get("metrics")) if metrics_present else None

    if status in {"pass", "warn"} and not evidence and not _has_structured_evidence(metrics):
        errors.append(f"gate {name!r} cannot be {status} without evidence")
        status = "error"
        critical = True
        recommendation = recommendation or "Attach human-readable evidence or structured metrics."

    if errors:
        critical = True

    normalized: dict[str, Any] = {
        "name": name,
        "status": status,
        "critical": critical,
        "evidence": evidence,
        "recommendation": recommendation,
    }
    if metrics_present:
        normalized["metrics"] = metrics
    return normalized, errors


def _normalize_capability(raw: Any, position: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {
            "name": f"invalid-capability-{position + 1}",
            "status": "error",
            "evidence": f"capability definition is {type(raw).__name__}, not a mapping",
            "recommendation": "Provide a mapping for this optional capability.",
            "scored": False,
            "points": 0.0,
            "max_points": 0.0,
        }

    aliases = {
        "ready": "pass",
        "degraded": "warn",
        "disabled": "unavailable",
        "blocked": "fail",
    }
    raw_status = _safe_text(raw.get("status", "unavailable")).strip().lower()
    status = aliases.get(raw_status, raw_status)
    if status not in VALID_STATUSES:
        status = "error"
    normalized: dict[str, Any] = {
        "name": _safe_text(raw.get("name")).strip() or f"capability-{position + 1}",
        "status": status,
        "evidence": _evidence_text(raw.get("evidence", "")).strip(),
        "recommendation": _evidence_text(raw.get("recommendation", "")).strip(),
        "scored": False,
        "points": 0.0,
        "max_points": 0.0,
    }
    if "metrics" in raw:
        normalized["metrics"] = _json_safe(raw.get("metrics"))
    return normalized


def finalize_scorecard(
    checks: Iterable[Mapping[str, Any]] | None,
    capabilities: Iterable[Mapping[str, Any]] | None = (),
) -> dict[str, Any]:
    """Finalize exactly ten evidence-backed gates into a JSON-ready scorecard.

    Optional capabilities are normalized for display but never affect points or
    overall status. Invalid scored-gate schemas fail closed and are returned as
    validation errors instead of raising.
    """
    raw_checks, collection_error = _materialize(checks, label="checks")
    validation_errors = [collection_error] if collection_error else []
    normalized_checks: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_checks):
        normalized, errors = _normalize_check(raw, position)
        normalized_checks.append(normalized)
        validation_errors.extend(errors)

    if len(normalized_checks) != SCORED_GATE_COUNT:
        validation_errors.append(
            f"expected exactly {SCORED_GATE_COUNT} scored gates, got {len(normalized_checks)}"
        )
        # With the wrong denominator there is no meaningful partial grade.
        # Preserve the definitions for diagnosis but fail every supplied gate
        # closed so the reported score remains the sum of normalized points.
        for check in normalized_checks:
            check["status"] = "error"
            check["critical"] = True
            if not check["recommendation"]:
                check["recommendation"] = "Fix the scorecard gate schema before grading."

    names: dict[str, list[int]] = {}
    for position, check in enumerate(normalized_checks):
        names.setdefault(check["name"].casefold(), []).append(position)
    for positions in names.values():
        if len(positions) < 2:
            continue
        duplicate_name = normalized_checks[positions[0]]["name"]
        validation_errors.append(f"duplicate scored gate name: {duplicate_name!r}")
        for position in positions:
            normalized_checks[position]["status"] = "error"
            normalized_checks[position]["critical"] = True
            if not normalized_checks[position]["recommendation"]:
                normalized_checks[position]["recommendation"] = "Use a unique scored gate name."

    for check in normalized_checks:
        check["points"] = STATUS_POINTS[check["status"]]
        check["max_points"] = 1.0
        check["scored"] = True

    raw_capabilities, capability_error = _materialize(capabilities, label="capabilities")
    normalized_capabilities = [
        _normalize_capability(raw, position)
        for position, raw in enumerate(raw_capabilities)
    ]
    if capability_error:
        normalized_capabilities.append(
            _normalize_capability(
                {
                    "name": "capability-schema",
                    "status": "error",
                    "evidence": capability_error,
                },
                len(normalized_capabilities),
            )
        )

    score = float(sum(float(check["points"]) for check in normalized_checks))
    critical_failure = any(
        check["critical"] and check["status"] in {"fail", "error"}
        for check in normalized_checks
    )
    all_pass = (
        not validation_errors
        and len(normalized_checks) == SCORED_GATE_COUNT
        and all(check["status"] == "pass" for check in normalized_checks)
    )
    if all_pass and score == MAX_SCORE:
        overall_status = "ready"
    elif validation_errors or critical_failure:
        overall_status = "blocked"
    else:
        overall_status = "degraded"

    return {
        "schema_version": SCHEMA_VERSION,
        "score": score,
        "max_score": MAX_SCORE,
        "overall_status": overall_status,
        "scored_gate_count": len(normalized_checks),
        "checks": normalized_checks,
        "capabilities": normalized_capabilities,
        "validation_errors": validation_errors,
    }


__all__ = [
    "MAX_SCORE",
    "SCHEMA_VERSION",
    "SCORED_GATE_COUNT",
    "STATUS_POINTS",
    "VALID_STATUSES",
    "finalize_scorecard",
]
