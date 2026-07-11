"""Runtime seam for bounded automatic durable-memory capture."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from . import config as config_module
from . import memory_candidates
from .config import Config
from .perf_telemetry import record_perf_event

logger = logging.getLogger(__name__)

_EXPLICIT_MEMORY_TOOLS = frozenset({"remember", "append_lesson"})
_SUCCESSFUL_TOOL_STATUSES = frozenset({"worked"})


def _successful_explicit_memory_write(tool_calls: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        str(call.get("name") or "") in _EXPLICIT_MEMORY_TOOLS
        and str(call.get("status") or "") in _SUCCESSFUL_TOOL_STATUSES
        for call in tool_calls
    )


def _record_result(result: Mapping[str, Any], *, source: str) -> None:
    counts_value = result.get("counts")
    state_value = result.get("state")
    reasons_value = result.get("reason_counts")
    counts = counts_value if isinstance(counts_value, Mapping) else {}
    state = state_value if isinstance(state_value, Mapping) else {}
    reasons = reasons_value if isinstance(reasons_value, Mapping) else {}
    record_perf_event(
        "memory_candidate",
        source="agent" if source == "agent" else "chat",
        status=str(result.get("status") or "unknown"),
        reason=str(result.get("reason") or ""),
        extracted=int(counts.get("extracted") or 0),
        evaluated=int(counts.get("evaluated") or 0),
        eligible=int(counts.get("eligible") or 0),
        stored=int(counts.get("stored") or 0),
        rejected=int(counts.get("rejected") or 0),
        daily_writes=int(state.get("daily_writes") or 0),
        auto_fingerprints=int(state.get("auto_fingerprints") or 0),
        reason_counts={str(key): int(value) for key, value in reasons.items()},
    )


def capture_completed_user_turn(
    cfg: Config,
    original_user_text: str,
    *,
    completed: bool,
    tool_calls: Sequence[Mapping[str, Any]] = (),
    source: str = "chat",
) -> dict[str, Any]:
    """Capture at most one memory after a verified completion boundary.

    Only ``original_user_text`` reaches the deterministic candidate processor.
    Assistant, tool, retrieval, and specialist output are never inspected.
    """

    safe_source = "agent" if source == "agent" else "chat"
    if not completed:
        result = {
            "status": "skipped",
            "reason": "incomplete_turn",
            "counts": {},
            "reason_counts": {},
            "state": {},
        }
        _record_result(result, source=safe_source)
        return result
    if _successful_explicit_memory_write(tool_calls):
        result = {
            "status": "skipped",
            "reason": "explicit_memory_write",
            "counts": {},
            "reason_counts": {},
            "state": {},
        }
        _record_result(result, source=safe_source)
        return result

    try:
        return memory_candidates.process_memory_candidates(
            original_user_text,
            tuple(str(item) for item in cfg.memories),
            config_module.MEMORY_CANDIDATE_STATE_FILE,
            bool(cfg.memory_auto_capture_enabled),
            cfg.remember_fact,
            telemetry=lambda result: _record_result(result, source=safe_source),
            daily_limit=int(cfg.memory_auto_daily_limit),
            entry_limit=int(cfg.memory_auto_entry_limit),
            char_limit=int(cfg.memory_auto_char_limit),
        )
    except Exception as exc:  # Completion must never fail because memory capture did.
        logger.debug("Automatic memory capture failed: %s", exc)
        result = {
            "status": "error",
            "reason": type(exc).__name__,
            "counts": {},
            "reason_counts": {"runtime_error": 1},
            "state": {},
        }
        _record_result(result, source=safe_source)
        return result


__all__ = ["capture_completed_user_turn"]
