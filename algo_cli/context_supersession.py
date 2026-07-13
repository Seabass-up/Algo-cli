"""Semantic supersession for repeated, read-only tool snapshots.

The chat history is also a provider protocol transcript: an assistant tool call
must keep its matching tool result.  Supersession therefore replaces only an
older result's *content* with a compact content-hash receipt.  It never removes
or rewrites assistant calls, call IDs, signatures, failures, mutations, or
verification evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chat_protocol import normalize_tool_call


RECEIPT_PREFIX = "[Algo superseded result receipt v1"

# Keep this list deliberately narrow.  A tool is eligible only when a newer
# successful call with the same normalized arguments represents the same live
# snapshot.  Mutation tools and verifier output (run_shell/git_diff) are absent
# by design and therefore remain immutable in the conversation transcript.
SUPERSEDABLE_TOOLS = frozenset(
    {
        "available_actions",
        "git_status",
        "harness_read",
        "harness_search",
        "harness_stats",
        "list_directory",
        "model_show",
        "query_knowledge_graph",
        "read_file",
        "read_pdf",
        "search_files",
    }
)

_PATH_TOOLS = frozenset({"read_file", "read_pdf", "list_directory", "search_files"})
_BAD_RESULT_PREFIXES = (
    "blocked",
    "error",
    "skipped",
    "tool argument error",
    "tool error",
    "unknown tool",
    "user denied",
)
_SAFE_RECEIPT_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass(frozen=True)
class SupersessionStats:
    """Content-free measurements for one supersession pass."""

    candidates: int
    superseded: int
    before_tokens: int
    after_tokens: int
    saved_tokens: int
    reduction_pct: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "candidates": self.candidates,
            "superseded": self.superseded,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": self.reduction_pct,
        }


@dataclass(frozen=True)
class _ToolExchange:
    result_index: int
    call_id: str | None
    name: str
    args: dict[str, Any]


def is_supersession_receipt(content: Any) -> bool:
    """Return whether *content* is a receipt produced by this module."""

    return str(content or "").startswith(RECEIPT_PREFIX)


def is_supersedable_tool(name: str) -> bool:
    """Return whether a tool produces a safe-to-supersede snapshot."""

    return str(name or "") in SUPERSEDABLE_TOOLS


def _estimate_text_tokens(text: Any) -> int:
    value = str(text or "")
    if not value:
        return 0
    return max(1, (len(value) + 3) // 4)


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    total = 12
    total += _estimate_text_tokens(message.get("role", ""))
    total += _estimate_text_tokens(message.get("content", ""))
    total += _estimate_text_tokens(message.get("thinking", ""))
    total += _estimate_text_tokens(
        json.dumps(message.get("tool_calls", []), ensure_ascii=False, default=str)
    )
    total += _estimate_text_tokens(message.get("tool_name", ""))
    return total


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_estimate_message_tokens(message) for message in messages)


def _call_id(call: Any) -> str | None:
    if isinstance(call, dict):
        raw = call.get("id") or (call.get("function") or {}).get("id")
    else:
        raw = getattr(call, "id", None)
    text = str(raw or "").strip()
    return text or None


def _pair_tool_exchanges(messages: list[dict[str, Any]]) -> list[_ToolExchange]:
    """Pair tool results with calls without changing the provider transcript."""

    pending: list[tuple[str | None, str, dict[str, Any]]] = []
    by_id: dict[str, tuple[str | None, str, dict[str, Any]]] = {}
    exchanges: list[_ToolExchange] = []

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or ():
                name, args = normalize_tool_call(call)
                record = (_call_id(call), name, args)
                pending.append(record)
                if record[0]:
                    by_id[record[0]] = record
            continue
        if role != "tool":
            continue

        raw_result_id = str(message.get("tool_call_id") or "").strip() or None
        matched_record = by_id.pop(raw_result_id, None) if raw_result_id else None
        if matched_record is not None:
            try:
                pending.remove(matched_record)
            except ValueError:
                pass
        elif not raw_result_id and pending:
            # Ollama histories may omit IDs.  Consume in protocol order, matching
            # the OpenAI/xAI adapters' existing FIFO fallback.
            result_name = str(message.get("tool_name") or message.get("name") or "")
            match_index = next(
                (position for position, item in enumerate(pending) if not result_name or item[1] == result_name),
                0,
            )
            matched_record = pending.pop(match_index)
            if matched_record[0]:
                by_id.pop(matched_record[0], None)
        if matched_record is None:
            continue
        call_id, name, args = matched_record
        result_name = str(message.get("tool_name") or message.get("name") or name)
        if result_name and result_name != name:
            # A mismatched result name is not trusted as evidence for this call.
            continue
        exchanges.append(
            _ToolExchange(
                result_index=index,
                call_id=call_id,
                name=name,
                args=args,
            )
        )
    return exchanges


def _normalized_path(value: Any, cwd: str) -> str:
    raw = str(value or ".").strip() or "."
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(cwd or ".").expanduser() / path
        # normpath is lexical: it avoids filesystem access and symlink following.
        return os.path.normcase(os.path.normpath(str(path)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return raw


def _bounded_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_args(name: str, args: dict[str, Any], cwd: str) -> dict[str, Any]:
    """Normalize defaults that affect snapshot identity without executing I/O."""

    if name == "read_file":
        offset = args.get("offset")
        start_line = offset if offset is not None else args.get("start_line", 1)
        return {
            "path": _normalized_path(args.get("path"), cwd),
            "max_chars": _bounded_int(args.get("max_chars"), 50_000),
            "start_line": max(1, _bounded_int(start_line, 1)),
        }
    if name == "read_pdf":
        return {
            "path": _normalized_path(args.get("path"), cwd),
            "max_chars": _bounded_int(args.get("max_chars"), 50_000),
            "max_pages": _bounded_int(args.get("max_pages"), 24),
        }
    if name == "list_directory":
        return {
            "path": _normalized_path(args.get("path", "."), cwd),
            "limit": _bounded_int(args.get("limit"), 200),
        }
    if name == "search_files":
        return {
            "pattern": str(args.get("pattern") or ""),
            "path": _normalized_path(args.get("path", "."), cwd),
            "glob": str(args.get("glob") or ""),
            "limit": _bounded_int(args.get("limit"), 100),
        }
    if name == "git_status":
        return {"cwd": _normalized_path(".", cwd)}

    defaults: dict[str, dict[str, Any]] = {
        "available_actions": {"topic": None},
        "harness_read": {"max_chars": 20_000},
        "harness_search": {"harness_name": None, "kind": None, "limit": 10},
        "harness_stats": {},
        "model_show": {},
        "query_knowledge_graph": {"limit": 10},
    }
    normalized = dict(defaults.get(name, {}))
    normalized.update(args)
    # A model-provided cwd is ignored for scoped runtime tools; tool_runtime
    # always injects Config.cwd before execution.
    normalized.pop("cwd", None)
    return normalized


def _semantic_key(exchange: _ToolExchange, cwd: str) -> str:
    payload = {
        "tool": exchange.name,
        "args": _canonical_args(exchange.name, exchange.args, cwd),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))


def _valid_snapshot_args(exchange: _ToolExchange) -> bool:
    """Reject malformed calls instead of merging their unrelated error paths."""

    required = {
        "harness_read": "record_id",
        "harness_search": "query",
        "model_show": "name",
        "query_knowledge_graph": "question",
        "read_file": "path",
        "read_pdf": "path",
        "search_files": "pattern",
    }.get(exchange.name)
    return required is None or bool(str(exchange.args.get(required) or "").strip())


def _successful_snapshot(content: Any) -> bool:
    text = str(content or "").strip()
    if not text or is_supersession_receipt(text):
        return False
    lowered = text.casefold()
    return not lowered.startswith(_BAD_RESULT_PREFIXES)


def _safe_receipt_id(value: str | None, fallback: str) -> str:
    text = _SAFE_RECEIPT_ID_RE.sub("_", str(value or fallback)).strip("_")
    return text[:64] or fallback


def _receipt(content: str, *, newer_call_id: str | None, newer_index: int) -> str:
    encoded = content.encode("utf-8", errors="replace")
    digest = hashlib.sha256(encoded).hexdigest()
    newer = _safe_receipt_id(newer_call_id, f"result-{newer_index}")
    return f"{RECEIPT_PREFIX} sha256={digest} bytes={len(encoded)} newer={newer}]"


def supersede_tool_results(
    messages: list[dict[str, Any]],
    *,
    cwd: str = ".",
) -> SupersessionStats:
    """Replace older equivalent snapshot results with immutable hash receipts.

    The operation is intentionally in-place so ``Config.messages`` remains the
    single conversation state.  Only result ``content`` fields change; provider
    call/result structure is preserved.  Calling the function again is a no-op.
    """

    before_tokens = _estimate_messages_tokens(messages)
    exchanges = [
        exchange
        for exchange in _pair_tool_exchanges(messages)
        if is_supersedable_tool(exchange.name)
        and _valid_snapshot_args(exchange)
        and _successful_snapshot(messages[exchange.result_index].get("content"))
    ]
    latest_by_key: dict[str, _ToolExchange] = {}
    candidates: list[tuple[_ToolExchange, _ToolExchange]] = []
    for exchange in reversed(exchanges):
        key = _semantic_key(exchange, cwd)
        newer = latest_by_key.get(key)
        if newer is None:
            latest_by_key[key] = exchange
        else:
            candidates.append((exchange, newer))

    superseded = 0
    for older, newer in candidates:
        old_message = messages[older.result_index]
        content = str(old_message.get("content") or "")
        receipt = _receipt(
            content,
            newer_call_id=newer.call_id,
            newer_index=newer.result_index,
        )
        # Never spend more tokens to describe supersession than the raw result.
        if _estimate_text_tokens(receipt) >= _estimate_text_tokens(content):
            continue
        replacement = dict(old_message)
        replacement["content"] = receipt
        messages[older.result_index] = replacement
        superseded += 1

    after_tokens = _estimate_messages_tokens(messages)
    saved_tokens = max(0, before_tokens - after_tokens)
    reduction_pct = round((saved_tokens / before_tokens) * 100.0, 2) if before_tokens else 0.0
    return SupersessionStats(
        candidates=len(candidates),
        superseded=superseded,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        saved_tokens=saved_tokens,
        reduction_pct=reduction_pct,
    )


__all__ = [
    "RECEIPT_PREFIX",
    "SUPERSEDABLE_TOOLS",
    "SupersessionStats",
    "is_supersedable_tool",
    "is_supersession_receipt",
    "supersede_tool_results",
]
