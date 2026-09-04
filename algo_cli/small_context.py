"""Small-context ledger support for models with runtime windows below 75k tokens.

The ledger gives compact models a big-model-like recall path: full optional
context is written to a temporary markdown file while the request only carries a
short refresh trigger that points the model/tool loop back to that file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import (
    _ensure_windows_private_directory,
    _move_file_write_through,
    _path_is_reparse_point,
    _portable_directory_identity,
    _portable_state_identity,
    _recheck_directory_chain,
    _windows_create_private_file,
    _windows_pinned_directory_chain,
    _windows_private_dacl,
)

SMALL_CONTEXT_THRESHOLD = 75_000
DEFAULT_ROOT = Path(tempfile.gettempdir()) / "algo_cli_small_context"
LEDGER_TTL_SECONDS = 24 * 60 * 60
LEDGER_STAGING_TTL_SECONDS = 5 * 60
LEDGER_MAX_FILES = 64
_LEDGER_NAME_RE = re.compile(r"[0-9]{10,24}-[A-Za-z0-9._-]{1,80}-[0-9a-f]{16}\.md\Z")
_LEDGER_TEMP_RE = re.compile(r"\.[0-9]{10,24}-[A-Za-z0-9._-]{1,80}-[0-9a-f]{16}\.md\.[0-9a-f]{16}\.tmp\Z")


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
    echo_authority: bool = False,
) -> SmallContextLedger | None:
    """Write a temp ledger file when the runtime cap is below 75k tokens."""
    if not is_small_context(runtime_cap) or echo_authority:
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
    windows_root_chain = _ensure_windows_private_directory(root) if os.name == "nt" else ()
    if os.name != "nt":
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root_info = root.lstat()
    if _path_is_reparse_point(root, root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise OSError("small-context ledger root must be a private directory")
    if hasattr(os, "getuid") and root_info.st_uid != os.getuid():
        raise OSError("small-context ledger root must be owned by the current user")
    if os.name == "posix":
        os.chmod(root, 0o700)
        root_info = root.lstat()
    elif os.name == "nt" and not _windows_private_dacl(root):
        raise OSError("small-context ledger root must have a private DACL")
    if windows_root_chain:
        _recheck_directory_chain(windows_root_chain)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    name = f"{time.time_ns()}-{_safe_name(model)}-{digest}.md"
    path = root / name
    temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    encoded = text.encode("utf-8")
    if _full_path_ledger_io_required():
        _write_ledger_by_path(
            root,
            root_info=root_info,
            name=name,
            temp_name=temp_name,
            encoded=encoded,
            root_chain=windows_root_chain,
        )
        return SmallContextLedger(
            path=path,
            model=model,
            runtime_cap=int(runtime_cap),
            block_names=block_names,
            bytes_written=len(encoded),
            token_estimate=estimate_text_tokens(text),
        )
    directory_flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    directory_fd = os.open(root, directory_flags)
    opened_info = os.fstat(directory_fd)
    if (opened_info.st_dev, opened_info.st_ino) != (root_info.st_dev, root_info.st_ino):
        os.close(directory_fd)
        raise OSError("small-context ledger root changed during open")
    temp_fd: int | None = None
    published = False
    try:
        _cleanup_ledgers(directory_fd, now=time.time())
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
        temp_fd = os.open(temp_name, file_flags, 0o600, dir_fd=directory_fd)
        offset = 0
        while offset < len(encoded):
            written = os.write(temp_fd, encoded[offset:])
            if written <= 0:
                raise OSError("small-context ledger write made no progress")
            offset += written
        os.fsync(temp_fd)
        written_info = os.fstat(temp_fd)
        if not stat.S_ISREG(written_info.st_mode) or written_info.st_nlink != 1 or written_info.st_size != len(encoded):
            raise OSError("small-context ledger staging identity failed")
        os.close(temp_fd)
        temp_fd = None
        os.link(
            temp_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        published = True
        os.unlink(temp_name, dir_fd=directory_fd)
        final_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(final_info.st_mode) or final_info.st_nlink != 1 or final_info.st_size != len(encoded):
            raise OSError("small-context ledger publication identity failed")
        if os.name == "posix" and stat.S_IMODE(final_info.st_mode) != 0o600:
            os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
        current_root = root.lstat()
        if stat.S_ISLNK(current_root.st_mode) or (current_root.st_dev, current_root.st_ino) != (
            opened_info.st_dev,
            opened_info.st_ino,
        ):
            raise OSError("small-context ledger root changed before handoff")
    except Exception:
        if temp_fd is not None:
            os.close(temp_fd)
        for candidate in (temp_name, name if published else ""):
            if not candidate:
                continue
            try:
                os.unlink(candidate, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(directory_fd)
    return SmallContextLedger(
        path=path,
        model=model,
        runtime_cap=int(runtime_cap),
        block_names=block_names,
        bytes_written=len(text.encode("utf-8")),
        token_estimate=estimate_text_tokens(text),
    )


def _full_path_ledger_io_required() -> bool:
    return os.name == "nt"


def _ledger_file_identity(information: os.stat_result) -> tuple[int, ...]:
    return (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
        int(information.st_size),
    )


def _write_ledger_by_path(
    root: Path,
    *,
    root_info: os.stat_result,
    name: str,
    temp_name: str,
    encoded: bytes,
    root_chain: tuple[tuple[Path, tuple[int, ...]], ...] = (),
) -> None:
    """Publish through full paths where directory descriptors are unavailable."""

    if os.name == "nt":
        with _windows_pinned_directory_chain(root) as pinned_chain:
            _write_ledger_by_bound_path(
                root,
                root_info=root_info,
                name=name,
                temp_name=temp_name,
                encoded=encoded,
                root_chain=pinned_chain,
            )
        return
    _write_ledger_by_bound_path(
        root,
        root_info=root_info,
        name=name,
        temp_name=temp_name,
        encoded=encoded,
        root_chain=root_chain,
    )


def _write_ledger_by_bound_path(
    root: Path,
    *,
    root_info: os.stat_result,
    name: str,
    temp_name: str,
    encoded: bytes,
    root_chain: tuple[tuple[Path, tuple[int, ...]], ...],
) -> None:
    root_identity = _portable_directory_identity(root_info)
    if root_chain:
        _recheck_directory_chain(root_chain)
    _cleanup_ledgers_by_path(root, now=time.time())
    path = root / name
    temp_path = root / temp_name
    file_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    descriptor: int | None = None
    published = False
    try:
        if root_chain:
            _recheck_directory_chain(root_chain)
        descriptor = (
            _windows_create_private_file(temp_path) if os.name == "nt" else os.open(temp_path, file_flags, 0o600)
        )
        if os.name == "nt":
            hardened = temp_path.lstat()
            opened = os.fstat(descriptor)
            if (
                _path_is_reparse_point(temp_path, hardened)
                or _portable_state_identity(hardened) != _portable_state_identity(opened)
                or not _windows_private_dacl(temp_path)
            ):
                raise OSError("small-context ledger staging ACL is unsafe")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("small-context ledger write made no progress")
            offset += written
        os.fsync(descriptor)
        staged = os.fstat(descriptor)
        if not stat.S_ISREG(staged.st_mode) or staged.st_nlink != 1 or staged.st_size != len(encoded):
            raise OSError("small-context ledger staging identity failed")
        os.close(descriptor)
        descriptor = None
        current_stage = temp_path.lstat()
        if (
            _path_is_reparse_point(temp_path, current_stage)
            or _portable_state_identity(current_stage) != _portable_state_identity(staged)
            or (os.name == "nt" and not _windows_private_dacl(temp_path))
        ):
            raise OSError("small-context ledger staging identity changed")
        if root_chain:
            _recheck_directory_chain(root_chain)
        _move_file_write_through(temp_path, path, replace=False)
        published = True
        final_info = path.lstat()
        if (
            _path_is_reparse_point(path, final_info)
            or not stat.S_ISREG(final_info.st_mode)
            or final_info.st_nlink != 1
            or final_info.st_size != len(encoded)
            or _ledger_file_identity(final_info) != _ledger_file_identity(staged)
            or (os.name == "nt" and not _windows_private_dacl(path))
        ):
            raise OSError("small-context ledger publication identity failed")
        current_root = root.lstat()
        if _path_is_reparse_point(root, current_root) or _portable_directory_identity(current_root) != root_identity:
            raise OSError("small-context ledger root changed before handoff")
        if root_chain:
            _recheck_directory_chain(root_chain)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        for candidate in (temp_path, path if published else None):
            if candidate is None:
                continue
            try:
                candidate.unlink()
            except OSError:
                pass
        raise


def _cleanup_ledgers_by_path(root: Path, *, now: float) -> None:
    retained: list[tuple[float, Path, tuple[int, ...]]] = []
    for path in root.iterdir():
        is_ledger = _LEDGER_NAME_RE.fullmatch(path.name) is not None
        is_staging = _LEDGER_TEMP_RE.fullmatch(path.name) is not None
        if not is_ledger and not is_staging:
            continue
        try:
            information = path.lstat()
        except OSError:
            continue
        if is_staging:
            if now - float(information.st_mtime) <= LEDGER_STAGING_TTL_SECONDS:
                continue
            if _path_is_reparse_point(path, information):
                if stat.S_ISDIR(information.st_mode):
                    path.rmdir()
                else:
                    path.unlink()
                continue
            if not stat.S_ISREG(information.st_mode):
                raise OSError("small-context staging entry is unsafe")
            identity = _portable_state_identity(information)
            if _portable_state_identity(path.lstat()) != identity:
                raise OSError("small-context staging entry changed during cleanup")
            path.unlink()
            continue
        if _path_is_reparse_point(path, information) or not stat.S_ISREG(information.st_mode):
            continue
        identity = _portable_state_identity(information)
        if now - float(information.st_mtime) > LEDGER_TTL_SECONDS:
            try:
                if _portable_state_identity(path.lstat()) == identity:
                    path.unlink()
            except OSError:
                pass
            continue
        retained.append((float(information.st_mtime), path, identity))
    for _mtime, path, identity in sorted(retained, key=lambda value: value[0], reverse=True)[LEDGER_MAX_FILES - 1 :]:
        try:
            if _portable_state_identity(path.lstat()) == identity:
                path.unlink()
        except OSError:
            pass


def _cleanup_ledgers(directory_fd: int, *, now: float) -> None:
    """Delete only expired/excess ledger-shaped regular files from a pinned root."""

    retained: list[tuple[float, str]] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            name = entry.name
            if _LEDGER_NAME_RE.fullmatch(name) is None:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if now - float(info.st_mtime) > LEDGER_TTL_SECONDS:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
                continue
            retained.append((float(info.st_mtime), name))
    for _mtime, name in sorted(retained, reverse=True)[LEDGER_MAX_FILES - 1 :]:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            pass


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
