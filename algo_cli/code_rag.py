"""Working-directory code retrieval (RAG over cfg.cwd source files).

Harness RAG covers skills/wiki/memory but never the project the user is
actually working in. A small local model's biggest weakness is not knowing the
codebase; this module gives it line-anchored code chunks relevant to the turn.

Design (deliberately close to harness.py, which is battle-tested):
- Per-cwd JSON index at CONFIG_DIR/code_index/<digest>.json.
- Incremental: chunks are keyed by (relative_path, start_line); a file whose
  size+mtime are unchanged reuses its chunks and embeddings. Changed files
  reuse embeddings for content-identical chunks by stable content hash.
- Embedding is capped per turn (like harness EMBED_PER_TURN_CAP) so the first
  few turns in a new project don't stall on a full-project embed.
- Retrieval is cosine top-k, numpy fast-path with a scalar fallback.

Best-effort throughout: any failure returns empty and the turn proceeds
without code context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

try:
    import numpy as _np
    _NUMPY = True
except ImportError:
    _np = None  # type: ignore[assignment]
    _NUMPY = False

from .config import CONFIG_DIR, _atomic_write_text
from .retrieval_algorithms import stable_top_k

EmbedFn = Callable[[list[str]], list[list[float]]]

CODE_INDEX_DIR = CONFIG_DIR / "code_index"
CHUNK_LINES = 60
CHUNK_OVERLAP = 10
MAX_FILES = 600
MAX_FILE_BYTES = 400_000
MAX_CHUNKS = 4000
EMBED_PER_TURN_CAP = 64
SNIPPET_CHARS = 600

CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".rb", ".php", ".swift", ".scala",
    ".sh", ".ps1", ".sql", ".lua", ".r", ".jl", ".ml", ".ex", ".exs",
    ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".md",
})
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist",
    "build", "target", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", ".tox", ".idea", ".vscode", "coverage", ".cache",
})

# Same policy as harness.SECRET_RE: never index files whose names suggest
# credentials. Their contents would otherwise be embedded, persisted under
# ~/.algo_cli/code_index/, and injected into prompts (off-machine in cloud mode).
SECRET_RE = re.compile(
    r"(?:^|[/\\._-])"
    r"(?:secrets?|tokens?|credentials?|auth(?:orization)?|passwords?|passwd|"
    r"api[_-]?keys?|access[_-]?tokens?|private[_-]?keys?|\.env)"
    r"(?:[/\\._-]|s?$|s?[/\\._-])",
    re.IGNORECASE,
)

# Some local embedders only use a short prefix of each input, so the text we
# embed must front-load the salient content.
EMBED_TEXT_CHARS = 280
_SYMBOL_LINE_RE = re.compile(
    r"^\s*(?:def |class |function |func |fn |pub fn |impl |interface |type \w+ |const |export )",
)

# Per-process index cache + rescan throttle: without these, every turn
# re-parses a multi-MB JSON index and re-walks up to MAX_FILES files.
_INDEX_MEM: dict[str, dict[str, Any]] = {}
_LAST_SCAN: dict[str, float] = {}
SCAN_TTL_SECONDS = 15.0


def _index_path_for(cwd: str) -> Path:
    digest = hashlib.sha1(str(Path(cwd).resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return CODE_INDEX_DIR / f"{digest}.json"


def _iter_source_files(root: Path) -> list[Path]:
    resolved_root = root.resolve()
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS and not d.startswith(".") and not SECRET_RE.search(d)
        ]
        for name in files:
            if Path(name).suffix.lower() not in CODE_EXTENSIONS:
                continue
            path = Path(current) / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = name
            if SECRET_RE.search(rel):
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved_rel = resolved.relative_to(resolved_root).as_posix()
            except (OSError, ValueError):
                # Broken links, permission failures, and links escaping cwd are
                # skipped. In-root file symlinks are allowed after this check.
                continue
            if SECRET_RE.search(resolved_rel):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(path)
            if len(found) >= MAX_FILES:
                return found
    return found


def _chunk_file(path: Path, root: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    if not lines:
        return []
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    chunks: list[dict[str, Any]] = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    for start in range(0, len(lines), step):
        block = lines[start:start + CHUNK_LINES]
        body = "\n".join(block).strip()
        if not body:
            continue
        chunk_text = f"{rel}:{start + 1}\n{body}"
        chunks.append({
            "relative_path": rel,
            "start_line": start + 1,
            "end_line": min(len(lines), start + CHUNK_LINES),
            "text": chunk_text,
            "content_hash": _chunk_content_hash(chunk_text),
        })
        if start + CHUNK_LINES >= len(lines):
            break
    return chunks


def _chunk_body(text: str) -> str:
    """Exclude the mutable line-location header from semantic chunk identity."""
    _header, separator, body = str(text or "").partition("\n")
    return body if separator else str(text or "")


def _chunk_content_hash(text: str) -> str:
    return hashlib.sha256(_chunk_body(text).encode("utf-8", errors="replace")).hexdigest()


def _reuse_content_embeddings(
    fresh: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> int:
    """Copy embeddings onto content-identical chunks after line/mtime changes."""
    reusable: dict[str, list[dict[str, Any]]] = {}
    for chunk in previous:
        if not chunk.get("embedding") or not chunk.get("embedding_model"):
            continue
        content_hash = str(chunk.get("content_hash") or _chunk_content_hash(str(chunk.get("text") or "")))
        reusable.setdefault(content_hash, []).append(chunk)
    reused = 0
    for chunk in fresh:
        content_hash = str(chunk.get("content_hash") or _chunk_content_hash(str(chunk.get("text") or "")))
        chunk["content_hash"] = content_hash
        matches = reusable.get(content_hash) or []
        match_index = next(
            (
                index
                for index, candidate in enumerate(matches)
                if _chunk_body(str(candidate.get("text") or "")) == _chunk_body(str(chunk.get("text") or ""))
            ),
            None,
        )
        if match_index is None:
            continue
        prior = matches.pop(match_index)
        chunk["embedding"] = prior["embedding"]
        chunk["embedding_model"] = prior["embedding_model"]
        reused += 1
    return reused


def _load_index(cwd: str) -> dict[str, Any]:
    path = _index_path_for(cwd)
    if not path.exists():
        return {"cwd": str(Path(cwd).resolve()), "files": {}, "chunks": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("chunks"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"cwd": str(Path(cwd).resolve()), "files": {}, "chunks": []}


def _save_index(cwd: str, index: dict[str, Any]) -> None:
    CODE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(_index_path_for(cwd), json.dumps(index, separators=(",", ":")))
    _INDEX_MEM[str(Path(cwd).resolve())] = index


def embed_text_for(chunk: dict[str, Any]) -> str:
    """Salient embed text for a chunk, front-loaded for short-input embedders.

    Priority: location header, then symbol-definition lines (def/class/fn/...),
    then leading body lines — packed into EMBED_TEXT_CHARS.
    """
    text = str(chunk.get("text", ""))
    lines = text.splitlines()
    if not lines:
        return text[:EMBED_TEXT_CHARS]
    header = lines[0]  # "rel:start" location line
    body = lines[1:]
    symbols = [ln.strip() for ln in body if _SYMBOL_LINE_RE.match(ln)]
    leading = [ln.strip() for ln in body if ln.strip() and ln.strip() not in symbols]
    out: list[str] = [header]
    budget = EMBED_TEXT_CHARS - len(header) - 1
    for line in symbols + leading:
        if budget - (len(line) + 1) < 0:
            break
        out.append(line)
        budget -= len(line) + 1
    return "\n".join(out)


def invalidate_cache(cwd: str | None = None) -> None:
    """Drop the in-memory index cache (all cwds when None). For tests/reload."""
    if cwd is None:
        _INDEX_MEM.clear()
        _LAST_SCAN.clear()
        return
    key = str(Path(cwd).resolve())
    _INDEX_MEM.pop(key, None)
    _LAST_SCAN.pop(key, None)


def persisted_index_count() -> int:
    """Return the number of persisted code-index files without creating state."""

    if CODE_INDEX_DIR.is_symlink() or CODE_INDEX_DIR.is_file():
        return 1
    try:
        return sum(1 for path in CODE_INDEX_DIR.iterdir() if path.is_file() or path.is_symlink())
    except OSError:
        return 0


def purge_persisted_indexes() -> int:
    """Delete every persisted code-index file and clear process-local caches."""

    invalidate_cache()
    # Never follow a user-created directory symlink while deleting generated
    # state. Remove only the link (or an unexpected file at the index path).
    if CODE_INDEX_DIR.is_symlink() or CODE_INDEX_DIR.is_file():
        CODE_INDEX_DIR.unlink()
        return 1
    try:
        paths = tuple(CODE_INDEX_DIR.iterdir())
    except FileNotFoundError:
        return 0
    removed = 0
    for path in paths:
        if not (path.is_file() or path.is_symlink()):
            continue
        path.unlink()
        removed += 1
    try:
        CODE_INDEX_DIR.rmdir()
    except OSError:
        pass
    return removed


def build_or_update_index(cwd: str, *, force: bool = False) -> dict[str, Any]:
    """Rescan cwd, reusing chunks for unchanged files (size+mtime). No embedding.

    Rescans are throttled to SCAN_TTL_SECONDS per cwd; within the window the
    in-memory index is returned as-is (a fresh edit shows up on the next scan).
    """
    root = Path(cwd).resolve()
    key = str(root)
    now = time.monotonic()
    if not force and key in _INDEX_MEM and (now - _LAST_SCAN.get(key, 0.0)) < SCAN_TTL_SECONDS:
        return _INDEX_MEM[key]
    _LAST_SCAN[key] = now
    index = _INDEX_MEM.get(key) or _load_index(cwd)
    old_files: dict[str, Any] = index.get("files", {}) if index.get("cwd") == str(root) else {}
    old_chunks_by_file: dict[str, list[dict[str, Any]]] = {}
    for chunk in index.get("chunks", []) if index.get("cwd") == str(root) else []:
        old_chunks_by_file.setdefault(chunk.get("relative_path", ""), []).append(chunk)

    new_files: dict[str, Any] = {}
    new_chunks: list[dict[str, Any]] = []
    reused_files = 0
    reused_chunk_embeddings = 0
    rebuilt_chunks = 0
    for path in _iter_source_files(root):
        try:
            st = path.stat()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix() if root in path.parents or path.parent == root else path.name
        sig = {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
        prior = old_files.get(rel)
        if prior and prior.get("size") == sig["size"] and prior.get("mtime_ns") == sig["mtime_ns"] and rel in old_chunks_by_file:
            reused = old_chunks_by_file[rel]
            new_chunks.extend(reused)
            new_files[rel] = sig
            reused_files += 1
        else:
            fresh = _chunk_file(path, root)
            reused_chunk_embeddings += _reuse_content_embeddings(
                fresh,
                old_chunks_by_file.get(rel, []),
            )
            rebuilt_chunks += len(fresh)
            new_chunks.extend(fresh)
            new_files[rel] = sig
        if len(new_chunks) >= MAX_CHUNKS:
            break

    index = {
        "cwd": str(root),
        "files": new_files,
        "chunks": new_chunks,
        "refresh_stats": {
            "reused_files": reused_files,
            "content_reused_embeddings": reused_chunk_embeddings,
            "rebuilt_chunks": rebuilt_chunks,
        },
    }
    _save_index(cwd, index)  # also refreshes _INDEX_MEM
    return index


def ensure_embeddings(cwd: str, embed_fn: EmbedFn, model: str, *, cap: int = EMBED_PER_TURN_CAP) -> dict[str, Any]:
    """Embed up to `cap` chunks missing an embedding for `model`. Returns the index."""
    index = build_or_update_index(cwd)
    chunks = index.get("chunks", [])
    pending = [c for c in chunks if not c.get("embedding") or c.get("embedding_model") != model]
    if not pending:
        return index
    batch = pending[:cap]
    try:
        vectors = embed_fn([embed_text_for(c) for c in batch])
    except Exception:
        return index
    if len(vectors) != len(batch):
        return index
    for chunk, vec in zip(batch, vectors):
        chunk["embedding"] = vec
        chunk["embedding_model"] = model
    _save_index(cwd, index)
    return index


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def retrieve(cwd: str, query: str, embed_fn: EmbedFn, model: str, *, k: int = 4) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    index = ensure_embeddings(cwd, embed_fn, model)
    candidates = [c for c in index.get("chunks", []) if c.get("embedding") and c.get("embedding_model") == model]
    if not candidates:
        return []
    try:
        qvecs = embed_fn([query])
    except Exception:
        return []
    if not qvecs:
        return []
    qvec = qvecs[0]
    if _NUMPY:
        # Normalize both sides so this path computes true cosine and agrees
        # with the scalar fallback even for non-unit embedders.
        mat = _np.array([c["embedding"] for c in candidates], dtype=_np.float32)
        norms = _np.linalg.norm(mat, axis=1)
        norms[norms == 0.0] = 1.0
        mat = mat / norms[:, None]
        qv = _np.array(qvec, dtype=_np.float32)
        qnorm = float(_np.linalg.norm(qv))
        if qnorm > 0.0:
            qv = qv / qnorm
        sims = (mat @ qv).tolist()
        scored = [(float(s), candidates[i]) for i, s in enumerate(sims) if s > 0.0]
    else:
        scored = [(_cosine(qvec, c["embedding"]), c) for c in candidates]
        scored = [(s, c) for s, c in scored if s > 0.0]
    scored = stable_top_k(scored, k, score=lambda pair: pair[0])
    out: list[dict[str, Any]] = []
    for sim, chunk in scored:
        out.append({
            "relative_path": chunk.get("relative_path", ""),
            "start_line": chunk.get("start_line", 1),
            "end_line": chunk.get("end_line", 1),
            "text": chunk.get("text", ""),
            "score": round(float(sim), 4),
        })
    return out


def format_code_context(results: list[dict[str, Any]]) -> str:
    if not results:
        return ""
    lines = [
        "Relevant code from the working directory (read_file the path for full context):",
        "",
    ]
    for r in results:
        body = r.get("text", "")
        if len(body) > SNIPPET_CHARS:
            body = body[:SNIPPET_CHARS].rstrip() + "\n…"
        loc = f"{r.get('relative_path', '?')}:{r.get('start_line', 1)}-{r.get('end_line', 1)}"
        lines.append(f"### {loc}")
        lines.append("```")
        lines.append(body)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


def looks_like_code_project(cwd: str) -> bool:
    """Cheap gate: does cwd contain enough source to be worth indexing?"""
    root = Path(cwd)
    if not root.is_dir():
        return False
    markers = (
        "pyproject.toml", "setup.py", "package.json", "Cargo.toml", "go.mod",
        "pom.xml", "build.gradle", ".git", "requirements.txt", "tsconfig.json",
    )
    try:
        for marker in markers:
            if (root / marker).exists():
                return True
        # Otherwise require at least a few source files at the top two levels.
        count = 0
        for path in root.iterdir():
            if path.is_file() and path.suffix.lower() in CODE_EXTENSIONS:
                count += 1
                if count >= 3:
                    return True
    except OSError:
        return False
    return False
