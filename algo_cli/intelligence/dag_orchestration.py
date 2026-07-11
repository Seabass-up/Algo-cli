"""B53. Dependency Graph Agent Orchestration.

Orchestrate agents as a dependency graph with parallel fanout,
iterative cycles, and scratchboard shared memory.
Source: agentflow pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable


class NodeStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()
    SKIPPED = auto()


@dataclass
class DAGNode:
    id: str
    execute: Callable[[dict], Any]
    depends_on: list[str] = field(default_factory=list)
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error: str | None = None


@dataclass
class DAGEdge:
    source: str
    target: str


@dataclass
class CycleSpec:
    """Iterative cycle: repeat until success criteria met."""
    nodes: list[str]
    max_iterations: int = 5
    success_check: Callable[[dict], bool] | None = None


class DependencyGraph:
    """DAG executor with parallel fanout and iterative cycles."""

    def __init__(self) -> None:
        self._nodes: dict[str, DAGNode] = {}
        self._edges: list[DAGEdge] = []
        self._cycles: list[CycleSpec] = []
        self._scratchboard: dict[str, Any] = {}

    def add_node(self, node: DAGNode) -> None:
        self._nodes[node.id] = node

    def add_edge(self, source: str, target: str) -> None:
        self._edges.append(DAGEdge(source, target))

    def add_cycle(self, cycle: CycleSpec) -> None:
        self._cycles.append(cycle)

    def topological_order(self) -> list[str]:
        """Kahn's algorithm for topological sort."""
        in_degree: dict[str, int] = {n: 0 for n in self._nodes}
        adj: dict[str, list[str]] = {n: [] for n in self._nodes}
        for e in self._edges:
            adj[e.source].append(e.target)
            in_degree[e.target] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        order: list[str] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(self._nodes):
            raise ValueError("Cycle detected in DAG")
        return order

    def _get_ready_nodes(self) -> list[str]:
        """Nodes whose dependencies are all DONE."""
        ready = []
        for nid, node in self._nodes.items():
            if node.status != NodeStatus.PENDING:
                continue
            deps = self._dependencies(nid)
            if all(self._nodes[d].status == NodeStatus.DONE for d in deps):
                ready.append(nid)
        return ready

    def _dependencies(self, node_id: str) -> list[str]:
        return [e.source for e in self._edges if e.target == node_id]

    def run(self, initial_context: dict | None = None) -> dict[str, Any]:
        """Execute the DAG, returning results for all nodes."""
        ctx = initial_context or {}
        ctx.update(self._scratchboard)

        for _ in range(len(self._nodes) * 2):  # safety limit
            ready = self._get_ready_nodes()
            if not ready:
                break
            for nid in ready:
                node = self._nodes[nid]
                node.status = NodeStatus.RUNNING
                try:
                    dep_results = {d: self._nodes[d].result for d in self._dependencies(nid)}
                    ctx.setdefault("_results", {}).update(dep_results)
                    node.result = node.execute(ctx)
                    if node.result is not None:
                        ctx[nid] = node.result
                    node.status = NodeStatus.DONE
                except Exception as e:
                    node.error = str(e)
                    node.status = NodeStatus.FAILED
                    ctx.setdefault("_errors", []).append({"node": nid, "error": str(e)})

        # Handle cycles
        for cycle in self._cycles:
            for iteration in range(cycle.max_iterations):
                for nid in cycle.nodes:
                    node = self._nodes[nid]
                    node.status = NodeStatus.PENDING
                for nid in cycle.nodes:
                    node = self._nodes[nid]
                    node.status = NodeStatus.RUNNING
                    try:
                        dep_results = {d: self._nodes[d].result for d in self._dependencies(nid)}
                        ctx.setdefault("_results", {}).update(dep_results)
                        node.result = node._DAGNode__wrapped_execute(ctx) if hasattr(node, "_DAGNode__wrapped_execute") else node.execute(ctx)
                        if node.result is not None:
                            ctx[nid] = node.result
                        node.status = NodeStatus.DONE
                    except Exception:
                        node.status = NodeStatus.FAILED

                if cycle.success_check and cycle.success_check(ctx):
                    break

        self._scratchboard.update(ctx)
        return dict(ctx)

    @property
    def scratchboard(self) -> dict[str, Any]:
        return dict(self._scratchboard)