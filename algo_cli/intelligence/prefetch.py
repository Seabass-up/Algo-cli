"""Content Prefetch — predictive cache warming via transition matrix.

Borrowed from Windows.Networking.BackgroundTransfer.ContentPrefetchTask.dll:
Windows allows apps to register prefetch tasks that run before the user opens
the app — warming the cache with content the user is likely to request.

This module learns access patterns ("after reading X, user usually reads Y")
and pre-loads predicted files into an LRU cache before they're requested.

Pattern: B33 in ALGO.md.
"""
from __future__ import annotations

import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class AccessRecord:
    """A single file access record."""
    path: str
    timestamp: float
    action: str = "read"


class PredictivePrefetch:
    """Predict next likely accesses based on temporal transition patterns.

    Usage:
        cache = PredictivePrefetch(max_cache=32)
        content = cache.get("foo.py", loader=read_file)
        # cache learns: after foo.py, user often reads test_foo.py
        # next time foo.py is read, test_foo.py is pre-warmed
    """

    def __init__(self, max_cache: int = 32, max_history: int = 1000):
        self.max_cache = max_cache
        self.max_history = max_history
        self.access_history: list[AccessRecord] = []
        self.prefetch_cache: OrderedDict[str, Any] = OrderedDict()
        self.transition_counts: Counter = Counter()
        # Per-path access frequency (for cold-start predictions)
        self.access_frequency: Counter = Counter()

    # --- learning ---------------------------------------------------------

    def record_access(self, path: str, action: str = "read") -> None:
        """Record a file access for pattern learning."""
        now = time.time()
        # Learn transition: previous access → this access
        if self.access_history:
            prev = self.access_history[-1]
            key = f"{prev.path}|{prev.action}->{path}|{action}"
            self.transition_counts[key] += 1
        self.access_history.append(AccessRecord(path, now, action))
        self.access_frequency[path] += 1
        # Bound history
        if len(self.access_history) > self.max_history:
            self.access_history = self.access_history[-self.max_history // 2 :]

    # --- prediction -------------------------------------------------------

    def predict_next(self, current_path: str, action: str = "read") -> list[str]:
        """Predict likely next accesses given current access.

        Returns paths sorted by transition probability (descending).
        """
        candidates: Counter = Counter()
        prefix = f"{current_path}|{action}->"
        for key, count in self.transition_counts.items():
            if key.startswith(prefix):
                suffix = key[len(prefix):]
                next_path = suffix.split("|")[0]
                candidates[next_path] = count

        # If no transitions learned, fall back to most-accessed paths
        if not candidates:
            for path, freq in self.access_frequency.most_common(self.max_cache):
                if path != current_path:
                    candidates[path] = freq

        return [p for p, _ in candidates.most_common(self.max_cache)]

    # --- cache management -------------------------------------------------

    def warm_cache(self, paths: list[str], loader: Callable[[str], Any]) -> int:
        """Pre-load predicted paths into cache. Returns number warmed."""
        warmed = 0
        for path in paths:
            if path not in self.prefetch_cache:
                try:
                    self.prefetch_cache[path] = loader(path)
                    warmed += 1
                except Exception:
                    pass  # prefetch failures are silent (best-effort)
        self._evict()
        return warmed

    def prefetch_for(self, current_path: str, loader: Callable[[str], Any]) -> int:
        """Predict and warm cache for likely-next accesses."""
        predicted = self.predict_next(current_path)
        return self.warm_cache(predicted, loader)

    def get(self, path: str, loader: Callable[[str], Any]) -> Any:
        """Get from cache or load on miss. Records access for learning."""
        if path in self.prefetch_cache:
            self.prefetch_cache.move_to_end(path)
            self.record_access(path, "read")
            return self.prefetch_cache[path]
        result = loader(path)
        self.prefetch_cache[path] = result
        self.record_access(path, "read")
        self._evict()
        # After loading, prefetch likely-next files in background
        # (caller can call prefetch_for explicitly for async warming)
        return result

    def _evict(self) -> None:
        """Evict oldest entries if over capacity (LRU)."""
        while len(self.prefetch_cache) > self.max_cache:
            self.prefetch_cache.popitem(last=False)

    # --- queries ----------------------------------------------------------

    def cache_size(self) -> int:
        return len(self.prefetch_cache)

    def cache_hit_rate(self) -> float:
        """Calculate cache hit rate from access history."""
        if not self.access_history:
            return 0.0
        hits = sum(
            1 for rec in self.access_history
            if rec.path in self.prefetch_cache
        )
        return hits / len(self.access_history)

    def cached_paths(self) -> list[str]:
        return list(self.prefetch_cache.keys())

    def stats(self) -> dict[str, Any]:
        return {
            "cache_size": self.cache_size(),
            "cache_capacity": self.max_cache,
            "history_size": len(self.access_history),
            "transitions_learned": len(self.transition_counts),
            "unique_paths_accessed": len(self.access_frequency),
            "cache_hit_rate": round(self.cache_hit_rate(), 3),
        }

    # --- persistence ------------------------------------------------------

    def export_transitions(self) -> dict[str, int]:
        """Export transition counts for persistence/analysis."""
        return dict(self.transition_counts)

    def import_transitions(self, data: dict[str, int]) -> None:
        """Import previously learned transition counts."""
        for key, count in data.items():
            self.transition_counts[key] += count

    def export_frequency(self) -> dict[str, int]:
        """Export access frequency for persistence."""
        return dict(self.access_frequency)

    def import_frequency(self, data: dict[str, int]) -> None:
        """Import previously learned access frequency."""
        for path, count in data.items():
            self.access_frequency[path] += count