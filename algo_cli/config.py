"""Configuration and persistent state."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping

from .model_aliases import normalize_codex_model


# Branding compatibility constants. New names take precedence; old names remain
# readable so existing installs continue to start cleanly.
NEW_CONFIG_DIR_NAME = ".algo_cli"
OLD_CONFIG_DIR_NAME = ".ollama_cli"
NEW_ENV_PREFIX = "ALGO_CLI_"
OLD_ENV_PREFIX = "OLLAMA_CLI_"


def _resolve_config_dir() -> Path:
    """Resolve the active config directory with full dual-support for the rebrand.

    Precedence (highest first):
    1. Explicit ALGO_CLI_CONFIG_DIR
    2. Explicit OLLAMA_CLI_CONFIG_DIR (legacy compat)
    3. If ~/.algo_cli already exists on disk → use it
    4. Default to ~/.algo_cli (new location)
    """
    # 1. New explicit env wins
    new_explicit = os.environ.get(f"{NEW_ENV_PREFIX}CONFIG_DIR")
    if new_explicit:
        return Path(new_explicit).expanduser()

    # 2. Old explicit env (legacy)
    old_explicit = os.environ.get(f"{OLD_ENV_PREFIX}CONFIG_DIR")
    if old_explicit:
        return Path(old_explicit).expanduser()

    home = Path.home()
    new_dir = home / NEW_CONFIG_DIR_NAME

    # 3. If the new location already exists, prefer it
    if new_dir.exists():
        return new_dir

    # 4. Default to the new location (migration logic will detect old data later)
    return new_dir


CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
MEMORY_FILE = CONFIG_DIR / "memory.json"
MEMORY_CANDIDATE_STATE_FILE = CONFIG_DIR / "memory_candidate_state.json"
HISTORY_DIR = CONFIG_DIR / "saves"
CONTEXT_ARCHIVE_DIR = CONFIG_DIR / "context_archives"
PROMPT_HISTORY_FILE = CONFIG_DIR / "prompt_history.txt"
PERF_HISTORY_FILE = CONFIG_DIR / "perf_history.jsonl"
EMBED_PERF_FILE = CONFIG_DIR / "embed_perf.jsonl"
DEFAULT_RUNTIME_ENV_FILE = CONFIG_DIR / "env"
DOTENV_RUNTIME_ENV_FILE = CONFIG_DIR / ".env"
LEGACY_MIGRATION_COMPLETE_FILE = ".migrated_from_legacy"
LEGACY_MIGRATION_INCOMPLETE_FILE = ".legacy_migration_incomplete"


class LegacyMigrationError(RuntimeError):
    """Legacy state could not be migrated without weakening memory policy."""


DEFAULT_MODEL = os.environ.get(f"{NEW_ENV_PREFIX}MODEL") or os.environ.get(f"{OLD_ENV_PREFIX}MODEL") or "qwen3"
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_THEME = os.environ.get(f"{NEW_ENV_PREFIX}THEME") or os.environ.get(f"{OLD_ENV_PREFIX}THEME") or "tokyo-night"
DEFAULT_CHAT_STREAM_TIMEOUT_SECONDS = 300.0
CODE_RAG_CONSENT_VERSION = 1
MEMORY_AUTO_CAPTURE_CONSENT_VERSION = 1

ATTEMPT_LEDGER_MAX_ENTRIES = 48
ATTEMPT_LEDGER_MAX_TOOL_CHARS = 128
ATTEMPT_LEDGER_MAX_RESULT_COUNT = 1_000_000_000
MAX_JSON_STATE_BYTES = 16 * 1024 * 1024
_ATTEMPT_LEDGER_STATUSES = frozenset(
    {"worked", "failed", "denied", "skipped", "timed_out", "cancelled", "unknown_outcome"}
)
_ATTEMPT_TOOL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_HMAC_RECEIPT_RE = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")
_ATTEMPT_SUMMARY_RE = re.compile(
    r"status=(worked|failed|denied|skipped|timed_out|cancelled|unknown_outcome); "
    r"chars=([0-9]{1,10}); bytes=([0-9]{1,10}); "
    r"digest=(hmac-sha256:[0-9a-f]{64})\Z"
)
_ECHO_TOOL_RE = re.compile(r"echo_veil_[a-z0-9_]{1,64}\Z")
_PROTECTED_SESSION_COMMANDS = frozenset({"/memory", "/memories", "/remember", "/forget"})
_ECHO_SUMMARY_MARKER_RE = re.compile(r"\becho[ _-]+veil(?:[ _-]|\b)", re.IGNORECASE)
_PROTECTED_ECHO_SUMMARY = "[Protected Echo Veil material omitted from persisted summary.]"


def sanitize_attempt_ledger(value: Any) -> list[dict[str, Any]]:
    """Retain only the typed, content-free attempt receipt schema.

    Releases before the receipt schema persisted free-form argument previews
    and result prefixes.  Those entries cannot be migrated safely because the
    plaintext may itself be protected tool material, so they are dropped.
    """

    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    allowed_keys = {
        "timestamp",
        "signature",
        "tool",
        "args_receipt",
        "status",
        "summary",
        "retry_allowed",
    }
    required_keys = allowed_keys - {"retry_allowed"}
    for item in value[-ATTEMPT_LEDGER_MAX_ENTRIES:]:
        if not isinstance(item, dict) or not required_keys.issubset(item) or not set(item).issubset(allowed_keys):
            continue
        timestamp = item.get("timestamp")
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
            or not 0 <= float(timestamp) <= 100_000_000_000
        ):
            continue
        tool = item.get("tool")
        status = item.get("status")
        signature = item.get("signature")
        args_receipt = item.get("args_receipt")
        summary = item.get("summary")
        if not isinstance(tool, str) or _ATTEMPT_TOOL_RE.fullmatch(tool) is None:
            continue
        if not isinstance(status, str) or status not in _ATTEMPT_LEDGER_STATUSES:
            continue
        if not isinstance(signature, str) or _HMAC_RECEIPT_RE.fullmatch(signature) is None:
            continue
        if not isinstance(args_receipt, str) or _HMAC_RECEIPT_RE.fullmatch(args_receipt) is None:
            continue
        if not isinstance(summary, str):
            continue
        summary_match = _ATTEMPT_SUMMARY_RE.fullmatch(summary)
        if summary_match is None or summary_match.group(1) != status:
            continue
        chars = int(summary_match.group(2))
        encoded_bytes = int(summary_match.group(3))
        if (
            chars > ATTEMPT_LEDGER_MAX_RESULT_COUNT
            or encoded_bytes > ATTEMPT_LEDGER_MAX_RESULT_COUNT
            or encoded_bytes < chars
        ):
            continue
        if "retry_allowed" in item and not isinstance(item.get("retry_allowed"), bool):
            continue
        clean: dict[str, Any] = {
            "timestamp": float(timestamp),
            "signature": signature,
            "tool": tool,
            "args_receipt": args_receipt,
            "status": status,
            "summary": summary,
        }
        if "retry_allowed" in item:
            clean["retry_allowed"] = item["retry_allowed"]
        sanitized.append(clean)
    return sanitized


def _echo_tool_name(value: object) -> str:
    name = str(value or "").strip().casefold()
    return name if _ECHO_TOOL_RE.fullmatch(name) is not None else ""


def sanitize_persisted_summary(value: object) -> str:
    """Remove legacy Echo result material before summary persistence/use."""

    text = str(value or "")
    if _ECHO_SUMMARY_MARKER_RE.search(text):
        return _PROTECTED_ECHO_SUMMARY
    return text


def _config_selects_echo_authority(config: object) -> bool:
    if isinstance(config, dict):
        enabled = config.get("echo_veil_enabled", False)
        protection = config.get("echo_veil_protection", "optional")
    else:
        enabled = getattr(config, "echo_veil_enabled", False)
        protection = getattr(config, "echo_veil_protection", "optional")
    return bool(enabled) or str(protection or "optional").strip().casefold() == "required"


def echo_authority_selected_for_persistence(config: object) -> bool:
    """Expose the config-only authority check without importing the adapter."""

    return _config_selects_echo_authority(config)


def persisted_session_summary(config: object) -> str:
    """Return only summary text whose protected provenance is recoverable."""

    if _config_selects_echo_authority(config):
        return ""
    if isinstance(config, dict):
        value = config.get("session_summary", "")
    else:
        value = getattr(config, "session_summary", "")
    return sanitize_persisted_summary(value)


def _echo_result_receipt(content: object) -> str:
    text = str(content or "")
    encoded = text.encode("utf-8", errors="replace")
    return (
        "[Protected Echo Veil tool result omitted from persisted history; "
        f"chars={min(len(text), ATTEMPT_LEDGER_MAX_RESULT_COUNT)}; "
        f"bytes={min(len(encoded), ATTEMPT_LEDGER_MAX_RESULT_COUNT)}.]"
    )


def _protected_persisted_tool_call(
    name: str,
    arguments: object,
    *,
    echo_authority: bool,
) -> bool:
    if _echo_tool_name(name):
        return True
    if str(name or "").strip().casefold() != "session_command":
        return False
    parsed = arguments
    if isinstance(parsed, str):
        if len(parsed.encode("utf-8", errors="replace")) > 4_096:
            return echo_authority
        try:
            parsed = json.loads(parsed)
        except (json.JSONDecodeError, UnicodeError):
            return echo_authority
    if not isinstance(parsed, dict) or set(parsed) - {"command"}:
        return echo_authority
    command = parsed.get("command")
    if not isinstance(command, str) or len(command.encode("utf-8", errors="replace")) > 4_096:
        return echo_authority
    root = command.strip().split(maxsplit=1)[0].casefold() if command.strip() else ""
    return root in _PROTECTED_SESSION_COMMANDS


def project_messages_for_persistence(
    messages: object,
    *,
    echo_authority: bool = False,
) -> list[dict[str, Any]]:
    """Project Echo calls/results while leaving current-turn RAM messages intact."""

    if not isinstance(messages, list):
        return []
    projected: list[dict[str, Any]] = []
    pending_by_id: dict[str, tuple[str, bool]] = {}
    pending_calls: list[tuple[str, bool]] = []
    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        message = dict(raw_message)
        role = str(message.get("role") or "")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            calls: list[Any] = []
            for raw_call in message["tool_calls"]:
                if not isinstance(raw_call, dict):
                    if not echo_authority:
                        calls.append(raw_call)
                    continue
                call = dict(raw_call)
                function = call.get("function")
                if not isinstance(function, dict):
                    if not echo_authority:
                        calls.append(call)
                    continue
                function_copy = dict(function)
                normalized_name = str(function_copy.get("name") or "").strip()
                protected_call = _protected_persisted_tool_call(
                    normalized_name,
                    function_copy.get("arguments"),
                    echo_authority=echo_authority,
                )
                call_id = str(call.get("id") or "").strip()
                if call_id and normalized_name:
                    pending_by_id[call_id] = (normalized_name, protected_call)
                if normalized_name:
                    pending_calls.append((normalized_name, protected_call))
                if protected_call:
                    arguments = function_copy.get("arguments")
                    function_copy["arguments"] = "{}" if isinstance(arguments, str) else {}
                    call["function"] = function_copy
                calls.append(call)
            message["tool_calls"] = calls
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            paired = pending_by_id.pop(call_id, None) if call_id else None
            if paired is not None:
                try:
                    pending_calls.remove(paired)
                except ValueError:
                    pass
            elif not call_id and pending_calls:
                paired = pending_calls.pop(0)
            paired_name, paired_protected = paired or ("", False)
            observed_name = str(message.get("tool_name") or message.get("name") or paired_name).strip()
            protected_result = paired_protected or bool(_echo_tool_name(observed_name))
            unpaired = paired is None
            if protected_result or (echo_authority and unpaired):
                safe_name = observed_name if _ATTEMPT_TOOL_RE.fullmatch(observed_name) is not None else "protected_tool"
                protected: dict[str, Any] = {
                    "role": "tool",
                    "name": safe_name,
                    "tool_name": safe_name,
                    "content": (
                        _echo_result_receipt(message.get("content") or message.get("thinking"))
                        if protected_result
                        else "[Unpaired tool result omitted from Echo-authoritative persisted history.]"
                    ),
                }
                if call_id:
                    protected["tool_call_id"] = call_id[:256]
                message = protected
        projected.append(message)
    return projected


def code_rag_consent_granted(cfg: Any) -> bool:
    """Return whether this config explicitly accepted the current code-RAG policy."""

    return bool(getattr(cfg, "code_rag_enabled", False)) and (
        getattr(cfg, "code_rag_consent_version", 0) == CODE_RAG_CONSENT_VERSION
    )


def memory_auto_capture_consent_granted(cfg: Any) -> bool:
    """Return whether automatic memory capture has current explicit consent."""

    return bool(getattr(cfg, "memory_auto_capture_enabled", False)) and (
        getattr(cfg, "memory_auto_capture_consent_version", 0) == MEMORY_AUTO_CAPTURE_CONSENT_VERSION
    )


def safe_conversation_name(name: str) -> str:
    safe_name = "".join(ch for ch in str(name) if ch.isalnum() or ch in ("-", "_")).strip()
    if not safe_name:
        raise ValueError("Save name must contain letters, numbers, hyphen, or underscore.")
    return safe_name


LEGACY_DEFAULT_SYSTEM = """You are Algo CLI: a concise, terminal-native coding assistant (local Ollama, Ollama Cloud, or Grok).

Use tools when they materially help. When the user named a file or path, open it directly (read_file/grep) instead of exploratory list/search chains.

Operating rules:
- Prefer narrow reads and targeted grep over broad directory walks.
- Do not run destructive commands unless the user clearly asked and the operation is approved.
- Treat web results, harness RAG, and knowledge-graph blocks as hints — verify with tools before acting.
- When reconciling structured files, rank sources by authority and preserve the target schema. If a target value traces to stale lower-authority context, replace that existing semantic slot with the authoritative value instead of merely adding a differently named duplicate.
- Keep user-facing text brief: lead with the answer or action, minimize preamble and recap.
- Use append_lesson or remember only when the user explicitly asks to store a lesson or fact. Automatic capture is off until the user explicitly enables /memory-auto; its bounded completion gate then sees only original user text.
- For Algo algorithm/pattern catalog guidance, use and update docs/ALGO.md.
- Format code blocks with language tags and include paths when citing code."""

DEFAULT_SYSTEM = """You are Algo CLI: a concise, terminal-native agent runtime for coding, research, and operational work.

Inference may come from local Ollama or connected cloud providers such as Ollama Cloud, xAI Grok, and ChatGPT/Codex. Your job is to plan, act with tools, verify results, and retain useful context across sessions.

Use tools when they materially help. When the user named a file or path, open it directly (read_file/grep) instead of exploratory list/search chains.

Operating rules:
- Prefer narrow reads and targeted grep over broad directory walks.
- Do not run destructive commands unless the user clearly asked and the operation is approved.
- Treat web results, harness RAG, and knowledge-graph blocks as hints — verify with tools before acting.
- Keep user-facing text brief: lead with the answer or action, minimize preamble and recap.
- Use append_lesson or remember only when the user explicitly asks to store a lesson or fact. Automatic capture is off until the user explicitly enables /memory-auto; its bounded completion gate then sees only original user text.
- For Algo algorithm/pattern catalog guidance, use and update docs/ALGO.md.
- Format code blocks with language tags and include paths when citing code."""


def _config_relative_path(path: Path) -> Path | None:
    try:
        base = Path(os.path.abspath(os.fspath(CONFIG_DIR)))
        candidate = Path(os.path.abspath(os.fspath(path)))
        if os.name == "nt":
            # Collapse case, 8.3, junction, and long-name spellings before
            # classifying whether a path belongs to the private state root.
            base = Path(os.path.realpath(os.fspath(base)))
            candidate = Path(os.path.realpath(os.fspath(candidate)))
        return candidate.relative_to(base)
    except (OSError, TypeError, ValueError):
        return None


def _ensure_private_config_parent(path: Path, *, require_windows_private: bool = False) -> bool:
    """Create/check owner-only config directories for config-contained paths."""

    relative = _config_relative_path(path)
    if relative is None:
        if os.name == "nt":
            if require_windows_private:
                # An explicitly configured external store is not Algo-owned
                # namespace. Validate it, but never rewrite the caller's
                # directory or existing target ACL as a side effect.
                _ensure_windows_real_directory(path.parent)
                # The caller's parent may intentionally be traversable or
                # readable (for example, a per-user runtime directory).  It
                # need not inherit the secret file's confidentiality policy;
                # it must instead exclude untrusted child creation/mutation.
                # `_atomic_write_text` creates the named stage with a
                # protected DACL in the CreateFileW call itself.
                if not _windows_safe_creation_dacl(path.parent):
                    raise OSError("external private Windows persistence parent ACL is unsafe")
                if path.exists() or path.is_symlink():
                    information = path.lstat()
                    if (
                        _path_is_reparse_point(path, information)
                        or not stat.S_ISREG(information.st_mode)
                        or information.st_nlink != 1
                    ):
                        raise OSError("private Windows persistence target is unsafe")
                    if not _windows_private_dacl(path):
                        raise OSError("external private Windows persistence target ACL is unsafe")
            else:
                _ensure_windows_real_directory(path.parent)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
        return bool(os.name == "nt" and require_windows_private)
    base = Path(os.path.abspath(os.fspath(CONFIG_DIR)))
    if os.name == "nt":
        _ensure_windows_private_directory(base)
    else:
        base.mkdir(parents=True, mode=0o700, exist_ok=True)
    current = base
    directories = [base]
    for part in relative.parts[:-1]:
        current /= part
        if os.name == "nt":
            _ensure_windows_private_directory(current)
        else:
            current.mkdir(mode=0o700, exist_ok=True)
        directories.append(current)
    for directory in directories:
        info = directory.lstat()
        if _path_is_reparse_point(directory, info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("config persistence directory identity is unsafe")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError("config persistence directory ownership is unsafe")
        if os.name == "posix":
            os.chmod(directory, 0o700)
        elif os.name == "nt" and not _windows_private_dacl(directory):
            raise OSError("config persistence directory ACL is unsafe")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if _path_is_reparse_point(path, info) or not stat.S_ISREG(info.st_mode):
            raise OSError("config persistence target identity is unsafe")
        if os.name == "nt":
            if info.st_nlink != 1:
                raise OSError("config persistence target ACL or link identity is unsafe")
            if not _windows_private_dacl(path):
                _windows_canonicalize_private_path(path, directory=False)
    if os.name == "nt":
        _directory_chain(Path(os.path.abspath(os.fspath(path.parent))))
    return True


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text using fsync + atomic replace to avoid truncated state files."""
    private_windows_stage = bool(os.name == "nt" and path.name.startswith(".") and path.name.endswith(".elsie-pending"))
    private_config_path = _ensure_private_config_parent(
        path,
        require_windows_private=private_windows_stage,
    )
    # Every Windows caller persists internal state.  Use a protected DACL for
    # the stage at CreateFileW time even for an explicitly configured path
    # outside CONFIG_DIR; never expose bytes under an inherited ACL first.
    private_windows_path = os.name == "nt"
    encoded = text.encode("utf-8")
    parent_guard = (
        _windows_pinned_directory_chain(Path(os.path.abspath(os.fspath(path.parent))))
        if os.name == "nt"
        else nullcontext(())
    )
    with parent_guard as windows_parent_chain:
        fd = -1
        tmp_name = ""
        if os.name == "nt":
            for _attempt in range(128):
                temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
                try:
                    fd = _windows_create_private_file(temporary)
                except FileExistsError:
                    continue
                tmp_name = os.fspath(temporary)
                break
            else:
                raise OSError("config persistence staging name is unavailable")
        else:
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            temporary = Path(tmp_name)
            temp_path_info = temporary.lstat()
            temp_descriptor_info = os.fstat(fd)
            if (
                _path_is_reparse_point(temporary, temp_path_info)
                or not stat.S_ISREG(temp_path_info.st_mode)
                or temp_path_info.st_nlink != 1
                or _portable_state_identity(temp_path_info) != _portable_state_identity(temp_descriptor_info)
                or (private_windows_path and not _windows_private_dacl(temporary))
            ):
                raise OSError("config persistence staging file is unsafe")
            # Keep persisted text byte-stable across platforms.  The default
            # text mode rewrites ``\n`` to ``\r\n`` on Windows.
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
                fd = -1
                tmp.write(text)
                tmp.flush()
                os.fsync(tmp.fileno())
            staged = Path(tmp_name).lstat()
            staged_identity = _portable_state_identity(staged)
            if (
                _path_is_reparse_point(Path(tmp_name), staged)
                or not stat.S_ISREG(staged.st_mode)
                or staged.st_nlink != 1
                or staged.st_size != len(encoded)
                or (private_windows_path and not _windows_private_dacl(Path(tmp_name)))
            ):
                raise OSError("config persistence staging file changed before publication")
            if windows_parent_chain:
                _recheck_directory_chain(windows_parent_chain)
            _move_file_write_through(tmp_name, path, replace=True)
            if private_config_path and os.name == "posix":
                os.chmod(path, 0o600)
            elif private_windows_path:
                _verify_windows_file_publication(
                    path,
                    expected_payload=encoded,
                    expected_identity=staged_identity,
                    require_private_dacl=True,
                )
            if windows_parent_chain:
                _recheck_directory_chain(windows_parent_chain)
            if os.name != "nt":
                dir_fd = os.open(
                    path.parent,
                    os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0)),
                )
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise


def _path_is_reparse_point(path: Path, information: os.stat_result | None = None) -> bool:
    """Return whether *path* is redirected by a link/reparse-point boundary."""

    value = information if information is not None else path.lstat()
    if stat.S_ISLNK(value.st_mode):
        return True
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return os.name == "nt" and bool(int(getattr(value, "st_file_attributes", 0)) & reparse_attribute)


def _portable_state_identity(information: os.stat_result) -> tuple[int, ...]:
    """Normalize identities that differ between Windows path and CRT handles."""

    common = (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
        int(information.st_nlink),
        int(information.st_size),
        int(information.st_mtime_ns),
    )
    if os.name == "nt":
        # Windows path stat and CRT fstat can project different permission bits
        # and ctime meanings for the same file. File attributes carry the
        # security-relevant read-only/reparse state consistently on both views.
        return (*common, int(getattr(information, "st_file_attributes", 0)))
    return (*common, int(information.st_mode), int(information.st_uid), int(information.st_ctime_ns))


def _portable_directory_identity(information: os.stat_result) -> tuple[int, ...]:
    common = (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
    )
    if os.name == "nt":
        return (*common, int(getattr(information, "st_file_attributes", 0)))
    return (*common, int(information.st_uid), int(information.st_mode))


def _portable_lock_identity(information: os.stat_result) -> tuple[int, ...]:
    """Bind a lock inode without treating another waiter's byte write as a swap."""

    common = (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
        int(information.st_nlink),
    )
    if os.name == "nt":
        return (*common, int(getattr(information, "st_file_attributes", 0)))
    return (*common, int(information.st_uid), int(information.st_mode))


def _portable_publication_identity(information: os.stat_result) -> tuple[int, ...]:
    """Bind a renamed file while excluding rename-driven POSIX ctime changes."""

    common = (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
        int(information.st_nlink),
        int(information.st_size),
        int(information.st_mtime_ns),
    )
    if os.name == "nt":
        return (*common, int(getattr(information, "st_file_attributes", 0)))
    return (*common, int(information.st_mode), int(information.st_uid))


def _windows_current_user_sid_string() -> str:
    """Return the current process user's SID without leaking native handles."""

    if os.name != "nt":
        raise OSError("Windows identity APIs are unavailable")
    import ctypes
    from ctypes import wintypes

    advapi32 = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    win_error = getattr(ctypes, "WinError")
    get_last_error = getattr(ctypes, "get_last_error")
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise win_error(get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if required.value <= 0:
            raise win_error(get_last_error())
        token_buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            1,  # TokenUser
            token_buffer,
            required,
            ctypes.byref(required),
        ):
            raise win_error(get_last_error())
        current_sid = ctypes.cast(token_buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
        sid_text = ctypes.c_void_p()
        if not current_sid or not advapi32.ConvertSidToStringSidW(current_sid, ctypes.byref(sid_text)):
            raise win_error(get_last_error())
        try:
            return ctypes.wstring_at(sid_text)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _windows_harden_private_dacl(path: Path) -> None:
    """Install a protected current-user/System DACL on a new private path."""

    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    advapi32 = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    win_error = getattr(ctypes, "WinError")
    get_last_error = getattr(ctypes, "get_last_error")
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorOwner.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    advapi32.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD

    information = path.lstat()
    inheritance = "OICI" if stat.S_ISDIR(information.st_mode) else ""
    current_sid = _windows_current_user_sid_string()
    sddl = f"O:{current_sid}D:P(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;{current_sid})"
    security_descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(security_descriptor),
        None,
    ):
        raise win_error(get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        owner = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        if (
            not advapi32.GetSecurityDescriptorDacl(
                security_descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            )
            or not present.value
            or not dacl.value
        ):
            raise OSError("private Windows DACL could not be constructed")
        if (
            not advapi32.GetSecurityDescriptorOwner(
                security_descriptor,
                ctypes.byref(owner),
                ctypes.byref(owner_defaulted),
            )
            or not owner.value
        ):
            raise OSError("private Windows owner could not be constructed")
        result = advapi32.SetNamedSecurityInfoW(
            os.fspath(path),
            1,  # SE_FILE_OBJECT
            0x00000001 | 0x00000004 | 0x80000000,  # OWNER + DACL + PROTECTED_DACL
            owner,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise win_error(result)
    finally:
        kernel32.LocalFree(security_descriptor)
    if not _windows_private_dacl(path):
        raise OSError("private Windows DACL verification failed")


def _windows_dacl_is_safe(
    path: Path,
    *,
    require_current_owner: bool,
    reject_untrusted_read: bool,
    require_protected_dacl: bool,
    namespace_control_only: bool = False,
    allow_any_owner: bool = False,
    trust_windows_installer: bool = False,
) -> bool:
    """Validate owner and allow ACEs without trusting inherited mode bits."""

    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        get_named_security = advapi32.GetNamedSecurityInfoW
        get_named_security.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        get_named_security.restype = wintypes.DWORD
        advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        advapi32.EqualSid.restype = wintypes.BOOL
        advapi32.IsValidSid.argtypes = (ctypes.c_void_p,)
        advapi32.IsValidSid.restype = wintypes.BOOL
        advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
        advapi32.GetLengthSid.restype = wintypes.DWORD
        advapi32.ConvertStringSidToSidW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.GetAclInformation.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_int,
        )
        advapi32.GetAclInformation.restype = wintypes.BOOL
        advapi32.GetAce.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.GetAce.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorControl.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.WORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p

        class _AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("ace_count", wintypes.DWORD),
                ("acl_bytes_in_use", wintypes.DWORD),
                ("acl_bytes_free", wintypes.DWORD),
            ]

        class _AceHeader(ctypes.Structure):
            _fields_ = [
                ("ace_type", ctypes.c_ubyte),
                ("ace_flags", ctypes.c_ubyte),
                ("ace_size", wintypes.WORD),
            ]

        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        security_descriptor = ctypes.c_void_p()
        result = get_named_security(
            os.fspath(path),
            1,  # SE_FILE_OBJECT
            0x00000001 | 0x00000004,  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if result != 0 or not owner.value or not dacl.value or not security_descriptor.value:
            if security_descriptor.value:
                kernel32.LocalFree(security_descriptor)
            return False
        if require_protected_dacl:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if (
                not advapi32.GetSecurityDescriptorControl(
                    security_descriptor,
                    ctypes.byref(control),
                    ctypes.byref(revision),
                )
                or not int(control.value) & 0x1000  # SE_DACL_PROTECTED
            ):
                kernel32.LocalFree(security_descriptor)
                return False
        converted_sids: list[ctypes.c_void_p] = []
        try:
            trusted_sid_texts = [
                _windows_current_user_sid_string(),
                "S-1-5-18",  # LocalSystem
                "S-1-5-32-544",  # Builtin Administrators
                "S-1-3-0",  # Creator Owner (normally inheritance-only)
                "S-1-3-4",  # Owner Rights
            ]
            if trust_windows_installer:
                trusted_sid_texts.append("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
            trusted_sids: list[ctypes.c_void_p] = []
            for sid_text in trusted_sid_texts:
                sid = ctypes.c_void_p()
                if not advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
                    return False
                converted_sids.append(sid)
                trusted_sids.append(sid)
            if not allow_any_owner:
                trusted_owner_indexes = [0] if require_current_owner else [0, 1, 2]
                if trust_windows_installer:
                    trusted_owner_indexes.append(len(trusted_sids) - 1)
                if not any(advapi32.EqualSid(owner, trusted_sids[index]) for index in trusted_owner_indexes):
                    return False

            acl_information = _AclSizeInformation()
            if not advapi32.GetAclInformation(
                dacl,
                ctypes.byref(acl_information),
                ctypes.sizeof(acl_information),
                2,  # AclSizeInformation
            ):
                return False
            if namespace_control_only:
                unsafe_rights = (
                    0x00000010  # FILE_WRITE_EA
                    | 0x00000040  # FILE_DELETE_CHILD
                    | 0x00000100  # FILE_WRITE_ATTRIBUTES
                    | 0x00010000  # DELETE
                    | 0x00040000  # WRITE_DAC
                    | 0x00080000  # WRITE_OWNER
                    | 0x10000000  # GENERIC_ALL
                    | 0x40000000  # GENERIC_WRITE
                )
            else:
                unsafe_rights = (
                    0x00000002  # FILE_WRITE_DATA / FILE_ADD_FILE
                    | 0x00000004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
                    | 0x00000010  # FILE_WRITE_EA
                    | 0x00000040  # FILE_DELETE_CHILD
                    | 0x00000100  # FILE_WRITE_ATTRIBUTES
                    | 0x00010000  # DELETE
                    | 0x00040000  # WRITE_DAC
                    | 0x00080000  # WRITE_OWNER
                    | 0x10000000  # GENERIC_ALL
                    | 0x40000000  # GENERIC_WRITE
                )
            if reject_untrusted_read:
                unsafe_rights |= (
                    0x00000001  # FILE_READ_DATA / FILE_LIST_DIRECTORY
                    | 0x00000008  # FILE_READ_EA
                    | 0x00000020  # FILE_EXECUTE / FILE_TRAVERSE
                    | 0x00000080  # FILE_READ_ATTRIBUTES
                    | 0x00020000  # READ_CONTROL
                    | 0x20000000  # GENERIC_EXECUTE
                    | 0x80000000  # GENERIC_READ
                )
            allow_ace_types = {0, 4, 5, 9, 11}
            simple_allow_ace_types = {0, 9}
            for index in range(int(acl_information.ace_count)):
                ace_pointer = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)) or not ace_pointer.value:
                    return False
                header = ctypes.cast(ace_pointer, ctypes.POINTER(_AceHeader)).contents
                ace_size = int(header.ace_size)
                if ace_size < 8:
                    return False
                if int(header.ace_flags) & 0x08:  # INHERIT_ONLY_ACE
                    continue
                if int(header.ace_type) not in allow_ace_types:
                    continue
                mask = int(ctypes.c_uint32.from_address(ace_pointer.value + 4).value)
                if not mask & unsafe_rights:
                    continue
                # Object and compound allow ACE layouts place the SID after
                # variable fields. Reject them conservatively when writable.
                if int(header.ace_type) not in simple_allow_ace_types or ace_size < 12:
                    return False
                sid_address = ace_pointer.value + 8
                sid = ctypes.c_void_p(sid_address)
                if not advapi32.IsValidSid(sid):
                    return False
                sid_length = int(advapi32.GetLengthSid(sid))
                if sid_length <= 0 or 8 + sid_length > ace_size:
                    return False
                if not any(advapi32.EqualSid(sid, trusted) for trusted in trusted_sids):
                    return False
            return True
        finally:
            for sid in converted_sids:
                kernel32.LocalFree(sid)
            kernel32.LocalFree(security_descriptor)
    except Exception:
        return False


def _windows_private_dacl(path: Path) -> bool:
    """Require current-user ownership and no untrusted read/write allow ACE."""

    return _windows_dacl_is_safe(
        path,
        require_current_owner=True,
        reject_untrusted_read=True,
        require_protected_dacl=True,
    )


def _windows_safe_creation_dacl(path: Path) -> bool:
    """Allow trusted-owned ancestors that exclude untrusted namespace writes."""

    return _windows_dacl_is_safe(
        path,
        require_current_owner=False,
        reject_untrusted_read=False,
        require_protected_dacl=False,
    )


def _windows_namespace_control_dacl(path: Path, *, boundary: bool = False) -> bool:
    """Reject principals able to replace an existing ancestry edge."""

    return _windows_dacl_is_safe(
        path,
        require_current_owner=False,
        reject_untrusted_read=False,
        require_protected_dacl=False,
        namespace_control_only=True,
        allow_any_owner=False,
        trust_windows_installer=True,
    )


def _windows_create_private_file(path: Path) -> int:
    """Create one new file with its owner/protected DACL present atomically."""

    if os.name != "nt" or not path.is_absolute():
        raise OSError("atomic Windows private-file creation is unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    advapi32 = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    win_error = getattr(ctypes, "WinError")
    get_last_error = getattr(ctypes, "get_last_error")
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("security_descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        ]

    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    current_sid = _windows_current_user_sid_string()
    sddl = f"O:{current_sid}D:P(A;;FA;;;SY)(A;;FA;;;{current_sid})"
    security_descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(security_descriptor),
        None,
    ):
        raise win_error(get_last_error())
    handle: int | None = None
    try:
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            security_descriptor,
            False,
        )
        created = create_file(
            os.fspath(path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0x00000001 | 0x00000002,  # SHARE_READ | SHARE_WRITE; deliberately no SHARE_DELETE
            ctypes.byref(attributes),
            1,  # CREATE_NEW
            0x00000080 | 0x00200000,  # FILE_ATTRIBUTE_NORMAL | OPEN_REPARSE_POINT
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if created in {None, invalid_handle}:
            error = int(get_last_error())
            if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(error, "private staging path already exists", os.fspath(path))
            raise win_error(error)
        handle = int(created)
        try:
            descriptor = int(
                getattr(msvcrt, "open_osfhandle")(
                    handle,
                    os.O_RDWR | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOINHERIT", 0)),
                )
            )
        except Exception:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
            handle = None
            raise
        handle = None  # open_osfhandle transferred ownership to the CRT fd
        try:
            path_info = path.lstat()
            descriptor_info = os.fstat(descriptor)
            final_path = _windows_descriptor_final_path(descriptor)
            if (
                _path_is_reparse_point(path, path_info)
                or not stat.S_ISREG(descriptor_info.st_mode)
                or descriptor_info.st_nlink != 1
                or _portable_state_identity(path_info) != _portable_state_identity(descriptor_info)
                or final_path is None
                or not os.path.samefile(final_path, path)
                or not _windows_private_dacl(path)
            ):
                raise OSError("atomic Windows private staging identity is unsafe")
        except Exception:
            try:
                os.close(descriptor)
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return descriptor
    finally:
        if handle is not None:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        kernel32.LocalFree(security_descriptor)


def _windows_create_private_directory(path: Path) -> None:
    """Create one directory with its owner/protected DACL present atomically."""

    if os.name != "nt" or not path.is_absolute():
        raise OSError("atomic Windows private-directory creation is unavailable")
    import ctypes
    from ctypes import wintypes

    advapi32 = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    win_error = getattr(ctypes, "WinError")
    get_last_error = getattr(ctypes, "get_last_error")
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("security_descriptor", ctypes.c_void_p),
            ("inherit_handle", wintypes.BOOL),
        ]

    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes))
    create_directory.restype = wintypes.BOOL
    current_sid = _windows_current_user_sid_string()
    sddl = f"O:{current_sid}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{current_sid})"
    security_descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(security_descriptor),
        None,
    ):
        raise win_error(get_last_error())
    try:
        attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            security_descriptor,
            False,
        )
        if not create_directory(os.fspath(path), ctypes.byref(attributes)):
            error = int(get_last_error())
            if error in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(error, "private directory already exists", os.fspath(path))
            raise win_error(error)
        try:
            information = path.lstat()
            if (
                _path_is_reparse_point(path, information)
                or not stat.S_ISDIR(information.st_mode)
                or not _windows_private_dacl(path)
            ):
                raise OSError("atomic Windows private directory is unsafe")
        except Exception:
            try:
                path.rmdir()
            except OSError:
                pass
            raise
    finally:
        kernel32.LocalFree(security_descriptor)


def _windows_native_handle_final_path(handle: int) -> Path:
    """Return the DOS path bound to one native Windows file handle."""

    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_final_path.restype = wintypes.DWORD
    size = int(get_final_path(wintypes.HANDLE(handle), None, 0, 0))
    if size <= 0 or size > 32_768:
        raise OSError("Windows handle path is unavailable")
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = int(get_final_path(wintypes.HANDLE(handle), buffer, len(buffer), 0))
    if written <= 0 or written >= len(buffer):
        raise OSError("Windows handle path is unavailable")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


@contextmanager
def _windows_pinned_directory_chain(
    path: Path,
) -> Iterator[tuple[tuple[Path, tuple[int, ...]], ...]]:
    """Pin every Windows ancestry edge against delete/rename until exit."""

    if os.name != "nt":
        yield _directory_chain(path)
        return
    import ctypes
    from ctypes import wintypes

    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.anchor or absolute.anchor.startswith("\\\\"):
        raise OSError("state directory ancestry boundary is unsupported")
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    win_error = getattr(ctypes, "WinError")
    get_last_error = getattr(ctypes, "get_last_error")
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    class _FileAttributeTagInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    handles: list[int] = []
    captured: list[tuple[Path, tuple[int, ...]]] = []
    current = Path(absolute.anchor)
    try:
        for part in (None, *absolute.parts[1:]):
            if part is not None:
                current /= part
            opened = create_file(
                os.fspath(current),
                0x00020000 | 0x00000080,  # READ_CONTROL | FILE_READ_ATTRIBUTES
                0x00000001 | 0x00000002,  # SHARE_READ | SHARE_WRITE; deliberately no SHARE_DELETE
                None,
                3,  # OPEN_EXISTING
                0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
                None,
            )
            invalid_handle = ctypes.c_void_p(-1).value
            if opened in {None, invalid_handle}:
                raise win_error(get_last_error())
            handle = int(opened)
            handles.append(handle)
            attributes = _FileAttributeTagInformation()
            if not get_information(
                wintypes.HANDLE(handle),
                9,  # FileAttributeTagInfo
                ctypes.byref(attributes),
                ctypes.sizeof(attributes),
            ):
                raise win_error(get_last_error())
            information = current.lstat()
            final_path = _windows_native_handle_final_path(handle)
            final_information = final_path.lstat()
            if (
                int(attributes.file_attributes) & 0x00000400  # FILE_ATTRIBUTE_REPARSE_POINT
                or _path_is_reparse_point(current, information)
                or _path_is_reparse_point(final_path, final_information)
                or not stat.S_ISDIR(information.st_mode)
                or not stat.S_ISDIR(final_information.st_mode)
                or _portable_directory_identity(information) != _portable_directory_identity(final_information)
                or not os.path.samefile(current, final_path)
                or not _windows_namespace_control_dacl(current, boundary=current == Path(absolute.anchor))
            ):
                raise OSError("state directory ancestry is unsafe")
            captured.append((current, _portable_directory_identity(information)))
        result = tuple(captured)
        yield result
        _recheck_directory_chain(result)
    finally:
        for handle in reversed(handles):
            close_handle(wintypes.HANDLE(handle))


def _windows_canonicalize_private_path(path: Path, *, directory: bool) -> os.stat_result:
    """Repair a trusted-owned path without accepting a replaceable object."""

    pin_path = path if directory else path.parent
    with _windows_pinned_directory_chain(pin_path) as ancestry:
        before = path.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            _path_is_reparse_point(path, before)
            or not expected_type(before.st_mode)
            or (not directory and before.st_nlink != 1)
            or not _windows_safe_creation_dacl(path)
            or not _windows_safe_creation_dacl(path.parent)
        ):
            raise OSError("private Windows path cannot be canonicalized safely")
        identity = _portable_directory_identity(before) if directory else _portable_state_identity(before)
        descriptor: int | None = None
        try:
            if not directory:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | int(getattr(os, "O_BINARY", 0))
                    | int(getattr(os, "O_CLOEXEC", 0))
                    | int(getattr(os, "O_NOFOLLOW", 0)),
                )
                opened = os.fstat(descriptor)
                final_path = _windows_descriptor_final_path(descriptor)
                if (
                    _portable_state_identity(opened) != identity
                    or final_path is None
                    or not os.path.samefile(final_path, path)
                ):
                    raise OSError("private Windows path changed while opening")
            _windows_harden_private_dacl(path)
            after = path.lstat()
            current_identity = _portable_directory_identity(after) if directory else _portable_state_identity(after)
            if (
                _path_is_reparse_point(path, after)
                or not expected_type(after.st_mode)
                or current_identity != identity
                or (not directory and after.st_nlink != 1)
                or not _windows_private_dacl(path)
            ):
                raise OSError("private Windows path changed during canonicalization")
            _recheck_directory_chain(ancestry)
            return after
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _directory_chain(path: Path) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    """Capture every existing absolute directory component without following links."""

    if os.name == "nt":
        with _windows_pinned_directory_chain(path) as pinned:
            return pinned

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    captured_entries: list[tuple[Path, tuple[int, ...]]] = []
    if not current.anchor:
        raise OSError("state directory path is not absolute")
    root_info = current.lstat()
    if _path_is_reparse_point(current, root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise OSError("state directory ancestry is unsafe")
    if os.name == "nt" and not _windows_namespace_control_dacl(current, boundary=True):
        raise OSError("state directory ancestry authorization is unsafe")
    captured_entries.append((current, _portable_directory_identity(root_info)))
    for part in absolute.parts[1:]:
        current /= part
        information = current.lstat()
        if _path_is_reparse_point(current, information) or not stat.S_ISDIR(information.st_mode):
            raise OSError("state directory ancestry is unsafe")
        if os.name == "nt" and not _windows_namespace_control_dacl(current):
            raise OSError("state directory ancestry authorization is unsafe")
        captured_entries.append((current, _portable_directory_identity(information)))
    return tuple(captured_entries)


def _recheck_directory_chain(captured: tuple[tuple[Path, tuple[int, ...]], ...]) -> None:
    for path, expected in captured:
        current = path.lstat()
        if (
            _path_is_reparse_point(path, current)
            or not stat.S_ISDIR(current.st_mode)
            or _portable_directory_identity(current) != expected
            or (os.name == "nt" and not _windows_namespace_control_dacl(path, boundary=path == Path(path.anchor)))
        ):
            raise OSError("state directory ancestry changed while being read")


def _ensure_windows_private_directory(
    path: Path,
    *,
    require_new: bool = False,
) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    """Create a private directory without writing through a lexical reparse boundary."""

    if os.name != "nt":
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        return _directory_chain(path)
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    if not current.anchor:
        raise OSError("private directory path is not absolute")
    with _windows_pinned_directory_chain(current) as root_chain:
        captured = list(root_chain)
    for part in absolute.parts[1:]:
        candidate = current / part
        created = False
        try:
            information = candidate.lstat()
            if require_new and candidate == absolute:
                raise FileExistsError(os.fspath(candidate))
        except FileNotFoundError:
            with _windows_pinned_directory_chain(current) as parent_chain:
                if not _windows_safe_creation_dacl(current):
                    raise OSError("private directory creation parent ACL is unsafe")
                try:
                    _windows_create_private_directory(candidate)
                    created = True
                except FileExistsError:
                    # A racing creator is acceptable only if the resulting
                    # entry independently satisfies every invariant.
                    if require_new and candidate == absolute:
                        raise
                _recheck_directory_chain(parent_chain)
            information = candidate.lstat()
        if _path_is_reparse_point(candidate, information) or not stat.S_ISDIR(information.st_mode):
            raise OSError("private directory ancestry is unsafe")
        if created and not _windows_private_dacl(candidate):
            raise OSError("new private directory ACL is unsafe")
        with _windows_pinned_directory_chain(candidate) as candidate_chain:
            captured = list(candidate_chain)
        current = candidate
    if not _windows_private_dacl(absolute):
        information = _windows_canonicalize_private_path(absolute, directory=True)
        captured[-1] = (absolute, _portable_directory_identity(information))
    if len(captured) > 1 and not _windows_safe_creation_dacl(captured[-2][0]):
        raise OSError("private directory parent ACL is unsafe")
    result = tuple(captured)
    _recheck_directory_chain(result)
    return result


def _ensure_windows_real_directory(path: Path) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    """Create a generic Windows parent without traversing a lexical reparse point."""

    if os.name != "nt":
        path.mkdir(parents=True, exist_ok=True)
        return _directory_chain(path)
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    if not current.anchor:
        raise OSError("directory path is not absolute")
    with _windows_pinned_directory_chain(current) as root_chain:
        captured = list(root_chain)
    for part in absolute.parts[1:]:
        candidate = current / part
        try:
            information = candidate.lstat()
        except FileNotFoundError:
            with _windows_pinned_directory_chain(current) as parent_chain:
                try:
                    candidate.mkdir()
                except FileExistsError:
                    pass
                _recheck_directory_chain(parent_chain)
            information = candidate.lstat()
        if _path_is_reparse_point(candidate, information) or not stat.S_ISDIR(information.st_mode):
            raise OSError("directory ancestry is unsafe")
        with _windows_pinned_directory_chain(candidate) as candidate_chain:
            captured = list(candidate_chain)
        current = candidate
    result = tuple(captured)
    _recheck_directory_chain(result)
    return result


def _move_file_write_through(source: Path | str, destination: Path | str, *, replace: bool) -> None:
    """Move one same-volume file with durable Windows rename metadata."""

    if os.name != "nt":
        if replace:
            os.replace(source, destination)
        else:
            os.rename(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    win_error = getattr(ctypes, "WinError")
    get_last_error = getattr(ctypes, "get_last_error")
    move_file = kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    flags = 0x00000008 | (0x00000001 if replace else 0)  # WRITE_THROUGH | REPLACE_EXISTING
    if not move_file(os.fspath(source), os.fspath(destination), flags):
        raise win_error(get_last_error())


def _verify_windows_file_publication(
    path: Path,
    *,
    expected_payload: bytes,
    expected_identity: tuple[int, ...],
    require_private_dacl: bool,
) -> None:
    """Bind a write-through publication back to its exact staged file and bytes."""

    descriptor = os.open(
        path,
        os.O_RDONLY
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0)),
    )
    try:
        opened = os.fstat(descriptor)
        before = path.lstat()
        if (
            _path_is_reparse_point(path, before)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _portable_state_identity(opened) != expected_identity
            or _portable_state_identity(before) != expected_identity
            or (require_private_dacl and not _windows_private_dacl(path))
        ):
            raise OSError("state publication identity is unsafe")
        chunks: list[bytes] = []
        remaining = len(expected_payload) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            b"".join(chunks) != expected_payload
            or _path_is_reparse_point(path, current)
            or after.st_nlink != 1
            or _portable_state_identity(after) != expected_identity
            or _portable_state_identity(current) != expected_identity
            or (require_private_dacl and not _windows_private_dacl(path))
        ):
            raise OSError("state publication changed during verification")
    finally:
        os.close(descriptor)


def _windows_descriptor_final_path(descriptor: int) -> Path | None:
    if os.name != "nt":
        return None
    try:
        import msvcrt

        handle = int(getattr(msvcrt, "get_osfhandle")(descriptor))
        return _windows_native_handle_final_path(handle)
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise OSError("state descriptor path is unavailable") from exc


def _state_payload_by_path(
    selected: Path,
    *,
    relative: Path | None,
    max_bytes: int,
    file_flags: int,
    require_single_link: bool = False,
) -> bytes:
    """Windows-safe full-path reader with ancestry and handle-path binding."""

    lexical = Path(os.path.abspath(os.fspath(selected)))
    # Native Windows paths must retain every lexical component so junctions
    # remain visible to the reparse check. POSIX fallback tests may begin below
    # an immutable system alias such as macOS /var -> /private/var.
    absolute = lexical if os.name == "nt" else Path(os.path.realpath(os.fspath(lexical.parent))) / lexical.name
    ancestry_guard = (
        _windows_pinned_directory_chain(absolute.parent)
        if os.name == "nt"
        else nullcontext(_directory_chain(absolute.parent))
    )
    with ancestry_guard as ancestry:
        return _state_payload_from_bound_path(
            absolute,
            relative=relative,
            max_bytes=max_bytes,
            file_flags=file_flags,
            ancestry=ancestry,
            require_single_link=require_single_link,
        )


def _state_payload_from_bound_path(
    absolute: Path,
    *,
    relative: Path | None,
    max_bytes: int,
    file_flags: int,
    ancestry: tuple[tuple[Path, tuple[int, ...]], ...],
    require_single_link: bool,
) -> bytes:
    """Read one file while its already-authorized ancestry remains pinned."""

    private_root = Path(os.path.realpath(os.fspath(CONFIG_DIR))) if relative is not None else None
    if private_root is not None:
        private_paths = [
            path
            for path, _identity in ancestry
            if (
                (resolved := Path(os.path.realpath(os.fspath(path)))) == private_root
                or private_root in resolved.parents
            )
        ]
        if not private_paths or Path(os.path.realpath(os.fspath(private_paths[0]))) != private_root:
            raise OSError("config state root is unsafe")
        for directory in private_paths:
            information = directory.lstat()
            if os.name == "posix":
                if information.st_uid != os.getuid() or information.st_mode & 0o022:
                    raise OSError("config state directory permissions are unsafe")
            elif os.name == "nt" and not _windows_private_dacl(directory):
                _windows_canonicalize_private_path(directory, directory=True)

    before = absolute.lstat()
    if (
        _path_is_reparse_point(absolute, before)
        or not stat.S_ISREG(before.st_mode)
        or not 0 <= before.st_size <= max_bytes
    ):
        raise OSError("state file is not a bounded regular file")
    if relative is not None or require_single_link:
        if before.st_nlink != 1:
            raise OSError("config state file identity is unsafe")
    if relative is not None:
        if os.name == "posix" and (before.st_uid != os.getuid() or before.st_mode & 0o022):
            raise OSError("config state file permissions are unsafe")
        if os.name == "nt" and not _windows_private_dacl(absolute):
            before = _windows_canonicalize_private_path(absolute, directory=False)

    descriptor = os.open(absolute, file_flags | int(getattr(os, "O_BINARY", 0)))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _portable_state_identity(opened) != _portable_state_identity(before):
            raise OSError("state file changed while opening")
        final_path = _windows_descriptor_final_path(descriptor)
        if final_path is not None:
            final_info = final_path.lstat()
            if (
                _path_is_reparse_point(final_path, final_info)
                or _portable_state_identity(final_info) != _portable_state_identity(opened)
                or not os.path.samefile(final_path, absolute)
            ):
                raise OSError("state descriptor path changed while opening")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        current = absolute.lstat()
        if (
            len(payload) > max_bytes
            or _portable_state_identity(after) != _portable_state_identity(opened)
            or _portable_state_identity(current) != _portable_state_identity(opened)
            or _path_is_reparse_point(absolute, current)
        ):
            raise OSError("state file changed while being read")
        _recheck_directory_chain(ancestry)
        return payload
    finally:
        os.close(descriptor)


def _directory_descriptor_io_supported() -> bool:
    return os.name != "nt" and os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd


def _state_descriptor_payload(path: Path, *, max_bytes: int) -> bytes:
    """Read one stable regular state file through pinned no-follow descriptors."""

    if not 0 < int(max_bytes) <= MAX_JSON_STATE_BYTES:
        raise OSError("state read bound is invalid")
    selected = Path(path)
    relative = _config_relative_path(selected)
    directory_flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    file_flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_NONBLOCK", 0))
    )
    if os.name == "nt":
        return _state_payload_by_path(
            selected,
            relative=relative,
            max_bytes=max_bytes,
            file_flags=file_flags,
        )
    if not _directory_descriptor_io_supported():
        raise OSError("secure directory-descriptor state reads are unavailable")
    descriptors: list[int] = []
    try:
        if relative is None:
            parent_fd = os.open(selected.parent, directory_flags)
            descriptors.append(parent_fd)
            file_name = selected.name
            root_binding: tuple[Path, int, int] | None = None
        else:
            root = Path(os.path.abspath(os.fspath(CONFIG_DIR)))
            root_fd = os.open(root, directory_flags)
            descriptors.append(root_fd)
            root_info = os.fstat(root_fd)
            if not stat.S_ISDIR(root_info.st_mode):
                raise OSError("config state root is unsafe")
            if hasattr(os, "getuid") and root_info.st_uid != os.getuid():
                raise OSError("config state root ownership is unsafe")
            if root_info.st_mode & 0o022:
                raise OSError("config state root permissions are unsafe")
            root_binding = (root, root_info.st_dev, root_info.st_ino)
            parent_fd = root_fd
            for part in relative.parts[:-1]:
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                descriptors.append(child_fd)
                child_info = os.fstat(child_fd)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise OSError("config state parent is unsafe")
                if hasattr(os, "getuid") and child_info.st_uid != os.getuid():
                    raise OSError("config state parent ownership is unsafe")
                if child_info.st_mode & 0o022:
                    raise OSError("config state parent permissions are unsafe")
                parent_fd = child_fd
            file_name = relative.parts[-1]
        file_fd = os.open(file_name, file_flags, dir_fd=parent_fd)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= max_bytes:
            raise OSError("state file is not a bounded regular file")
        if relative is not None:
            if hasattr(os, "getuid") and before.st_uid != os.getuid():
                raise OSError("config state file ownership is unsafe")
            if before.st_nlink != 1 or before.st_mode & 0o022:
                raise OSError("config state file identity is unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(file_fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(file_fd)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(payload) > max_bytes or any(
            getattr(before, field, None) != getattr(after, field, None) for field in stable_fields
        ):
            raise OSError("state file changed while being read")
        current = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise OSError("state file path changed while being read")
        if root_binding is not None:
            root, root_dev, root_ino = root_binding
            current_root = root.lstat()
            if stat.S_ISLNK(current_root.st_mode) or (current_root.st_dev, current_root.st_ino) != (root_dev, root_ino):
                raise OSError("config state root changed while being read")
        return payload
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_json_file(
    path: Path,
    default: Any,
    *,
    preserve_corrupt: bool = True,
    max_bytes: int = MAX_JSON_STATE_BYTES,
) -> Any:
    try:
        payload = _state_descriptor_payload(path, max_bytes=max_bytes)
        return json.loads(payload.decode("utf-8", errors="strict"))
    except (FileNotFoundError, NotADirectoryError):
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        if preserve_corrupt:
            backup = path.with_suffix(path.suffix + ".corrupt")
            try:
                _atomic_write_text(backup, payload.decode("utf-8", errors="replace"))
            except OSError:
                pass
        return default
    except OSError:
        return default


@contextmanager
def _exclusive_state_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Cross-platform advisory lock for state-file transactions.

    On Windows, uses msvcrt.locking with non-blocking attempts and retries.
    Stale lock files from crashed processes are automatically released by the OS
    when file handles close, but permission errors on lock file creation are
    retried with a short backoff.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    private_config_path = _ensure_private_config_parent(path)
    ancestry_guard = _windows_pinned_directory_chain(lock_path.parent) if os.name == "nt" else nullcontext(())
    with ancestry_guard as windows_lock_chain:
        with _exclusive_state_lock_bound(
            lock_path,
            timeout_seconds=timeout_seconds,
            private_config_path=private_config_path,
            windows_lock_chain=windows_lock_chain,
        ):
            yield


@contextmanager
def _exclusive_state_lock_bound(
    lock_path: Path,
    *,
    timeout_seconds: float,
    private_config_path: bool,
    windows_lock_chain: tuple[tuple[Path, tuple[int, ...]], ...],
) -> Iterator[None]:
    if lock_path.exists() or lock_path.is_symlink():
        lock_info = lock_path.lstat()
        if (
            _path_is_reparse_point(lock_path, lock_info)
            or not stat.S_ISREG(lock_info.st_mode)
            or (os.name == "nt" and lock_info.st_nlink != 1)
        ):
            raise OSError("state lock identity is unsafe")
        if os.name == "nt" and not _windows_private_dacl(lock_path):
            if not private_config_path:
                raise OSError("external state lock ACL is unsafe")
            _windows_canonicalize_private_path(lock_path, directory=False)
    deadline = time.monotonic() + timeout_seconds
    # Retry opening the lock file if permission is denied briefly
    lock_file = None
    while lock_file is None:
        try:
            if windows_lock_chain:
                _recheck_directory_chain(windows_lock_chain)
            if os.name == "nt":
                try:
                    descriptor = os.open(
                        lock_path,
                        os.O_RDWR
                        | os.O_APPEND
                        | int(getattr(os, "O_BINARY", 0))
                        | int(getattr(os, "O_CLOEXEC", 0))
                        | int(getattr(os, "O_NOFOLLOW", 0)),
                    )
                except FileNotFoundError:
                    try:
                        descriptor = _windows_create_private_file(lock_path)
                    except FileExistsError:
                        continue
            else:
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_APPEND
                    | int(getattr(os, "O_BINARY", 0))
                    | int(getattr(os, "O_CLOEXEC", 0))
                    | int(getattr(os, "O_NOFOLLOW", 0)),
                    0o600,
                )
            try:
                opened = os.fstat(descriptor)
                current = lock_path.lstat()
                if (
                    _path_is_reparse_point(lock_path, current)
                    or not stat.S_ISREG(opened.st_mode)
                    or _portable_lock_identity(current) != _portable_lock_identity(opened)
                    or (os.name == "nt" and opened.st_nlink != 1)
                ):
                    raise OSError("state lock identity is unsafe")
                if os.name == "nt":
                    if not _windows_private_dacl(lock_path):
                        raise OSError("state lock ACL or identity is unsafe")
                final_path = _windows_descriptor_final_path(descriptor)
                if final_path is not None and not os.path.samefile(final_path, lock_path):
                    raise OSError("state lock descriptor path is unsafe")
                if windows_lock_chain:
                    _recheck_directory_chain(windows_lock_chain)
                lock_file = os.fdopen(descriptor, "a+b")
                descriptor = -1
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"x")
                lock_file.flush()
            try:
                os.chmod(lock_path, 0o600)
            except (OSError, NotImplementedError):
                pass
            info = os.fstat(lock_file.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name == "nt" and info.st_nlink != 1):
                lock_file.close()
                lock_file = None
                raise OSError("state lock identity is unsafe")
        except PermissionError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for state lock: {lock_path}")
            time.sleep(0.05)
            lock_file = None
            continue
        except OSError:
            raise
    try:
        if os.name == "nt":
            import msvcrt

            lock_region = getattr(msvcrt, "locking")
            lock_nonblocking = getattr(msvcrt, "LK_NBLCK")
            unlock = getattr(msvcrt, "LK_UNLCK")
            while True:
                try:
                    lock_file.seek(0)
                    lock_region(lock_file.fileno(), lock_nonblocking, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for state lock: {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                lock_region(lock_file.fileno(), unlock, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for state lock: {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if lock_file is not None:
            lock_file.close()
        if windows_lock_chain:
            _recheck_directory_chain(windows_lock_chain)
        # Do not unlink the lock file. Removing an advisory lock path while
        # another thread/process is waiting can create two different inodes and
        # split the lock, allowing concurrent read-modify-write transactions.


def runtime_env_path(path: Path | str | None = None) -> Path:
    """Return the active runtime-env path without creating or reading it.

    The same precedence is used for reads and writes.  In particular, a new
    ``algo-cli config`` setup writes to ``~/.algo_cli/env`` when neither an
    explicit env-file path nor a legacy ``.env`` file already exists.
    """

    configured_path = path or os.environ.get(f"{NEW_ENV_PREFIX}ENV_FILE") or os.environ.get(f"{OLD_ENV_PREFIX}ENV_FILE")
    if configured_path is not None:
        return Path(configured_path).expanduser()
    if DEFAULT_RUNTIME_ENV_FILE.exists() or not DOTENV_RUNTIME_ENV_FILE.exists():
        return DEFAULT_RUNTIME_ENV_FILE
    return DOTENV_RUNTIME_ENV_FILE


def load_runtime_env(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    env_path = runtime_env_path(path)
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded

    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return loaded

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _RUNTIME_ENV_KEY_RE.fullmatch(key):
            continue
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            # Values written by update_runtime_env use JSON quoting so spaces,
            # quotes, and Windows paths round-trip exactly.  Keep accepting
            # legacy shell-style values when they are not valid JSON strings.
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
            else:
                value = decoded if isinstance(decoded, str) else value[1:-1]
        elif len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        try:
            if key in os.environ and not override:
                loaded[key] = os.environ[key]
                continue
            os.environ[key] = value
            loaded[key] = value
        except (OSError, ValueError):
            # A corrupted env file must not make every CLI command fail during
            # startup (for example, a value containing a NUL byte).
            continue
    return loaded


_RUNTIME_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _runtime_env_assignment_key(raw_line: str) -> str | None:
    """Return an env key from a line without interpreting its secret value."""

    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        return None
    key = line.split("=", 1)[0].strip()
    return key if _RUNTIME_ENV_KEY_RE.fullmatch(key) else None


def _format_runtime_env_value(value: str) -> str:
    """Encode an env value without allowing comments or newlines to escape."""

    if not value:
        return ""
    if all(not char.isspace() and char not in {"#", "'", '"', "\\"} for char in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def update_runtime_env(
    values: Mapping[str, str | None],
    path: Path | str | None = None,
) -> Path:
    """Atomically update selected runtime-env values with private file mode.

    ``None`` removes a value.  Existing comments and unrelated settings remain
    byte-for-byte intact apart from normalizing the final newline.  Values are
    applied to the current process as well, so a guided setup can start an
    OAuth flow immediately after saving its client configuration.
    """

    normalized: dict[str, str | None] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key).strip()
        if not _RUNTIME_ENV_KEY_RE.fullmatch(key):
            raise ValueError(f"Invalid runtime environment key: {raw_key!r}")
        if raw_value is not None:
            value = str(raw_value)
            if "\n" in value or "\r" in value:
                raise ValueError(f"Runtime environment value for {key} must not contain a newline.")
            normalized[key] = value
        else:
            normalized[key] = None

    env_path = runtime_env_path(path)
    # This is a read-modify-write update, so atomic replacement alone is not
    # sufficient: two concurrent provider setup commands could otherwise each
    # read the old file and discard the other's setting.  Reuse the state lock
    # used by the rest of the config layer for a single serialized transaction.
    with _exclusive_state_lock(env_path):
        try:
            original_lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            original_lines = []
        except OSError as exc:
            raise RuntimeError(f"Could not read runtime environment file: {exc}") from exc

        output_lines: list[str] = []
        handled: set[str] = set()
        for raw_line in original_lines:
            assignment_key = _runtime_env_assignment_key(raw_line)
            if assignment_key is None or assignment_key not in normalized:
                output_lines.append(raw_line)
                continue
            if assignment_key in handled:
                # Drop duplicate definitions for an updated key.  Leaving both
                # in place makes future setup runs dependent on line order.
                continue
            handled.add(assignment_key)
            configured_value = normalized[assignment_key]
            if configured_value is not None:
                output_lines.append(f"{assignment_key}={_format_runtime_env_value(configured_value)}")

        for env_key, configured_value in normalized.items():
            if env_key not in handled and configured_value is not None:
                output_lines.append(f"{env_key}={_format_runtime_env_value(configured_value)}")

        text = "\n".join(output_lines)
        if text:
            text += "\n"
        _atomic_write_text(env_path, text)
        try:
            os.chmod(env_path, 0o600)
        except (OSError, NotImplementedError):
            pass

    for env_key, configured_value in normalized.items():
        if configured_value is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = configured_value
    return env_path


_INVALID_CONFIG_VALUE = object()


def _coerce_config_value(current: Any, value: Any) -> Any:
    if value is None:
        return _INVALID_CONFIG_VALUE
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
        return _INVALID_CONFIG_VALUE
    if isinstance(current, int) and not isinstance(current, bool):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return _INVALID_CONFIG_VALUE
        return _INVALID_CONFIG_VALUE
    if isinstance(current, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return _INVALID_CONFIG_VALUE
        return _INVALID_CONFIG_VALUE
    if isinstance(current, str):
        return value if isinstance(value, str) else _INVALID_CONFIG_VALUE
    if isinstance(current, list):
        return value if isinstance(value, list) else _INVALID_CONFIG_VALUE
    if isinstance(current, dict):
        return value if isinstance(value, dict) else _INVALID_CONFIG_VALUE
    return value


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    # Explicit provider selected by the model picker. "auto" preserves legacy
    # name-based inference for existing configs and command-line overrides.
    model_provider: str = "auto"  # auto | ollama | xai | chatgpt
    system: str = DEFAULT_SYSTEM
    theme: str = DEFAULT_THEME
    auto_mode: bool = False
    # Session-only auto-approve set by answering "a" at an approval prompt.
    # Never persisted: see save(); resets on every new session.
    session_auto_approve: bool = False
    show_thinking: bool = True
    # Per-model Codex reasoning effort. Empty entries use the provider default
    # (medium); keeping a map lets Sol, Terra, and Luna have independent knobs.
    chatgpt_reasoning_efforts: dict[str, str] = field(default_factory=dict)
    num_ctx: int = 8192
    temperature: float = 0.4
    chat_stream_timeout_seconds: float = DEFAULT_CHAT_STREAM_TIMEOUT_SECONDS
    max_tool_iterations: int = 24
    tool_think_every: int = 10
    prune_after_messages: int = 80
    prune_keep_recent: int = 40
    embedding_backend: str = "auto"  # "auto" | "local"; "cloud" currently falls back to local
    cloud_embedding_model: str = "nomic-embed-text:latest"  # Reserved until cloud embeddings are supported.
    harness_embed_model: str = "qwen3-embedding:latest"  # Local Ollama embed model for harness + lessons RAG
    embed_dimensions: int | None = None  # Optional override; None lets the model decide (e.g. 4096 for qwen3-embedding)
    echo_veil_enabled: bool = False  # Select Echo as the sole ordinary-memory backend
    echo_veil_capacity: int = 400  # Maximum active Echo Veil memories before decay
    echo_veil_protection: str = "optional"  # required also binds the exact qualified runtime identity
    echo_veil_profile: str = "echo-universal-qwen3-v1"  # Shared local Echo authority
    echo_veil_scope: str = "local-user"  # Authorization scope bound into ciphertext
    echo_veil_state_dir: str = ""  # Empty uses Echo Veil's owner-only platform data root
    echo_veil_embedding_dimension: int = 1024
    # Keep protected recall on a bounded CPU runner so a large local agent
    # model can remain resident on the GPU across memory operations.
    echo_veil_embedding_keep_alive_seconds: int = 0
    echo_veil_embedding_context_length: int = 16_384
    echo_veil_embedding_gpu_layers: int = 0
    # Deprecated compatibility fields. They are ignored by the authoritative
    # adapter; raw key references in config are no longer accepted.
    echo_veil_production: bool = False
    echo_veil_crypto_key_path: str | None = None
    memory_auto_capture_enabled: bool = False  # Explicit opt-in only; see consent version below
    memory_auto_capture_consent_version: int = 0
    memory_auto_daily_limit: int = 5  # User may lower; admission hard-maxes at 5/day
    memory_auto_entry_limit: int = 64  # User may lower; admission hard-maxes at 64 fingerprints
    memory_auto_char_limit: int = 12_000  # User may lower; admission hard-maxes at 12k total chars
    skill_crystallize_enabled: bool = False
    skill_crystallize_every: int = 3
    runs_since_crystallize: int = 0
    host: str = DEFAULT_HOST
    cloud: bool = False
    onboarded: bool = False
    auto_cloud_connect: bool = False
    safe_mode: bool = True
    verify_mode: bool = False
    intuition_recall_enabled: bool = False
    intuition_capture_enabled: bool = False
    # Enforced for new Agent runs by default. Ordinary chat and non-approval
    # baseline reads do not use the Agent Block route ceiling.
    algorithmic_tool_policy_enabled: bool = True
    reflex_enabled: bool = False
    model_adaptive: bool = True  # adapt num_ctx/temperature/reflection to model size+provider
    code_rag_enabled: bool = False  # opt in to retrieving cfg.cwd source chunks each turn
    code_rag_consent_version: int = 0  # set only by explicit /code-rag on consent
    external_harness_sources_enabled: bool = False  # opt in to ~/.codex, ~/.claude, ~/.openclaw, etc.
    reasoning_chat_enabled: bool = False  # run reasoning preflight in the chat loop, not just pipelines
    # --- Reasoning engine flags ---
    reasoning_mode: str = "react"  # react | reflexion | tot | got | mcts | qcr | neuro_symbolic | hybrid
    reasoning_depth: int = 4  # Max depth/rounds for tree/graph search
    reasoning_branches: int = 3  # Branch factor for tree/graph expansion
    reasoning_qcr_samples: int = 5  # Number of CoT fragments for QCR aggregation
    reasoning_reflexion_attempts: int = 3  # Max self-critique rounds
    reasoning_ns_rounds: int = 3  # Max neuro-symbolic verify rounds
    reasoning_auto_reflexion: bool = False  # Auto-apply Reflexion on failed blocks
    reasoning_auto_verify: bool = False  # Auto-verify implement blocks with neuro-symbolic
    index_compute_lab_auto_inject: bool = False
    session_mode: str = "explore"  # execute | explore | publish
    keep_alive: str = "30m"
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    session_summary: str = ""
    context_state: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    attempt_ledger: list[dict[str, Any]] = field(default_factory=list)

    @property
    def client_host(self) -> str | None:
        return None if self.cloud else self.host

    @property
    def auto_approve_active(self) -> bool:
        """True when approvals are skipped, persistently (/auto) or for this session ('a')."""
        return self.auto_mode or self.session_auto_approve

    def save(self) -> None:
        _ensure_private_config_parent(CONFIG_FILE)
        _ensure_private_config_parent(HISTORY_DIR / ".directory-authority")
        self.session_summary = persisted_session_summary(self)
        self.attempt_ledger = sanitize_attempt_ledger(self.attempt_ledger)
        data = asdict(self)
        data.pop("messages", None)
        data.pop("memories", None)
        data.pop("session_auto_approve", None)
        _atomic_write_text(CONFIG_FILE, json.dumps(data, indent=2))

    def save_memories(self) -> None:
        if self.echo_veil_enabled or self.echo_veil_protection.strip().casefold() == "required":
            raise RuntimeError("plaintext memory persistence is disabled while Echo Veil is authoritative")
        with _exclusive_state_lock(MEMORY_FILE):
            _atomic_write_text(MEMORY_FILE, json.dumps([str(item) for item in self.memories], indent=2))

    def remember_fact(self, fact: str) -> bool:
        """Append a memory fact under a lock to avoid lost updates."""
        fact = str(fact).strip()
        if not fact:
            return False
        from .ada_memory_echo_veil import (
            echo_veil_authority_selected,
            remember_with_echo_veil,
        )

        if echo_veil_authority_selected(self):
            return remember_with_echo_veil(
                self,
                fact,
                source="user_explicit",
            )
        with _exclusive_state_lock(MEMORY_FILE):
            loaded = _load_json_file(MEMORY_FILE, [])
            current = [str(item) for item in loaded] if isinstance(loaded, list) else []
            added = fact not in current
            if added:
                current.append(fact)
                _atomic_write_text(MEMORY_FILE, json.dumps(current, indent=2))
        self.memories = current
        return added

    def reconcile_memory_facts(
        self,
        *,
        additions: Iterable[str] = (),
        remove_if: Callable[[str], bool] | None = None,
    ) -> dict[str, int | bool]:
        """Atomically remove stale facts and add normalized-deduplicated facts.

        Existing retained strings and their order are preserved exactly. A
        pre-change backup is written beside ``memory.json`` so a bulk migration
        can be reversed without relying on positional ``/forget`` operations.
        Fact bodies are deliberately absent from the returned telemetry.
        """

        if self.echo_veil_enabled or self.echo_veil_protection.strip().casefold() == "required":
            raise RuntimeError("legacy plaintext reconciliation is prohibited while Echo Veil is authoritative")

        def normalized_key(value: str) -> str:
            return " ".join(value.split()).casefold()

        with _exclusive_state_lock(MEMORY_FILE):
            loaded = _load_json_file(MEMORY_FILE, [])
            current = [str(item) for item in loaded] if isinstance(loaded, list) else []
            retained = [fact for fact in current if remove_if is None or not remove_if(fact)]
            removed = len(current) - len(retained)
            seen = {normalized_key(fact) for fact in retained if normalized_key(fact)}
            added = 0
            for candidate in additions:
                fact = str(candidate).strip()
                key = normalized_key(fact)
                if not key or key in seen:
                    continue
                retained.append(fact)
                seen.add(key)
                added += 1
            changed = removed > 0 or added > 0
            if changed:
                if MEMORY_FILE.exists():
                    backup_path = MEMORY_FILE.with_suffix(MEMORY_FILE.suffix + ".reconcile.bak")
                    _atomic_write_text(backup_path, MEMORY_FILE.read_text(encoding="utf-8"))
                _atomic_write_text(MEMORY_FILE, json.dumps(retained, indent=2))
        self.memories = retained
        return {
            "changed": changed,
            "removed": removed,
            "added": added,
            "total": len(retained),
        }

    def forget_memory_index(self, index: int) -> str:
        """Remove a memory by zero-based index against the latest persisted list."""
        if self.echo_veil_enabled or self.echo_veil_protection.strip().casefold() == "required":
            raise RuntimeError("legacy plaintext deletion is prohibited while Echo Veil is authoritative")
        with _exclusive_state_lock(MEMORY_FILE):
            loaded = _load_json_file(MEMORY_FILE, [])
            current = [str(item) for item in loaded] if isinstance(loaded, list) else []
            removed = current.pop(index)
            _atomic_write_text(MEMORY_FILE, json.dumps(current, indent=2))
        self.memories = current
        return removed

    def save_conversation(self, name: str) -> Path:
        safe_name = safe_conversation_name(name)
        path = HISTORY_DIR / f"{safe_name}.json"
        persisted_messages = project_messages_for_persistence(
            self.messages,
            echo_authority=_config_selects_echo_authority(self),
        )
        persisted_summary = persisted_session_summary(self)
        _atomic_write_text(
            path,
            json.dumps(
                {
                    "messages": persisted_messages,
                    "session_summary": persisted_summary,
                    "context_state": self.context_state,
                },
                indent=2,
                default=str,
            ),
        )
        return path

    def load_conversation(self, name: str) -> int:
        safe_name = safe_conversation_name(name)
        path = HISTORY_DIR / f"{safe_name}.json"
        missing = object()
        loaded = _load_json_file(
            path,
            missing,
            preserve_corrupt=False,
        )
        if loaded is missing:
            raise FileNotFoundError(f"No saved conversation named '{safe_name}'")
        if isinstance(loaded, list):
            projected_messages = project_messages_for_persistence(
                loaded,
                echo_authority=_config_selects_echo_authority(self),
            )
            self.messages = projected_messages
            self.session_summary = ""
            self.context_state = {}
            if projected_messages != loaded:
                _atomic_write_text(path, json.dumps(projected_messages, indent=2, default=str))
        elif isinstance(loaded, dict):
            messages = loaded.get("messages", [])
            projected_messages = project_messages_for_persistence(
                messages,
                echo_authority=_config_selects_echo_authority(self),
            )
            self.messages = projected_messages
            summary = loaded.get("session_summary", "")
            self.session_summary = "" if _config_selects_echo_authority(self) else sanitize_persisted_summary(summary)
            context_state = loaded.get("context_state", {})
            self.context_state = context_state if isinstance(context_state, dict) else {}
            sanitized_document = {
                **loaded,
                "messages": projected_messages,
                "session_summary": self.session_summary,
                "context_state": self.context_state,
            }
            if sanitized_document != loaded:
                _atomic_write_text(path, json.dumps(sanitized_document, indent=2, default=str))
        else:
            self.messages = []
            self.session_summary = ""
            self.context_state = {}
        return len(self.messages)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            CONFIG_FILE.lstat()
            config_present = True
        except FileNotFoundError:
            config_present = False
        except OSError:
            config_present = True
        config_invalid = False
        if config_present:
            invalid = object()
            data = _load_json_file(
                CONFIG_FILE,
                invalid,
                preserve_corrupt=False,
            )
            if not isinstance(data, dict):
                config_invalid = True
                data = {}
            echo_enabled = data.get("echo_veil_enabled", False)
            echo_protection = data.get("echo_veil_protection", "optional")
            if type(echo_enabled) is not bool or not isinstance(echo_protection, str):
                config_invalid = True
            if not config_invalid:
                for key, value in data.items():
                    if hasattr(cfg, key) and key not in {
                        "messages",
                        "memories",
                        "session_auto_approve",
                    }:
                        coerced = _coerce_config_value(getattr(cfg, key), value)
                        if coerced is not _INVALID_CONFIG_VALUE:
                            setattr(cfg, key, coerced)
        if config_invalid:
            # An unreadable authority selection must never silently reactivate
            # legacy plaintext memory. Required mode will surface a fixed
            # unavailable-state error until the config is repaired explicitly.
            cfg.echo_veil_enabled = True
            cfg.echo_veil_protection = "required"
            cfg.skill_crystallize_enabled = False
        cfg.session_summary = persisted_session_summary(cfg)
        cfg.attempt_ledger = sanitize_attempt_ledger(cfg.attempt_ledger)
        # Releases before the versioned consent gate persisted only a boolean,
        # which is not evidence that the user accepted cwd snippets crossing the
        # active provider boundary. Fail closed until /code-rag on records the
        # current policy version.
        if not code_rag_consent_granted(cfg):
            cfg.code_rag_enabled = False
        # A persisted boolean from an older release is not proof that the user
        # accepted automatic durable writes under the current policy.
        if not memory_auto_capture_consent_granted(cfg):
            cfg.memory_auto_capture_enabled = False
        provider = str(cfg.model_provider or "auto").strip().casefold()
        cfg.model_provider = provider if provider in {"auto", "ollama", "xai", "chatgpt"} else "auto"
        # Older/live sessions may have persisted a short GPT-5.6 name before
        # alias-aware routing was available. Canonicalize in memory on load so
        # startup cannot send Sol/Terra/Luna to the local Ollama provider.
        if cfg.model_provider != "ollama":
            cfg.model = normalize_codex_model(cfg.model)
        # Refresh only the exact prompt shipped by older releases. User-authored
        # system prompts are preserved verbatim.
        if cfg.system == LEGACY_DEFAULT_SYSTEM:
            cfg.system = DEFAULT_SYSTEM
        if (
            not cfg.echo_veil_enabled
            and cfg.echo_veil_protection.strip().casefold() != "required"
            and MEMORY_FILE.exists()
        ):
            loaded = _load_json_file(MEMORY_FILE, [])
            if isinstance(loaded, list):
                cfg.memories = [str(item) for item in loaded]
        return cfg


# --- Rebrand migration helpers (used by main.py during startup) ---

LEGACY_CONFIG_DIR = Path.home() / OLD_CONFIG_DIR_NAME


def has_legacy_data() -> bool:
    """True if the old ~/.ollama_cli directory exists and contains real user data."""
    if not LEGACY_CONFIG_DIR.exists():
        return False
    # Consider it "real data" if it has at least one of the key files/dirs
    markers = [
        LEGACY_CONFIG_DIR / "config.json",
        LEGACY_CONFIG_DIR / "memory.json",
        LEGACY_CONFIG_DIR / "identity",
        LEGACY_CONFIG_DIR / "skills",
        LEGACY_CONFIG_DIR / "run_history.jsonl",
    ]
    return any(m.exists() for m in markers)


def get_legacy_backup_dir() -> Path:
    """Where we will copy the old data as a safety backup."""
    return Path.home() / ".ollama_cli.backup"


def _write_private_migration_file_windows(
    root: Path,
    relative: PurePosixPath,
    payload: bytes,
) -> None:
    """Publish one private migration artifact without exposing partial bytes."""

    target = root.joinpath(*relative.parts)
    _ensure_windows_private_directory(target.parent)
    with _windows_pinned_directory_chain(target.parent) as parent_chain:
        _write_private_migration_file_windows_bound(
            target,
            payload,
            parent_chain=parent_chain,
        )


def _write_private_migration_file_windows_bound(
    target: Path,
    payload: bytes,
    *,
    parent_chain: tuple[tuple[Path, tuple[int, ...]], ...],
) -> None:
    stage = target.with_name(f".{target.name}.migration-{secrets.token_hex(16)}.tmp")
    descriptor: int | None = None
    try:
        _recheck_directory_chain(parent_chain)
        descriptor = _windows_create_private_file(stage)
        path_info = stage.lstat()
        descriptor_info = os.fstat(descriptor)
        if (
            _path_is_reparse_point(stage, path_info)
            or not stat.S_ISREG(descriptor_info.st_mode)
            or descriptor_info.st_nlink != 1
            or _portable_state_identity(path_info) != _portable_state_identity(descriptor_info)
            or not _windows_private_dacl(stage)
        ):
            raise OSError("legacy migration staging file is unsafe")
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("legacy migration write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        staged_info = os.fstat(descriptor)
        if staged_info.st_nlink != 1 or staged_info.st_size != len(payload):
            raise OSError("legacy migration staging file changed while writing")
        os.close(descriptor)
        descriptor = None
        current = stage.lstat()
        staged_identity = _portable_state_identity(staged_info)
        if (
            _path_is_reparse_point(stage, current)
            or _portable_state_identity(current) != staged_identity
            or not _windows_private_dacl(stage)
        ):
            raise OSError("legacy migration staging file changed before publication")
        _recheck_directory_chain(parent_chain)
        _move_file_write_through(stage, target, replace=False)
        _verify_windows_file_publication(
            target,
            expected_payload=payload,
            expected_identity=staged_identity,
            require_private_dacl=True,
        )
        _recheck_directory_chain(parent_chain)
    except Exception:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            descriptor = None
        try:
            stage.unlink()
        except OSError:
            pass
        raise


def _write_private_migration_file(
    root: Path,
    relative_path: str,
    payload: bytes,
) -> None:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("legacy migration destination is invalid")
    if os.name == "nt":
        _write_private_migration_file_windows(root, relative, payload)
        return
    target = root.joinpath(*relative.parts)
    if os.name == "posix":
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(target.parent, 0o700)
    descriptor = os.open(
        target,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOFOLLOW", 0)),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("legacy migration write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def perform_legacy_migration() -> bool:
    """Migrate legacy state without shadow-copying Echo-protected material.

    Never deletes the original. Echo-selected installations receive only a
    strict settings projection; memory, history, derived artifacts, and auth
    bytes are not copied or backed up automatically.
    """
    old = LEGACY_CONFIG_DIR
    new = CONFIG_DIR
    backup = get_legacy_backup_dir()

    if not has_legacy_data():
        return False
    try:
        destination_info = new.lstat()
    except FileNotFoundError:
        destination_info = None
    if destination_info is not None:
        if _path_is_reparse_point(new, destination_info) or not stat.S_ISDIR(destination_info.st_mode):
            raise LegacyMigrationError("legacy migration destination is unsafe")
        if hasattr(os, "getuid") and destination_info.st_uid != os.getuid():
            raise LegacyMigrationError("legacy migration destination is unsafe")
        if os.name == "posix" and destination_info.st_mode & 0o022:
            raise LegacyMigrationError("legacy migration destination is unsafe")
        if os.name == "nt":
            try:
                destination_chain = _directory_chain(new)
            except OSError as exc:
                raise LegacyMigrationError("legacy migration destination is unsafe") from exc
            if not _windows_private_dacl(new) or not _windows_safe_creation_dacl(new.parent):
                raise LegacyMigrationError("legacy migration destination is unsafe")
            _recheck_directory_chain(destination_chain)

        def migration_marker_present(name: str, expected: bytes) -> bool:
            try:
                payload = _state_descriptor_payload(new / name, max_bytes=512)
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise LegacyMigrationError("legacy migration destination is unsafe") from exc
            if payload != expected:
                raise LegacyMigrationError("legacy migration marker is invalid")
            return True

        incomplete_present = migration_marker_present(
            LEGACY_MIGRATION_INCOMPLETE_FILE,
            b"Algo CLI legacy migration publication is incomplete.\n",
        )
        complete_present = migration_marker_present(
            LEGACY_MIGRATION_COMPLETE_FILE,
            b"Legacy configuration migrated by Algo CLI.\n",
        )
        # The incomplete marker is created and synced before any published
        # configuration entry.  It is removed only after the completion marker
        # and its parent directory are durable.  Its presence therefore always
        # requires explicit recovery, even when a completion marker is also
        # visible after a post-link crash.
        if incomplete_present:
            raise LegacyMigrationError("protected legacy migration is incomplete")
        try:
            config_payload = _state_descriptor_payload(
                new / "config.json",
                max_bytes=MAX_JSON_STATE_BYTES,
            )
        except FileNotFoundError:
            config_payload = None
        except OSError as exc:
            raise LegacyMigrationError("legacy migration destination is unsafe") from exc
        if config_payload is None:
            raise LegacyMigrationError("legacy migration destination is incomplete")
        try:
            parsed_config = json.loads(config_payload.decode("utf-8", errors="strict"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LegacyMigrationError("legacy migration configuration is invalid") from exc
        if not isinstance(parsed_config, dict):
            raise LegacyMigrationError("legacy migration configuration is invalid")
        if complete_present and not config_payload:
            raise LegacyMigrationError("legacy migration destination is incomplete")
        # A securely readable existing Algo config (or a complete prior
        # migration) is authoritative and must never be clobbered.
        return False

    echo_selected = True
    final_directory_created = False
    try:
        import shutil
        import uuid

        from .grace_memory_receipts import (
            inventory_legacy_tree,
            legacy_config_selects_echo,
            read_pinned_legacy_artifact,
            sanitized_legacy_config,
        )

        # Unsafe or malformed legacy configuration is conservatively treated
        # as protection-selected until a pinned parse proves otherwise.
        echo_selected = legacy_config_selects_echo(old)
        inventory = inventory_legacy_tree(old, echo_selected=echo_selected)
        if inventory.truncated:
            raise LegacyMigrationError("legacy migration inventory is incomplete")

        tmp_new = new.with_name(f"{new.name}.migration-{uuid.uuid4().hex}")
        if os.name == "nt":
            _ensure_windows_private_directory(tmp_new, require_new=True)
        else:
            tmp_new.mkdir(mode=0o700)
        if echo_selected:
            projected = sanitized_legacy_config(old)
            _write_private_migration_file(
                tmp_new,
                "config.json",
                json.dumps(projected, indent=2, sort_keys=True).encode("utf-8"),
            )
        else:
            payloads = [
                (
                    artifact.relative_path,
                    read_pinned_legacy_artifact(old, artifact),
                )
                for artifact in inventory.artifacts
                if artifact.automatic_copy_allowed
            ]
            for relative_path, payload in payloads:
                _write_private_migration_file(tmp_new, relative_path, payload)
            if not backup.exists():
                tmp_backup = backup.with_name(f"{backup.name}.migration-{uuid.uuid4().hex}")
                if os.name == "nt":
                    _ensure_windows_private_directory(tmp_backup, require_new=True)
                else:
                    tmp_backup.mkdir(mode=0o700)
                try:
                    for relative_path, payload in payloads:
                        _write_private_migration_file(
                            tmp_backup,
                            relative_path,
                            payload,
                        )
                    if os.name == "nt":
                        with _windows_pinned_directory_chain(backup.parent) as backup_parent_chain:
                            if not _windows_safe_creation_dacl(backup.parent):
                                raise OSError("legacy backup publication parent ACL is unsafe")
                            _move_file_write_through(tmp_backup, backup, replace=False)
                            published_backup = backup.lstat()
                            if (
                                _path_is_reparse_point(backup, published_backup)
                                or not stat.S_ISDIR(published_backup.st_mode)
                                or not _windows_private_dacl(backup)
                            ):
                                raise OSError("legacy backup publication is unsafe")
                            _recheck_directory_chain(backup_parent_chain)
                    else:
                        _move_file_write_through(tmp_backup, backup, replace=False)
                except Exception:
                    shutil.rmtree(tmp_backup, ignore_errors=True)
                    raise

        artifact_counts: dict[str, int] = {}
        blocked_counts: dict[str, int] = {}
        for artifact in inventory.artifacts:
            label = artifact.classification.value
            artifact_counts[label] = artifact_counts.get(label, 0) + 1
            if not artifact.automatic_copy_allowed:
                blocked_counts[label] = blocked_counts.get(label, 0) + 1
        migration_receipt = {
            "schema_version": 1,
            "echo_authority_selected": echo_selected,
            "inventory_truncated": False,
            "original_retained": True,
            "backup_created": bool(not echo_selected and backup.exists()),
            "explicit_review_required": bool(blocked_counts),
            "artifact_counts": dict(sorted(artifact_counts.items())),
            "blocked_artifact_counts": dict(sorted(blocked_counts.items())),
        }
        _write_private_migration_file(
            tmp_new,
            ".legacy_migration_receipt.json",
            json.dumps(migration_receipt, indent=2, sort_keys=True).encode("utf-8"),
        )
        _write_private_migration_file(
            tmp_new,
            LEGACY_MIGRATION_COMPLETE_FILE,
            b"Legacy configuration migrated by Algo CLI.\n",
        )
        _write_private_migration_file(
            tmp_new,
            LEGACY_MIGRATION_INCOMPLETE_FILE,
            b"Algo CLI legacy migration publication is incomplete.\n",
        )
        if os.name == "nt":
            # A same-volume write-through directory rename publishes the fully
            # fsynced tree without the unsupported CRT directory-fsync path.
            # The incomplete marker remains present across the publication;
            # a second write-through rename clears it only after the complete
            # tree is durably addressable at the final name.
            with _windows_pinned_directory_chain(new.parent) as parent_chain:
                if not _windows_safe_creation_dacl(new.parent):
                    raise OSError("legacy migration publication parent ACL is unsafe")
                _move_file_write_through(tmp_new, new, replace=False)
                final_directory_created = True
                published_info = new.lstat()
                if (
                    _path_is_reparse_point(new, published_info)
                    or not stat.S_ISDIR(published_info.st_mode)
                    or not _windows_private_dacl(new)
                ):
                    raise OSError("legacy migration publication is unsafe")
                if (
                    _state_descriptor_payload(
                        new / LEGACY_MIGRATION_COMPLETE_FILE,
                        max_bytes=512,
                    )
                    != b"Legacy configuration migrated by Algo CLI.\n"
                ):
                    raise OSError("legacy migration completion marker is invalid")
                incomplete_destination = new / LEGACY_MIGRATION_INCOMPLETE_FILE
                cleared_marker = new / f".legacy_migration_cleared-{uuid.uuid4().hex}"
                _recheck_directory_chain(parent_chain)
                _move_file_write_through(incomplete_destination, cleared_marker, replace=False)
                if incomplete_destination.exists() or incomplete_destination.is_symlink():
                    raise OSError("legacy migration incomplete marker remained published")
                _recheck_directory_chain(parent_chain)
                try:
                    cleared_marker.unlink()
                except OSError:
                    # The content-free cleared marker is outside the recovery
                    # protocol once its write-through rename has committed.
                    pass
            return True

        # Destination publication is no-clobber: mkdir reserves the exact name,
        # then every regular staged file is linked exclusively into it.  The
        # incomplete marker is durable first and the completion marker last.
        new.mkdir(mode=0o700)
        final_directory_created = True
        incomplete_source = tmp_new / LEGACY_MIGRATION_INCOMPLETE_FILE
        incomplete_destination = new / LEGACY_MIGRATION_INCOMPLETE_FILE
        os.link(incomplete_source, incomplete_destination, follow_symlinks=False)
        incomplete_source.unlink()

        def fsync_new_directory() -> None:
            descriptor = os.open(
                new,
                os.O_RDONLY
                | int(getattr(os, "O_DIRECTORY", 0))
                | int(getattr(os, "O_NOFOLLOW", 0))
                | int(getattr(os, "O_CLOEXEC", 0)),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        fsync_new_directory()
        completion_source = tmp_new / LEGACY_MIGRATION_COMPLETE_FILE
        for source in sorted(tmp_new.rglob("*")):
            if source == completion_source:
                continue
            relative = source.relative_to(tmp_new)
            destination = new / relative
            if source.is_dir():
                destination.mkdir(mode=0o700, exist_ok=False)
                continue
            os.link(source, destination, follow_symlinks=False)
            source.unlink()
        fsync_new_directory()
        completion_destination = new / LEGACY_MIGRATION_COMPLETE_FILE
        os.link(
            completion_source,
            completion_destination,
            follow_symlinks=False,
        )
        completion_source.unlink()
        fsync_new_directory()
        incomplete_destination.unlink()
        fsync_new_directory()
        for directory in sorted(
            (path for path in tmp_new.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.rmdir()
        tmp_new.rmdir()

        return True
    except Exception as exc:
        try:
            if "tmp_new" in locals() and tmp_new.exists():
                import shutil

                shutil.rmtree(tmp_new, ignore_errors=True)
        except Exception:
            pass
        if final_directory_created or echo_selected:
            raise LegacyMigrationError("legacy migration could not be completed safely") from exc
        # Optional legacy migration may remain best effort only when no final
        # namespace was reserved and pinned configuration proved Echo disabled.
        return False


def load_legacy_migration_receipt() -> dict[str, Any]:
    """Return only validated, content-free legacy migration telemetry."""

    path = CONFIG_DIR / ".legacy_migration_receipt.json"
    loaded = _load_json_file(
        path,
        {},
        preserve_corrupt=False,
        max_bytes=64 * 1024,
    )
    expected = {
        "schema_version",
        "echo_authority_selected",
        "inventory_truncated",
        "original_retained",
        "backup_created",
        "explicit_review_required",
        "artifact_counts",
        "blocked_artifact_counts",
    }
    if not isinstance(loaded, dict) or set(loaded) != expected:
        return {}
    if loaded.get("schema_version") != 1 or any(
        type(loaded.get(key)) is not bool
        for key in (
            "echo_authority_selected",
            "inventory_truncated",
            "original_retained",
            "backup_created",
            "explicit_review_required",
        )
    ):
        return {}
    for field_name in ("artifact_counts", "blocked_artifact_counts"):
        values = loaded.get(field_name)
        if not isinstance(values, dict) or len(values) > 32:
            return {}
        if any(
            not isinstance(key, str)
            or not re.fullmatch(r"[a-z_]{1,32}", key)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 10_000
            for key, value in values.items()
        ):
            return {}
    return loaded


def migrate_legacy_sidecar_files() -> list[str]:
    """Copy small auth/env files left behind when full migration was skipped.

    Full ``perform_legacy_migration`` only runs when ~/.algo_cli is empty. If the
    new directory was populated first, provider credentials and environment
    files can remain only under ~/.ollama_cli.
    """
    from .grace_memory_receipts import (
        ElsieReceiptError,
        inventory_legacy_tree,
        legacy_config_selects_echo,
        read_pinned_legacy_artifact,
    )

    if LEGACY_CONFIG_DIR.exists() and legacy_config_selects_echo(LEGACY_CONFIG_DIR):
        return []

    moved: list[str] = []
    try:
        inventory = inventory_legacy_tree(LEGACY_CONFIG_DIR, echo_selected=False)
    except ElsieReceiptError:
        return []
    if inventory.truncated:
        return []
    allowed_names = {
        "xai_auth.json",
        "google_workspace_auth.json",
        "google_workspace_pending_login.json",
        "chatgpt_auth.json",
        ".env",
        "env",
    }
    artifacts = {
        artifact.relative_path: artifact
        for artifact in inventory.artifacts
        if artifact.relative_path in allowed_names and artifact.automatic_copy_allowed
    }
    if artifacts:
        if os.name == "nt":
            _ensure_windows_private_directory(CONFIG_DIR)
        else:
            CONFIG_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(CONFIG_DIR, 0o700)
    for name in sorted(artifacts):
        if (CONFIG_DIR / name).exists() or (CONFIG_DIR / name).is_symlink():
            continue
        try:
            payload = read_pinned_legacy_artifact(
                LEGACY_CONFIG_DIR,
                artifacts[name],
                max_bytes=4 * 1024 * 1024,
            )
            _write_private_migration_file(CONFIG_DIR, name, payload)
            moved.append(name)
        except (ElsieReceiptError, OSError):
            continue
    return moved
