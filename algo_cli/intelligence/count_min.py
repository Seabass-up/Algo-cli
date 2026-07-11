"""Count-Min Sketch — sublinear-space frequency estimation.

A probabilistic data structure that estimates the frequency of items in a
data stream using d independent hash functions and a d×w counter array.
Memory is O(d*w) instead of O(unique_items).  Estimates never underestimate
but may overestimate (false positives only).

Harness use:
  - Track tool call frequency ("how often is read_file called?")
  - Track error frequency ("how many times has embed failed?")
  - Detect heavy hitters in the agent loop
  - Feed frequency data to the intuition engine

Operations:
  - update(item, count=1): O(d) — increment counters
  - estimate(item): O(d) — return min of d counters
  - merge(other): add two sketches
  - heavy_hitters(threshold): items above threshold (requires tracking)

Properties:
  - Never underestimates (true_count <= estimate)
  - Overestimate bounded by: ε·N with probability 1-δ
    where w = ceil(e/ε), d = ceil(ln(1/δ)), N = total count
"""
from __future__ import annotations

import hashlib
import math
from typing import Any


class CountMinSketch:
    """Count-Min Sketch with d×w counter array.

    Args:
        epsilon: Error bound (overestimate <= epsilon * total_count).
        delta: Failure probability (Pr[overestimate > epsilon*N] < delta).
    """

    def __init__(self, epsilon: float = 0.01, delta: float = 0.01) -> None:
        self.epsilon = epsilon
        self.delta = delta
        self.width = max(1, int(math.ceil(math.e / epsilon)))
        self.depth = max(1, int(math.ceil(math.log(1 / delta))))
        self._table: list[list[int]] = [[0] * self.width for _ in range(self.depth)]
        self.total_count = 0

    def _hash(self, item: Any, row: int) -> int:
        """Hash item to a column index for a given row."""
        data = f"{row}:{item}".encode("utf-8")
        h = int.from_bytes(hashlib.md5(data).digest()[:8], "little")
        return h % self.width

    def update(self, item: Any, count: int = 1) -> None:
        """Increment the count for item by count (default 1)."""
        for row in range(self.depth):
            col = self._hash(item, row)
            self._table[row][col] += count
        self.total_count += count

    def estimate(self, item: Any) -> int:
        """Estimate the frequency of item. Never underestimates."""
        return min(
            self._table[row][self._hash(item, row)]
            for row in range(self.depth)
        )

    def merge(self, other: CountMinSketch) -> None:
        """Merge another sketch into this one (element-wise max)."""
        if self.width != other.width or self.depth != other.depth:
            raise ValueError("Cannot merge sketches with different dimensions")
        for row in range(self.depth):
            for col in range(self.width):
                self._table[row][col] += other._table[row][col]
        self.total_count += other.total_count

    def inner_product(self, other: CountMinSketch) -> int:
        """Estimate the inner product of two frequency vectors.

        Useful for computing dot products of item frequency distributions.
        """
        if self.width != other.width or self.depth != other.depth:
            raise ValueError("Dimensions must match")
        return min(
            sum(self._table[row][col] * other._table[row][col]
                for col in range(self.width))
            for row in range(self.depth)
        )

    def stats(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "depth": self.depth,
            "memory_cells": self.width * self.depth,
            "total_count": self.total_count,
            "epsilon": self.epsilon,
            "delta": self.delta,
        }


class HeavyHitters:
    """Track top-K frequent items using Space-Saving algorithm.

    Maintains an exact count of the top-K items seen so far, using O(K) memory.
    Based on Metwally et al. (2005) Space-Saving algorithm.

    Args:
        k: Number of heavy hitters to track.
    """

    def __init__(self, k: int = 10) -> None:
        self.k = k
        self._counts: dict[str, int] = {}
        self._total = 0

    def update(self, item: Any, count: int = 1) -> None:
        """Observe an item."""
        key = str(item)
        self._total += count
        if key in self._counts:
            self._counts[key] += count
        elif len(self._counts) < self.k:
            self._counts[key] = count
        else:
            # Replace the item with minimum count
            min_key = min(self._counts, key=self._counts.get)
            min_count = self._counts[min_key]
            del self._counts[min_key]
            self._counts[key] = min_count + count

    def top_k(self) -> list[tuple[str, int]]:
        """Return the top-K items sorted by count (descending)."""
        return sorted(self._counts.items(), key=lambda x: -x[1])

    def estimate(self, item: Any) -> int:
        """Estimate the count of an item (lower bound)."""
        return self._counts.get(str(item), 0)

    def stats(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "tracked_items": len(self._counts),
            "total_observations": self._total,
            "top": self.top_k()[:5],
        }