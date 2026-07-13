"""B85. CodeRank: weighted structural importance ranking for code graphs."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CodeRankResult:
    symbol: str
    rank: float
    kind: str = ""
    file: str = ""


class CodeRank:
    """Compute deterministic weighted PageRank over a code graph.

    Personalization lets callers bias the random walk toward files or symbols
    mentioned by the current task. Dangling rank is redistributed through the
    same personalization vector so rank mass is conserved.
    """

    def __init__(
        self,
        damping: float = 0.85,
        max_iterations: int = 100,
        tolerance: float = 1e-6,
    ) -> None:
        if not 0.0 <= damping < 1.0:
            raise ValueError("damping must be in [0, 1)")
        if max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        if tolerance <= 0.0:
            raise ValueError("tolerance must be positive")
        self._damping = damping
        self._max_iter = max_iterations
        self._tolerance = tolerance

    def compute(
        self,
        nodes: list[str],
        edges: list[tuple[str, str]],
        weights: dict[tuple[str, str], float] | None = None,
        *,
        personalization: dict[str, float] | None = None,
    ) -> list[CodeRankResult]:
        """Rank nodes using weighted, optionally personalized PageRank.

        Unknown edge endpoints and non-positive/non-finite weights are ignored.
        Duplicate node IDs are collapsed while preserving their first-seen
        order; duplicate edges are accumulated.
        """

        unique_nodes = list(dict.fromkeys(nodes))
        if not unique_nodes:
            return []

        count = len(unique_nodes)
        node_index = {node: index for index, node in enumerate(unique_nodes)}
        outgoing: list[dict[int, float]] = [{} for _ in range(count)]
        for source, target in edges:
            source_index = node_index.get(source)
            target_index = node_index.get(target)
            if source_index is None or target_index is None:
                continue
            weight = 1.0 if weights is None else weights.get((source, target), 1.0)
            if not math.isfinite(weight) or weight <= 0.0:
                continue
            outgoing[source_index][target_index] = (
                outgoing[source_index].get(target_index, 0.0) + weight
            )

        preference = self._preference_vector(unique_nodes, personalization)
        ranks = preference.copy()
        for _ in range(self._max_iter):
            dangling_mass = sum(
                ranks[index] for index, targets in enumerate(outgoing) if not targets
            )
            new_ranks = [
                ((1.0 - self._damping) + self._damping * dangling_mass) * value
                for value in preference
            ]
            for source_index, targets in enumerate(outgoing):
                if not targets:
                    continue
                total_weight = sum(targets.values())
                distributed = self._damping * ranks[source_index] / total_weight
                for target_index, weight in targets.items():
                    new_ranks[target_index] += distributed * weight

            difference = sum(abs(current - prior) for current, prior in zip(new_ranks, ranks))
            ranks = new_ranks
            if difference < self._tolerance:
                break

        total = sum(ranks)
        if total > 0.0:
            ranks = [rank / total for rank in ranks]
        results = [
            CodeRankResult(symbol=node, rank=ranks[index])
            for index, node in enumerate(unique_nodes)
        ]
        return sorted(results, key=lambda result: (-result.rank, result.symbol))

    @staticmethod
    def _preference_vector(
        nodes: list[str],
        personalization: dict[str, float] | None,
    ) -> list[float]:
        if personalization:
            values = [max(0.0, float(personalization.get(node, 0.0))) for node in nodes]
            values = [value if math.isfinite(value) else 0.0 for value in values]
            total = sum(values)
            if total > 0.0:
                return [value / total for value in values]
        uniform = 1.0 / len(nodes)
        return [uniform] * len(nodes)

    def find_critical(
        self,
        results: list[CodeRankResult],
        top_k: int = 10,
    ) -> list[CodeRankResult]:
        """Return the top-K structurally critical nodes."""

        return results[:max(0, top_k)]

    def find_bottlenecks(
        self,
        results: list[CodeRankResult],
        threshold: float = 0.05,
    ) -> list[CodeRankResult]:
        """Return nodes whose rank is above the requested mean-relative threshold."""

        if not results:
            return []
        average = sum(result.rank for result in results) / len(results)
        return [result for result in results if result.rank > average * (1.0 + threshold)]
