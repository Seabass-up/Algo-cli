"""Persistent thread records for Agent Block and multi-agent runs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config


THREADS_FILE_NAME = "agent_threads.json"
THREADS_SCHEMA_VERSION = 1
MAX_THREAD_RECORDS = 100
MAX_THREAD_TURNS = 16
MAX_THREAD_OUTPUT_CHARS = 12_000
_VALID_STATUSES = frozenset({"queued", "running", "complete", "partial", "failed", "cancelled"})


def threads_path() -> Path:
    """Resolve lazily so test/runtime config-directory changes are honored."""

    return config.CONFIG_DIR / THREADS_FILE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _empty_store() -> dict[str, Any]:
    return {"version": THREADS_SCHEMA_VERSION, "threads": []}


def _normalize_record(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    thread_id = _clean_text(raw.get("id"), 64)
    if not thread_id:
        return None
    status = _clean_text(raw.get("status"), 24).lower()
    if status not in _VALID_STATUSES:
        status = "failed"
    turns = raw.get("turns", [])
    blocks = raw.get("blocks", [])
    children = raw.get("children", [])
    return {
        "id": thread_id,
        "parent_id": _clean_text(raw.get("parent_id"), 64),
        "title": _clean_text(raw.get("title"), 120),
        "task": _clean_text(raw.get("task"), 8_000),
        "role": _clean_text(raw.get("role"), 80) or "orchestrator",
        "pipeline": _clean_text(raw.get("pipeline"), 80) or "default",
        "model": _clean_text(raw.get("model"), 120),
        "status": status,
        "created_at": _clean_text(raw.get("created_at"), 64) or _now(),
        "updated_at": _clean_text(raw.get("updated_at"), 64) or _now(),
        "output": _clean_text(raw.get("output"), MAX_THREAD_OUTPUT_CHARS),
        "error": _clean_text(raw.get("error"), 2_000),
        "children": [str(item)[:64] for item in children if str(item).strip()]
        if isinstance(children, list)
        else [],
        "turns": [item for item in turns[-MAX_THREAD_TURNS:] if isinstance(item, dict)]
        if isinstance(turns, list)
        else [],
        "blocks": [item for item in blocks if isinstance(item, dict)]
        if isinstance(blocks, list)
        else [],
    }


def load_threads(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or threads_path()
    loaded = config._load_json_file(target, _empty_store())
    if not isinstance(loaded, dict) or loaded.get("version") != THREADS_SCHEMA_VERSION:
        return []
    records = loaded.get("threads", [])
    if not isinstance(records, list):
        return []
    normalized = [_normalize_record(record) for record in records]
    return [record for record in normalized if record is not None]


def _mutate(
    callback: Callable[[list[dict[str, Any]]], Any],
    *,
    path: Path | None = None,
) -> Any:
    target = path or threads_path()
    with config._exclusive_state_lock(target):
        records = load_threads(target)
        result = callback(records)
        records.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        del records[MAX_THREAD_RECORDS:]
        payload = {"version": THREADS_SCHEMA_VERSION, "threads": records}
        config._atomic_write_text(target, json.dumps(payload, indent=2))
        return result


def _new_id(existing: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate


def create_thread(
    task: str,
    *,
    role: str = "orchestrator",
    pipeline: str = "default",
    model: str = "",
    parent_id: str = "",
    title: str = "",
    status: str = "queued",
    start_turn: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid agent thread status: {status}")
    clean_task = _clean_text(task, 8_000)
    clean_title = _clean_text(title, 120) or " ".join(clean_task.split())[:80] or "Agent task"

    def add(records: list[dict[str, Any]]) -> dict[str, Any]:
        thread_id = _new_id({record["id"] for record in records})
        now = _now()
        record = {
            "id": thread_id,
            "parent_id": _clean_text(parent_id, 64),
            "title": clean_title,
            "task": clean_task,
            "role": _clean_text(role, 80) or "orchestrator",
            "pipeline": _clean_text(pipeline, 80) or "default",
            "model": _clean_text(model, 120),
            "status": "running" if start_turn else status,
            "created_at": now,
            "updated_at": now,
            "output": "",
            "error": "",
            "children": [],
            "turns": (
                [{"task": clean_task, "status": "running", "started_at": now, "output": ""}]
                if start_turn
                else []
            ),
            "blocks": [],
        }
        records.append(record)
        if parent_id:
            for parent in records:
                if parent["id"] == parent_id and thread_id not in parent["children"]:
                    parent["children"].append(thread_id)
                    parent["updated_at"] = now
                    break
        return dict(record)

    return _mutate(add, path=path)


def update_thread(thread_id: str, *, path: Path | None = None, **changes: Any) -> dict[str, Any]:
    allowed = {"title", "task", "role", "pipeline", "model", "status", "output", "error", "blocks"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported agent thread fields: {', '.join(sorted(unknown))}")
    if "status" in changes and changes["status"] not in _VALID_STATUSES:
        raise ValueError(f"Invalid agent thread status: {changes['status']}")

    def update(records: list[dict[str, Any]]) -> dict[str, Any]:
        for record in records:
            if record["id"] != thread_id:
                continue
            for key, value in changes.items():
                if key == "output":
                    record[key] = _clean_text(value, MAX_THREAD_OUTPUT_CHARS)
                elif key == "error":
                    record[key] = _clean_text(value, 2_000)
                elif key == "blocks":
                    record[key] = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
                else:
                    record[key] = _clean_text(value, 8_000 if key == "task" else 120)
            record["updated_at"] = _now()
            return dict(record)
        raise KeyError(f"Unknown agent thread '{thread_id}'.")

    return _mutate(update, path=path)


def begin_turn(
    thread_id: str,
    task: str,
    *,
    pipeline: str | None = None,
    model: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    def begin(records: list[dict[str, Any]]) -> dict[str, Any]:
        for record in records:
            if record["id"] != thread_id:
                continue
            now = _now()
            record["status"] = "running"
            record["updated_at"] = now
            if not record["task"]:
                record["task"] = _clean_text(task, 8_000)
            if pipeline:
                record["pipeline"] = _clean_text(pipeline, 80)
            if model:
                record["model"] = _clean_text(model, 120)
            record["turns"].append(
                {"task": _clean_text(task, 8_000), "status": "running", "started_at": now, "output": ""}
            )
            record["turns"] = record["turns"][-MAX_THREAD_TURNS:]
            return dict(record)
        raise KeyError(f"Unknown agent thread '{thread_id}'.")

    return _mutate(begin, path=path)


def finish_turn(
    thread_id: str,
    *,
    status: str,
    output: str = "",
    error: str = "",
    blocks: list[dict[str, Any]] | None = None,
    pipeline: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    if status not in _VALID_STATUSES - {"queued", "running"}:
        raise ValueError(f"Invalid terminal agent thread status: {status}")

    def finish(records: list[dict[str, Any]]) -> dict[str, Any]:
        for record in records:
            if record["id"] != thread_id:
                continue
            now = _now()
            record["status"] = status
            record["updated_at"] = now
            record["output"] = _clean_text(output, MAX_THREAD_OUTPUT_CHARS)
            record["error"] = _clean_text(error, 2_000)
            if blocks is not None:
                record["blocks"] = [item for item in blocks if isinstance(item, dict)]
            if pipeline:
                record["pipeline"] = _clean_text(pipeline, 80)
            if record["turns"]:
                turn = record["turns"][-1]
                if turn.get("status") == "running":
                    turn.update(
                        {
                            "status": status,
                            "finished_at": now,
                            "output": _clean_text(output, MAX_THREAD_OUTPUT_CHARS),
                            "error": _clean_text(error, 2_000),
                        }
                    )
            return dict(record)
        raise KeyError(f"Unknown agent thread '{thread_id}'.")

    return _mutate(finish, path=path)


def resolve_thread(thread_ref: str, *, path: Path | None = None) -> dict[str, Any]:
    ref = _clean_text(thread_ref, 64).lower()
    if not ref:
        raise KeyError("Agent thread ID is required.")
    records = load_threads(path)
    exact = [record for record in records if record["id"].lower() == ref]
    if exact:
        return exact[0]
    matches = [record for record in records if record["id"].lower().startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise KeyError(f"Agent thread prefix '{thread_ref}' is ambiguous.")
    raise KeyError(f"Unknown agent thread '{thread_ref}'. Use /agent threads to list runs.")


def list_threads(*, limit: int = 20, path: Path | None = None) -> list[dict[str, Any]]:
    return load_threads(path)[: max(1, min(int(limit), 100))]


def context_handoff(record: dict[str, Any], *, limit: int = 8_000) -> str:
    """Produce bounded, explicit evidence context for resume/fork operations."""

    blocks = record.get("blocks", [])
    block_lines = []
    if isinstance(blocks, list):
        for block in blocks[-8:]:
            if not isinstance(block, dict):
                continue
            code = f" ({block.get('status_code')})" if block.get("status_code") else ""
            block_lines.append(
                f"- {block.get('role', '?')}: {block.get('status', '?')}{code}"
            )
    text = (
        f"Thread: {record.get('id', '?')}\n"
        f"Original task: {record.get('task', '')}\n"
        f"Last status: {record.get('status', '?')}\n"
        f"Block evidence:\n{chr(10).join(block_lines) or '- none'}\n\n"
        f"Last output:\n{record.get('output', '') or '(no output)'}"
    )
    return text[:limit]
