"""Native identity layer for Algo CLI.

Four user-editable Markdown files in ~/.algo_cli/identity/ define the CLI's
persona, the user's profile, and accumulated lessons. Contents are cached by
mtime and prepended to the system prompt on every turn.

Design note: reads are stat-gated. On an unchanged turn the only cost is four
os.stat() calls (microseconds). On a changed file the cost is one read of that
file. Nothing is re-embedded or re-tokenized here; this module is the fast
always-inject layer.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

try:
    import numpy as _np
    _NUMPY = True
except ImportError:
    _np = None  # type: ignore[assignment]
    _NUMPY = False

from .cache_admission import WindowTinyLFUCache
from .config import CONFIG_DIR, _atomic_write_text, _exclusive_state_lock
from .retrieval_algorithms import stable_top_k


IDENTITY_DIR = CONFIG_DIR / "identity"
SOUL_PATH = IDENTITY_DIR / "SOUL.md"
IDENTITY_PATH = IDENTITY_DIR / "IDENTITY.md"
USER_PATH = IDENTITY_DIR / "USER.md"
LESSONS_PATH = IDENTITY_DIR / "lessons-learned.md"
LESSONS_INDEX_PATH = IDENTITY_DIR / "lessons_index.json"

ALL_PATHS: tuple[Path, ...] = (SOUL_PATH, IDENTITY_PATH, USER_PATH, LESSONS_PATH)

DEFAULT_EMBED_MODEL = "qwen3-embedding:latest"
QUERY_VEC_CACHE_SIZE = 32
LESSON_MIN_CHARS = 30
LESSONS_INDEX_VERSION = 2

EmbedFn = Callable[[list[str]], list[list[float]]]


DEFAULT_SOUL = """# Algo CLI - Soul

## Voice
- Concise. Terminal-native. No filler greetings.
- Honest about uncertainty. Say "I don't know" when you don't.
- Show file paths and exact commands when relevant.

## Operating values
- Local-first. Prefer local Ollama before cloud.
- Read before writing. Verify before claiming.
- Treat memory and wiki as navigation, not authority.
- Format code blocks with language tags.

## Behavior
- Use tools when they materially help. Do not narrate them.
- Do not run destructive commands without explicit approval.
- After a tool call, summarize the result before the next step.
"""

LEGACY_DEFAULT_IDENTITY = """# Algo CLI - Identity

You are Algo CLI: a local-first terminal coding assistant built on Ollama models.

You run on the user's machine. You have direct access to their filesystem through tools.
You can search a harness of skills, prompts, memories, and wiki pages across the user's
agent ecosystem (Codex, Claude, OpenClaw, Mercury, Pi, and shared .agents).

You are not a chatbot. You are a working partner for terminal tasks.
"""

DEFAULT_IDENTITY = """# Algo CLI - Identity

You are Algo CLI: a local-first agent runtime for coding, research, and operational work.

You run on the user's machine and can use local or connected cloud inference through
Ollama, Ollama Cloud, xAI Grok, and ChatGPT/Codex. You plan, act with tools, verify
results, and retain useful context across sessions.

Your harness searches Algo CLI's built-in skills, documentation, memories, and
repository intelligence. Additional local agent stores (Codex, Claude, OpenClaw,
Mercury, Pi, and shared .agents) are available only after the user explicitly enables
them. You are a working partner, not a passive chat interface.
"""

DEFAULT_USER = """# About the User

<!-- Edit this file to teach the CLI about yourself. The more specific, the better. -->

## Who I am
(Your name, role, what you work on.)

## How I work
(Editors, languages, shells, workflow preferences.)

## What I want from this CLI
(Be terse? Verbose? Ask before acting? Prefer local models?)

## Things to never do
(Hard nos, e.g. "never auto-push to main".)
"""

DEFAULT_LESSONS = """# Lessons Learned

<!-- Append new lessons below. Each lesson is a paragraph or short list, separated by blank lines. -->
<!-- Add via /lesson <text> or by editing this file directly. -->
"""


_DEFAULTS: dict[Path, str] = {
    SOUL_PATH: DEFAULT_SOUL,
    IDENTITY_PATH: DEFAULT_IDENTITY,
    USER_PATH: DEFAULT_USER,
    LESSONS_PATH: DEFAULT_LESSONS,
}


@dataclass
class CacheEntry:
    mtime_ns: int
    content: str


_CACHE: dict[Path, CacheEntry] = {}


def scaffold_if_needed() -> list[Path]:
    """Create missing identity files and refresh the untouched legacy identity."""
    created: list[Path] = []
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    for path, default in _DEFAULTS.items():
        if not path.exists():
            _atomic_write_text(path, default)
            created.append(path)
        elif path == IDENTITY_PATH:
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if existing == LEGACY_DEFAULT_IDENTITY:
                _atomic_write_text(path, DEFAULT_IDENTITY)
                _CACHE.pop(path, None)
    return created


def read_cached(path: Path) -> str:
    """Read file content, mtime-cached. Returns '' if missing or unreadable."""
    if not path.exists():
        return ""
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return ""
    entry = _CACHE.get(path)
    if entry and entry.mtime_ns == mtime_ns:
        return entry.content
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    _CACHE[path] = CacheEntry(mtime_ns=mtime_ns, content=content)
    return content


def detect_changes() -> list[Path]:
    """List identity files whose mtime differs from the cache. Stat-only, no reads."""
    changed: list[Path] = []
    for path in ALL_PATHS:
        if not path.exists():
            continue
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        entry = _CACHE.get(path)
        if entry is None or entry.mtime_ns != mtime_ns:
            changed.append(path)
    return changed


def identity_mtime_key() -> tuple[int, ...]:
    """Stable fingerprint of identity file mtimes for context-usage cache keys."""
    key: list[int] = []
    for path in ALL_PATHS:
        if not path.exists():
            key.append(0)
            continue
        try:
            key.append(path.stat().st_mtime_ns)
        except OSError:
            key.append(0)
    return tuple(key)


def build_identity_block(retrieved_lessons: list[str] | None = None) -> str:
    """Assemble the identity prefix for the system prompt.

    If retrieved_lessons is None: full lessons-learned.md is inlined (fallback path).
    If retrieved_lessons is a list (possibly empty): use only those chunks.
    """
    identity_text = read_cached(IDENTITY_PATH).strip()
    soul_text = read_cached(SOUL_PATH).strip()
    user_text = read_cached(USER_PATH).strip()
    parts: list[str] = []
    if identity_text:
        parts.append(f"## Identity\n{identity_text}")
    if soul_text:
        parts.append(f"## Soul\n{soul_text}")
    if user_text:
        parts.append(f"## About the User\n{user_text}")
    if retrieved_lessons is not None:
        if retrieved_lessons:
            joined = "\n\n".join(retrieved_lessons)
            parts.append(f"## Relevant Lessons\n{joined}")
    else:
        lessons_text = read_cached(LESSONS_PATH).strip()
        if lessons_text:
            parts.append(f"## Lessons Learned\n{lessons_text}")
    return "\n\n".join(parts)


# ---------- Lessons embedding / RAG ----------

_LESSONS_INDEX: dict[str, Any] | None = None
_QUERY_VEC_CACHE: WindowTinyLFUCache[tuple[str, int, str], list[float]] = WindowTinyLFUCache(
    max(1, QUERY_VEC_CACHE_SIZE)
)


def _chunk_lessons(text: str) -> list[str]:
    """Split lessons-learned.md into chunks at `## ` headings. Drops top-level title."""
    if not text.strip():
        return []
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                joined = "\n".join(current).strip()
                if joined:
                    chunks.append(joined)
            current = [line]
        elif current or line.strip():
            current.append(line)
    if current:
        joined = "\n".join(current).strip()
        if joined:
            chunks.append(joined)
    return [c for c in chunks if len(c) >= LESSON_MIN_CHARS and not c.startswith("# Lessons Learned")]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _load_lessons_index() -> dict[str, Any] | None:
    global _LESSONS_INDEX
    if _LESSONS_INDEX is not None:
        return _LESSONS_INDEX
    if not LESSONS_INDEX_PATH.exists():
        return None
    try:
        _LESSONS_INDEX = json.loads(LESSONS_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LESSONS_INDEX = None
    return _LESSONS_INDEX


def _save_lessons_index(idx: dict[str, Any]) -> None:
    global _LESSONS_INDEX
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(LESSONS_INDEX_PATH, json.dumps(idx, indent=2))
    _LESSONS_INDEX = idx


def _index_embedding_model(idx: dict[str, Any]) -> str:
    """Return the persisted embedding identity, including legacy index support."""
    return str(idx.get("embedding_model") or idx.get("model") or "").strip()


def _normalise_vector(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    try:
        vector = [float(component) for component in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(component) for component in vector):
        return None
    return vector


def _index_vector_dimensions(idx: dict[str, Any]) -> int | None:
    """Return a validated vector width, 0 for an empty index, or None if corrupt."""
    chunks = idx.get("chunks")
    if not isinstance(chunks, list):
        return None
    if not chunks:
        return 0

    dimensions: int | None = None
    for chunk in chunks:
        if not isinstance(chunk, dict):
            return None
        vector = _normalise_vector(chunk.get("vector"))
        if vector is None:
            return None
        if dimensions is None:
            dimensions = len(vector)
        elif len(vector) != dimensions:
            return None

    stored_dimensions = idx.get("vector_dimensions")
    if stored_dimensions is not None:
        try:
            persisted = int(stored_dimensions)
        except (TypeError, ValueError):
            return None
        if persisted != dimensions:
            return None
    return dimensions


def lessons_index_stale(
    model: str | None = None,
    dimensions: int | None = None,
) -> bool:
    """Return whether lesson text or the requested embedding space changed.

    Older indexes remain readable, but an active model identity mismatch forces
    a rebuild even when the source Markdown has not changed. A configured
    vector width is checked when supplied.
    """
    if not LESSONS_PATH.exists():
        return False
    try:
        file_mtime = LESSONS_PATH.stat().st_mtime_ns
    except OSError:
        return False
    idx = _load_lessons_index()
    if idx is None:
        return True
    try:
        index_mtime = int(idx.get("mtime_ns", -1))
    except (TypeError, ValueError):
        return True
    if index_mtime != file_mtime:
        return True
    if model is not None and _index_embedding_model(idx) != model.strip():
        return True
    index_dimensions = _index_vector_dimensions(idx)
    if index_dimensions is None:
        return True
    if dimensions is not None and index_dimensions not in {0, int(dimensions)}:
        return True
    return False


def lessons_index_status() -> dict[str, Any]:
    if not LESSONS_PATH.exists():
        return {"file": False, "index": False, "chunk_count": 0, "model": None, "stale": False}
    idx = _load_lessons_index()
    if idx is None:
        return {"file": True, "index": False, "chunk_count": 0, "model": None, "stale": True}
    return {
        "file": True,
        "index": True,
        "chunk_count": len(idx.get("chunks", [])),
        "model": _index_embedding_model(idx) or None,
        "dimensions": _index_vector_dimensions(idx),
        "stale": lessons_index_stale(),
    }


def rebuild_lessons_index(
    embed_fn: EmbedFn,
    model: str = DEFAULT_EMBED_MODEL,
    *,
    expected_dimensions: int | None = None,
) -> dict[str, Any]:
    """Embed all lesson chunks in one batched call. Persists to disk."""
    if not LESSONS_PATH.exists():
        return {"chunk_count": 0, "ready": False, "reason": "no_file"}
    try:
        text = LESSONS_PATH.read_text(encoding="utf-8", errors="replace")
        mtime_ns = LESSONS_PATH.stat().st_mtime_ns
    except OSError as exc:
        return {"chunk_count": 0, "ready": False, "reason": f"read_error: {exc}"}
    chunks = _chunk_lessons(text)
    if not chunks:
        idx = {
            "version": LESSONS_INDEX_VERSION,
            "mtime_ns": mtime_ns,
            "model": model,
            "embedding_model": model,
            "vector_dimensions": 0,
            "chunks": [],
        }
        _save_lessons_index(idx)
        return {"chunk_count": 0, "dimensions": 0, "ready": True}
    try:
        vectors = embed_fn(chunks)
    except Exception as exc:
        return {"chunk_count": 0, "ready": False, "reason": f"embed_error: {exc}"}
    if len(vectors) != len(chunks):
        return {"chunk_count": 0, "ready": False, "reason": "embed_count_mismatch"}
    normalised_vectors: list[list[float]] = []
    vector_dimensions: int | None = None
    for vector in vectors:
        normalised = _normalise_vector(vector)
        if normalised is None:
            return {"chunk_count": 0, "ready": False, "reason": "invalid_embedding_vector"}
        if vector_dimensions is None:
            vector_dimensions = len(normalised)
        elif len(normalised) != vector_dimensions:
            return {"chunk_count": 0, "ready": False, "reason": "embed_dimension_mismatch"}
        normalised_vectors.append(normalised)
    assert vector_dimensions is not None
    if expected_dimensions is not None and vector_dimensions != int(expected_dimensions):
        return {
            "chunk_count": 0,
            "ready": False,
            "reason": "embed_dimension_mismatch",
            "expected_dimensions": int(expected_dimensions),
            "actual_dimensions": vector_dimensions,
        }
    idx = {
        "version": LESSONS_INDEX_VERSION,
        "mtime_ns": mtime_ns,
        "model": model,
        "embedding_model": model,
        "vector_dimensions": vector_dimensions,
        "chunks": [
            {"text": chunk, "vector": vector}
            for chunk, vector in zip(chunks, normalised_vectors)
        ],
    }
    _save_lessons_index(idx)
    _QUERY_VEC_CACHE.clear()
    return {"chunk_count": len(chunks), "dimensions": vector_dimensions, "ready": True}


def retrieve_lessons(query: str, embed_fn: EmbedFn, model: str = DEFAULT_EMBED_MODEL, k: int = 5) -> list[str]:
    """Return top-K lesson chunks by cosine similarity. Empty list if index unavailable."""
    idx = _load_lessons_index()
    if idx is None or not idx.get("chunks"):
        return []
    if lessons_index_stale(model):
        return []
    vector_dimensions = _index_vector_dimensions(idx)
    if vector_dimensions is None or vector_dimensions <= 0:
        return []
    query = (query or "").strip()
    if not query:
        return []
    cache_key = (model, vector_dimensions, query)
    _QUERY_VEC_CACHE.resize(max(1, QUERY_VEC_CACHE_SIZE))
    qvec = _QUERY_VEC_CACHE.get(cache_key)
    if qvec is None:
        try:
            vecs = embed_fn([query])
        except Exception:
            return []
        if not vecs:
            return []
        qvec = _normalise_vector(vecs[0])
        if qvec is None or len(qvec) != vector_dimensions:
            return []
        _QUERY_VEC_CACHE.put(cache_key, qvec)
    elif len(qvec) != vector_dimensions:
        return []

    chunks = idx["chunks"]
    vectors = [_normalise_vector(chunk.get("vector")) for chunk in chunks]
    if any(vector is None or len(vector) != vector_dimensions for vector in vectors):
        return []
    safe_vectors = [vector for vector in vectors if vector is not None]
    if _NUMPY and chunks:
        import numpy as np
        mat = np.array(safe_vectors, dtype=np.float32)
        qv = np.array(qvec, dtype=np.float32)
        mat_norms = np.linalg.norm(mat, axis=1)
        q_norm = float(np.linalg.norm(qv))
        if q_norm <= 0.0:
            return []
        denom = mat_norms * q_norm
        sims = np.divide(mat @ qv, denom, out=np.zeros_like(mat_norms), where=denom > 0).tolist()
        scored: list[tuple[float, str]] = [
            (float(s), chunks[i]["text"]) for i, s in enumerate(sims) if s > 0.0
        ]
    else:
        scored = []
        for chunk, vector in zip(chunks, safe_vectors):
            sim = _cosine(qvec, vector)
            if sim > 0.0:
                scored.append((sim, chunk["text"]))
    top_scored = stable_top_k(scored, k, score=lambda pair: pair[0])
    return [text for _, text in top_scored]


def status() -> list[dict[str, Any]]:
    """Per-file metadata for the /identity command."""
    rows: list[dict[str, Any]] = []
    for path in ALL_PATHS:
        row: dict[str, Any] = {"path": str(path), "name": path.name, "exists": path.exists()}
        if path.exists():
            try:
                st = path.stat()
                row["size"] = int(st.st_size)
                row["modified"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                row["size"] = 0
                row["modified"] = "?"
        rows.append(row)
    return rows


def append_lesson(text: str) -> Path:
    """Append a timestamped lesson to lessons-learned.md and invalidate its cache."""
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    if not LESSONS_PATH.exists():
        _atomic_write_text(LESSONS_PATH, DEFAULT_LESSONS)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## {stamp}\n{text.strip()}\n"
    with _exclusive_state_lock(LESSONS_PATH):
        with LESSONS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(entry)
    _CACHE.pop(LESSONS_PATH, None)
    return LESSONS_PATH


def write_user_profile(content: str) -> Path:
    """Overwrite USER.md with new content and invalidate its cache."""
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(USER_PATH, content)
    _CACHE.pop(USER_PATH, None)
    return USER_PATH
