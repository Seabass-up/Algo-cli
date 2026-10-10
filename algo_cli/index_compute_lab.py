"""Bridge to index-compute-lab ranked association graph for automatic turn context."""

from __future__ import annotations

import os
from pathlib import Path

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
    return None


def lab_available() -> bool:
    return False


def ranked_mtime_ns() -> int:
    return 0


def run_ask(question: str, *, limit: int = DEFAULT_LIMIT, timeout: float = QUERY_TIMEOUT_SECONDS) -> str:
    return "Error: index-compute-lab is retired; use native Continuum."


def context_for_query(question: str, *, limit: int = DEFAULT_LIMIT, use_cache: bool = True) -> str:
    return ""


def atoms_dir() -> Path | None:
    return None


def ensure_harness_roots_file() -> bool:
    return False



PIPELINE_STEPS: tuple[tuple[str, list[str]], ...] = (
    ("association_atoms", ["association_atoms.py"]),
    ("atom_association_map", ["atom_association_map.py"]),
    ("key_index", ["key_index.py"]),
    ("normalizer", ["normalizer.py"]),
    ("resolver", ["resolver.py"]),
)


def _run_lab_script(root: Path, script: str, extra_args: list[str] | None = None, *, timeout: float = 600.0) -> str:
    return "Error: index-compute-lab is retired; no script was invoked."


def run_pipeline(*, include_removable_seed: bool = False, removable_scopes: list[str] | None = None) -> str:
    return "Error: index-compute-lab is retired; no pipeline was invoked."


def write_graph_note(title: str, body: str) -> str:
    return "Error: index-compute-lab is retired; use scoped memory_remember."
