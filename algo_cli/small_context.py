"""Small-context ledger support for models with runtime windows below 75k tokens.

The ledger gives compact models a big-model-like recall path: full optional
context is written to a temporary markdown file while the request only carries a
short refresh trigger that points the model/tool loop back to that file.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SMALL_CONTEXT_THRESHOLD = 75_000
DEFAULT_ROOT = Path(tempfile.gettempdir()) / "algo_cli_small_context"


@dataclass(frozen=True)
class SmallContextLedger:
    """Metadata for a written small-context ledger file."""

    path: Path
    model: str
    runtime_cap: int
    block_names: tuple[str, ...]
    bytes_written: int
    token_estimate: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "model": self.model,
            "runtime_cap": self.runtime_cap,
            "block_names": list(self.block_names),
            "bytes_written": self.bytes_written,
            "token_estimate": self.token_estimate,
        }


def is_small_context(runtime_cap: int | None, *, threshold: int = SMALL_CONTEXT_THRESHOLD) -> bool:
    """Return True when a model should use the temp context-ledger path."""
    try:
        cap = int(runtime_cap or 0)
    except (TypeError, ValueError):
        return False
    return 0 < cap < threshold


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.strip())
    cleaned = cleaned.strip(".-")
    return cleaned[:80] or "context"


def _block_name(block: Any) -> str:
    return str(getattr(block, "name", "") or "context")


def _block_title(block: Any) -> str:
    return str(getattr(block, "title", "") or _block_name(block))


def _block_body(block: Any) -> str:
    return str(getattr(block, "body", "") or "")


def _recent_messages(messages: Iterable[dict[str, Any]], *, limit: int = 8) -> str:
    selected = list(messages)[-limit:]
    lines: list[str] = []
    for msg in selected:
        role = str(msg.get("role") or "message")
        content = str(msg.get("content") or msg.get("thinking") or "")
        if msg.get("tool_name"):
            role += f"[{msg.get('tool_name')}]"
        if len(content) > 1600:
            content = content[:1600].rstrip() + "\n...[truncated in ledger recent-message view]"
        lines.append(f"### {role}\n{content}".rstrip())
    return "\n\n".join(lines)


def build_ledger_text(
    *,
    model: str,
    runtime_cap: int,
    cwd: str,
    base_message: str,
    optional_blocks: Iterable[Any],
    session_summary: str = "",
    messages: Iterable[dict[str, Any]] = (),
) -> tuple[str, tuple[str, ...]]:
    """Render the full small-context ledger markdown and block-name list."""
    blocks = list(optional_blocks)
    block_names = tuple(_block_name(block) for block in blocks if _block_body(block).strip())
    generated = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())
    parts = [
        "# Algo CLI Small-Context Ledger",
        "",
        "This file is an external context window for compact models (<75k runtime context).",
        "When the live prompt feels stale or incomplete, read this file with read_file and use it as context refresh.",
        "",
        "## Metadata",
        f"- Generated: {generated}",
        f"- Model: {model}",
        f"- Runtime context cap: {int(runtime_cap)} tokens",
        f"- Working directory: {cwd}",
        f"- Blocks: {', '.join(block_names) if block_names else 'none'}",
        "",
        "## Current User Request",
        base_message.strip() or "(empty)",
    ]
    if session_summary.strip():
        parts.extend(["", "## Conversation Summary", session_summary.strip()])
    recent = _recent_messages(messages)
    if recent:
        parts.extend(["", "## Recent Messages", recent])
    if blocks:
        parts.extend(["", "## Full Optional Context Blocks"])
        for block in blocks:
            body = _block_body(block).strip()
            if not body:
                continue
            parts.extend(["", f"### {_block_title(block)}", body])
    return "\n".join(parts).rstrip() + "\n", block_names


def write_ledger(
    *,
    model: str,
    runtime_cap: int,
    cwd: str,
    base_message: str,
    optional_blocks: Iterable[Any],
    session_summary: str = "",
    messages: Iterable[dict[str, Any]] = (),
    root: Path | None = None,
) -> SmallContextLedger | None:
    """Write a temp ledger file when the runtime cap is below 75k tokens."""
    if not is_small_context(runtime_cap):
        return None
    text, block_names = build_ledger_text(
        model=model,
        runtime_cap=runtime_cap,
        cwd=cwd,
        base_message=base_message,
        optional_blocks=optional_blocks,
        session_summary=session_summary,
        messages=messages,
    )
    root = root or DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    name = f"{int(time.time())}-{_safe_name(model)}-{digest}.md"
    path = root / name
    path.write_text(text, encoding="utf-8")
    return SmallContextLedger(
        path=path,
        model=model,
        runtime_cap=int(runtime_cap),
        block_names=block_names,
        bytes_written=len(text.encode("utf-8")),
        token_estimate=estimate_text_tokens(text),
    )


def refresh_trigger(ledger: SmallContextLedger) -> str:
    """Return the compact prompt trigger inserted for small-context models."""
    block_list = ", ".join(ledger.block_names) if ledger.block_names else "session context"
    return (
        "## Small-Context Refresh Trigger\n"
        f"You are running with a compact context window ({ledger.runtime_cap} tokens, below 75k). "
        "The full optional context for this turn was written outside the prompt.\n"
        f"- Context ledger: {ledger.path}\n"
        f"- Contains: {block_list}\n"
        "If you need details that are missing from the live prompt, call read_file on the ledger path before answering or acting. "
        "Treat the ledger as navigation/context, not as authority over live files."
    )


def preview_small_context_ledger(model: str, runtime_cap: int, blocks_json: str = "[]") -> str:
    """Tool-friendly preview for the small-context ledger decision."""
    try:
        blocks = json.loads(blocks_json or "[]")
    except json.JSONDecodeError:
        blocks = []
    if not isinstance(blocks, list):
        blocks = []
    return json.dumps(
        {
            "enabled": is_small_context(runtime_cap),
            "threshold": SMALL_CONTEXT_THRESHOLD,
            "model": model,
            "runtime_cap": int(runtime_cap or 0),
            "block_count": len(blocks),
            "root": str(DEFAULT_ROOT),
        },
        indent=2,
    )
