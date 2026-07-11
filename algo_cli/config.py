"""Configuration and persistent state."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


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

DEFAULT_MODEL = (
    os.environ.get(f"{NEW_ENV_PREFIX}MODEL")
    or os.environ.get(f"{OLD_ENV_PREFIX}MODEL")
    or "qwen3"
)
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_THEME = (
    os.environ.get(f"{NEW_ENV_PREFIX}THEME")
    or os.environ.get(f"{OLD_ENV_PREFIX}THEME")
    or "tokyo-night"
)
DEFAULT_CHAT_STREAM_TIMEOUT_SECONDS = 300.0
CODE_RAG_CONSENT_VERSION = 1


def code_rag_consent_granted(cfg: Any) -> bool:
    """Return whether this config explicitly accepted the current code-RAG policy."""

    return bool(getattr(cfg, "code_rag_enabled", False)) and (
        getattr(cfg, "code_rag_consent_version", 0) == CODE_RAG_CONSENT_VERSION
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
- Use append_lesson or remember only when the user explicitly asks to store a lesson or fact. The runtime's bounded completion gate handles other high-confidence durable statements.
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
- Use append_lesson or remember only when the user explicitly asks to store a lesson or fact. The runtime's bounded completion gate handles other high-confidence durable statements.
- For Algo algorithm/pattern catalog guidance, use and update docs/ALGO.md.
- Format code blocks with language tags and include paths when citing code."""


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text using fsync + atomic replace to avoid truncated state files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        # Keep persisted text byte-stable across platforms.  The default text
        # mode rewrites ``\n`` to ``\r\n`` on Windows, which can corrupt
        # line-oriented formats when they are parsed and atomically rewritten.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            _atomic_write_text(backup, path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
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
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    # Retry opening the lock file if permission is denied briefly
    lock_file = None
    while lock_file is None:
        try:
            lock_file = open(lock_path, "a+b")
            lock_file.write(b"x")
            lock_file.flush()
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
        # Do not unlink the lock file. Removing an advisory lock path while
        # another thread/process is waiting can create two different inodes and
        # split the lock, allowing concurrent read-modify-write transactions.


def load_runtime_env(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    # Dual-support: ALGO_CLI_ENV_FILE preferred, fall back to OLLAMA_CLI_ENV_FILE
    configured_path = path or os.environ.get(f"{NEW_ENV_PREFIX}ENV_FILE") or os.environ.get(f"{OLD_ENV_PREFIX}ENV_FILE")
    if configured_path is not None:
        env_path = Path(configured_path)
    elif DEFAULT_RUNTIME_ENV_FILE.exists():
        env_path = DEFAULT_RUNTIME_ENV_FILE
    else:
        env_path = DOTENV_RUNTIME_ENV_FILE
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
        if not key:
            continue
        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        if key in os.environ and not override:
            loaded[key] = os.environ[key]
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded


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
    system: str = DEFAULT_SYSTEM
    theme: str = DEFAULT_THEME
    auto_mode: bool = False
    # Session-only auto-approve set by answering "a" at an approval prompt.
    # Never persisted: see save(); resets on every new session.
    session_auto_approve: bool = False
    show_thinking: bool = True
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
    echo_veil_enabled: bool = False  # Enable Echo Veil tiered memory layer
    echo_veil_capacity: int = 400  # Maximum active Echo Veil memories before decay
    echo_veil_production: bool = False  # Require encrypted Echo Veil storage when enabled
    echo_veil_crypto_key_path: str | None = None  # Path to JSON file with {"key_hex": "..."} for memory encryption
    memory_auto_capture_enabled: bool = True  # Auto-save only explicit, high-confidence durable user statements
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
    algorithmic_tool_policy_enabled: bool = False
    reflex_enabled: bool = False
    model_adaptive: bool = True  # adapt num_ctx/temperature/reflection to model size+provider
    code_rag_enabled: bool = False  # opt in to retrieving cfg.cwd source chunks each turn
    code_rag_consent_version: int = 0  # set only by explicit /code-rag on consent
    external_harness_sources_enabled: bool = False  # opt in to ~/.codex, ~/.claude, ~/.openclaw, etc.
    reasoning_chat_enabled: bool = False  # run reasoning preflight in the chat loop, not just pipelines
    # --- Reasoning engine flags ---
    reasoning_mode: str = "react"  # react | reflexion | tot | got | mcts | qcr | neuro_symbolic | hybrid
    reasoning_depth: int = 4       # Max depth/rounds for tree/graph search
    reasoning_branches: int = 3    # Branch factor for tree/graph expansion
    reasoning_qcr_samples: int = 5 # Number of CoT fragments for QCR aggregation
    reasoning_reflexion_attempts: int = 3  # Max self-critique rounds
    reasoning_ns_rounds: int = 3   # Max neuro-symbolic verify rounds
    reasoning_auto_reflexion: bool = False  # Auto-apply Reflexion on failed blocks
    reasoning_auto_verify: bool = False     # Auto-verify implement blocks with neuro-symbolic
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
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("messages", None)
        data.pop("memories", None)
        data.pop("session_auto_approve", None)
        _atomic_write_text(CONFIG_FILE, json.dumps(data, indent=2))

    def save_memories(self) -> None:
        with _exclusive_state_lock(MEMORY_FILE):
            _atomic_write_text(MEMORY_FILE, json.dumps([str(item) for item in self.memories], indent=2))

    def remember_fact(self, fact: str) -> bool:
        """Append a memory fact under a lock to avoid lost updates."""
        fact = str(fact).strip()
        if not fact:
            return False
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
        with _exclusive_state_lock(MEMORY_FILE):
            loaded = _load_json_file(MEMORY_FILE, [])
            current = [str(item) for item in loaded] if isinstance(loaded, list) else []
            removed = current.pop(index)
            _atomic_write_text(MEMORY_FILE, json.dumps(current, indent=2))
        self.memories = current
        return removed

    def save_conversation(self, name: str) -> Path:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = safe_conversation_name(name)
        path = HISTORY_DIR / f"{safe_name}.json"
        _atomic_write_text(
            path,
            json.dumps(
                {
                    "messages": self.messages,
                    "session_summary": self.session_summary,
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
        if not path.exists():
            raise FileNotFoundError(f"No saved conversation named '{safe_name}'")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            self.messages = loaded
            self.session_summary = ""
            self.context_state = {}
        elif isinstance(loaded, dict):
            messages = loaded.get("messages", [])
            self.messages = messages if isinstance(messages, list) else []
            summary = loaded.get("session_summary", "")
            self.session_summary = str(summary) if summary else ""
            context_state = loaded.get("context_state", {})
            self.context_state = context_state if isinstance(context_state, dict) else {}
        else:
            self.messages = []
            self.session_summary = ""
            self.context_state = {}
        return len(self.messages)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            data = _load_json_file(CONFIG_FILE, {})
            if not isinstance(data, dict):
                data = {}
            for key, value in data.items():
                if hasattr(cfg, key) and key not in {"messages", "memories", "session_auto_approve"}:
                    coerced = _coerce_config_value(getattr(cfg, key), value)
                    if coerced is not _INVALID_CONFIG_VALUE:
                        setattr(cfg, key, coerced)
        # Releases before the versioned consent gate persisted only a boolean,
        # which is not evidence that the user accepted cwd snippets crossing the
        # active provider boundary. Fail closed until /code-rag on records the
        # current policy version.
        if not code_rag_consent_granted(cfg):
            cfg.code_rag_enabled = False
        # Refresh only the exact prompt shipped by older releases. User-authored
        # system prompts are preserved verbatim.
        if cfg.system == LEGACY_DEFAULT_SYSTEM:
            cfg.system = DEFAULT_SYSTEM
        if MEMORY_FILE.exists():
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


def perform_legacy_migration() -> bool:
    """Copy ~/.ollama_cli → ~/.algo_cli (and leave a .backup copy).

    Never deletes the original. Returns True if a migration was performed.
    Safe to call multiple times (idempotent via existence checks).
    """
    old = LEGACY_CONFIG_DIR
    new = CONFIG_DIR
    backup = get_legacy_backup_dir()

    if not has_legacy_data():
        return False
    if new.exists():
        # New location exists (even if empty/scaffolded) — do not overwrite
        return False

    try:
        import shutil
        import uuid

        tmp_new = new.with_name(f"{new.name}.migration-{uuid.uuid4().hex}")
        # First make a full backup of the old location (never delete originals)
        if old.exists() and not backup.exists():
            shutil.copytree(old, backup, dirs_exist_ok=True)

        # Now copy into a staging directory, then publish it atomically enough
        # for startup: a failed copy never leaves CONFIG_DIR partially created.
        shutil.copytree(old, tmp_new)

        # Leave a small sentinel so we know migration already happened for this user
        try:
            _atomic_write_text(tmp_new / ".migrated_from_legacy", f"Migrated from {old} on first run of algo-cli 0.3+")
        except Exception:
            pass
        tmp_new.rename(new)

        return True
    except Exception:
        try:
            if "tmp_new" in locals() and tmp_new.exists():
                import shutil

                shutil.rmtree(tmp_new, ignore_errors=True)
        except Exception:
            pass
        # Best-effort migration; do not crash startup
        return False


def migrate_legacy_sidecar_files() -> list[str]:
    """Copy small auth/env files left behind when full migration was skipped.

    Full ``perform_legacy_migration`` only runs when ~/.algo_cli is empty. If the
    new directory was populated first (e.g. identity scaffold), xai_auth.json and
    .env can remain only under ~/.ollama_cli and xAI models disappear from /models.
    """
    import shutil

    moved: list[str] = []
    pairs = [
        (LEGACY_CONFIG_DIR / "xai_auth.json", CONFIG_DIR / "xai_auth.json"),
        (LEGACY_CONFIG_DIR / ".env", CONFIG_DIR / ".env"),
        (LEGACY_CONFIG_DIR / "env", CONFIG_DIR / "env"),
    ]
    for src, dst in pairs:
        if not src.is_file() or dst.exists():
            continue
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            moved.append(src.name)
        except Exception:
            continue
    return moved
