"""Persistent thread records for Agent Block and multi-agent runs."""

from __future__ import annotations

import json
import hmac
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config
from .grace_memory_receipts import (
    ElsieReceiptAuthority,
    ElsieReceiptError,
    ReceiptNamespace,
    advance_elsie_store_anchor,
    load_elsie_store_anchor,
    require_elsie_store_anchor,
)


THREADS_FILE_NAME = "agent_threads.json"
THREADS_SCHEMA_VERSION = 5
_COMPATIBLE_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, THREADS_SCHEMA_VERSION})
MAX_THREAD_RECORDS = 100
MAX_THREAD_TURNS = 16
MAX_THREAD_OUTPUT_CHARS = 12_000
MAX_BLOCK_CONTEXT_CHARS = 3_000
MAX_THREAD_STORE_BYTES = 16 * 1024 * 1024
_VALID_STATUSES = frozenset({"queued", "running", "complete", "partial", "failed", "cancelled"})
_CONTENT_RECEIPT_RE = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")


def threads_path() -> Path:
    """Resolve lazily so test/runtime config-directory changes are honored."""

    return config.CONFIG_DIR / THREADS_FILE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _content_receipt(
    authority: ElsieReceiptAuthority | None,
    namespace: ReceiptNamespace,
    label: str,
    value: Any,
) -> str:
    if authority is None:
        raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
    text = str(value or "")
    if not text:
        return ""
    return authority.receipt(
        namespace,
        {"field": label, "content": text},
    )


def _retained_receipt(
    raw: dict[str, Any],
    key: str,
    label: str,
    value: str,
    *,
    authority: ElsieReceiptAuthority | None,
    namespace: ReceiptNamespace,
) -> str:
    """Project plaintext once and preserve only validated keyed receipts thereafter."""

    if authority is None:
        raise ElsieReceiptError("protected agent thread receipt authority is unavailable")

    if value:
        return _content_receipt(authority, namespace, label, value)
    existing = str(raw.get(key) or "").strip()
    return existing if _CONTENT_RECEIPT_RE.fullmatch(existing) else ""


def _empty_store() -> dict[str, Any]:
    return {"version": THREADS_SCHEMA_VERSION, "threads": []}


def _store_subject(path: Path) -> str:
    return os.path.abspath(os.fspath(path))


def _pending_store_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.elsie-pending")


def _pending_store_exists(path: Path) -> bool:
    pending = _pending_store_path(path)
    try:
        descriptor = pending.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ElsieReceiptError("protected agent thread recovery state is unsafe") from exc
    if (
        not stat.S_ISREG(descriptor.st_mode)
        or descriptor.st_nlink != 1
        or (hasattr(os, "getuid") and descriptor.st_uid != os.getuid())
        or descriptor.st_mode & 0o022
    ):
        raise ElsieReceiptError("protected agent thread recovery state is unsafe")
    return True


def _receipt_authority(
    authority: ElsieReceiptAuthority | None,
    *,
    existing_only: bool,
) -> ElsieReceiptAuthority:
    if authority is not None:
        return authority
    if existing_only:
        return ElsieReceiptAuthority.from_existing_key_store()
    return ElsieReceiptAuthority.from_key_store()


def _protected_record(raw: Any) -> bool:
    return isinstance(raw, dict) and raw.get("protected_memory_authority") is True


def _require_receipt_shape(raw: dict[str, Any], key: str) -> None:
    value = raw.get(key, "")
    if not isinstance(value, str) or (value and _CONTENT_RECEIPT_RE.fullmatch(value) is None):
        raise ElsieReceiptError("protected agent thread receipt is invalid")


def _require_projected_record(raw: dict[str, Any]) -> None:
    """Reject v4 protected rows that reintroduce bodies or malformed receipts."""

    if raw.get("title") not in {None, "", "Protected Agent thread"}:
        raise ElsieReceiptError("protected agent thread title is not projected")
    for key in ("task", "output", "error"):
        if raw.get(key) not in {None, ""}:
            raise ElsieReceiptError("protected agent thread body is not projected")
    for key in ("task_receipt", "output_receipt", "error_receipt"):
        _require_receipt_shape(raw, key)
    workspace = raw.get("workspace", {})
    if isinstance(workspace, dict) and set(workspace).intersection(
        {
            "cwd",
            "workspace_root",
            "repository_root",
            "git_common_dir",
            "branch",
            "status",
            "error",
            "status_digest",
            "tracked_diff_digest",
            "untracked_digest",
        }
    ):
        raise ElsieReceiptError("protected agent workspace is not projected")
    if isinstance(workspace, dict):
        for key in ("status_receipt", "tracked_diff_receipt", "untracked_receipt"):
            _require_receipt_shape(workspace, key)
    turns = raw.get("turns", [])
    if not isinstance(turns, list):
        raise ElsieReceiptError("protected agent turns are invalid")
    for turn in turns:
        if not isinstance(turn, dict):
            raise ElsieReceiptError("protected agent turn is invalid")
        for key in ("task", "output", "error"):
            if turn.get(key) not in {None, ""}:
                raise ElsieReceiptError("protected agent turn body is not projected")
        for key in ("task_receipt", "output_receipt", "error_receipt"):
            _require_receipt_shape(turn, key)
    blocks = raw.get("blocks", [])
    if not isinstance(blocks, list):
        raise ElsieReceiptError("protected agent blocks are invalid")
    for block in blocks:
        if not isinstance(block, dict):
            raise ElsieReceiptError("protected agent block is invalid")
        for key in (
            "status_reason",
            "context_output",
            "verification_warning",
            "git_status",
            "status_digest",
            "tracked_diff_digest",
            "untracked_digest",
        ):
            if block.get(key) not in {None, ""}:
                raise ElsieReceiptError("protected agent block body is not projected")
        writes = block.get("successful_writes")
        if writes not in (None, [], ()):
            raise ElsieReceiptError("protected agent write paths are not projected")
        for key in (
            "status_reason_receipt",
            "context_output_receipt",
            "verification_warning_receipt",
            "status_receipt",
            "tracked_diff_receipt",
            "untracked_receipt",
        ):
            _require_receipt_shape(block, key)
    checkpoint = raw.get("checkpoint", {})
    if isinstance(checkpoint, dict) and checkpoint:
        if checkpoint.get("uncertain_mutation_steps") not in (None, [], ()):
            raise ElsieReceiptError("protected agent checkpoint is not projected")
        _require_receipt_shape(checkpoint, "uncertain_mutation_steps_receipt")


def _normalize_workspace(
    raw: Any,
    *,
    protected: bool = False,
    authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any]:
    """Bound persisted workspace evidence without retaining diff contents."""

    if not isinstance(raw, dict):
        return {}
    if protected:
        if authority is None:
            raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
        protected_workspace: dict[str, Any] = {
            key: _clean_text(raw.get(key), 64)
            for key in (
                "head",
                "initial_head",
                "captured_at",
            )
            if raw.get(key) is not None
        }
        for source_key, receipt_key, label in (
            ("status_digest", "status_receipt", "workspace_status_digest"),
            (
                "tracked_diff_digest",
                "tracked_diff_receipt",
                "workspace_tracked_diff_digest",
            ),
            (
                "untracked_digest",
                "untracked_receipt",
                "workspace_untracked_digest",
            ),
        ):
            protected_workspace[receipt_key] = _retained_receipt(
                raw,
                receipt_key,
                label,
                _clean_text(raw.get(source_key), 64),
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_CHECKPOINT,
            )
        protected_workspace["available"] = bool(raw.get("available", False))
        for key in ("clean", "is_linked_worktree"):
            if key in raw:
                protected_workspace[key] = bool(raw.get(key))
        if "untracked_total" in raw:
            try:
                protected_workspace["untracked_total"] = max(
                    0,
                    min(int(raw.get("untracked_total") or 0), 1_000_000),
                )
            except (TypeError, ValueError):
                protected_workspace["untracked_total"] = 0
        if not protected_workspace.get("initial_head") and protected_workspace.get("head"):
            protected_workspace["initial_head"] = protected_workspace["head"]
        return protected_workspace
    text_limits = {
        "cwd": 2_000,
        "workspace_root": 2_000,
        "repository_root": 2_000,
        "git_common_dir": 2_000,
        "branch": 240,
        "head": 64,
        "initial_head": 64,
        "status": 4_000,
        "status_digest": 64,
        "tracked_diff_digest": 64,
        "untracked_digest": 64,
        "captured_at": 64,
        "error": 1_000,
    }
    workspace: dict[str, Any] = {
        key: _clean_text(raw.get(key), limit) for key, limit in text_limits.items() if raw.get(key) is not None
    }
    try:
        untracked_total = int(raw.get("untracked_total") or 0)
    except (TypeError, ValueError):
        untracked_total = 0
    workspace["available"] = bool(raw.get("available", False))
    if "clean" in raw:
        workspace["clean"] = bool(raw.get("clean"))
    if "is_linked_worktree" in raw:
        workspace["is_linked_worktree"] = bool(raw.get("is_linked_worktree"))
    if "untracked_total" in raw:
        workspace["untracked_total"] = max(0, min(untracked_total, 1_000_000))
    if not workspace.get("initial_head") and workspace.get("head"):
        workspace["initial_head"] = workspace["head"]
    return workspace


def _merge_workspace(
    previous: Any,
    current: Any,
    *,
    protected: bool = False,
    authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any]:
    before = _normalize_workspace(
        previous,
        protected=protected,
        authority=authority,
    )
    after = _normalize_workspace(
        current,
        protected=protected,
        authority=authority,
    )
    if not after:
        return before
    after["initial_head"] = before.get("initial_head") or before.get("head") or after.get("head", "")
    return after


def _normalize_run_contract(raw: Any) -> dict[str, Any]:
    """Persist only the private structural link to the immutable contract."""

    if not isinstance(raw, dict):
        return {}
    value = {
        "contract_id": _clean_text(raw.get("contract_id"), 96),
        "digest": _clean_text(raw.get("digest"), 64),
        "run_nonce": _clean_text(raw.get("run_nonce"), 64),
        "mode": _clean_text(raw.get("mode"), 16),
        "approval_mode": _clean_text(raw.get("approval_mode"), 16),
        "journal_file": _clean_text(raw.get("journal_file"), 128),
    }
    return {key: item for key, item in value.items() if item}


def _normalize_checkpoint(
    raw: Any,
    *,
    protected: bool = False,
    authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    try:
        next_block = int(raw.get("next_block_ordinal") or 0)
        last_verified = int(raw.get("last_verified_sequence", -1))
    except (TypeError, ValueError):
        return {}
    uncertain = raw.get("uncertain_mutation_steps", [])
    normalized_uncertain = (
        [_clean_text(item, 128) for item in uncertain[:64] if str(item).strip()] if isinstance(uncertain, list) else []
    )
    value = {
        "next_block_ordinal": max(0, min(next_block, 32)),
        "last_verified_sequence": max(-1, last_verified),
        "uncertain_mutation_steps": [] if protected else normalized_uncertain,
        "uncertain_mutation_step_count": len(normalized_uncertain) if protected else 0,
        "uncertain_mutation_steps_receipt": "",
        "terminal": bool(raw.get("terminal", False)),
        "terminal_status": _clean_text(raw.get("terminal_status"), 24),
    }
    if protected:
        if authority is None:
            raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
        value["uncertain_mutation_steps_receipt"] = _retained_receipt(
            raw,
            "uncertain_mutation_steps_receipt",
            "checkpoint_uncertain_mutations",
            json.dumps(normalized_uncertain, separators=(",", ":")) if normalized_uncertain else "",
            authority=authority,
            namespace=ReceiptNamespace.AGENT_THREAD_CHECKPOINT,
        )
    return value


def _normalize_block(
    raw: Any,
    *,
    protected: bool = False,
    authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if protected and authority is None:
        raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
    context_output = _clean_text(raw.get("context_output"), MAX_BLOCK_CONTEXT_CHARS)
    status_reason = _clean_text(raw.get("status_reason"), 2_000)
    verification_warning = _clean_text(raw.get("verification_warning"), 2_000)
    value: dict[str, Any] = {
        "role": _clean_text(raw.get("role"), 80),
        "status": _clean_text(raw.get("status"), 24),
        "status_code": _clean_text(raw.get("status_code"), 80),
        "status_reason": "" if protected else status_reason,
        "status_reason_receipt": (
            _retained_receipt(
                raw,
                "status_reason_receipt",
                "block_status_reason",
                status_reason,
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_OUTPUT,
            )
            if protected
            else ""
        ),
        "context_output": "" if protected else context_output,
        "context_output_receipt": (
            _retained_receipt(
                raw,
                "context_output_receipt",
                "block_context",
                context_output,
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_CONTEXT,
            )
            if protected
            else ""
        ),
        "verification_warning": "" if protected else verification_warning,
        "verification_warning_receipt": (
            _retained_receipt(
                raw,
                "verification_warning_receipt",
                "block_verification_warning",
                verification_warning,
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_OUTPUT,
            )
            if protected
            else ""
        ),
        "git_head": _clean_text(raw.get("git_head"), 64),
        "git_status": "" if protected else _clean_text(raw.get("git_status"), 4_000),
        "status_digest": "" if protected else _clean_text(raw.get("status_digest"), 64),
        "status_receipt": (
            _retained_receipt(
                raw,
                "status_receipt",
                "block_status_digest",
                _clean_text(raw.get("status_digest"), 64),
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_CHECKPOINT,
            )
            if protected
            else ""
        ),
        "tracked_diff_digest": ("" if protected else _clean_text(raw.get("tracked_diff_digest"), 64)),
        "tracked_diff_receipt": (
            _retained_receipt(
                raw,
                "tracked_diff_receipt",
                "block_tracked_diff_digest",
                _clean_text(raw.get("tracked_diff_digest"), 64),
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_CHECKPOINT,
            )
            if protected
            else ""
        ),
        "untracked_digest": ("" if protected else _clean_text(raw.get("untracked_digest"), 64)),
        "untracked_receipt": (
            _retained_receipt(
                raw,
                "untracked_receipt",
                "block_untracked_digest",
                _clean_text(raw.get("untracked_digest"), 64),
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_CHECKPOINT,
            )
            if protected
            else ""
        ),
        "git_clean": bool(raw.get("git_clean", False)),
    }
    try:
        value["tool_calls"] = max(0, min(int(raw.get("tool_calls") or 0), 100_000))
        value["duration_ms"] = max(
            0.0,
            min(float(raw.get("duration_ms") or 0.0), 86_400_000.0),
        )
    except (TypeError, ValueError):
        value["tool_calls"] = 0
        value["duration_ms"] = 0.0
    writes = raw.get("successful_writes", [])
    normalized_writes = (
        [_clean_text(item, 2_000) for item in writes[:64] if str(item).strip()] if isinstance(writes, list) else []
    )
    value["successful_writes"] = [] if protected else normalized_writes
    value["successful_write_count"] = len(normalized_writes) if protected else 0
    return value


def _normalize_turn(
    raw: Any,
    *,
    protected: bool,
    authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if protected and authority is None:
        raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
    task = _clean_text(raw.get("task"), 8_000)
    output = _clean_text(raw.get("output"), MAX_THREAD_OUTPUT_CHARS)
    error = _clean_text(raw.get("error"), 2_000)
    value: dict[str, Any] = {
        "status": _clean_text(raw.get("status"), 24),
        "started_at": _clean_text(raw.get("started_at"), 64),
        "finished_at": _clean_text(raw.get("finished_at"), 64),
        "task": "" if protected else task,
        "output": "" if protected else output,
        "error": "" if protected else error,
    }
    if protected:
        value.update(
            {
                "task_receipt": _retained_receipt(
                    raw,
                    "task_receipt",
                    "turn_task",
                    task,
                    authority=authority,
                    namespace=ReceiptNamespace.AGENT_THREAD_CONTEXT,
                ),
                "output_receipt": _retained_receipt(
                    raw,
                    "output_receipt",
                    "turn_output",
                    output,
                    authority=authority,
                    namespace=ReceiptNamespace.AGENT_THREAD_OUTPUT,
                ),
                "error_receipt": _retained_receipt(
                    raw,
                    "error_receipt",
                    "turn_error",
                    error,
                    authority=authority,
                    namespace=ReceiptNamespace.AGENT_THREAD_OUTPUT,
                ),
            }
        )
    return value


def _normalize_record(
    raw: Any,
    *,
    force_protected: bool = False,
    authority: ElsieReceiptAuthority | None = None,
) -> dict[str, Any] | None:
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
    protected = force_protected or raw.get("protected_memory_authority") is True
    if protected and authority is None:
        raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
    task = _clean_text(raw.get("task"), 8_000)
    output = _clean_text(raw.get("output"), MAX_THREAD_OUTPUT_CHARS)
    error = _clean_text(raw.get("error"), 2_000)
    normalized_turns = [
        normalized
        for item in (turns[-MAX_THREAD_TURNS:] if isinstance(turns, list) else [])
        if (
            normalized := _normalize_turn(
                item,
                protected=protected,
                authority=authority,
            )
        )
        is not None
    ]
    return {
        "id": thread_id,
        "parent_id": _clean_text(raw.get("parent_id"), 64),
        "title": "Protected Agent thread" if protected else _clean_text(raw.get("title"), 120),
        "task": "" if protected else task,
        "task_receipt": (
            _retained_receipt(
                raw,
                "task_receipt",
                "record_task",
                task,
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_CONTEXT,
            )
            if protected
            else ""
        ),
        "role": _clean_text(raw.get("role"), 80) or "orchestrator",
        "pipeline": _clean_text(raw.get("pipeline"), 80) or "default",
        "model": _clean_text(raw.get("model"), 120),
        "status": status,
        "created_at": _clean_text(raw.get("created_at"), 64) or _now(),
        "updated_at": _clean_text(raw.get("updated_at"), 64) or _now(),
        "output": "" if protected else output,
        "output_receipt": (
            _retained_receipt(
                raw,
                "output_receipt",
                "record_output",
                output,
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_OUTPUT,
            )
            if protected
            else ""
        ),
        "error": "" if protected else error,
        "error_receipt": (
            _retained_receipt(
                raw,
                "error_receipt",
                "record_error",
                error,
                authority=authority,
                namespace=ReceiptNamespace.AGENT_THREAD_OUTPUT,
            )
            if protected
            else ""
        ),
        "protected_memory_authority": protected,
        "children": ([str(item)[:64] for item in children if str(item).strip()] if isinstance(children, list) else []),
        "turns": normalized_turns,
        "blocks": (
            [
                normalized
                for item in blocks
                if (
                    normalized := _normalize_block(
                        item,
                        protected=protected,
                        authority=authority,
                    )
                )
                is not None
            ]
            if isinstance(blocks, list)
            else []
        ),
        "workspace": _normalize_workspace(
            raw.get("workspace"),
            protected=protected,
            authority=authority,
        ),
        "run_contract": _normalize_run_contract(raw.get("run_contract")),
        "checkpoint": _normalize_checkpoint(
            raw.get("checkpoint"),
            protected=protected,
            authority=authority,
        ),
    }


def _store_payload(
    records: list[dict[str, Any]],
    *,
    authority: ElsieReceiptAuthority | None,
    sequence: int = 0,
    previous_store_receipt: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"version": THREADS_SCHEMA_VERSION, "threads": records}
    if any(_protected_record(record) for record in records):
        if authority is None:
            raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or (sequence == 1 and previous_store_receipt)
            or (sequence > 1 and _CONTENT_RECEIPT_RE.fullmatch(previous_store_receipt) is None)
        ):
            raise ElsieReceiptError("protected agent thread store sequence is invalid")
        payload.update(
            {
                "receipt_binding": authority.binding.as_dict(),
                "store_sequence": sequence,
                "previous_store_receipt": previous_store_receipt,
            }
        )
        payload["store_receipt"] = authority.store_receipt(
            ReceiptNamespace.AGENT_THREAD_STORE,
            payload,
        )
    return payload


def _validate_protected_store(
    loaded: dict[str, Any],
    authority: ElsieReceiptAuthority,
) -> tuple[int, str]:
    if set(loaded) != {
        "version",
        "threads",
        "receipt_binding",
        "store_sequence",
        "previous_store_receipt",
        "store_receipt",
    }:
        raise ElsieReceiptError("protected agent thread store fields are invalid")
    if loaded.get("version") != THREADS_SCHEMA_VERSION:
        raise ElsieReceiptError("legacy protected agent thread store is unauthenticated")
    authority.require_binding(loaded.get("receipt_binding"))
    sequence = loaded.get("store_sequence")
    previous = loaded.get("previous_store_receipt")
    receipt = loaded.get("store_receipt")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or not isinstance(previous, str)
        or (sequence == 1 and previous)
        or (sequence > 1 and _CONTENT_RECEIPT_RE.fullmatch(previous) is None)
        or not isinstance(receipt, str)
        or _CONTENT_RECEIPT_RE.fullmatch(receipt) is None
    ):
        raise ElsieReceiptError("protected agent thread store sequence is invalid")
    unsigned = {key: value for key, value in loaded.items() if key != "store_receipt"}
    expected = authority.store_receipt(ReceiptNamespace.AGENT_THREAD_STORE, unsigned)
    if not hmac.compare_digest(receipt, expected):
        raise ElsieReceiptError("protected agent thread store authentication failed")
    return sequence, receipt


def _discard_pending_store(target: Path) -> None:
    pending = _pending_store_path(target)
    try:
        pending.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ElsieReceiptError("protected agent thread recovery state is unsafe") from exc


def _recover_pending_store(
    target: Path,
    authority: ElsieReceiptAuthority,
    *,
    previous_sequence: int,
    previous_store_receipt: str,
    anchor_store: Any | None = None,
    current_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    pending_path = _pending_store_path(target)
    pending = config._load_json_file(
        pending_path,
        None,
        preserve_corrupt=False,
    )
    if pending is None:
        return None, False
    if not isinstance(pending, dict):
        raise ElsieReceiptError("protected agent thread recovery state is invalid")
    sequence, receipt = _validate_protected_store(pending, authority)
    if (
        current_payload is not None
        and sequence == previous_sequence
        and hmac.compare_digest(receipt, previous_store_receipt)
    ):
        current_sequence, current_receipt = _validate_protected_store(
            current_payload,
            authority,
        )
        if (
            current_sequence != sequence
            or not hmac.compare_digest(current_receipt, receipt)
            or current_payload != pending
        ):
            raise ElsieReceiptError("protected agent thread recovery payload is inconsistent")
        require_elsie_store_anchor(
            authority,
            ReceiptNamespace.AGENT_THREAD_STORE,
            subject=_store_subject(target),
            sequence=sequence,
            store_receipt=receipt,
            anchor_store=anchor_store,
        )
        _discard_pending_store(target)
        return None, True
    if sequence != previous_sequence + 1 or pending.get("previous_store_receipt") != previous_store_receipt:
        raise ElsieReceiptError("protected agent thread recovery sequence is invalid")
    head = load_elsie_store_anchor(
        authority,
        ReceiptNamespace.AGENT_THREAD_STORE,
        subject=_store_subject(target),
        anchor_store=anchor_store,
    )
    if head is None:
        if previous_sequence != 0 or previous_store_receipt:
            raise ElsieReceiptError("protected agent thread recovery anchor is missing")
        _discard_pending_store(target)
        return None, True
    anchored_receipt = f"hmac-sha256:{head.head_digest}"
    if head.sequence == previous_sequence and hmac.compare_digest(anchored_receipt, previous_store_receipt):
        _discard_pending_store(target)
        return None, True
    if head.sequence != sequence or not hmac.compare_digest(
        anchored_receipt,
        receipt,
    ):
        raise ElsieReceiptError("protected agent thread recovery anchor is inconsistent")
    config._atomic_write_text(target, json.dumps(pending, indent=2))
    _discard_pending_store(target)
    return pending, True


def _publish_protected_store(
    target: Path,
    records: list[dict[str, Any]],
    authority: ElsieReceiptAuthority,
    *,
    previous_sequence: int,
    previous_store_receipt: str,
    anchor_store: Any | None = None,
) -> None:
    sequence = previous_sequence + 1
    payload = _store_payload(
        records,
        authority=authority,
        sequence=sequence,
        previous_store_receipt=previous_store_receipt,
    )
    store_receipt = str(payload["store_receipt"])
    pending_path = _pending_store_path(target)
    config._atomic_write_text(pending_path, json.dumps(payload, indent=2))
    try:
        advance_elsie_store_anchor(
            authority,
            ReceiptNamespace.AGENT_THREAD_STORE,
            subject=_store_subject(target),
            sequence=sequence,
            previous_store_receipt=previous_store_receipt,
            store_receipt=store_receipt,
            anchor_store=anchor_store,
        )
    except Exception:
        # A failed CAS can be a definite pre-CAS refusal or an unknown outcome.
        # Remove the stage only when the authenticated head still proves the
        # previous state; retain it when the desired head landed so explicit
        # startup recovery can finish the post-CAS publication window.
        try:
            head = load_elsie_store_anchor(
                authority,
                ReceiptNamespace.AGENT_THREAD_STORE,
                subject=_store_subject(target),
                anchor_store=anchor_store,
            )
        except ElsieReceiptError:
            raise
        anchored_receipt = f"hmac-sha256:{head.head_digest}" if head is not None else ""
        desired_landed = (
            head is not None and head.sequence == sequence and hmac.compare_digest(anchored_receipt, store_receipt)
        )
        previous_retained = (head is None and previous_sequence == 0 and not previous_store_receipt) or (
            head is not None
            and head.sequence == previous_sequence
            and hmac.compare_digest(anchored_receipt, previous_store_receipt)
        )
        if previous_retained and not desired_landed:
            _discard_pending_store(target)
        raise
    try:
        config._atomic_write_text(target, json.dumps(payload, indent=2))
    except Exception:
        # The authenticated pending payload is deliberately retained.  A
        # subsequent protected open can reconcile this exact post-CAS window.
        raise
    _discard_pending_store(target)


def _load_threads_unlocked(
    target: Path,
    *,
    protected: bool,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
    allow_first_protected_write: bool = False,
    allow_pending_recovery: bool = False,
) -> tuple[
    list[dict[str, Any]],
    ElsieReceiptAuthority | None,
    int,
    str,
    bool,
    bool,
]:
    target_missing = False
    if protected:
        try:
            payload = config._state_descriptor_payload(
                target,
                max_bytes=MAX_THREAD_STORE_BYTES,
            )
        except FileNotFoundError:
            target_missing = True
            loaded: Any = _empty_store()
        except OSError as exc:
            raise ElsieReceiptError("protected agent thread store is unavailable") from exc
        else:
            try:
                loaded = json.loads(payload.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ElsieReceiptError("protected agent thread store is malformed") from exc
    else:
        loaded = config._load_json_file(target, _empty_store(), preserve_corrupt=False)
    if not isinstance(loaded, dict) or loaded.get("version") not in _COMPATIBLE_SCHEMA_VERSIONS:
        loaded = _empty_store()
    records = loaded.get("threads", [])
    if not isinstance(records, list):
        records = []
    raw_protected = any(_protected_record(record) for record in records)
    if raw_protected and loaded.get("version") != THREADS_SCHEMA_VERSION:
        raise ElsieReceiptError("legacy protected agent thread store is unauthenticated")
    protected_markers = {
        "receipt_binding",
        "store_sequence",
        "previous_store_receipt",
        "store_receipt",
    }.intersection(loaded)
    if protected_markers and not raw_protected:
        raise ElsieReceiptError("protected agent thread store marker is inconsistent")
    pending_exists = _pending_store_exists(target)
    if protected and not records and not allow_first_protected_write and not pending_exists:
        authority = receipt_authority
        if authority is None and target_missing:
            authority = ElsieReceiptAuthority.from_optional_existing_key_store()
            if authority is None:
                return [], None, 0, "", False, False
        elif authority is None:
            raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
        existing_head = load_elsie_store_anchor(
            authority,
            ReceiptNamespace.AGENT_THREAD_STORE,
            subject=_store_subject(target),
            anchor_store=anchor_store,
        )
        if existing_head is not None:
            raise ElsieReceiptError("protected agent thread store is missing or rolled back")
        return [], authority, 0, "", False, False
    authority = (
        _receipt_authority(
            receipt_authority,
            existing_only=raw_protected or not allow_first_protected_write,
        )
        if protected or raw_protected
        else receipt_authority
    )
    store_sequence = 0
    store_receipt = ""
    recovered_pending = False
    if raw_protected:
        if authority is None:
            raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
        store_sequence, store_receipt = _validate_protected_store(loaded, authority)
        try:
            require_elsie_store_anchor(
                authority,
                ReceiptNamespace.AGENT_THREAD_STORE,
                subject=_store_subject(target),
                sequence=store_sequence,
                store_receipt=store_receipt,
                anchor_store=anchor_store,
            )
        except ElsieReceiptError:
            if not allow_pending_recovery:
                if pending_exists:
                    raise ElsieReceiptError("protected agent thread recovery is required") from None
                raise
            recovered, handled_pending = _recover_pending_store(
                target,
                authority,
                previous_sequence=store_sequence,
                previous_store_receipt=store_receipt,
                anchor_store=anchor_store,
                current_payload=loaded,
            )
            if recovered is None:
                raise
            loaded = recovered
            records = loaded["threads"]
            recovered_pending = handled_pending
            store_sequence, store_receipt = _validate_protected_store(
                loaded,
                authority,
            )
        else:
            if pending_exists:
                if not allow_pending_recovery:
                    raise ElsieReceiptError("protected agent thread recovery is required")
                recovered, handled_pending = _recover_pending_store(
                    target,
                    authority,
                    previous_sequence=store_sequence,
                    previous_store_receipt=store_receipt,
                    anchor_store=anchor_store,
                    current_payload=loaded,
                )
                if recovered is not None:
                    loaded = recovered
                    records = loaded["threads"]
                    store_sequence, store_receipt = _validate_protected_store(
                        loaded,
                        authority,
                    )
                recovered_pending = handled_pending
        for record in records:
            if _protected_record(record):
                _require_projected_record(record)
    elif protected and authority is not None:
        recovery_result = (
            _recover_pending_store(
                target,
                authority,
                previous_sequence=0,
                previous_store_receipt="",
                anchor_store=anchor_store,
            )
            if allow_pending_recovery
            else (None, False)
        )
        recovered, handled_pending = recovery_result
        if recovered is not None:
            loaded = recovered
            records = loaded["threads"]
            raw_protected = True
            recovered_pending = handled_pending
            store_sequence, store_receipt = _validate_protected_store(
                loaded,
                authority,
            )
            for record in records:
                if _protected_record(record):
                    _require_projected_record(record)
        else:
            if pending_exists:
                raise ElsieReceiptError("protected agent thread recovery is required")
            existing_head = load_elsie_store_anchor(
                authority,
                ReceiptNamespace.AGENT_THREAD_STORE,
                subject=_store_subject(target),
                anchor_store=anchor_store,
            )
            if existing_head is not None:
                raise ElsieReceiptError("legacy agent thread store conflicts with protected anchor")
    normalized = [
        _normalize_record(
            record,
            force_protected=protected,
            authority=authority,
        )
        for record in records
    ]
    return (
        [record for record in normalized if record is not None],
        authority,
        store_sequence,
        store_receipt,
        raw_protected,
        recovered_pending,
    )


def load_threads(
    path: Path | None = None,
    *,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> list[dict[str, Any]]:
    target = path or threads_path()
    if not protected:
        records, _, _, _, _, _ = _load_threads_unlocked(
            target,
            protected=False,
            receipt_authority=receipt_authority,
            anchor_store=anchor_store,
            allow_first_protected_write=False,
        )
        return records
    try:
        target.lstat()
    except FileNotFoundError:
        if not _pending_store_exists(target):
            authority = receipt_authority
            if authority is None:
                authority = ElsieReceiptAuthority.from_optional_existing_key_store()
                if authority is None:
                    return []
            existing_head = load_elsie_store_anchor(
                authority,
                ReceiptNamespace.AGENT_THREAD_STORE,
                subject=_store_subject(target),
                anchor_store=anchor_store,
            )
            if existing_head is not None:
                raise ElsieReceiptError("protected agent thread store is missing or rolled back")
            return []
    except OSError as exc:
        raise ElsieReceiptError("protected agent thread store is unavailable") from exc
    with config._exclusive_state_lock(target):
        records, _, _, _, raw_protected, _ = _load_threads_unlocked(
            target,
            protected=True,
            receipt_authority=receipt_authority,
            anchor_store=anchor_store,
            allow_first_protected_write=False,
        )
        if records and not raw_protected:
            raise ElsieReceiptError("protected agent thread preparation is required")
        return records


def prepare_protected_thread_store(
    path: Path | None = None,
    *,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> bool:
    """Explicitly migrate or reconcile protected thread state under lock."""

    target = path or threads_path()
    try:
        target.lstat()
    except FileNotFoundError:
        if not _pending_store_exists(target):
            authority = receipt_authority
            if authority is None:
                authority = ElsieReceiptAuthority.from_optional_existing_key_store()
                if authority is None:
                    return False
            existing_head = load_elsie_store_anchor(
                authority,
                ReceiptNamespace.AGENT_THREAD_STORE,
                subject=_store_subject(target),
                anchor_store=anchor_store,
            )
            if existing_head is not None:
                raise ElsieReceiptError("protected agent thread store is missing or rolled back")
            return False
    except OSError as exc:
        raise ElsieReceiptError("protected agent thread store is unavailable") from exc
    with config._exclusive_state_lock(target):
        records, authority, sequence, store_receipt, raw_protected, recovered = _load_threads_unlocked(
            target,
            protected=True,
            receipt_authority=receipt_authority,
            anchor_store=anchor_store,
            allow_first_protected_write=False,
            allow_pending_recovery=True,
        )
        if records and not raw_protected:
            if authority is None:
                raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
            _publish_protected_store(
                target,
                records,
                authority,
                previous_sequence=sequence,
                previous_store_receipt=store_receipt,
                anchor_store=anchor_store,
            )
            return True
        return recovered


def _mutate(
    callback: Callable[
        [list[dict[str, Any]], ElsieReceiptAuthority | None],
        Any,
    ],
    *,
    path: Path | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> Any:
    target = path or threads_path()
    with config._exclusive_state_lock(target):
        records, authority, sequence, store_receipt, _, _ = _load_threads_unlocked(
            target,
            protected=protected,
            receipt_authority=receipt_authority,
            anchor_store=anchor_store,
            allow_first_protected_write=protected,
        )
        result = callback(records, authority)
        records.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        del records[MAX_THREAD_RECORDS:]
        if any(_protected_record(record) for record in records):
            if authority is None:
                raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
            _publish_protected_store(
                target,
                records,
                authority,
                previous_sequence=sequence,
                previous_store_receipt=store_receipt,
                anchor_store=anchor_store,
            )
        else:
            payload = _store_payload(records, authority=authority)
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
    workspace: dict[str, Any] | None = None,
    run_contract: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    path: Path | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, Any]:
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid agent thread status: {status}")
    clean_task = _clean_text(task, 8_000)
    clean_title = _clean_text(title, 120) or " ".join(clean_task.split())[:80] or "Agent task"

    def add(
        records: list[dict[str, Any]],
        authority: ElsieReceiptAuthority | None,
    ) -> dict[str, Any]:
        if protected and authority is None:
            raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
        thread_id = _new_id({record["id"] for record in records})
        now = _now()
        record = {
            "id": thread_id,
            "parent_id": _clean_text(parent_id, 64),
            "title": "Protected Agent thread" if protected else clean_title,
            "task": "" if protected else clean_task,
            "task_receipt": (
                _content_receipt(
                    authority,
                    ReceiptNamespace.AGENT_THREAD_CONTEXT,
                    "record_task",
                    clean_task,
                )
                if protected
                else ""
            ),
            "role": _clean_text(role, 80) or "orchestrator",
            "pipeline": _clean_text(pipeline, 80) or "default",
            "model": _clean_text(model, 120),
            "status": "running" if start_turn else status,
            "created_at": now,
            "updated_at": now,
            "output": "",
            "output_receipt": "",
            "error": "",
            "error_receipt": "",
            "protected_memory_authority": protected,
            "children": [],
            "turns": (
                [
                    {
                        "task": "" if protected else clean_task,
                        "task_receipt": (
                            _content_receipt(
                                authority,
                                ReceiptNamespace.AGENT_THREAD_CONTEXT,
                                "turn_task",
                                clean_task,
                            )
                            if protected
                            else ""
                        ),
                        "status": "running",
                        "started_at": now,
                        "output": "",
                        "output_receipt": "",
                        "error": "",
                        "error_receipt": "",
                    }
                ]
                if start_turn
                else []
            ),
            "blocks": [],
            "workspace": _normalize_workspace(
                workspace,
                protected=protected,
                authority=authority,
            ),
            "run_contract": _normalize_run_contract(run_contract),
            "checkpoint": _normalize_checkpoint(
                checkpoint,
                protected=protected,
                authority=authority,
            ),
        }
        records.append(record)
        if parent_id:
            for parent in records:
                if parent["id"] == parent_id and thread_id not in parent["children"]:
                    parent["children"].append(thread_id)
                    parent["updated_at"] = now
                    break
        return dict(record)

    return _mutate(
        add,
        path=path,
        protected=protected,
        receipt_authority=receipt_authority,
        anchor_store=anchor_store,
    )


def update_thread(
    thread_id: str,
    *,
    path: Path | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
    **changes: Any,
) -> dict[str, Any]:
    allowed = {
        "title",
        "task",
        "role",
        "pipeline",
        "model",
        "status",
        "output",
        "error",
        "blocks",
        "workspace",
        "run_contract",
        "checkpoint",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported agent thread fields: {', '.join(sorted(unknown))}")
    if "status" in changes and changes["status"] not in _VALID_STATUSES:
        raise ValueError(f"Invalid agent thread status: {changes['status']}")

    def update(
        records: list[dict[str, Any]],
        authority: ElsieReceiptAuthority | None,
    ) -> dict[str, Any]:
        for record in records:
            if record["id"] != thread_id:
                continue
            record_protected = protected or record.get("protected_memory_authority") is True
            if record_protected and authority is None:
                raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
            for key, value in changes.items():
                if key == "output":
                    cleaned = _clean_text(value, MAX_THREAD_OUTPUT_CHARS)
                    record[key] = "" if record_protected else cleaned
                    record["output_receipt"] = (
                        _content_receipt(
                            authority,
                            ReceiptNamespace.AGENT_THREAD_OUTPUT,
                            "record_output",
                            cleaned,
                        )
                        if record_protected
                        else ""
                    )
                elif key == "error":
                    cleaned = _clean_text(value, 2_000)
                    record[key] = "" if record_protected else cleaned
                    record["error_receipt"] = (
                        _content_receipt(
                            authority,
                            ReceiptNamespace.AGENT_THREAD_OUTPUT,
                            "record_error",
                            cleaned,
                        )
                        if record_protected
                        else ""
                    )
                elif key == "blocks":
                    record[key] = (
                        [
                            normalized
                            for item in value
                            if (
                                normalized := _normalize_block(
                                    item,
                                    protected=record_protected,
                                    authority=authority,
                                )
                            )
                            is not None
                        ]
                        if isinstance(value, list)
                        else []
                    )
                elif key == "workspace":
                    record[key] = _merge_workspace(
                        record.get("workspace"),
                        value,
                        protected=record_protected,
                        authority=authority,
                    )
                elif key == "run_contract":
                    record[key] = _normalize_run_contract(value)
                elif key == "checkpoint":
                    record[key] = _normalize_checkpoint(
                        value,
                        protected=record_protected,
                        authority=authority,
                    )
                else:
                    cleaned = _clean_text(value, 8_000 if key == "task" else 120)
                    if record_protected and key in {"task", "title"}:
                        record[key] = "" if key == "task" else "Protected Agent thread"
                        if key == "task":
                            record["task_receipt"] = _content_receipt(
                                authority,
                                ReceiptNamespace.AGENT_THREAD_CONTEXT,
                                "record_task",
                                cleaned,
                            )
                    else:
                        record[key] = cleaned
            record["protected_memory_authority"] = record_protected
            record["updated_at"] = _now()
            return dict(record)
        raise KeyError(f"Unknown agent thread '{thread_id}'.")

    return _mutate(
        update,
        path=path,
        protected=protected,
        receipt_authority=receipt_authority,
        anchor_store=anchor_store,
    )


def begin_turn(
    thread_id: str,
    task: str,
    *,
    pipeline: str | None = None,
    model: str | None = None,
    workspace: dict[str, Any] | None = None,
    run_contract: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    path: Path | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, Any]:
    def begin(
        records: list[dict[str, Any]],
        authority: ElsieReceiptAuthority | None,
    ) -> dict[str, Any]:
        for record in records:
            if record["id"] != thread_id:
                continue
            record_protected = protected or record.get("protected_memory_authority") is True
            if record_protected and authority is None:
                raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
            now = _now()
            record["status"] = "running"
            record["updated_at"] = now
            clean_task = _clean_text(task, 8_000)
            if record_protected:
                record["task"] = ""
                record["task_receipt"] = _content_receipt(
                    authority,
                    ReceiptNamespace.AGENT_THREAD_CONTEXT,
                    "record_task",
                    clean_task,
                )
            elif not record["task"]:
                record["task"] = clean_task
            if pipeline:
                record["pipeline"] = _clean_text(pipeline, 80)
            if model:
                record["model"] = _clean_text(model, 120)
            if workspace is not None:
                record["workspace"] = _merge_workspace(
                    record.get("workspace"),
                    workspace,
                    protected=record_protected,
                    authority=authority,
                )
            if run_contract is not None:
                record["run_contract"] = _normalize_run_contract(run_contract)
            if checkpoint is not None:
                record["checkpoint"] = _normalize_checkpoint(
                    checkpoint,
                    protected=record_protected,
                    authority=authority,
                )
            record["turns"].append(
                {
                    "task": "" if record_protected else clean_task,
                    "task_receipt": (
                        _content_receipt(
                            authority,
                            ReceiptNamespace.AGENT_THREAD_CONTEXT,
                            "turn_task",
                            clean_task,
                        )
                        if record_protected
                        else ""
                    ),
                    "status": "running",
                    "started_at": now,
                    "output": "",
                    "output_receipt": "",
                    "error": "",
                    "error_receipt": "",
                }
            )
            record["turns"] = record["turns"][-MAX_THREAD_TURNS:]
            record["protected_memory_authority"] = record_protected
            return dict(record)
        raise KeyError(f"Unknown agent thread '{thread_id}'.")

    return _mutate(
        begin,
        path=path,
        protected=protected,
        receipt_authority=receipt_authority,
        anchor_store=anchor_store,
    )


def finish_turn(
    thread_id: str,
    *,
    status: str,
    output: str = "",
    error: str = "",
    blocks: list[dict[str, Any]] | None = None,
    pipeline: str | None = None,
    workspace: dict[str, Any] | None = None,
    run_contract: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    path: Path | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, Any]:
    if status not in _VALID_STATUSES - {"queued", "running"}:
        raise ValueError(f"Invalid terminal agent thread status: {status}")

    def finish(
        records: list[dict[str, Any]],
        authority: ElsieReceiptAuthority | None,
    ) -> dict[str, Any]:
        for record in records:
            if record["id"] != thread_id:
                continue
            record_protected = protected or record.get("protected_memory_authority") is True
            if record_protected and authority is None:
                raise ElsieReceiptError("protected agent thread receipt authority is unavailable")
            now = _now()
            record["status"] = status
            record["updated_at"] = now
            clean_output = _clean_text(output, MAX_THREAD_OUTPUT_CHARS)
            clean_error = _clean_text(error, 2_000)
            record["output"] = "" if record_protected else clean_output
            record["output_receipt"] = (
                _content_receipt(
                    authority,
                    ReceiptNamespace.AGENT_THREAD_OUTPUT,
                    "record_output",
                    clean_output,
                )
                if record_protected
                else ""
            )
            record["error"] = "" if record_protected else clean_error
            record["error_receipt"] = (
                _content_receipt(
                    authority,
                    ReceiptNamespace.AGENT_THREAD_OUTPUT,
                    "record_error",
                    clean_error,
                )
                if record_protected
                else ""
            )
            if blocks is not None:
                record["blocks"] = [
                    normalized
                    for item in blocks
                    if (
                        normalized := _normalize_block(
                            item,
                            protected=record_protected,
                            authority=authority,
                        )
                    )
                    is not None
                ]
            if pipeline:
                record["pipeline"] = _clean_text(pipeline, 80)
            if workspace is not None:
                record["workspace"] = _merge_workspace(
                    record.get("workspace"),
                    workspace,
                    protected=record_protected,
                    authority=authority,
                )
            if run_contract is not None:
                record["run_contract"] = _normalize_run_contract(run_contract)
            if checkpoint is not None:
                record["checkpoint"] = _normalize_checkpoint(
                    checkpoint,
                    protected=record_protected,
                    authority=authority,
                )
            if record["turns"]:
                turn = record["turns"][-1]
                if turn.get("status") == "running":
                    turn.update(
                        {
                            "status": status,
                            "finished_at": now,
                            "output": "" if record_protected else clean_output,
                            "output_receipt": (
                                _content_receipt(
                                    authority,
                                    ReceiptNamespace.AGENT_THREAD_OUTPUT,
                                    "turn_output",
                                    clean_output,
                                )
                                if record_protected
                                else ""
                            ),
                            "error": "" if record_protected else clean_error,
                            "error_receipt": (
                                _content_receipt(
                                    authority,
                                    ReceiptNamespace.AGENT_THREAD_OUTPUT,
                                    "turn_error",
                                    clean_error,
                                )
                                if record_protected
                                else ""
                            ),
                        }
                    )
            record["protected_memory_authority"] = record_protected
            return dict(record)
        raise KeyError(f"Unknown agent thread '{thread_id}'.")

    return _mutate(
        finish,
        path=path,
        protected=protected,
        receipt_authority=receipt_authority,
        anchor_store=anchor_store,
    )


def resolve_thread(
    thread_ref: str,
    *,
    path: Path | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> dict[str, Any]:
    ref = _clean_text(thread_ref, 64).lower()
    if not ref:
        raise KeyError("Agent thread ID is required.")
    records = load_threads(
        path,
        protected=protected,
        receipt_authority=receipt_authority,
        anchor_store=anchor_store,
    )
    exact = [record for record in records if record["id"].lower() == ref]
    if exact:
        return exact[0]
    matches = [record for record in records if record["id"].lower().startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise KeyError(f"Agent thread prefix '{thread_ref}' is ambiguous.")
    raise KeyError(f"Unknown agent thread '{thread_ref}'. Use /agent threads to list runs.")


def list_threads(
    *,
    limit: int = 20,
    path: Path | None = None,
    protected: bool = False,
    receipt_authority: ElsieReceiptAuthority | None = None,
    anchor_store: Any | None = None,
) -> list[dict[str, Any]]:
    return load_threads(
        path,
        protected=protected,
        receipt_authority=receipt_authority,
        anchor_store=anchor_store,
    )[: max(1, min(int(limit), 100))]


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
                + (
                    f" · context receipt {block.get('context_output_receipt')}"
                    if record.get("protected_memory_authority") and block.get("context_output_receipt")
                    else ""
                )
            )
    if record.get("protected_memory_authority"):
        text = (
            f"Thread: {record.get('id', '?')}\n"
            f"Task receipt: {record.get('task_receipt') or '(none)'}\n"
            f"Last status: {record.get('status', '?')}\n"
            f"Initial HEAD: {record.get('workspace', {}).get('initial_head') or '(not recorded)'}\n"
            f"Current HEAD: {record.get('workspace', {}).get('head') or '(not recorded)'}\n"
            f"Block evidence:\n{chr(10).join(block_lines) or '- none'}\n\n"
            f"Last output receipt: {record.get('output_receipt') or '(none)'}"
        )
        return text[:limit]
    text = (
        f"Thread: {record.get('id', '?')}\n"
        f"Original task: {record.get('task', '')}\n"
        f"Last status: {record.get('status', '?')}\n"
        f"Workspace: {'recorded' if record.get('workspace', {}).get('workspace_root') else '(not recorded)'}\n"
        f"Branch: {record.get('workspace', {}).get('branch') or '(not recorded)'}\n"
        f"Initial HEAD: {record.get('workspace', {}).get('initial_head') or '(not recorded)'}\n"
        f"Current HEAD: {record.get('workspace', {}).get('head') or '(not recorded)'}\n"
        f"Block evidence:\n{chr(10).join(block_lines) or '- none'}\n\n"
        f"Last output:\n{record.get('output', '') or '(no output)'}"
    )
    return text[:limit]
