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
import stat
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
from .intelligence.project_graph import build_project_graph
from .intelligence.repo_map import rank_repo_map, render_repo_map, snapshot_project_graph
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
STRUCTURAL_WEIGHT = 0.18
REPO_MAP_TOKEN_BUDGET = 320

CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".sh",
        ".ps1",
        ".sql",
        ".lua",
        ".r",
        ".jl",
        ".ml",
        ".ex",
        ".exs",
        ".toml",
        ".cfg",
        ".ini",
        ".yaml",
        ".yml",
        ".json",
        ".md",
    }
)
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".next",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        ".tox",
        ".idea",
        ".vscode",
        "coverage",
        ".cache",
        "benchmark-results",
        "htmlcov",
    }
)

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
MAX_PERSISTED_INDEX_ENTRIES = 1024


def _index_path_for(cwd: str) -> Path:
    digest = hashlib.sha1(str(Path(cwd).resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return CODE_INDEX_DIR / f"{digest}.json"


def _iter_source_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            [
                directory
                for directory in dirs
                if directory not in SKIP_DIRS
                and not directory.startswith(".")
                and not SECRET_RE.search(directory)
                and not (Path(current) / directory).is_symlink()
            ],
            key=str.lower,
        )
        for name in sorted(files, key=str.lower):
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
                source_stat = path.lstat()
            except OSError:
                continue
            # Code snippets cross the active model boundary and are persisted in
            # the local code index.  Accept only a single-link regular file:
            # symlinks, devices/FIFOs, and hardlink aliases can otherwise smuggle
            # a legacy memory file into an apparently safe working directory.
            if (
                not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_nlink != 1
                or source_stat.st_size > MAX_FILE_BYTES
            ):
                continue
            found.append(path)
            if len(found) >= MAX_FILES:
                return found
    return found


def _validated_source_stat(path: Path) -> os.stat_result | None:
    """Return a safe source lstat or ``None`` without following an alias."""

    try:
        source_stat = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1 or source_stat.st_size > MAX_FILE_BYTES:
        return None
    return source_stat


def _read_source_text(path: Path) -> tuple[str, os.stat_result] | None:
    """Read one bounded regular single-link source through a no-follow fd.

    The pre/post descriptor checks make a leaf replacement or mutation during
    the read fail closed. ``O_NONBLOCK`` prevents a swap to a FIFO from hanging
    before ``fstat`` can reject it.
    """

    before = _validated_source_stat(path)
    if before is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > MAX_FILE_BYTES
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            return None
        remaining = MAX_FILE_BYTES + 1
        chunks: list[bytes] = []
        while remaining > 0:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(payload) > MAX_FILE_BYTES or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_nlink):
            return None
        return payload.decode("utf-8", errors="replace"), after
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _chunk_file(path: Path, root: Path) -> list[dict[str, Any]]:
    source = _read_source_text(path)
    if source is None:
        return []
    text, _source_stat = source
    return _chunk_source_text(text, path, root)


def _chunk_source_text(text: str, path: Path, root: Path) -> list[dict[str, Any]]:
    """Chunk text captured by ``_read_source_text`` without reopening it."""

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
        block = lines[start : start + CHUNK_LINES]
        body = "\n".join(block).strip()
        if not body:
            continue
        chunk_text = f"{rel}:{start + 1}\n{body}"
        chunks.append(
            {
                "relative_path": rel,
                "start_line": start + 1,
                "end_line": min(len(lines), start + CHUNK_LINES),
                "text": chunk_text,
                "content_hash": _chunk_content_hash(chunk_text),
            }
        )
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


def _index_sources_valid(cwd: str, index: dict[str, Any]) -> bool:
    """Verify every indexed source is still the exact safe file recorded."""

    root = Path(cwd).resolve()
    if index.get("cwd") != str(root):
        return False
    files = index.get("files")
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        return False
    for relative, signature in files.items():
        if not isinstance(relative, str) or not isinstance(signature, dict):
            return False
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            return False
        candidate = root / relative_path
        source_stat = _validated_source_stat(candidate)
        if source_stat is None:
            return False
        expected = (
            signature.get("device"),
            signature.get("inode"),
            signature.get("size"),
            signature.get("mtime_ns"),
        )
        actual = (
            int(source_stat.st_dev),
            int(source_stat.st_ino),
            int(source_stat.st_size),
            int(source_stat.st_mtime_ns),
        )
        if expected != actual:
            return False
    return True


def _save_index(cwd: str, index: dict[str, Any]) -> bool:
    if not _index_sources_valid(cwd, index):
        invalidate_cache(cwd)
        return False
    CODE_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(_index_path_for(cwd), json.dumps(index, separators=(",", ":")))
    _INDEX_MEM[str(Path(cwd).resolve())] = index
    return True


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
    """Return the number of entries below the code-index root without creating state."""

    try:
        root_info = CODE_INDEX_DIR.lstat()
    except FileNotFoundError:
        return 0
    except OSError:
        return 1
    if not stat.S_ISDIR(root_info.st_mode):
        return 1
    try:
        return sum(1 for _path in CODE_INDEX_DIR.iterdir())
    except OSError:
        return 1


def purge_persisted_indexes() -> int:
    """Delete the flat persisted code-index store without following aliases.

    Code indexes have always used a flat directory. Unexpected nested or
    special entries therefore fail closed instead of being silently ignored or
    traversed. The Echo preflight treats this exception as an unavailable
    protected state, so it cannot report a successful purge while plaintext
    remains below the declared root.
    """

    invalidate_cache()
    directory_flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    try:
        parent_fd = os.open(CODE_INDEX_DIR.parent, directory_flags)
    except FileNotFoundError:
        return 0
    try:
        try:
            root_info = os.stat(CODE_INDEX_DIR.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if stat.S_ISLNK(root_info.st_mode) or stat.S_ISREG(root_info.st_mode):
            os.unlink(CODE_INDEX_DIR.name, dir_fd=parent_fd)
            return 1
        if not stat.S_ISDIR(root_info.st_mode):
            raise OSError("code index root is not a regular file, symlink, or directory")
        root_fd = os.open(CODE_INDEX_DIR.name, directory_flags, dir_fd=parent_fd)
        try:
            opened_root = os.fstat(root_fd)
            if (opened_root.st_dev, opened_root.st_ino) != (root_info.st_dev, root_info.st_ino):
                raise OSError("code index root changed during purge")
            names = sorted(os.listdir(root_fd))
            if len(names) > MAX_PERSISTED_INDEX_ENTRIES:
                raise OSError("code index entry count exceeds the purge bound")
            entries: list[tuple[str, tuple[int, int, int]]] = []
            for name in names:
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    raise OSError("code index contains an unexpected nested or special entry")
                entries.append((name, (info.st_dev, info.st_ino, info.st_mode)))
            removed = 0
            for name, expected in entries:
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_mode) != expected:
                    raise OSError("code index entry changed during purge")
                os.unlink(name, dir_fd=root_fd)
                removed += 1
            if os.listdir(root_fd):
                raise OSError("code index changed during purge")
        finally:
            os.close(root_fd)
        current_root = os.stat(CODE_INDEX_DIR.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino):
            raise OSError("code index root changed during purge")
        os.rmdir(CODE_INDEX_DIR.name, dir_fd=parent_fd)
        return removed
    finally:
        os.close(parent_fd)


def build_or_update_index(cwd: str, *, force: bool = False) -> dict[str, Any]:
    """Rescan cwd, reusing chunks for unchanged files (size+mtime). No embedding.

    Rescans are throttled to SCAN_TTL_SECONDS per cwd; within the window the
    in-memory index is returned as-is (a fresh edit shows up on the next scan).
    """
    root = Path(cwd).resolve()
    key = str(root)
    now = time.monotonic()
    if not force and key in _INDEX_MEM and (now - _LAST_SCAN.get(key, 0.0)) < SCAN_TTL_SECONDS:
        cached = _INDEX_MEM[key]
        if _index_sources_valid(cwd, cached):
            return cached
        invalidate_cache(cwd)
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
        source = _read_source_text(path)
        if source is None:
            continue
        source_text, st = source
        rel = path.relative_to(root).as_posix() if root in path.parents or path.parent == root else path.name
        sig = {
            "device": int(st.st_dev),
            "inode": int(st.st_ino),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
        }
        prior = old_files.get(rel)
        if prior == sig and rel in old_chunks_by_file:
            reused = old_chunks_by_file[rel]
            new_chunks.extend(reused)
            new_files[rel] = sig
            reused_files += 1
        else:
            fresh = _chunk_source_text(source_text, path, root)
            reused_chunk_embeddings += _reuse_content_embeddings(
                fresh,
                old_chunks_by_file.get(rel, []),
            )
            rebuilt_chunks += len(fresh)
            new_chunks.extend(fresh)
            new_files[rel] = sig
        if len(new_chunks) >= MAX_CHUNKS:
            break

    prior_structural = index.get("structural") if isinstance(index, dict) else None
    if reused_files == len(new_files) and isinstance(prior_structural, dict):
        structural = prior_structural
    else:
        try:
            graph = build_project_graph(
                root,
                persist=False,
                source_files=new_files,
                include_git_recency=False,
            )
            structural = snapshot_project_graph(graph)
        except Exception:
            structural = {}

    index = {
        "cwd": str(root),
        "files": new_files,
        "chunks": new_chunks,
        "structural": structural,
        "refresh_stats": {
            "reused_files": reused_files,
            "content_reused_embeddings": reused_chunk_embeddings,
            "rebuilt_chunks": rebuilt_chunks,
        },
    }
    if not _save_index(cwd, index):
        return {"cwd": str(root), "files": {}, "chunks": [], "structural": {}}
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
    if not _save_index(cwd, index):
        return {"cwd": str(Path(cwd).resolve()), "files": {}, "chunks": [], "structural": {}}
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
    return float(dot / ((na**0.5) * (nb**0.5)))


def retrieve(
    cwd: str,
    query: str,
    embed_fn: EmbedFn,
    model: str,
    *,
    k: int = 4,
    structural_weight: float = STRUCTURAL_WEIGHT,
) -> list[dict[str, Any]]:
    """Retrieve chunks by semantic similarity fused with structural importance."""

    query = (query or "").strip()
    if not query:
        return []
    structural_weight = min(1.0, max(0.0, structural_weight))
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
        semantic_scored = [(float(score), candidates[index]) for index, score in enumerate(sims) if score > 0.0]
    else:
        semantic_scored = [(_cosine(qvec, chunk["embedding"]), chunk) for chunk in candidates]
        semantic_scored = [(score, chunk) for score, chunk in semantic_scored if score > 0.0]

    structural_snapshot = index.get("structural", {})
    structural_by_path = (
        {entry.path: entry.rank for entry in rank_repo_map(structural_snapshot, query)} if structural_weight else {}
    )
    scored = []
    for semantic_score, chunk in semantic_scored:
        structural_score = structural_by_path.get(str(chunk.get("relative_path", "")), 0.0)
        combined_score = (1.0 - structural_weight) * semantic_score + structural_weight * structural_score
        scored.append((combined_score, semantic_score, structural_score, chunk))
    scored = stable_top_k(scored, k, score=lambda row: row[0])
    out: list[dict[str, Any]] = []
    for combined_score, semantic_score, structural_score, chunk in scored:
        out.append(
            {
                "relative_path": chunk.get("relative_path", ""),
                "start_line": chunk.get("start_line", 1),
                "end_line": chunk.get("end_line", 1),
                "text": chunk.get("text", ""),
                "score": round(float(combined_score), 4),
                "semantic_score": round(float(semantic_score), 4),
                "structural_score": round(float(structural_score), 4),
                "retrieval_strategy": "semantic+structural" if structural_weight else "semantic",
            }
        )
    if out and structural_weight:
        repo_map = render_repo_map(
            structural_snapshot,
            query,
            token_budget=REPO_MAP_TOKEN_BUDGET,
        )
        if repo_map:
            out[0]["repo_map"] = repo_map
    return out


def format_code_context(results: list[dict[str, Any]]) -> str:
    if not results:
        return ""
    lines = [
        "Relevant code from the working directory (read_file the path for full context):",
        "",
    ]
    repo_map = str(results[0].get("repo_map", ""))
    if repo_map:
        lines.extend((repo_map, ""))
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
        "pyproject.toml",
        "setup.py",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        ".git",
        "requirements.txt",
        "tsconfig.json",
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
