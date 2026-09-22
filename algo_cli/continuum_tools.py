"""Governed native Continuum Memory tools for Algo's runtime."""

from __future__ import annotations

import hashlib
import json
from typing import Any


CONTINUUM_TOOL_NAMES = (
    "memory_init",
    "memory_verify",
    "memory_status",
    "memory_capture",
    "memory_remember",
    "memory_get",
    "memory_read",
    "memory_search",
    "memory_context",
    "memory_validate_context",
    "memory_revoke",
    "memory_resolve",
    "memory_explain",
    "memory_history",
    "memory_handoff",
)
CONTINUUM_READ_ONLY_TOOLS = frozenset(
    {
        "memory_verify",
        "memory_status",
        "memory_get",
        "memory_read",
        "memory_search",
        "memory_context",
        "memory_validate_context",
        "memory_explain",
        "memory_history",
    }
)
CONTINUUM_MUTATION_TOOLS = frozenset(CONTINUUM_TOOL_NAMES) - CONTINUUM_READ_ONLY_TOOLS
_REQUEST_ID_OPERATIONS = frozenset({"capture", "remember", "revoke", "resolve", "handoff"})
_DEFINITE_REFUSALS = frozenset(
    {
        "budget",
        "critical_unavailable",
        "dependency",
        "expired",
        "idempotency_conflict",
        "input",
        "integrity",
        "not_found",
        "policy",
        "revision_conflict",
        "revoked",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stable_request_id(operation: str, payload: dict[str, Any], scope: str) -> str:
    envelope = {"operation": operation, "scope": scope, "payload": payload}
    return "algo-native:" + hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def _failure(
    *,
    operation: str,
    mutates: bool,
    error: str,
    message: str,
    decision: str = "REFUSE_SYSTEM",
) -> dict[str, Any]:
    uncertain = mutates and error not in _DEFINITE_REFUSALS
    return {
        "ok": False,
        "decision": decision,
        "error": error,
        "message": message,
        "operation": operation,
        "status": "unknown_outcome" if uncertain else "denied",
        "requires_reconciliation": uncertain,
        "retry_allowed": False,
    }


def _record_postcondition(
    cfg: Any,
    operation: str,
    result: dict[str, Any],
    *,
    scope: str,
) -> dict[str, Any] | None:
    from .continuum_memory import invoke_continuum

    memory_id = result.get("id")
    revision = result.get("revision")
    if type(memory_id) is not str or type(revision) is not int or revision < 1:
        return None
    observed = invoke_continuum(cfg, "get", {"memory_id": memory_id}, scope=scope)
    memory = observed.get("memory") if observed.get("ok") is True else None
    if (
        type(memory) is not dict
        or memory.get("id") != memory_id
        or memory.get("revision") != revision
        or memory.get("data_digest") != result.get("data_digest")
    ):
        return None
    return {
        "operation": operation,
        "scope": scope,
        "record_id": memory_id,
        "revision": revision,
        "data_digest": memory["data_digest"],
    }


def _mutation_postcondition(
    cfg: Any,
    operation: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    *,
    scope: str,
) -> dict[str, Any] | None:
    from .continuum_memory import invoke_continuum

    if operation == "init":
        observed = invoke_continuum(cfg, "verify", {}, scope=scope)
        namespace = observed.get("namespace") if observed.get("ok") is True else None
        if type(namespace) is not dict or namespace.get("scope") != scope:
            return None
        return {
            "operation": operation,
            "scope": scope,
            "head": observed.get("head"),
            "verified": True,
        }
    if operation in {"capture", "remember", "handoff"}:
        return _record_postcondition(cfg, operation, result, scope=scope)
    if operation == "revoke":
        memory_id = result.get("id")
        revision = result.get("revision")
        if type(memory_id) is not str or type(revision) is not int or revision < 1:
            return None
        observed = invoke_continuum(cfg, "explain", {"memory_id": memory_id}, scope=scope)
        status = observed.get("status") if observed.get("ok") is True else None
        if (
            type(status) is not dict
            or observed.get("id") != memory_id
            or observed.get("revision") != revision
            or status.get("error") != "revoked"
            or status.get("usable") is not False
        ):
            return None
        return {"operation": operation, "scope": scope, "record_id": memory_id, "revision": revision}
    if operation == "resolve":
        revision = result.get("revision")
        winner = result.get("winner")
        withdrawn = result.get("withdrawn")
        expected = payload.get("expected_revisions")
        if (
            type(revision) is not int
            or revision < 1
            or type(winner) is not str
            or type(withdrawn) is not list
            or type(expected) is not dict
            or set(expected) != {winner, *withdrawn}
        ):
            return None
        states: dict[str, str] = {}
        for memory_id in sorted(expected):
            observed = invoke_continuum(cfg, "explain", {"memory_id": memory_id}, scope=scope)
            status = observed.get("status") if observed.get("ok") is True else None
            if type(status) is not dict or observed.get("revision") != revision:
                return None
            state = (
                "active"
                if status.get("decision") == "PRESENT" and status.get("usable") is True
                else status.get("error")
            )
            expected_state = "active" if memory_id == winner else "revoked"
            if state != expected_state:
                return None
            states[memory_id] = state
        return {
            "operation": operation,
            "scope": scope,
            "winner": winner,
            "revision": revision,
            "states": states,
        }
    return None


def _call(operation: str, payload: dict[str, Any], scope: str, cfg: Any) -> str:
    from .continuum_memory import ContinuumMemoryError, invoke_continuum

    tool_name = f"memory_{operation}"
    mutates = tool_name in CONTINUUM_MUTATION_TOOLS
    if scope not in {"shared", "private"}:
        return _canonical_json(
            _failure(
                operation=operation,
                mutates=mutates,
                error="input",
                message="Continuum Memory scope must be shared or private.",
                decision="REFUSE_INPUT",
            )
        )
    if cfg is None:
        return _canonical_json(
            _failure(
                operation=operation,
                mutates=mutates,
                error="runtime_config_required",
                message="Continuum Memory requires the active Algo runtime configuration.",
            )
        )
    invalid_container = (
        operation == "remember"
        and (
            type(payload.get("data")) is not dict
            or type(payload.get("sources")) is not dict
            or type(payload.get("depends_on")) is not dict
        )
    ) or (
        operation == "validate_context" and type(payload.get("packet")) is not dict
    ) or (
        operation == "resolve" and type(payload.get("expected_revisions")) is not dict
    ) or (
        operation == "handoff"
        and any(
            type(payload.get(field)) is not list
            or any(type(item) is not str for item in payload.get(field, []))
            for field in ("completed", "next_steps", "blockers")
        )
    )
    if invalid_container:
        return _canonical_json(
            _failure(
                operation=operation,
                mutates=mutates,
                error="input",
                message="Continuum Memory received a value with the wrong canonical container type.",
                decision="REFUSE_INPUT",
            )
        )
    request = dict(payload)
    if operation in _REQUEST_ID_OPERATIONS and request.get("request_id") is None:
        request.pop("request_id", None)
        request["request_id"] = _stable_request_id(operation, request, scope)
    try:
        value = invoke_continuum(cfg, operation, request, scope=scope)
    except ContinuumMemoryError:
        return _canonical_json(
            _failure(
                operation=operation,
                mutates=mutates,
                error="continuum_transport_unavailable",
                message="Continuum Memory could not complete the operation; no fallback was used.",
            )
        )
    if value.get("ok") is not True:
        error = str(value.get("error") or "internal")
        value = {
            **value,
            "operation": operation,
            "status": "unknown_outcome" if mutates and error not in _DEFINITE_REFUSALS else "denied",
            "requires_reconciliation": bool(mutates and error not in _DEFINITE_REFUSALS),
            "retry_allowed": False,
        }
        return _canonical_json(value)
    if not mutates:
        return _canonical_json({**value, "operation": operation, "scope": scope, "status": "worked"})
    try:
        postcondition = _mutation_postcondition(cfg, operation, request, value, scope=scope)
    except ContinuumMemoryError:
        postcondition = None
    if postcondition is None:
        return _canonical_json(
            {
                **value,
                "operation": operation,
                "scope": scope,
                "status": "unknown_outcome",
                "requires_reconciliation": True,
                "retry_allowed": False,
                "message": "Continuum mutation returned without a verified authoritative postcondition.",
            }
        )
    replayed = value.get("replayed_request") is True
    return _canonical_json(
        {
            **value,
            "operation": operation,
            "scope": scope,
            "status": "skipped" if replayed else "worked",
            "deduplicated": replayed,
            "requires_reconciliation": False,
            "retry_allowed": False,
            "authoritative_postcondition": postcondition,
        }
    )


def memory_init(scope: str, cfg: Any = None) -> str:
    """Initialize one explicit Continuum scope without importing retired stores."""
    return _call("init", {}, scope, cfg)


def memory_verify(scope: str, cfg: Any = None) -> str:
    """Verify Continuum encryption, journal linkage, and trusted checkpoint."""
    return _call("verify", {}, scope, cfg)


def memory_status(scope: str, limit: int = 100, cfg: Any = None) -> str:
    """Inspect scoped Continuum health, blockers, conflicts, and stale dependencies."""
    return _call("status", {"limit": limit}, scope, cfg)


def memory_capture(
    text: str,
    source: str,
    scope: str,
    request_id: str | None = None,
    cfg: Any = None,
) -> str:
    """Capture exact source text in Continuum and return its expandable source ID."""
    return _call("capture", {"text": text, "source": source, "request_id": request_id}, scope, cfg)


def memory_remember(
    memory_id: str,
    data: dict,
    scope: str,
    kind: str = "fact",
    slot: str | None = None,
    sources: dict | None = None,
    depends_on: dict | None = None,
    critical: bool = False,
    valid_from: float | None = None,
    expires_at: float | None = None,
    expected_revision: int = 0,
    restore: bool = False,
    request_id: str | None = None,
    cfg: Any = None,
) -> str:
    """Create or revision-bind typed Continuum memory in an explicit scope."""
    payload: dict[str, Any] = {
        "memory_id": memory_id,
        "data": data,
        "kind": kind,
        "slot": slot,
        "sources": {} if sources is None else sources,
        "depends_on": {} if depends_on is None else depends_on,
        "critical": critical,
        "valid_from": valid_from,
        "expires_at": expires_at,
        "expected_revision": expected_revision,
        "restore": restore,
        "request_id": request_id,
    }
    return _call("remember", payload, scope, cfg)


def memory_get(memory_id: str, scope: str, cfg: Any = None) -> str:
    """Get one exact current usable Continuum memory by ID."""
    return _call("get", {"memory_id": memory_id}, scope, cfg)


def memory_read(
    memory_id: str,
    scope: str,
    offset: int = 0,
    limit: int = 8192,
    cfg: Any = None,
) -> str:
    """Read an exact bounded range from a captured Continuum source."""
    return _call("read", {"memory_id": memory_id, "offset": offset, "limit": limit}, scope, cfg)


def memory_search(query: str, scope: str, limit: int = 10, cfg: Any = None) -> str:
    """Search usable Continuum memory lexically; an empty result remains unknown."""
    return _call("search", {"query": query, "limit": limit}, scope, cfg)


def memory_context(
    query: str,
    scope: str,
    budget_bytes: int = 12000,
    ttl_seconds: int = 300,
    cfg: Any = None,
) -> str:
    """Build a signed, byte-bounded Continuum context packet for one scope."""
    return _call(
        "context",
        {"query": query, "budget_bytes": budget_bytes, "ttl_seconds": ttl_seconds},
        scope,
        cfg,
    )


def memory_validate_context(packet: dict, scope: str, cfg: Any = None) -> str:
    """Validate a Continuum context packet against current head and dependencies."""
    return _call("validate_context", {"packet": packet}, scope, cfg)


def memory_revoke(
    memory_id: str,
    expected_revision: int,
    reason: str,
    scope: str,
    request_id: str | None = None,
    cfg: Any = None,
) -> str:
    """Revoke one Continuum memory while retaining encrypted history."""
    payload = {
        "memory_id": memory_id,
        "expected_revision": expected_revision,
        "reason": reason,
        "request_id": request_id,
    }
    return _call("revoke", payload, scope, cfg)


def memory_resolve(
    slot: str,
    winner: str,
    expected_revisions: dict,
    reason: str,
    scope: str,
    request_id: str | None = None,
    cfg: Any = None,
) -> str:
    """Resolve a Continuum slot conflict with every competitor acknowledged."""
    payload = {
        "slot": slot,
        "winner": winner,
        "expected_revisions": expected_revisions,
        "reason": reason,
        "request_id": request_id,
    }
    return _call("resolve", payload, scope, cfg)


def memory_explain(memory_id: str, scope: str, cfg: Any = None) -> str:
    """Explain Continuum validity and provenance without releasing revoked content."""
    return _call("explain", {"memory_id": memory_id}, scope, cfg)


def memory_history(memory_id: str, scope: str, limit: int = 20, cfg: Any = None) -> str:
    """Read metadata-only Continuum revision history for one memory ID."""
    return _call("history", {"memory_id": memory_id, "limit": limit}, scope, cfg)


def memory_handoff(
    memory_id: str,
    goal: str,
    completed: list[str],
    next_steps: list[str],
    blockers: list[str],
    scope: str,
    expected_revision: int = 0,
    request_id: str | None = None,
    cfg: Any = None,
) -> str:
    """Save a source-bound Continuum resumption plan over verified current context."""
    payload = {
        "memory_id": memory_id,
        "goal": goal,
        "completed": completed,
        "next_steps": next_steps,
        "blockers": blockers,
        "expected_revision": expected_revision,
        "request_id": request_id,
    }
    return _call("handoff", payload, scope, cfg)


CONTINUUM_TOOLS = (
    memory_init,
    memory_verify,
    memory_status,
    memory_capture,
    memory_remember,
    memory_get,
    memory_read,
    memory_search,
    memory_context,
    memory_validate_context,
    memory_revoke,
    memory_resolve,
    memory_explain,
    memory_history,
    memory_handoff,
)


__all__ = [
    "CONTINUUM_MUTATION_TOOLS",
    "CONTINUUM_READ_ONLY_TOOLS",
    "CONTINUUM_TOOLS",
    "CONTINUUM_TOOL_NAMES",
    *CONTINUUM_TOOL_NAMES,
]
