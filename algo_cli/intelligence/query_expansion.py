"""B49. Query Expansion + Cross-Encoder Reranking.

Generate query variants for better recall, then rerank with a cross-encoder
for precision.  Source: PyRagix pattern.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass
class QueryVariant:
    text: str
    source: str = "synonym"  # synonym, reformulation, keyword, original


@dataclass
class SearchResult:
    title: str
    url: str
    content: str
    score: float = 0.0


@dataclass
class RerankedResult:
    title: str
    url: str
    content: str
    original_score: float
    rerank_score: float
    rank: int = 0


class QueryExpander:
    """Expand a single query into multiple variants for better recall."""

    SYNONYM_MAP: dict[str, list[str]] = {
        "error": ["error", "exception", "failure", "crash", "bug"],
        "fast": ["fast", "quick", "efficient", "performant", "speed"],
        "test": ["test", "unit test", "integration test", "spec"],
        "config": ["config", "configuration", "settings", "options"],
        "search": ["search", "find", "locate", "query", "lookup"],
        "build": ["build", "compile", "make", "construct"],
        "agent": ["agent", "assistant", "bot", "worker"],
        "pattern": ["pattern", "approach", "strategy", "method", "technique"],
    }

    def expand(self, query: str, max_variants: int = 5) -> list[QueryVariant]:
        variants: list[QueryVariant] = [QueryVariant(query, "original")]
        words = re.findall(r"\w+", query.lower())

        # Synonym substitution
        for word in words:
            if word in self.SYNONYM_MAP:
                for syn in self.SYNONYM_MAP[word]:
                    if syn != word:
                        variant = re.sub(rf"\b{re.escape(word)}\b", syn, query, flags=re.IGNORECASE)
                        if variant.lower() != query.lower():
                            variants.append(QueryVariant(variant, "synonym"))

        # Keyword extraction
        keywords = " ".join(w for w in words if len(w) > 3)
        if keywords and keywords.lower() != query.lower():
            variants.append(QueryVariant(keywords, "keyword"))

        # Reformulation
        if len(words) > 2:
            reversed_q = " ".join(reversed(words))
            variants.append(QueryVariant(reversed_q, "reformulation"))

        # Deduplicate
        seen: set[str] = set()
        unique: list[QueryVariant] = []
        for v in variants:
            key = v.text.lower().strip()
            if key not in seen:
                seen.add(key)
                unique.append(v)

        return unique[:max_variants]


class CrossEncoderReranker:
    """Rerank search results using a scoring function.

    In production, this would use a cross-encoder model.  Here we use a
    lightweight BM25-style overlap scorer as a fallback.
    """

    def __init__(self, score_fn: Callable[[str, str], float] | None = None):
        self._score_fn = score_fn or self._overlap_score

    @staticmethod
    def _overlap_score(query: str, document: str) -> float:
        q_terms = set(query.lower().split())
        d_terms = document.lower().split()
        if not q_terms or not d_terms:
            return 0.0
        hits = sum(1 for t in d_terms if t in q_terms)
        return hits / (len(d_terms) ** 0.5 + 1e-9)

    def rerank(
        self,
        query: str,
        results: Sequence[SearchResult],
        top_k: int = 10,
    ) -> list[RerankedResult]:
        scored: list[RerankedResult] = []
        for r in results:
            rerank_score = self._score_fn(query, f"{r.title} {r.content}")
            scored.append(
                RerankedResult(
                    title=r.title,
                    url=r.url,
                    content=r.content,
                    original_score=r.score,
                    rerank_score=rerank_score,
                )
            )
        scored.sort(key=lambda x: x.rerank_score, reverse=True)
        for i, ranked_result in enumerate(scored):
            ranked_result.rank = i + 1
        return scored[:top_k]


def expand_and_rerank(
    query: str,
    search_fn: Callable[[str], list[SearchResult]],
    max_variants: int = 5,
    top_k: int = 10,
) -> list[RerankedResult]:
    """Expand query, search with all variants, deduplicate, rerank."""
    expander = QueryExpander()
    reranker = CrossEncoderReranker()

    variants = expander.expand(query, max_variants)
    all_results: dict[str, SearchResult] = {}
    for v in variants:
        for r in search_fn(v.text):
            if r.url not in all_results or r.score > all_results[r.url].score:
                all_results[r.url] = r

    return reranker.rerank(query, list(all_results.values()), top_k)
