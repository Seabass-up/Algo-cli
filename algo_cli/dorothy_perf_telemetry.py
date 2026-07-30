"""Allowlisted performance event buffering and private JSONL persistence."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from typing import Any, Mapping

from rich.text import Text

from .chat_protocol import get_attr
from .config import Config, PERF_HISTORY_FILE
from .display import console, show_info
from .private_event_store import PrivateEventStore, RetentionPolicy

PERF_BUFFER: list[dict[str, Any]] = []
_PERF_BUFFER_LOCK = threading.Lock()
_PERF_FLUSH_LOCK = threading.Lock()
_PERF_TAIL_BLOCK_BYTES = 64 * 1024
_PERF_TAIL_MAX_BYTES = 2 * 1024 * 1024
_PERF_BUFFER_MAX_RECORDS = 256
_PRIVATE_PERF_MAX_RECORDS = 1_000
_PRIVATE_PERF_MAX_BYTES = 4 * 1024 * 1024
_PRIVATE_PERF_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")
_NUMERIC_FIELDS = frozenset(
    {
        "timestamp",
        "duration_ms",
        "estimated_cost",
        "queue_position",
        "sequence",
        "catalog_tools",
        "visible_tools",
        "schema_tokens",
        "full_schema_tokens",
        "reduction_pct",
        "round",
        "prompt_tokens",
        "completion_tokens",
        "context_build_ms",
        "queue_ms",
        "model_load_ms",
        "prompt_eval_ms",
        "generation_ms",
        "model_elapsed_ms",
        "tool_ms_since_previous_round",
        "message_count",
        "tool_schema_count",
        "superseded_chars",
        "artifact_referenced_chars",
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
        "extracted",
        "evaluated",
        "eligible",
        "stored",
        "rejected",
        "daily_writes",
        "auto_fingerprints",
        "candidates",
        "superseded",
        "before_tokens",
        "after_tokens",
        "saved_tokens",
        "removed",
        "kept",
        "threshold",
        "messages_compacted",
        "keep_messages",
        "capability_mask",
    }
)
_BOOLEAN_FIELDS = frozenset({"cloud", "manual", "log_suppression"})
_LABEL_FIELDS = frozenset(
    {
        "tool",
        "status",
        "tier",
        "spawn_class",
        "confirmation",
        "model",
        "phase",
        "trigger",
        "source",
        "backend",
        "reason",
        "keep_alive",
    }
)
_LIST_LABEL_FIELDS = frozenset({"capabilities", "fired_rules"})
_CONTEXT_SOURCE_FIELDS = frozenset(
    {
        "identity",
        "policy_and_runtime",
        "repository_instructions",
        "tool_schemas",
        "harness_rag",
        "memory",
        "knowledge_graph",
        "conversation",
        "tool_results",
        "verification_receipts",
        "other_optional",
    }
)
_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "tool": frozenset(
        {
            "timestamp",
            "tool",
            "status",
            "duration_ms",
            "spawn_class",
            "estimated_cost",
            "log_suppression",
            "queue_position",
            "outcome",
            "sequence",
        }
    ),
    "qos": frozenset(
        {
            "timestamp",
            "tool",
            "spawn_class",
            "estimated_cost",
            "log_suppression",
            "queue_position",
        }
    ),
    "policy": frozenset(
        {
            "timestamp",
            "tool",
            "status",
            "tier",
            "capability_mask",
            "capabilities",
            "fired_rules",
            "guardrail_reason_count",
        }
    ),
    "authority": frozenset({"timestamp", "tool", "status", "confirmation"}),
    "tool_context": frozenset(
        {
            "timestamp",
            "catalog_tools",
            "visible_tools",
            "schema_tokens",
            "full_schema_tokens",
            "reduction_pct",
        }
    ),
    "model_round": frozenset(
        {
            "timestamp",
            "model",
            "round",
            "phase",
            "trigger",
            "prompt_tokens",
            "completion_tokens",
            "context_build_ms",
            "queue_ms",
            "model_load_ms",
            "prompt_eval_ms",
            "generation_ms",
            "model_elapsed_ms",
            "tool_ms_since_previous_round",
            "message_count",
            "tool_schema_count",
            "superseded_chars",
            "artifact_referenced_chars",
            "context_sources",
        }
    ),
    "chat": frozenset(
        {
            "timestamp",
            "model",
            "cloud",
            "keep_alive",
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
        }
    ),
    "memory_candidate": frozenset(
        {
            "timestamp",
            "source",
            "status",
            "reason",
            "extracted",
            "evaluated",
            "eligible",
            "stored",
            "rejected",
            "daily_writes",
            "auto_fingerprints",
            "reason_counts",
        }
    ),
    "semantic_supersession": frozenset(
        {
            "timestamp",
            "candidates",
            "superseded",
            "before_tokens",
            "after_tokens",
            "saved_tokens",
            "reduction_pct",
        }
    ),
    "prune": frozenset({"timestamp", "removed", "kept", "threshold"}),
    "compaction": frozenset(
        {"timestamp", "duration_ms", "messages_compacted", "keep_messages", "threshold", "manual"}
    ),
}
_OUTCOME_FIELDS = frozenset(
    {
        "action",
        "status",
        "invoked",
        "retry_allowed",
        "verification",
        "fencing_token",
        "error_code",
        "deduplicated",
        "compensation_action",
    }
)
_EMBED_NUMERIC_FIELDS = frozenset(
    {
        "timestamp",
        "batch_size",
        "wall_ms",
        "per_record_ms",
        "queue_completed",
        "queue_total",
        "selected_total",
        "embedded",
        "total_records",
        "total_ms",
        "count",
        "per_record_mean_ms",
        "single_record_ms",
        "batch_p50_ms",
        "batch_p95_ms",
        "batch_count",
    }
)
_EMBED_LABEL_FIELDS = frozenset({"event", "source", "backend", "model", "priority_policy"})
_TELEMETRY_REJECT_LOCK = threading.Lock()
_TELEMETRY_REJECTED = 0


def _safe_label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _SAFE_LABEL_RE.fullmatch(text) else None


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        return None
    return value


def _safe_label_list(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        return None
    labels = [_safe_label(item) for item in value]
    if any(item is None for item in labels):
        return None
    return [str(item) for item in labels]


def _safe_numeric_mapping(value: Any, *, allowed_keys: frozenset[str] | None = None) -> dict[str, int | float] | None:
    if not isinstance(value, Mapping) or len(value) > 64:
        return None
    projected: dict[str, int | float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if allowed_keys is not None and key not in allowed_keys:
            continue
        if allowed_keys is None and _safe_label(key) is None:
            continue
        number = _safe_number(raw_value)
        if number is not None:
            projected[key] = number
    return projected


def _sanitize_outcome(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    sanitized: dict[str, Any] = {}
    for field in _OUTCOME_FIELDS:
        raw = value.get(field)
        if field in {"invoked", "retry_allowed", "deduplicated"}:
            if isinstance(raw, bool):
                sanitized[field] = raw
        elif field == "fencing_token":
            number = _safe_number(raw)
            if number is not None:
                sanitized[field] = number
        else:
            label = _safe_label(raw)
            if label is not None:
                sanitized[field] = label
    return sanitized


def sanitize_perf_record(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return only the frozen structural schema for a known event class."""

    event = _safe_label(record.get("event"))
    if event is None or event not in _EVENT_FIELDS:
        return None
    sanitized: dict[str, Any] = {"event": event}
    allowed = _EVENT_FIELDS[event]
    for field in allowed:
        if field not in record:
            continue
        raw = record[field]
        if field in _NUMERIC_FIELDS or field == "guardrail_reason_count":
            number = _safe_number(raw)
            if number is not None:
                sanitized[field] = number
        elif field in _BOOLEAN_FIELDS:
            if isinstance(raw, bool):
                sanitized[field] = raw
        elif field in _LABEL_FIELDS:
            label = _safe_label(raw)
            if label is not None:
                sanitized[field] = label
        elif field in _LIST_LABEL_FIELDS:
            labels = _safe_label_list(raw)
            if labels is not None:
                sanitized[field] = labels
        elif field == "context_sources":
            mapping = _safe_numeric_mapping(raw, allowed_keys=_CONTEXT_SOURCE_FIELDS)
            if mapping is not None:
                sanitized[field] = mapping
        elif field == "reason_counts":
            mapping = _safe_numeric_mapping(raw)
            if mapping is not None:
                sanitized[field] = mapping
        elif field == "outcome":
            outcome = _sanitize_outcome(raw)
            if outcome is not None:
                sanitized[field] = outcome
    return sanitized


def _sanitize_embed_record(record: Mapping[str, Any], *, source: str, backend: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"timestamp": time.time()}
    candidates = {**record, "source": source}
    if backend is not None:
        candidates["backend"] = backend
    for field, raw in candidates.items():
        if field in _EMBED_NUMERIC_FIELDS:
            number = _safe_number(raw)
            if number is not None:
                payload[field] = number
        elif field in _EMBED_LABEL_FIELDS:
            label = _safe_label(raw)
            if label is not None:
                payload[field] = label
        elif field in {"batch_by_priority", "pending_by_priority"}:
            mapping = _safe_numeric_mapping(raw)
            if mapping is not None:
                payload[field] = mapping
    return payload


def _record_rejection() -> None:
    global _TELEMETRY_REJECTED
    with _TELEMETRY_REJECT_LOCK:
        _TELEMETRY_REJECTED += 1
        rejected = _TELEMETRY_REJECTED
    try:
        _runtime_status()["telemetry_rejected"] = rejected
    except Exception:
        pass


def _private_perf_store() -> PrivateEventStore:
    return PrivateEventStore(
        PERF_HISTORY_FILE.parent / "private" / "runtime_events.jsonl",
        policy=RetentionPolicy(
            max_records=_PRIVATE_PERF_MAX_RECORDS,
            max_bytes=_PRIVATE_PERF_MAX_BYTES,
            max_age_seconds=_PRIVATE_PERF_MAX_AGE_SECONDS,
        ),
    )


def private_perf_store_readiness() -> dict[str, Any]:
    """Return content-free permission and retention state for diagnostics."""

    return _private_perf_store().readiness()


def runtime_performance_snapshot(history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score comparable latency series and return the worst L3 CUSUM result."""
    from .evals.performance_regression import detect_cusum

    if history is not None:
        rows = list(history)
    else:
        with _PERF_BUFFER_LOCK:
            buffered = list(PERF_BUFFER)
        rows = [*load_perf_history(limit=120), *buffered]

    values_by_series: dict[str, list[float]] = {}
    latest_by_series: dict[str, int] = {}
    for position, item in enumerate(rows):
        event = str(item.get("event") or "")
        if event == "chat":
            try:
                duration_ms = float(item.get("total_duration") or 0) / 1_000_000
            except (TypeError, ValueError):
                continue
            if duration_ms <= 0:
                continue
            series_key = f"chat:{item.get('model') or '?'}"
        elif event == "tool" and str(item.get("status") or "") == "worked":
            try:
                duration_ms = float(item.get("duration_ms") or 0)
            except (TypeError, ValueError):
                continue
            if duration_ms <= 0:
                continue
            series_key = f"tool:{item.get('tool') or '?'}"
        else:
            continue
        values_by_series.setdefault(series_key, []).append(duration_ms)
        latest_by_series[series_key] = position

    if not values_by_series:
        return {"series": "none", **detect_cusum([]).to_dict()}

    scored = [(series, detect_cusum(values)) for series, values in values_by_series.items()]
    eligible = [item for item in scored if item[1].state.value != "insufficient_data"]
    candidates = eligible or scored

    state_risk = {
        "insufficient_data": 0,
        "improving": 1,
        "stable": 2,
        "regressing": 3,
    }

    def risk(item: tuple[str, Any]) -> tuple[float, float, int, int]:
        series, result = item
        scale = float(result.scale or 0.0)
        upward_pressure = float(result.positive_score) / scale if scale > 0 else 0.0
        return (
            float(state_risk[result.state.value]),
            upward_pressure,
            int(result.sample_count),
            latest_by_series[series],
        )

    series_key, result = max(candidates, key=risk)
    return {"series": series_key, **result.to_dict()}


def runtime_quality_snapshot(
    cfg: Config,
    *,
    tool_limit: int = 12,
    performance_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return safe, observable runtime quality signals for ``/selfcheck``.

    Tool cadence comes from the bounded attempt ledger. Private model reasoning
    is deliberately neither persisted nor inspected, so its availability is
    reported instead of manufacturing a score from hidden state.
    """
    from .evals.cot_quality import score_tool_sequence

    recent = cfg.attempt_ledger[-max(1, tool_limit) :]
    tool_names = [
        str(item.get("tool") or "")
        for item in recent
        if str(item.get("tool") or "").strip()
    ]
    return {
        "tool_sequence": score_tool_sequence(tool_names).to_dict(),
        "performance": runtime_performance_snapshot(performance_history),
        "reasoning_quality": {
            "status": "not_collected",
            "reason": "Private model reasoning is neither persisted nor inspected.",
        },
    }


def render_runtime_quality_snapshot(cfg: Config) -> str:
    """Render the safe quality snapshot used by ``/selfcheck``."""
    snapshot = runtime_quality_snapshot(cfg)
    sequence = snapshot["tool_sequence"]
    performance = snapshot["performance"]
    reasoning = snapshot["reasoning_quality"]
    baseline = performance.get("baseline")
    baseline_text = f", baseline {float(baseline):.1f} ms" if baseline is not None else ""
    return "\n".join(
        (
            "[bold primary]Runtime quality diagnostics[/]",
            (
                f"  tool cadence: {sequence['pattern']} "
                f"(score {sequence['sequence_score']:.2f}, "
                f"verification {'yes' if sequence['verification_present'] else 'no'})"
            ),
            (
                f"  latency trend: {performance['state']} "
                f"({performance['series']}, n={performance['sample_count']}{baseline_text})"
            ),
            f"  reasoning quality: {reasoning['status']} — {reasoning['reason']}",
        )
    )


def _runtime_status() -> dict[str, Any]:
    from . import main as _main

    return _main.RUNTIME_STATUS


def append_perf_record(record: dict[str, Any]) -> bool:
    sanitized = sanitize_perf_record(record)
    if sanitized is None:
        _record_rejection()
        return False
    with _PERF_BUFFER_LOCK:
        PERF_BUFFER.append(sanitized)
        # Tool and per-round receipts are high-frequency diagnostics. Buffer
        # them until the next chat/run boundary or a bounded batch fills so
        # telemetry does not add an fsync-shaped delay between model rounds.
        should_flush = len(PERF_BUFFER) >= 12 or sanitized.get("event") not in {
            "tool",
            "model_round",
        }
    if should_flush:
        flush_perf_records()
    return True


def flush_perf_records() -> bool:
    global PERF_BUFFER

    # Serialize flushers, but release the producer lock before disk I/O. The
    # object swap gives this flush an immutable batch while new events continue
    # accumulating in a fresh buffer.
    with _PERF_FLUSH_LOCK:
        with _PERF_BUFFER_LOCK:
            if not PERF_BUFFER:
                return True
            pending = PERF_BUFFER
            PERF_BUFFER = []
        safe_pending = [
            sanitized
            for record in pending
            if (sanitized := sanitize_perf_record(record)) is not None
        ]
        if len(safe_pending) != len(pending):
            _record_rejection()
        if not safe_pending:
            return True
        try:
            _private_perf_store().append({"kind": "perf_batch", "records": safe_pending})
        except Exception as exc:
            # Telemetry must never break the runtime. Preserve a bounded newest
            # suffix so a later writable flush can recover without a memory leak.
            with _PERF_BUFFER_LOCK:
                PERF_BUFFER = [*safe_pending, *PERF_BUFFER][-_PERF_BUFFER_MAX_RECORDS:]
            try:
                _runtime_status()["perf_store"] = {
                    "status": "degraded",
                    "error_type": type(exc).__name__,
                    "buffered": len(PERF_BUFFER),
                }
            except Exception:
                pass
            return False
        try:
            _runtime_status()["perf_store"] = {
                "status": "ready",
                "buffered": len(PERF_BUFFER),
            }
        except Exception:
            pass
        return True


def record_perf_event(event: str, **fields: Any) -> None:
    if "guardrail_reasons" in fields:
        reasons = fields.pop("guardrail_reasons")
        fields["guardrail_reason_count"] = len(reasons) if isinstance(reasons, (list, tuple)) else 0
    record = sanitize_perf_record({"event": event, "timestamp": time.time(), **fields})
    if record is None:
        _record_rejection()
        return
    _runtime_status()[f"last_{event}_metrics"] = record
    append_perf_record(record)


def record_chat_metrics(cfg: Config, chunk: Any) -> None:
    metric_names = (
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    )
    metrics = {name: get_attr(chunk, name, None) for name in metric_names}
    if not any(value is not None for value in metrics.values()):
        return
    record = {
        "event": "chat",
        "timestamp": time.time(),
        "model": cfg.model,
        "cloud": cfg.cloud,
        "keep_alive": cfg.keep_alive,
        **metrics,
    }
    _runtime_status()["last_metrics"] = record
    append_perf_record(record)


def log_embed_perf(record: dict[str, Any], *, source: str, backend: str | None = None) -> None:
    payload = _sanitize_embed_record(record, source=source, backend=backend)
    try:
        _private_perf_store().append({"kind": "embed", "record": payload})
    except (OSError, TypeError, ValueError):
        pass


def _load_legacy_perf_history(limit: int) -> list[dict[str, Any]]:
    if not PERF_HISTORY_FILE.exists():
        return []
    line_limit = max(1, int(limit))
    if os.name == "posix":
        try:
            os.chmod(PERF_HISTORY_FILE, 0o600)
        except OSError:
            pass
    try:
        with PERF_HISTORY_FILE.open("rb") as handle:
            handle.seek(0, 2)
            start = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            bytes_read = 0
            while start > 0 and newline_count < line_limit + 1 and bytes_read < _PERF_TAIL_MAX_BYTES:
                chunk_size = min(_PERF_TAIL_BLOCK_BYTES, start, _PERF_TAIL_MAX_BYTES - bytes_read)
                start -= chunk_size
                handle.seek(start)
                chunk = handle.read(chunk_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
                bytes_read += len(chunk)
            starts_on_boundary = start == 0
            if start > 0:
                handle.seek(start - 1)
                starts_on_boundary = handle.read(1) == b"\n"
    except OSError:
        return []
    payload = b"".join(reversed(chunks))
    if not starts_on_boundary:
        _partial, separator, payload = payload.partition(b"\n")
        if not separator:
            return []
    lines = payload.decode("utf-8", errors="replace").splitlines()[-line_limit:]
    rows: list[dict[str, Any]] = []
    for raw in lines:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            sanitized = sanitize_perf_record(item)
            if sanitized is not None:
                rows.append(sanitized)
    return rows


def load_perf_history(limit: int = 8) -> list[dict[str, Any]]:
    line_limit = max(1, int(limit))
    private_rows: list[dict[str, Any]] = []
    try:
        events = _private_perf_store().read_events(limit=max(32, line_limit))
    except OSError:
        events = []
    for event in events:
        if event.get("kind") != "perf_batch":
            continue
        records = event.get("records")
        if isinstance(records, list):
            private_rows.extend(
                sanitized
                for item in records
                if isinstance(item, dict)
                and (sanitized := sanitize_perf_record(item)) is not None
            )
    private_rows = private_rows[-line_limit:]
    if len(private_rows) >= line_limit:
        return private_rows
    legacy = _load_legacy_perf_history(line_limit - len(private_rows))
    return [*legacy, *private_rows][-line_limit:]


def format_duration_ns(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "?"
    if amount <= 0:
        return "0 ms"
    return f"{amount / 1_000_000:.1f} ms"


def show_perf_summary() -> None:
    recent = load_perf_history(limit=24)
    chats = [item for item in recent if item.get("event", "chat") == "chat"]
    tool_events = [item for item in recent if item.get("event") in {"tool", "compaction"}]
    rs = _runtime_status()
    latest = rs.get("last_metrics") or (chats[-1] if chats else None)
    if not latest:
        if not tool_events:
            show_info("No performance metrics captured yet. Run a chat turn first.")
            return
        show_info("No chat timing metrics captured yet. Showing runtime events only.")
    else:
        console.print("[bold primary]Latest latency[/]")
        console.print(
            f"  total {format_duration_ns(latest.get('total_duration'))}"
            f" | load {format_duration_ns(latest.get('load_duration'))}"
            f" | prompt {format_duration_ns(latest.get('prompt_eval_duration'))}"
            f" | eval {format_duration_ns(latest.get('eval_duration'))}"
        )
        console.print(
            f"  tokens in {latest.get('prompt_eval_count', '?')}"
            f" | out {latest.get('eval_count', '?')}"
            f" | keep_alive {latest.get('keep_alive', '?')}"
        )
    if chats:
        console.print("[bold primary]Recent[/]")
        for item in chats[-5:]:
            model_label = str(item.get("model", "?")).encode("ascii", "replace").decode("ascii")
            console.print(
                Text(
                    f"  {model_label}: "
                    f"total {format_duration_ns(item.get('total_duration'))}, "
                    f"load {format_duration_ns(item.get('load_duration'))}, "
                    f"prompt {format_duration_ns(item.get('prompt_eval_duration'))}, "
                    f"eval {format_duration_ns(item.get('eval_duration'))}"
                )
            )
    if tool_events:
        console.print("[bold primary]Runtime events[/]")
        for item in tool_events[-5:]:
            if item.get("event") == "tool":
                console.print(
                    Text(
                        f"  tool {item.get('tool', '?')}: "
                        f"{item.get('status', '?')} in {item.get('duration_ms', '?')} ms"
                    )
                )
            else:
                console.print(
                    Text(
                        f"  compaction: {item.get('duration_ms', '?')} ms, "
                        f"{item.get('messages_compacted', '?')} messages"
                    )
                )
