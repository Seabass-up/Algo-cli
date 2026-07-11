"""Session distribution analysis (I11).

Fable-5 traces are heavy-tailed: the top five sessions carried roughly a third
of the corpus. This module makes that bias visible for harness stats and
session-summary reporting.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionDistribution:
    session_count: int
    total_rows: int
    largest_session: int
    median_session: float
    top_sessions: tuple[tuple[str, int], ...]
    heavy_tail: bool
    effective_top_n: int
    summary: str

    def top_n_share(self, n: int) -> float:
        if self.total_rows <= 0 or n <= 0:
            return 0.0
        return sum(count for _name, count in self.top_sessions[:n]) / self.total_rows

    def to_dict(self) -> dict:
        return {
            "session_count": self.session_count,
            "total_rows": self.total_rows,
            "largest_session": self.largest_session,
            "median_session": self.median_session,
            "top_sessions": list(self.top_sessions),
            "heavy_tail": self.heavy_tail,
            "effective_top_n": self.effective_top_n,
            "top_share": round(self.top_n_share(self.effective_top_n), 3),
            "top5_share": round(self.top_n_share(5), 3),
            "summary": self.summary,
        }


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize_session_distribution(session_counts: dict[str, int], *, heavy_tail_top_n: int = 5, heavy_tail_share: float = 0.33) -> SessionDistribution:
    """Summarize per-session row counts and flag heavy-tail risk.

    For large corpora this uses the Fable-5 top-5 concentration rule. For very
    small corpora, ``top_n`` is adapted to roughly the largest half of sessions
    so a four-session corpus can still reveal obvious concentration.
    """
    cleaned = {str(name): max(0, int(count)) for name, count in (session_counts or {}).items()}
    sorted_counts = tuple(sorted(cleaned.items(), key=lambda item: item[1], reverse=True))
    counts = [count for _name, count in sorted_counts]
    total = sum(counts)
    effective_top_n = min(max(1, heavy_tail_top_n), max(1, len(sorted_counts) // 2)) if sorted_counts else 0
    top_share = 0.0 if total <= 0 or effective_top_n <= 0 else sum(count for _name, count in sorted_counts[:effective_top_n]) / total
    heavy = len(sorted_counts) > 1 and top_share >= heavy_tail_share
    summary = (
        f"sessions={len(sorted_counts)}, rows={total}, largest={counts[0] if counts else 0}, "
        f"median={_median(counts):.1f}, top{effective_top_n}_share={top_share:.2f}, "
        f"heavy_tail={heavy}"
    )
    return SessionDistribution(
        session_count=len(sorted_counts),
        total_rows=total,
        largest_session=counts[0] if counts else 0,
        median_session=_median(counts),
        top_sessions=sorted_counts,
        heavy_tail=heavy,
        effective_top_n=effective_top_n,
        summary=summary,
    )


__all__ = ["SessionDistribution", "summarize_session_distribution"]
