"""Compact, query-aware structural repository maps.

The design is inspired by the repository-map pattern used by coding agents,
but is implemented on Algo's dependency-free Python project graph and CodeRank
kernel. The serialized snapshot is intentionally small enough to live beside
the consent-gated code-RAG index.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .coderank import CodeRank
from .project_graph import ProjectGraph, SymbolNode


TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STOP_WORDS = frozenset({
    "a", "an", "and", "are", "at", "be", "by", "code", "do", "file", "for",
    "from", "how", "in", "is", "it", "of", "on", "or", "the", "this", "to",
    "use", "with",
})
MAX_SYMBOLS_PER_FILE = 80
MAX_SNAPSHOT_SYMBOLS = 12_000


@dataclass(frozen=True)
class RepoMapEntry:
    path: str
    rank: float
    lexical_score: float
    symbols: tuple[dict[str, Any], ...]


def snapshot_project_graph(graph: ProjectGraph) -> dict[str, Any]:
    """Create a bounded JSON-friendly structural snapshot from a project graph."""

    nodes = sorted(graph.files)
    edge_counts = Counter(
        (edge.source_file, edge.target_file)
        for edge in graph.imports
        if edge.target_file and edge.source_file != edge.target_file
    )
    edges = sorted(edge_counts)
    weights = {edge: float(weight) for edge, weight in edge_counts.items()}
    ranks = CodeRank().compute(nodes, edges, weights)
    max_rank = max((result.rank for result in ranks), default=0.0)
    normalized_ranks = {
        result.symbol: (result.rank / max_rank if max_rank else 0.0)
        for result in ranks
    }

    symbols_by_file: dict[str, list[dict[str, Any]]] = {path: [] for path in nodes}
    captured_symbols = 0
    for symbol in sorted(
        graph.symbols.values(),
        key=lambda item: (item.file, item.line, item.qualname),
    ):
        file_symbols = symbols_by_file.setdefault(symbol.file, [])
        if (
            captured_symbols >= MAX_SNAPSHOT_SYMBOLS
            or len(file_symbols) >= MAX_SYMBOLS_PER_FILE
        ):
            continue
        file_symbols.append(_symbol_row(symbol))
        captured_symbols += 1

    return {
        "files": {
            path: {
                "rank": round(normalized_ranks.get(path, 0.0), 6),
                "symbols": symbols_by_file.get(path, []),
            }
            for path in nodes
        },
        "edges": [
            [source, target, int(edge_counts[(source, target)])]
            for source, target in edges
        ],
    }


def _symbol_row(symbol: SymbolNode) -> dict[str, Any]:
    return {
        "name": symbol.name,
        "qualname": symbol.qualname,
        "kind": symbol.kind,
        "line": symbol.line,
        "end_line": symbol.end_line,
    }


def _query_tokens(query: str) -> set[str]:
    return {
        token.lower()
        for token in TOKEN_RE.findall(query or "")
        if len(token) > 1 and token.lower() not in STOP_WORDS
    }


def _lexical_score(path: str, symbols: list[dict[str, Any]], tokens: set[str]) -> float:
    if not tokens:
        return 0.0
    path_tokens = {token.lower() for token in TOKEN_RE.findall(path)}
    symbol_tokens: set[str] = set()
    for symbol in symbols:
        symbol_tokens.update(
            token.lower()
            for token in TOKEN_RE.findall(
                f"{symbol.get('name', '')} {symbol.get('qualname', '')}"
            )
        )
    path_hits = len(tokens & path_tokens)
    symbol_hits = len(tokens & symbol_tokens)
    score = (1.0 * path_hits + 0.8 * symbol_hits) / max(1.0, float(len(tokens)))
    return min(1.0, score)


def rank_repo_map(snapshot: dict[str, Any], query: str = "") -> list[RepoMapEntry]:
    """Rank snapshot files with personalized CodeRank and lexical task affinity."""

    files = snapshot.get("files")
    if not isinstance(files, dict) or not files:
        return []
    nodes = sorted(str(path) for path in files)
    edges: list[tuple[str, str]] = []
    weights: dict[tuple[str, str], float] = {}
    for row in snapshot.get("edges", []):
        if not isinstance(row, list) or len(row) != 3:
            continue
        source, target, raw_weight = str(row[0]), str(row[1]), row[2]
        edge = (source, target)
        edges.append(edge)
        weights[edge] = float(raw_weight)

    tokens = _query_tokens(query)
    lexical: dict[str, float] = {}
    for path in nodes:
        raw = files.get(path, {})
        symbols = raw.get("symbols", []) if isinstance(raw, dict) else []
        lexical[path] = _lexical_score(path, symbols, tokens)
    matched = {path: score for path, score in lexical.items() if score > 0.0}
    personalized = CodeRank().compute(
        nodes,
        edges,
        weights,
        personalization=matched or None,
    )
    max_rank = max((result.rank for result in personalized), default=0.0)
    rank_by_path = {
        result.symbol: (result.rank / max_rank if max_rank else 0.0)
        for result in personalized
    }
    entries = []
    for path in nodes:
        raw = files.get(path, {})
        symbols = raw.get("symbols", []) if isinstance(raw, dict) else []
        entries.append(
            RepoMapEntry(
                path=path,
                rank=round(rank_by_path.get(path, 0.0), 6),
                lexical_score=round(lexical[path], 6),
                symbols=tuple(symbol for symbol in symbols if isinstance(symbol, dict)),
            )
        )
    return sorted(
        entries,
        key=lambda entry: (
            -(0.75 * entry.rank + 0.25 * entry.lexical_score),
            entry.path,
        ),
    )


def render_repo_map(
    snapshot: dict[str, Any],
    query: str = "",
    *,
    token_budget: int = 600,
) -> str:
    """Render a deterministic structural outline within an approximate token budget."""

    if token_budget <= 0:
        return ""
    character_budget = token_budget * 4
    lines = ["Structural repository map (ranked for this task):"]
    used = len(lines[0])
    for entry in rank_repo_map(snapshot, query):
        header = f"{entry.path}  [structure={entry.rank:.3f}]"
        candidates = [header]
        for symbol in entry.symbols:
            candidates.append(
                f"  L{symbol.get('line', 1)} {symbol.get('kind', 'symbol')} "
                f"{symbol.get('qualname', symbol.get('name', ''))}"
            )
        block = "\n".join(candidates)
        if used + len(block) + 1 > character_budget:
            if len(lines) == 1 and used + len(header) + 1 <= character_budget:
                lines.append(header)
            break
        lines.extend(candidates)
        used += len(block) + 1
    return "\n".join(lines) if len(lines) > 1 else ""
