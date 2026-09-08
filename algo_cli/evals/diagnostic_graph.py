"""Dependency-aware diagnostic execution with explicit partial results."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any


@dataclass(frozen=True)
class DiagnosticNode:
    name: str
    dependencies: tuple[str, ...]
    run: Callable[[Mapping[str, dict[str, Any]]], dict[str, Any]]


def run_diagnostic_graph(nodes: Sequence[DiagnosticNode]) -> dict[str, dict[str, Any]]:
    """Validate the entire DAG before running callbacks; contain failures per node."""
    names = {node.name for node in nodes}
    if len(names) != len(nodes) or any(not node.name for node in nodes):
        raise ValueError("diagnostic_duplicate_or_empty_node")
    if any(set(node.dependencies) - names for node in nodes):
        raise ValueError("diagnostic_missing_dependency")
    ordered: list[DiagnosticNode] = []
    pending = list(nodes)
    visited: set[str] = set()
    while pending:
        ready = [node for node in pending if set(node.dependencies) <= visited]
        if not ready:
            raise ValueError("diagnostic_dependency_cycle")
        ordered.extend(ready)
        visited.update(node.name for node in ready)
        pending = [node for node in pending if node.name not in visited]

    results: dict[str, dict[str, Any]] = {}
    for node in ordered:
        blocked = [name for name in node.dependencies if results[name]["status"] != "pass"]
        start = perf_counter()
        if blocked:
            result = {
                "status": "unavailable",
                "reason": "; ".join(
                    f"{name}: {results[name].get('reason') or results[name]['status']}" for name in blocked
                ),
                "evidence": {},
            }
        else:
            try:
                result = node.run({name: results[name] for name in node.dependencies})
                if not isinstance(result, dict) or result.get("status") not in {"pass", "fail", "error", "unavailable"}:
                    raise ValueError("diagnostic_invalid_result")
            except Exception as exc:
                # Exceptions can contain private provider responses or filesystem paths.
                result = {"status": "error", "reason": f"diagnostic raised {type(exc).__name__}", "evidence": {}}
        results[node.name] = {
            **result,
            "dependencies": list(node.dependencies),
            "blocked_by": blocked,
            "executed": not blocked,
            "elapsed_ms": round((perf_counter() - start) * 1000, 3),
        }
    return results
