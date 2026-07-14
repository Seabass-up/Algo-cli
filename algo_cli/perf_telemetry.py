"""Performance event buffering and JSONL persistence."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

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


def append_perf_record(record: dict[str, Any]) -> None:
    with _PERF_BUFFER_LOCK:
        PERF_BUFFER.append(record)
        # Tool and per-round receipts are high-frequency diagnostics. Buffer
        # them until the next chat/run boundary or a bounded batch fills so
        # telemetry does not add an fsync-shaped delay between model rounds.
        should_flush = len(PERF_BUFFER) >= 12 or record.get("event") not in {
            "tool",
            "model_round",
        }
    if should_flush:
        flush_perf_records()


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
        try:
            _private_perf_store().append({"kind": "perf_batch", "records": pending})
        except Exception as exc:
            # Telemetry must never break the runtime. Preserve a bounded newest
            # suffix so a later writable flush can recover without a memory leak.
            with _PERF_BUFFER_LOCK:
                PERF_BUFFER = [*pending, *PERF_BUFFER][-_PERF_BUFFER_MAX_RECORDS:]
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
    record = {"event": event, "timestamp": time.time(), **fields}
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
    payload: dict[str, Any] = {"timestamp": time.time(), "source": source}
    if backend is not None:
        payload["backend"] = backend
    payload.update(record)
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
            rows.append(item)
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
            private_rows.extend(item for item in records if isinstance(item, dict))
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
