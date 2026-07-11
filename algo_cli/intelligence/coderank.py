"""B85. CodeRank: Structural Importance Ranking.

PageRank over code graph to find most structurally important symbols.
Source: PyCodeKG pattern.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CodeRankResult:
    symbol: str
    rank: float
    kind: str = ""
    file: str = ""


class CodeRank:
    """Compute PageRank over a code graph to find critical symbols."""

    def __init__(self, damping: float = 0.85, max_iterations: int = 100,
                 tolerance: float = 1e-6) -> None:
        self._damping = damping
        self._max_iter = max_iterations
        self._tolerance = tolerance

    def compute(self, nodes: list[str], edges: list[tuple[str, str]],
                weights: dict[tuple[str, str], float] | None = None) -> list[CodeRankResult]:
        """Compute PageRank for code symbols.

        Args:
            nodes: List of node IDs
            edges: List of (source, target) directed edges
            weights: Optional edge weights
        """
        if not nodes:
            return []

        n = len(nodes)
        node_idx = {node: i for i, node in enumerate(nodes)}

        # Build adjacency: out-links and in-links
        out_links: dict[int, list[int]] = {i: [] for i in range(n)}
        in_links: dict[int, list[int]] = {i: [] for i in range(n)}

        for src, tgt in edges:
            if src in node_idx and tgt in node_idx:
                si, ti = node_idx[src], node_idx[tgt]
                out_links[si].append(ti)
                in_links[ti].append(si)

        # Initialize ranks
        ranks = [1.0 / n] * n

        # Iterate
        for _ in range(self._max_iter):
            new_ranks = [0.0] * n
            for i in range(n):
                # Base rank from random jump
                new_ranks[i] = (1 - self._damping) / n

                # Rank from in-links
                for j in in_links[i]:
                    out_count = len(out_links[j])
                    if out_count > 0:
                        new_ranks[i] += self._damping * ranks[j] / out_count
                    else:
                        # Dangling node: distribute rank evenly
                        new_ranks[i] += self._damping * ranks[j] / n

            # Check convergence
            diff = sum(abs(new_ranks[i] - ranks[i]) for i in range(n))
            ranks = new_ranks
            if diff < self._tolerance:
                break

        # Sort by rank descending
        results = [
            CodeRankResult(symbol=nodes[i], rank=ranks[i])
            for i in range(n)
        ]
        results.sort(key=lambda x: x.rank, reverse=True)
        return results

    def find_critical(self, results: list[CodeRankResult],
                      top_k: int = 10) -> list[CodeRankResult]:
        """Find the top-K most structurally critical symbols."""
        return results[:top_k]

    def find_bottlenecks(self, results: list[CodeRankResult],
                         threshold: float = 0.05) -> list[CodeRankResult]:
        """Find symbols with unusually high rank (potential bottlenecks)."""
        if not results:
            return []
        avg_rank = sum(r.rank for r in results) / len(results)
        return [r for r in results if r.rank > avg_rank * (1 + threshold)]