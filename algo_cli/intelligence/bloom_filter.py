"""Bloom Filter — probabilistic set membership with false positives.

A Bloom filter answers "is this item in the set?" in O(k) time using
k hash functions and a bit array.  False positives are possible but
false negatives are impossible.  Memory is ~10x smaller than a hash set.

Harness use: dedup for embedding pipeline — "has this file already been
embedded?" without storing every path.  Also useful for URL dedup
("have we already fetched this URL?") and content dedup.

Operations:
  - add(item):     O(k) — set k bits
  - contains(item): O(k) — check k bits
  - false_positive_rate(): current estimated FPR
  - merge(other):  union two filters (same size/hashes)

Properties:
  - No false negatives (if contains() returns False, item was never added)
  - False positive rate tunable via bit_array_size and num_hashes
  - Cannot remove items (use CountingBloomFilter for that)
"""
from __future__ import annotations

import hashlib
import math
from typing import Any


class BloomFilter:
    """Standard Bloom filter with double-hashing (Kirsch-Mitzenmacher).

    Uses two base hashes h1=MD5, h2=SHA1 and derives k hashes via
    h_i(x) = h1(x) + i * h2(x), avoiding k separate hash computations.
    """

    def __init__(
        self,
        capacity: int = 10_000,
        false_positive_rate: float = 0.01,
    ) -> None:
        """Create a Bloom filter sized for *capacity* items at target FPR.

        Args:
            capacity: Expected number of items to add.
            false_positive_rate: Target false positive rate (e.g. 0.01 = 1%).
        """
        self.capacity = capacity
        self.target_fpr = false_positive_rate
        self.bit_array_size = self._optimal_m(capacity, false_positive_rate)
        self.num_hashes = self._optimal_k(self.bit_array_size, capacity)
        self._bits = bytearray((self.bit_array_size + 7) // 8)
        self.count = 0

    @staticmethod
    def _optimal_m(n: int, p: float) -> int:
        """Optimal bit array size: m = -n*ln(p) / (ln(2)^2)."""
        return max(1, int(-n * math.log(p) / (math.log(2) ** 2)))

    @staticmethod
    def _optimal_k(m: int, n: int) -> int:
        """Optimal number of hash functions: k = (m/n) * ln(2)."""
        return max(1, int((m / max(1, n)) * math.log(2)))

    def _hashes(self, item: Any) -> list[int]:
        """Generate k hash positions using double-hashing."""
        data = str(item).encode("utf-8")
        h1 = int.from_bytes(hashlib.md5(data).digest()[:8], "little")
        h2 = int.from_bytes(hashlib.sha1(data).digest()[:8], "little")
        m = self.bit_array_size
        return [(h1 + i * h2) % m for i in range(self.num_hashes)]

    def _set_bit(self, pos: int) -> None:
        self._bits[pos >> 3] |= (1 << (pos & 7))

    def _get_bit(self, pos: int) -> bool:
        return bool(self._bits[pos >> 3] & (1 << (pos & 7)))

    def add(self, item: Any) -> None:
        """Add an item to the filter."""
        for pos in self._hashes(item):
            self._set_bit(pos)
        self.count += 1

    def add_many(self, items: list[Any]) -> None:
        """Add multiple items."""
        for item in items:
            self.add(item)

    def contains(self, item: Any) -> bool:
        """Check if item might be in the set.

        Returns True if the item is possibly in the set (may be false positive).
        Returns False if the item is definitely not in the set.
        """
        return all(self._get_bit(pos) for pos in self._hashes(item))

    def __contains__(self, item: Any) -> bool:
        return self.contains(item)

    def false_positive_rate(self) -> float:
        """Current estimated false positive rate based on items added."""
        if self.count == 0:
            return 0.0
        k = self.num_hashes
        m = self.bit_array_size
        n = self.count
        return (1 - math.exp(-k * n / m)) ** k

    def merge(self, other: BloomFilter) -> None:
        """Merge another filter into this one (in-place union).

        Both filters must have the same bit_array_size and num_hashes.
        """
        if self.bit_array_size != other.bit_array_size:
            raise ValueError("Cannot merge filters with different sizes")
        if self.num_hashes != other.num_hashes:
            raise ValueError("Cannot merge filters with different hash counts")
        for i in range(len(self._bits)):
            self._bits[i] |= other._bits[i]
        self.count += other.count

    def bit_density(self) -> float:
        """Fraction of bits set — useful for monitoring saturation."""
        set_bits = sum(bin(b).count("1") for b in self._bits)
        return set_bits / self.bit_array_size

    def stats(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "bit_array_size": self.bit_array_size,
            "num_hashes": self.num_hashes,
            "items_added": self.count,
            "false_positive_rate": round(self.false_positive_rate(), 6),
            "bit_density": round(self.bit_density(), 4),
            "memory_bytes": len(self._bits),
        }


class CountingBloomFilter:
    """Counting Bloom filter — supports removal via counter per slot.

    Uses 4-bit counters (max count 15) instead of single bits.
    Slightly more memory than standard BloomFilter but supports delete().
    """

    def __init__(
        self,
        capacity: int = 10_000,
        false_positive_rate: float = 0.01,
    ) -> None:
        self.capacity = capacity
        m = BloomFilter._optimal_m(capacity, false_positive_rate)
        self.bit_array_size = m
        self.num_hashes = BloomFilter._optimal_k(m, capacity)
        self._counters = bytearray(m)  # each counter 0-255 (we cap at 255)
        self.count = 0

    def _hashes(self, item: Any) -> list[int]:
        data = str(item).encode("utf-8")
        h1 = int.from_bytes(hashlib.md5(data).digest()[:8], "little")
        h2 = int.from_bytes(hashlib.sha1(data).digest()[:8], "little")
        m = self.bit_array_size
        return [(h1 + i * h2) % m for i in range(self.num_hashes)]

    def add(self, item: Any) -> None:
        for pos in self._hashes(item):
            if self._counters[pos] < 255:
                self._counters[pos] += 1
        self.count += 1

    def remove(self, item: Any) -> None:
        """Remove an item. Only call if you know the item was previously added."""
        for pos in self._hashes(item):
            if self._counters[pos] > 0:
                self._counters[pos] -= 1
        self.count = max(0, self.count - 1)

    def contains(self, item: Any) -> bool:
        return all(self._counters[pos] > 0 for pos in self._hashes(item))

    def __contains__(self, item: Any) -> bool:
        return self.contains(item)

    def estimate_count(self, item: Any) -> int:
        """Estimate the number of times item was added (min of counters)."""
        return min(self._counters[pos] for pos in self._hashes(item))