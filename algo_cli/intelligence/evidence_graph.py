"""B63. Evidence Graph for Provenance.

Link every claim to source text snippets with traceable citations.
Source: Deep-Research-Agent pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Source:
    id: str
    title: str
    url: str = ""
    content: str = ""
    retrieved_at: str = ""
    trust_score: float = 0.5


@dataclass
class Claim:
    id: str
    text: str
    source_ids: list[str] = field(default_factory=list)
    evidence_spans: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    contradictions: list[str] = field(default_factory=list)


@dataclass
class EvidenceEdge:
    claim_id: str
    source_id: str
    span_start: int = 0
    span_end: int = 0
    relevance: float = 0.0


class EvidenceGraph:
    """Graph linking claims to source evidence for provenance."""

    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._claims: dict[str, Claim] = {}
        self._edges: list[EvidenceEdge] = []

    def add_source(self, source: Source) -> None:
        self._sources[source.id] = source

    def add_claim(self, claim: Claim) -> None:
        self._claims[claim.id] = claim
        for sid in claim.source_ids:
            if sid in self._sources:
                self._edges.append(EvidenceEdge(claim_id=claim.id, source_id=sid))

    def link_evidence(self, claim_id: str, source_id: str,
                      span_start: int = 0, span_end: int = 0,
                      relevance: float = 1.0) -> None:
        self._edges.append(EvidenceEdge(
            claim_id=claim_id, source_id=source_id,
            span_start=span_start, span_end=span_end, relevance=relevance,
        ))
        claim = self._claims.get(claim_id)
        if claim and source_id not in claim.source_ids:
            claim.source_ids.append(source_id)

    def get_evidence(self, claim_id: str) -> list[tuple[Source, EvidenceEdge]]:
        """Get all source evidence for a claim."""
        result: list[tuple[Source, EvidenceEdge]] = []
        for edge in self._edges:
            if edge.claim_id == claim_id:
                source = self._sources.get(edge.source_id)
                if source:
                    result.append((source, edge))
        return sorted(result, key=lambda x: x[1].relevance, reverse=True)

    def find_contradictions(self) -> list[tuple[str, str, str]]:
        """Find claims that contradict each other."""
        contradictions: list[tuple[str, str, str]] = []
        claim_list = list(self._claims.values())
        for i, a in enumerate(claim_list):
            for b in claim_list[i + 1:]:
                if a.source_ids and b.source_ids:
                    shared = set(a.source_ids) & set(b.source_ids)
                    if shared and a.text != b.text:
                        # Simple heuristic: if same source but different claims
                        contradictions.append((a.id, b.id, "same source, different claims"))
        return contradictions

    def render_citations(self, claim_id: str) -> str:
        """Render citations for a claim in [1], [2] format."""
        evidence = self.get_evidence(claim_id)
        if not evidence:
            return ""
        citations: list[str] = []
        for i, (source, edge) in enumerate(evidence, 1):
            span_text = ""
            if edge.span_end > edge.span_start:
                span_text = source.content[edge.span_start:edge.span_end]
            citations.append(f"[{i}] {source.title} — {span_text[:100]}")
        return "\n".join(citations)

    @property
    def source_count(self) -> int:
        return len(self._sources)

    @property
    def claim_count(self) -> int:
        return len(self._claims)

    @property
    def edge_count(self) -> int:
        return len(self._edges)