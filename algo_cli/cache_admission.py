"""Bounded Window TinyLFU cache admission for hot runtime data.

The cache keeps a small recency window in front of a larger main LRU segment.
When the window overflows, a bounded frequency sketch decides whether the
candidate should displace the main segment's least-recently-used entry. This
prevents one-off scans from evicting repeatedly used query embeddings.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Generic, Iterator, TypeVar

K = TypeVar("K")
V = TypeVar("V")

_MISSING = object()


class FrequencySketch(Generic[K]):
    """Small count-min sketch with periodic aging and stable hashing."""

    def __init__(self, capacity: int, *, depth: int = 4, sample_multiplier: int = 10) -> None:
        target_width = max(16, max(1, int(capacity)) * 4)
        self.width = 1 << math.ceil(math.log2(target_width))
        self.depth = min(4, max(2, int(depth)))
        self.sample_size = max(10, max(1, int(capacity)) * max(2, int(sample_multiplier)))
        self._rows = [[0] * self.width for _ in range(self.depth)]
        self._samples = 0
        self.decays = 0

    @staticmethod
    def _digest(key: object) -> bytes:
        return hashlib.blake2b(repr(key).encode("utf-8", errors="replace"), digest_size=16).digest()

    def _indexes(self, key: K) -> Iterator[tuple[int, int]]:
        digest = self._digest(key)
        for row in range(self.depth):
            offset = row * 4
            value = int.from_bytes(digest[offset:offset + 4], "little", signed=False)
            yield row, value & (self.width - 1)

    def increment(self, key: K) -> None:
        for row, index in self._indexes(key):
            if self._rows[row][index] < 2_147_483_647:
                self._rows[row][index] += 1
        self._samples += 1
        if self._samples >= self.sample_size:
            self._decay()

    def estimate(self, key: K) -> int:
        return min(self._rows[row][index] for row, index in self._indexes(key))

    def _decay(self) -> None:
        for row in self._rows:
            for index, count in enumerate(row):
                row[index] = count // 2
        self._samples //= 2
        self.decays += 1

    def clear(self) -> None:
        self._rows = [[0] * self.width for _ in range(self.depth)]
        self._samples = 0
        self.decays = 0


@dataclass
class CacheAdmissionStats:
    hits: int = 0
    misses: int = 0
    admissions: int = 0
    rejections: int = 0
    evictions: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class WindowTinyLFUCache(Generic[K, V]):
    """Thread-safe, entry-bounded Window TinyLFU cache.

    This intentionally exposes only the small mapping surface used by Algo CLI:
    ``get``, ``put``/item assignment, ``clear``, membership, and length.
    """

    def __init__(self, capacity: int, *, window_fraction: float = 0.20) -> None:
        self.window_fraction = min(0.50, max(0.01, float(window_fraction)))
        self._lock = threading.RLock()
        self.stats = CacheAdmissionStats()
        self._set_capacity(capacity)
        self._window: OrderedDict[K, V] = OrderedDict()
        self._main: OrderedDict[K, V] = OrderedDict()
        self._sketch: FrequencySketch[K] = FrequencySketch(self.capacity)

    def _set_capacity(self, capacity: int) -> None:
        self.capacity = max(1, int(capacity))
        self.window_capacity = max(1, min(self.capacity, round(self.capacity * self.window_fraction)))
        self.main_capacity = self.capacity - self.window_capacity

    def resize(self, capacity: int) -> None:
        target = max(1, int(capacity))
        with self._lock:
            if target == self.capacity:
                return
            retained = list(self._main.items()) + list(self._window.items())
            self._set_capacity(target)
            self._window.clear()
            self._main.clear()
            self._sketch = FrequencySketch(self.capacity)
            for key, value in retained[-self.capacity:]:
                self.put(key, value)
            self.stats = CacheAdmissionStats()

    def get(self, key: K, default: V | None = None) -> V | None:
        with self._lock:
            value: object = _MISSING
            if key in self._window:
                value = self._window[key]
                self._window.move_to_end(key)
            elif key in self._main:
                value = self._main[key]
                self._main.move_to_end(key)
            self._sketch.increment(key)
            if value is _MISSING:
                self.stats.misses += 1
                return default
            self.stats.hits += 1
            return value  # type: ignore[return-value]

    def put(self, key: K, value: V) -> bool:
        """Store a value after lookup and report overflow-candidate admission.

        Lookup records the request in the frequency sketch. ``put`` deliberately
        does not record it again, or every miss/fill would count twice and make
        one-off scans look hotter than they are.
        """
        with self._lock:
            if key in self._window:
                self._window[key] = value
                self._window.move_to_end(key)
                return True
            if key in self._main:
                self._main[key] = value
                self._main.move_to_end(key)
                return True

            self._window[key] = value
            self.stats.admissions += 1
            if len(self._window) <= self.window_capacity:
                return True

            candidate_key, candidate_value = self._window.popitem(last=False)
            if self.main_capacity <= 0:
                self.stats.evictions += 1
                return candidate_key != key
            if len(self._main) < self.main_capacity:
                self._main[candidate_key] = candidate_value
                return True

            victim_key = next(iter(self._main))
            candidate_frequency = self._sketch.estimate(candidate_key)
            victim_frequency = self._sketch.estimate(victim_key)
            if candidate_frequency > victim_frequency:
                self._main.popitem(last=False)
                self._main[candidate_key] = candidate_value
                self.stats.evictions += 1
                return True

            self.stats.rejections += 1
            self.stats.evictions += 1
            return False

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            requests = self.stats.hits + self.stats.misses
            return {
                **self.stats.to_dict(),
                "size": len(self),
                "capacity": self.capacity,
                "window_size": len(self._window),
                "main_size": len(self._main),
                "sketch_decays": self._sketch.decays,
                "hit_ratio": round(self.stats.hits / requests, 4) if requests else 0.0,
            }

    def clear(self) -> None:
        with self._lock:
            self._window.clear()
            self._main.clear()
            self._sketch.clear()
            self.stats = CacheAdmissionStats()

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._window or key in self._main

    def __len__(self) -> int:
        with self._lock:
            return len(self._window) + len(self._main)

    def __setitem__(self, key: K, value: V) -> None:
        self.put(key, value)


__all__ = ["CacheAdmissionStats", "FrequencySketch", "WindowTinyLFUCache"]
