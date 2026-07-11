"""Bridge to index-compute-lab ranked association graph for automatic turn context."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from .config import CONFIG_DIR, _atomic_write_text

MAX_INJECT_CHARS = 4_000
QUERY_TIMEOUT_SECONDS = 12.0
DEFAULT_LIMIT = 6

_GRAPH_REBRAND_NOTE = (
    "Note: canonical product concept in the graph is concept:algo-cli; "
    "ollama-cli / ollama-cli-concept in output are legacy cluster labels only. "
    "Current product: Algo CLI (`algo-cli`, package `algo_cli`). Prefer algo-cli for new work.\n\n"
)

# (question_hash, ranked_mtime_ns) -> (monotonic_expiry, text)
_QUERY_CACHE: dict[tuple[str, int], tuple[float, str]] = {}
_CACHE_TTL_SECONDS = 120.0


def resolve_lab_root() -> Path:
    explicit = (
        os.environ.get("ALGO_CLI_INDEX_COMPUTE_LAB_ROOT")
        or os.environ.get("INDEX_COMPUTE_LAB_ROOT")
    )
    if explicit:
        return Path(explicit).expanduser()
    return (Path.home() / "index-compute-lab").expanduser()


def lab_assets() -> tuple[Path, Path, Path] | None:
    root = resolve_lab_root()
    query_script = root / "query.py"
    ranked = root / "atoms" / "ranked-association-map.json"
    aliases = root / "atoms" / "alias-table.json"
    if not query_script.is_file() or not ranked.is_file() or not aliases.is_file():
        return None
    return root, ranked, aliases


def lab_available() -> bool:
    return lab_assets() is not None


def ranked_mtime_ns() -> int:
    assets = lab_assets()
    if assets is None:
        return 0
    try:
        return assets[1].stat().st_mtime_ns
    except OSError:
        return 0


def run_ask(question: str, *, limit: int = DEFAULT_LIMIT, timeout: float = QUERY_TIMEOUT_SECONDS) -> str:
    """Run `query.py ask` and return stdout (or error text)."""
    text = (question or "").strip()
    if not text:
        return ""
    assets = lab_assets()
    if assets is None:
        root = resolve_lab_root()
        return f"Error: index-compute-lab not ready under {root} (need query.py + atoms/*.json)"
    root, _ranked, _aliases = assets
    bounded = max(1, min(int(limit), 20))
    command = [sys.executable, str(root / "query.py"), "ask", text, "--limit", str(bounded)]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: knowledge graph query timed out after {timeout:.0f}s"
    except Exception as exc:
        return f"Error running knowledge graph query: {exc}"
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return f"Error: knowledge graph query failed ({result.returncode}): {output or 'no output'}"
    return output


def context_for_query(question: str, *, limit: int = DEFAULT_LIMIT, use_cache: bool = True) -> str:
    """Ranked-graph context for injection into the user turn (empty if unavailable)."""
    text = (question or "").strip()
    if not text or not lab_available():
        return ""
    mtime = ranked_mtime_ns()
    key = (hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], mtime)
    if use_cache:
        cached = _QUERY_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    raw = run_ask(text, limit=limit)
    if raw and "Filtered edges (0):" in raw and "aggregated)" in raw:
        fallback = run_ask(f"about {text}", limit=limit, timeout=QUERY_TIMEOUT_SECONDS)
        if fallback and not fallback.startswith("Error:") and "Co-occurring" in fallback:
            raw = fallback
    if not raw or raw.startswith("Error:"):
        return ""
    if len(raw) > MAX_INJECT_CHARS:
        raw = raw[:MAX_INJECT_CHARS].rstrip() + "\n...[truncated]"
    lower = raw.lower()
    if "ollama-cli" in lower or "ollama_cli" in lower:
        raw = _GRAPH_REBRAND_NOTE + raw
    if use_cache:
        _QUERY_CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, raw)
    return raw


def atoms_dir() -> Path | None:
    root = resolve_lab_root()
    atoms = root / "atoms"
    return atoms if atoms.is_dir() else None


def ensure_harness_roots_file() -> bool:
    """Keep harness_roots.json free of index-compute-lab entries.

    The live lab path is registered dynamically by harness.all_source_roots()
    via atoms_dir(). Writing the same root into harness_roots.json previously
    double-indexed agent-notes and other atoms markdown. This helper now removes
    any legacy ICL entries so refresh/index stays single-copy.
    """
    EXTRA = CONFIG_DIR / "harness_roots.json"
    if not EXTRA.exists():
        return False
    try:
        import json

        loaded = json.loads(EXTRA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, list):
        return False
    filtered = [
        item
        for item in loaded
        if not (isinstance(item, dict) and str(item.get("harness")) == "index-compute-lab")
    ]
    if len(filtered) == len(loaded):
        return False
    if filtered:
        _atomic_write_text(EXTRA, json.dumps(filtered, indent=2) + "\n")
    else:
        try:
            EXTRA.unlink()
        except OSError:
            _atomic_write_text(EXTRA, "[]\n")
    return True



PIPELINE_STEPS: tuple[tuple[str, list[str]], ...] = (
    ("association_atoms", ["association_atoms.py"]),
    ("atom_association_map", ["atom_association_map.py"]),
    ("key_index", ["key_index.py"]),
    ("normalizer", ["normalizer.py"]),
    ("resolver", ["resolver.py"]),
)


def _run_lab_script(root: Path, script: str, extra_args: list[str] | None = None, *, timeout: float = 600.0) -> str:
    command = [sys.executable, str(root / script), *(extra_args or [])]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: timed out running {script}"
    except Exception as exc:
        return f"Error running {script}: {exc}"
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return f"Error: {script} failed ({result.returncode}): {output or 'no output'}"
    return output or f"OK: {script}"


def run_pipeline(*, include_removable_seed: bool = False, removable_scopes: list[str] | None = None) -> str:
    """Run index-compute-lab graph rebuild steps after optional removable re-seed."""
    assets = lab_assets()
    if assets is None:
        return f"Error: index-compute-lab not ready under {resolve_lab_root()}"
    root, _ranked, _aliases = assets
    lines: list[str] = []
    if include_removable_seed:
        args: list[str] = []
        for scope in removable_scopes or []:
            args.extend(["--scope", scope])
        lines.append(_run_lab_script(root, "removable_drive_atoms.py", args, timeout=1800.0))
    for step_name, scripts in PIPELINE_STEPS:
        for script in scripts:
            lines.append(f"## {step_name}")
            lines.append(_run_lab_script(root, script, timeout=1800.0))
    _QUERY_CACHE.clear()
    return "\n".join(lines)


def write_graph_note(title: str, body: str) -> str:
    """Write a durable markdown note under atoms/ for harness RAG + human audit."""
    assets = lab_assets()
    if assets is None:
        return f"Error: index-compute-lab not ready under {resolve_lab_root()}"
    root, _ranked, _aliases = assets
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in title.lower()).strip("-") or "note"
    notes_dir = root / "atoms" / "agent-notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / f"{safe}.md"
    text = f"# {title.strip()}\n\n{(body or '').strip()}\n"
    _atomic_write_text(path, text)
    return f"Wrote {path} ({len(text)} chars). Run harness_refresh to embed for retrieval."
