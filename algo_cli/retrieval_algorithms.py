"""Deterministic bounded ranking primitives used by harness retrieval."""

from __future__ import annotations

import heapq
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

TOKEN_RE = re.compile(r"[\w.-]+", re.UNICODE)
FULL_SORT_THRESHOLD = 8_192
MOJIBAKE_REPLACEMENTS = (
    ("Â·", "·"),
    ("â€¦", "…"),
    ("â€™", "’"),
    ("â€œ", "“"),
    ("â€", "”"),
    ("â€“", "–"),
    ("â€”", "—"),
)


def lexical_tokens(text: str) -> list[str]:
    """Return normalized lexical tokens shared by BM25 and query parsing."""
    return [token.lower() for token in TOKEN_RE.findall(text or "") if len(token) > 1]


def repair_mojibake(text: str) -> str:
    """Repair common UTF-8-as-Windows-1252 artifacts at display boundaries."""
    repaired = str(text or "")
    for broken, replacement in MOJIBAKE_REPLACEMENTS:
        repaired = repaired.replace(broken, replacement)
    return repaired


def bm25_scores(
    documents: Sequence[str],
    query_terms: Sequence[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Score documents with Okapi BM25 using a query-local in-memory corpus."""
    return BM25Index(documents, k1=k1, b=b).scores(query_terms)


class BM25Index:
    """Reusable exact BM25 corpus statistics for repeated queries."""

    def __init__(
        self,
        documents: Sequence[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.k1 = float(k1)
        self.b = float(b)
        self._counters = [Counter(lexical_tokens(document)) for document in documents]
        self._lengths = [sum(counter.values()) for counter in self._counters]
        self._average_length = sum(self._lengths) / max(1, len(self._lengths))
        self._document_frequency: Counter[str] = Counter()
        for counter in self._counters:
            self._document_frequency.update(counter.keys())

    def scores(self, query_terms: Sequence[str]) -> list[float]:
        terms = tuple(dict.fromkeys(term.lower() for term in query_terms if term))
        if not terms:
            return [0.0] * len(self._counters)

        document_count = len(self._counters)
        if document_count == 0:
            return []
        scores: list[float] = []
        for counter, length in zip(self._counters, self._lengths):
            score = 0.0
            length_normalizer = (
                1.0 - self.b + self.b * length / max(1.0, self._average_length)
            )
            for term in terms:
                frequency = counter.get(term, 0)
                if frequency <= 0:
                    continue
                df = self._document_frequency.get(term, 0)
                inverse_document_frequency = math.log(
                    1.0 + (document_count - df + 0.5) / (df + 0.5)
                )
                score += inverse_document_frequency * (
                    frequency * (self.k1 + 1.0)
                    / (frequency + self.k1 * length_normalizer)
                )
            scores.append(score)
        return scores


def stable_top_k(items: Sequence[T], k: int, score: Callable[[T], float]) -> list[T]:
    """Return stable top-k with the faster strategy for the candidate scale.

    CPython's C-backed Timsort wins on the harness's bounded (<=4k) pools;
    heap selection wins once pools are materially larger. Keep the crossover
    explicit so a theoretically better complexity does not slow the live path.
    """
    limit = max(0, int(k))
    if limit == 0 or not items:
        return []
    if len(items) <= FULL_SORT_THRESHOLD:
        return sorted(items, key=score, reverse=True)[:limit]
    ranked = heapq.nlargest(
        min(limit, len(items)),
        enumerate(items),
        key=lambda pair: (score(pair[1]), -pair[0]),
    )
    return [item for _index, item in ranked]


__all__ = [
    "FULL_SORT_THRESHOLD",
    "BM25Index",
    "bm25_scores",
    "lexical_tokens",
    "repair_mojibake",
    "stable_top_k",
]
