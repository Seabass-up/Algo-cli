"""B37. GraphRAG Pipeline + Community Summaries (Microsoft GraphRAG Pattern).

Extracts entities/relationships from text, builds a knowledge graph,
detects communities, and supports local/global query modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ── data types ────────────────────────────────────────────────────────


@dataclass
class Entity:
    id: str
    name: str
    type: str = "generic"
    description: str = ""
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class Relationship:
    source: str
    target: str
    kind: str = "related"
    weight: float = 1.0
    description: str = ""


@dataclass
class Community:
    id: int
    entity_ids: set[str] = field(default_factory=set)
    summary: str = ""


@dataclass
class GraphRAGIndex:
    entities: dict[str, Entity] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)
    communities: list[Community] = field(default_factory=list)
    _adjacency: dict[str, list[str]] = field(default_factory=dict)

    def add_entity(self, e: Entity) -> None:
        self.entities[e.id] = e

    def add_relationship(self, r: Relationship) -> None:
        self.relationships.append(r)
        self._adjacency.setdefault(r.source, []).append(r.target)
        self._adjacency.setdefault(r.target, []).append(r.source)

    def neighbors(self, entity_id: str, depth: int = 1) -> set[str]:
        """BFS neighborhood."""
        visited: set[str] = {entity_id}
        frontier = {entity_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for node in frontier:
                for nb in self._adjacency.get(node, []):
                    if nb not in visited:
                        visited.add(nb)
                        next_frontier.add(nb)
            frontier = next_frontier
        return visited


# ── entity extraction ─────────────────────────────────────────────────

# Simple regex-based entity extraction for common patterns
_ENTITY_PATTERNS = [
    (r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", "person"),  # First Last
    (r"\b(\d{3,5}\s+\w+\s+(?:St|Ave|Blvd|Dr|Rd|Ln|Way|Ct|Pl))\b", "address"),
    (r"\$([\d,]+(?:\.\d{2})?)", "money"),
    (r"\b(PROJECT-\d{3})\b", "project"),
    (r"\b(EST-\d{4}-\d{3})\b", "estimate"),
    (r"\b(INV-[A-Z]+-\w+-\d+)\b", "invoice"),
]


def extract_entities(text: str) -> list[Entity]:
    """Extract entities from text using regex patterns."""
    entities: list[Entity] = []
    seen: set[str] = set()
    for pattern, etype in _ENTITY_PATTERNS:
        for match in re.finditer(pattern, text):
            name = match.group(1) if match.groups() else match.group(0)
            eid = f"{etype}:{name.lower().replace(' ', '_')}"
            if eid not in seen:
                seen.add(eid)
                entities.append(Entity(id=eid, name=name, type=etype))
    return entities


def extract_relationships(entities: list[Entity], text: str) -> list[Relationship]:
    """Extract co-occurrence relationships between entities in the same text."""
    rels: list[Relationship] = []
    for i, e1 in enumerate(entities):
        for e2 in entities[i + 1:]:
            # co-occurrence in same text
            if e1.name in text and e2.name in text:
                rels.append(Relationship(
                    source=e1.id, target=e2.id,
                    kind="co_occurs", weight=1.0,
                ))
    return rels


# ── community detection (label propagation) ──────────────────────────


def detect_communities(index: GraphRAGIndex, max_iterations: int = 10) -> list[Community]:
    """Label propagation community detection."""
    labels: dict[str, int] = {eid: i for i, eid in enumerate(index.entities)}
    for _ in range(max_iterations):
        changed = False
        for eid in index.entities:
            neighbor_labels = [labels.get(nb, labels[eid]) for nb in index._adjacency.get(eid, [])]
            if not neighbor_labels:
                continue
            # pick most common label
            from collections import Counter
            counts = Counter(neighbor_labels)
            new_label = counts.most_common(1)[0][0]
            if new_label != labels[eid]:
                labels[eid] = new_label
                changed = True
        if not changed:
            break
    # group by label
    communities: dict[int, set[str]] = {}
    for eid, label in labels.items():
        communities.setdefault(label, set()).add(eid)
    result = []
    for i, (label, members) in enumerate(sorted(communities.items())):
        result.append(Community(id=i, entity_ids=members))
    return result


def build_community_summaries(index: GraphRAGIndex, communities: list[Community]) -> None:
    """Generate simple summaries for each community."""
    for comm in communities:
        names = [index.entities[eid].name for eid in comm.entity_ids if eid in index.entities]
        types = [index.entities[eid].type for eid in comm.entity_ids if eid in index.entities]
        comm.summary = f"Community {comm.id}: {len(names)} entities ({', '.join(sorted(set(types)))}) — {', '.join(sorted(names)[:5])}"


# ── query modes ───────────────────────────────────────────────────────


def local_query(index: GraphRAGIndex, entity_id: str, depth: int = 1) -> dict:
    """Local query: neighborhood around one entity."""
    if entity_id not in index.entities:
        return {"error": f"Entity {entity_id} not found"}
    neighborhood = index.neighbors(entity_id, depth)
    entities = [index.entities[eid] for eid in neighborhood if eid in index.entities]
    rels = [r for r in index.relationships if r.source in neighborhood and r.target in neighborhood]
    return {
        "center": index.entities[entity_id].name,
        "entities": [e.name for e in entities],
        "relationships": [(r.source, r.kind, r.target) for r in rels],
        "size": len(neighborhood),
    }


def global_query(index: GraphRAGIndex, communities: list[Community]) -> dict:
    """Global query: summarize across all communities."""
    return {
        "communities": len(communities),
        "total_entities": len(index.entities),
        "total_relationships": len(index.relationships),
        "summaries": [c.summary for c in communities if c.summary],
    }


# ── pipeline ──────────────────────────────────────────────────────────


def build_index(texts: Iterable[str]) -> GraphRAGIndex:
    """Build a GraphRAG index from a collection of texts."""
    index = GraphRAGIndex()
    for text in texts:
        entities = extract_entities(text)
        for e in entities:
            index.add_entity(e)
        rels = extract_relationships(entities, text)
        for r in rels:
            index.add_relationship(r)
    communities = detect_communities(index)
    build_community_summaries(index, communities)
    index.communities = communities
    return index
