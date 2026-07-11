"""Log2 Histogram — memory-efficient latency telemetry.

Borrowed from Windows Performance Counters registry
(HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Perflib\\009):
Windows uses 16 log2-sized buckets covering 128µs to >30s — 6 orders of
magnitude in 72 bytes.  Binary-search insertion is O(log 16) = O(4).
Histograms are mergeable across sessions for aggregate statistics.

Pattern: B29 in ALGO.md.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Bucket upper bounds in microseconds.
# Source: Windows Performance Counter registry bucket definitions.
BOUNDARIES_US: tuple[float, ...] = (
    128,        # bucket 01: <= 128 µs
    256,        # bucket 02: <= 256 µs
    512,        # bucket 03: <= 512 µs
    1_024,      # bucket 04: <= 1 ms
    4_096,      # bucket 05: <= 4 ms
    16_384,     # bucket 06: <= 16 ms
    65_536,     # bucket 07: <= 64 ms
    131_072,    # bucket 08: <= 128 ms
    262_144,    # bucket 09: <= 256 ms
    524_288,    # bucket 10: <= 512 ms
    1_048_576,  # bucket 11: <= 1 s
    2_097_152,  # bucket 12: <= 2 s
    10_485_760, # bucket 13: <= 10 s
    20_971_520, # bucket 14: <= 20 s
    31_457_280, # bucket 15: <= 30 s
    float("inf"),  # bucket 16: > 30 s
)

# Human-readable labels for each bucket.
BUCKET_LABELS: tuple[str, ...] = (
    "<=128µs", "<=256µs", "<=512µs", "<=1ms", "<=4ms", "<=16ms",
    "<=64ms", "<=128ms", "<=256ms", "<=512ms", "<=1s", "<=2s",
    "<=10s", "<=20s", "<=30s", ">30s",
)


@dataclass
class Log2Histogram:
    """Log2-bucketed histogram — O(log b) insert, O(b) quantile.

    16 buckets cover 128µs to >30s in ~72 bytes.
    Mergeable: two histograms combine by bucket-wise addition.
    """

    buckets: list[int] = field(default_factory=lambda: [0] * len(BOUNDARIES_US))
    count: int = 0
    sum_us: float = 0.0
    min_us: float = float("inf")
    max_us: float = 0.0

    # --- core operations --------------------------------------------------

    def observe(self, value_us: float) -> None:
        """Record a latency observation in microseconds."""
        idx = self._bucket_index(value_us)
        self.buckets[idx] += 1
        self.count += 1
        self.sum_us += value_us
        if value_us < self.min_us:
            self.min_us = value_us
        if value_us > self.max_us:
            self.max_us = value_us

    def observe_seconds(self, value_s: float) -> None:
        """Record a latency observation in seconds."""
        self.observe(value_s * 1_000_000)

    def _bucket_index(self, value: float) -> int:
        """Binary search for bucket — O(log 16) = O(4)."""
        lo, hi = 0, len(BOUNDARIES_US) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if value <= BOUNDARIES_US[mid]:
                hi = mid
            else:
                lo = mid + 1
        return lo

    # --- queries ----------------------------------------------------------

    def percentile(self, p: float) -> float:
        """Estimate p-th percentile in microseconds — O(buckets).

        Uses linear interpolation within the bucket.
        """
        if self.count == 0:
            return 0.0
        target = self.count * p / 100.0
        cumulative = 0
        for i, bucket_count in enumerate(self.buckets):
            cumulative += bucket_count
            if cumulative >= target:
                lower = 0.0 if i == 0 else BOUNDARIES_US[i - 1]
                upper = BOUNDARIES_US[i]
                if bucket_count == 0:
                    return lower
                frac = (target - (cumulative - bucket_count)) / bucket_count
                return lower + frac * (upper - lower)
        return BOUNDARIES_US[-2]  # second-to-last (last is inf)

    def percentile_seconds(self, p: float) -> float:
        """Estimate p-th percentile in seconds."""
        return self.percentile(p) / 1_000_000

    def mean_us(self) -> float:
        """Arithmetic mean in microseconds."""
        return self.sum_us / self.count if self.count else 0.0

    def mean_seconds(self) -> float:
        return self.mean_us() / 1_000_000

    # --- merge ------------------------------------------------------------

    def merge(self, other: Log2Histogram) -> Log2Histogram:
        """Merge two histograms — O(buckets)."""
        result = Log2Histogram()
        result.buckets = [a + b for a, b in zip(self.buckets, other.buckets)]
        result.count = self.count + other.count
        result.sum_us = self.sum_us + other.sum_us
        result.min_us = min(self.min_us, other.min_us)
        result.max_us = max(self.max_us, other.max_us)
        return result

    # --- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "buckets": list(self.buckets),
            "count": self.count,
            "sum_us": self.sum_us,
            "min_us": self.min_us if self.min_us != float("inf") else 0.0,
            "max_us": self.max_us,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Log2Histogram:
        h = cls()
        h.buckets = list(data.get("buckets", [0] * len(BOUNDARIES_US)))
        h.count = data.get("count", 0)
        h.sum_us = data.get("sum_us", 0.0)
        h.min_us = data.get("min_us", float("inf"))
        h.max_us = data.get("max_us", 0.0)
        return h

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> Log2Histogram:
        return cls.from_dict(json.loads(s))

    # --- display ----------------------------------------------------------

    def summary(self) -> dict[str, float]:
        """Compact summary for dashboards."""
        return {
            "count": self.count,
            "mean_us": round(self.mean_us(), 1),
            "p50_us": round(self.percentile(50), 1),
            "p90_us": round(self.percentile(90), 1),
            "p99_us": round(self.percentile(99), 1),
            "min_us": round(self.min_us, 1) if self.min_us != float("inf") else 0,
            "max_us": round(self.max_us, 1),
        }

    def histogram_text(self) -> str:
        """ASCII histogram for terminal display."""
        if self.count == 0:
            return "(empty)"
        max_count = max(self.buckets) or 1
        lines = []
        for i, count in enumerate(self.buckets):
            if count == 0:
                continue
            bar_len = int(count / max_count * 40)
            bar = "█" * bar_len
            lines.append(f"  {BUCKET_LABELS[i]:>8s} │{bar:<40s} │ {count}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry — per-tool latency histograms
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Log2Histogram] = {}


def get_histogram(name: str) -> Log2Histogram:
    """Get or create a named histogram (e.g., 'read_file', 'embed', 'model')."""
    if name not in _REGISTRY:
        _REGISTRY[name] = Log2Histogram()
    return _REGISTRY[name]


def record_latency(name: str, duration_s: float) -> None:
    """Record a latency observation for a named operation."""
    get_histogram(name).observe_seconds(duration_s)


def all_summaries() -> dict[str, dict[str, float]]:
    """Get summaries for all registered histograms."""
    return {name: h.summary() for name, h in _REGISTRY.items()}


def save_to_file(path: Path) -> None:
    """Persist all histograms to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {name: h.to_dict() for name, h in _REGISTRY.items()}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_from_file(path: Path) -> None:
    """Load histograms from a JSON file (merges into registry)."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    for name, hist_data in data.items():
        existing = _REGISTRY.get(name)
        loaded = Log2Histogram.from_dict(hist_data)
        if existing:
            _REGISTRY[name] = existing.merge(loaded)
        else:
            _REGISTRY[name] = loaded


def clear_registry() -> None:
    """Clear all registered histograms (for testing)."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Context manager for easy timing
# ---------------------------------------------------------------------------

class LatencyTimer:
    """Context manager that records latency into a named histogram.

    Usage:
        with LatencyTimer("read_file"):
            content = read_file(path)
    """

    def __init__(self, name: str):
        self.name = name
        self._start = 0.0

    def __enter__(self) -> LatencyTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        duration = time.perf_counter() - self._start
        record_latency(self.name, duration)