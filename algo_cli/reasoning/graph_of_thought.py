"""Graph-of-Thoughts (GoT) Reasoning.

Generalizes ToT to arbitrary directed acyclic graphs (DAGs) where:
- Thoughts are vertices with typed edges
- Supports merge (combine multiple thoughts), feedback (revise), and distill (compact)
- Closer to recurrent/human cognition patterns
- Better for tasks requiring iterative refinement or synthesis of partial results
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..chat_protocol import get_attr


class EdgeType(Enum):
    NEXT = "next"          # Sequential reasoning step
    MERGE = "merge"        # Combine multiple thoughts
    FEEDBACK = "feedback"  # Revise a thought based on downstream info
    DISTILL = "distill"    # Compact intermediate reasoning


@dataclass
class ThoughtVertex:
    """A vertex in the Graph-of-Thought."""
    id: str
    thought: str
    value: float
    kind: str = "reasoning"  # reasoning, hypothesis, evidence, synthesis
    state: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_context(self) -> str:
        return f"[{self.id}] ({self.kind}, val={self.value:.2f}): {self.thought[:300]}"


@dataclass
class ThoughtEdge:
    """A directed edge connecting two thought vertices."""
    source_id: str
    target_id: str
    edge_type: EdgeType
    label: str = ""
    weight: float = 1.0


@dataclass
class GraphOfThought:
    """Graph-of-Thought reasoner."""
    vertices: dict[str, ThoughtVertex] = field(default_factory=dict)
    edges: list[ThoughtEdge] = field(default_factory=list)
    next_id: int = 0
    best_vertex: ThoughtVertex | None = None
    best_score: float = 0.0

    def add_vertex(self, thought: str, value: float, kind: str = "reasoning", state: str = "") -> ThoughtVertex:
        vid = f"v{self.next_id}"
        self.next_id += 1
        vertex = ThoughtVertex(id=vid, thought=thought, value=value, kind=kind, state=state)
        self.vertices[vid] = vertex
        if value > self.best_score:
            self.best_score = value
            self.best_vertex = vertex
        return vertex

    def add_edge(self, source_id: str, target_id: str, edge_type: EdgeType, label: str = "") -> ThoughtEdge:
        edge = ThoughtEdge(source_id=source_id, target_id=target_id, edge_type=edge_type, label=label)
        self.edges.append(edge)
        return edge

    def predecessors(self, vertex_id: str) -> list[ThoughtVertex]:
        """Return vertices with edges pointing into this vertex."""
        pred_ids = {e.source_id for e in self.edges if e.target_id == vertex_id}
        return [self.vertices[pid] for pid in pred_ids if pid in self.vertices]

    def successors(self, vertex_id: str) -> list[ThoughtVertex]:
        """Return vertices this vertex points to."""
        succ_ids = {e.target_id for e in self.edges if e.source_id == vertex_id}
        return [self.vertices[sid] for sid in succ_ids if sid in self.vertices]

    def frontier(self) -> list[ThoughtVertex]:
        """Return leaf vertices (no successors) that could be expanded."""
        has_children = {e.source_id for e in self.edges}
        return [v for v in self.vertices.values() if v.id not in has_children]

    def context_snapshot(self, max_chars: int = 4000) -> str:
        """Serialize the graph state for LLM context."""
        lines = ["## Current Reasoning Graph"]
        total = 0
        for v in self.vertices.values():
            entry = v.to_context()
            if total + len(entry) > max_chars:
                lines.append(f"... ({len(self.vertices)} total vertices)")
                break
            lines.append(entry)
            total += len(entry)
        if self.edges:
            lines.append(f"\nEdges: {len(self.edges)} connections")
        return "\n".join(lines)

    def _merge_thoughts(self, vertex_ids: list[str], client: Any, model: str) -> ThoughtVertex | None:
        """Merge multiple thoughts into a synthesis vertex."""
        vertices = [self.vertices[vid] for vid in vertex_ids if vid in self.vertices]
        if not vertices:
            return None
        parts = [v.to_context() for v in vertices]
        prompt = (
            "Synthesize these reasoning steps into one unified thought:\n\n"
            + "\n\n".join(parts)
            + "\n\nProduce a single concise synthesis."
        )
        try:
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a reasoning synthesizer."},
                    {"role": "user", "content": prompt},
                ],
                stream=False,
            )
            text = get_attr(get_attr(response, "message", {}), "content", "").strip()
        except Exception:
            text = " + ".join(v.thought[:80] for v in vertices)

        merged = self.add_vertex(thought=text, value=max(v.value for v in vertices), kind="synthesis")
        for vid in vertex_ids:
            if vid in self.vertices:
                self.add_edge(vid, merged.id, EdgeType.MERGE, label="merge")
        return merged

    def _feedback_loop(self, vertex_id: str, client: Any, model: str) -> ThoughtVertex | None:
        """Apply feedback: revise a thought using information from its successors."""
        vertex = self.vertices.get(vertex_id)
        if not vertex:
            return None
        successors = self.successors(vertex_id)
        if not successors:
            return None
        succ_context = "\n".join(s.to_context() for s in successors)
        prompt = (
            f"Original thought [{vertex_id}]: {vertex.thought}\n\n"
            f"Downstream findings:\n{succ_context}\n\n"
            "Revise the original thought to incorporate these findings."
        )
        try:
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a reasoning reviser."},
                    {"role": "user", "content": prompt},
                ],
                stream=False,
            )
            text = get_attr(get_attr(response, "message", {}), "content", "").strip()
        except Exception:
            text = vertex.thought

        revised = self.add_vertex(thought=text, value=vertex.value + 0.1, kind="revised")
        self.add_edge(vertex_id, revised.id, EdgeType.FEEDBACK, label="revise")
        # Connect the original successors to the revised version
        for s in successors:
            self.add_edge(revised.id, s.id, EdgeType.NEXT, label="post-revision")
        return revised

    def _distill(self, vertex_ids: list[str], client: Any, model: str) -> ThoughtVertex | None:
        """Distill a set of vertices into a compact summary vertex."""
        vertices = [self.vertices[vid] for vid in vertex_ids if vid in self.vertices]
        if not vertices:
            return None
        prompt = (
            "Distill these reasoning steps into a compact summary (max 200 words):\n\n"
            + "\n\n".join(v.to_context() for v in vertices)
        )
        try:
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a reasoning compactor."},
                    {"role": "user", "content": prompt},
                ],
                stream=False,
            )
            text = get_attr(get_attr(response, "message", {}), "content", "").strip()
        except Exception:
            text = " | ".join(v.thought[:60] for v in vertices)

        distilled = self.add_vertex(thought=text, value=max(v.value for v in vertices), kind="distilled")
        for vid in vertex_ids:
            if vid in self.vertices:
                self.add_edge(vid, distilled.id, EdgeType.DISTILL, label="distill")
        return distilled


EXPANSION_PROMPT = """Given the reasoning graph state and the task, generate {n} next reasoning steps.

Task: {task}

{graph_context}

Produce {n} distinct next thoughts as JSON array:
[{{"thought": "...", "kind": "reasoning|hypthesis|evidence", "state": "updated state"}}]"""


def run_got(
    *,
    task: str,
    client: Any,
    model: str,
    max_rounds: int = 4,
    branch_factor: int = 2,
    enable_merge: bool = True,
    enable_feedback: bool = True,
    enable_distill: bool = True,
    distill_threshold: int = 6,
) -> GraphOfThought:
    """Run a Graph-of-Thought reasoning episode.

    Args:
        task: The reasoning task.
        client: Ollama client.
        model: Model name.
        max_rounds: Maximum expansion rounds.
        branch_factor: Thoughts generated per frontier node.
        enable_merge: Allow merge operations.
        enable_feedback: Allow feedback loops.
        enable_distill: Allow distillation when graph gets large.
        distill_threshold: Distill when vertex count exceeds this.

    Returns:
        The GraphOfThought with best_vertex set.
    """
    got = GraphOfThought()

    # Seed with initial thought
    got.add_vertex(thought=f"Task: {task}", value=1.0, kind="hypothesis", state=task)

    for round_num in range(max_rounds):
        frontier = got.frontier()
        if not frontier:
            break

        # Expand frontier
        for vertex in frontier[:3]:  # Limit expansion breadth
            prompt = EXPANSION_PROMPT.format(
                n=branch_factor,
                task=task,
                graph_context=got.context_snapshot(),
            )
            try:
                response = client.chat(
                    model=model,
                    messages=[
                        {"role": "system", "content": "You are a reasoning graph expander."},
                        {"role": "user", "content": prompt},
                    ],
                    stream=False,
                    format="json",
                )
                text = get_attr(get_attr(response, "message", {}), "content", "")
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    for item in parsed[:branch_factor]:
                        if isinstance(item, dict):
                            new_v = got.add_vertex(
                                thought=str(item.get("thought", "")),
                                value=0.5,
                                kind=str(item.get("kind", "reasoning")),
                                state=str(item.get("state", vertex.state)),
                            )
                            got.add_edge(vertex.id, new_v.id, EdgeType.NEXT)
            except (json.JSONDecodeError, ValueError, TypeError):
                # Fallback: add a simple continuation
                new_v = got.add_vertex(thought=f"(continuation from {vertex.id})", value=0.4, kind="reasoning")
                got.add_edge(vertex.id, new_v.id, EdgeType.NEXT)

        # Merge phase: combine related frontier nodes
        if enable_merge and len(frontier) >= 2:
            got._merge_thoughts([v.id for v in frontier[:3]], client, model)

        # Feedback phase: revise high-value nodes with successors
        if enable_feedback and round_num >= 1:
            high_value = [v for v in got.vertices.values() if v.value >= 0.6 and v.kind in ("hypothesis", "reasoning")]
            for v in high_value[:2]:
                got._feedback_loop(v.id, client, model)

        # Distill phase: compact when graph is large
        if enable_distill and len(got.vertices) > distill_threshold:
            leaves = got.frontier()[:distill_threshold]
            got._distill([v.id for v in leaves], client, model)

    return got
