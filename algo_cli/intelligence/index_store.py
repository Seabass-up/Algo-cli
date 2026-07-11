"""Small persistence helpers for project intelligence indexes."""

from __future__ import annotations

from pathlib import Path

from .project_graph import ProjectGraph, build_project_graph, load_project_graph


def ensure_project_graph(root: Path | str, *, rebuild: bool = False) -> ProjectGraph:
    root_path = Path(root).resolve()
    if not rebuild:
        graph = load_project_graph(root_path)
        if graph is not None:
            return graph
    return build_project_graph(root_path, persist=True)
