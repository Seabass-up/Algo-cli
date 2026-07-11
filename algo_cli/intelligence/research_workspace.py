"""B68. Virtual File System for Research Artifacts.

Branch, persist, rejoin research investigations.
Source: Morgana pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ResearchArtifact:
    id: str
    name: str
    content: str
    artifact_type: str = "text"  # text, outline, finding, source, draft
    created_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)


@dataclass
class ResearchBranch:
    id: str
    parent_id: str | None = None
    name: str = ""
    artifacts: dict[str, ResearchArtifact] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    status: str = "active"  # active, merged, abandoned


class ResearchWorkspace:
    """Virtual file system for research artifacts with branching."""

    def __init__(self) -> None:
        self._branches: dict[str, ResearchBranch] = {}
        self._counter = 0
        # Create root branch
        self.create_branch("main", name="Main Research")

    def create_branch(self, branch_id: str, parent_id: str | None = None,
                      name: str = "") -> ResearchBranch:
        branch = ResearchBranch(id=branch_id, parent_id=parent_id, name=name)
        self._branches[branch_id] = branch
        return branch

    def add_artifact(self, branch_id: str, artifact: ResearchArtifact) -> None:
        branch = self._branches.get(branch_id)
        if not branch:
            raise KeyError(f"Branch {branch_id} not found")
        branch.artifacts[artifact.id] = artifact

    def get_artifact(self, branch_id: str, artifact_id: str) -> ResearchArtifact | None:
        branch = self._branches.get(branch_id)
        if not branch:
            return None
        if artifact_id in branch.artifacts:
            return branch.artifacts[artifact_id]
        # Walk parent chain
        if branch.parent_id:
            return self.get_artifact(branch.parent_id, artifact_id)
        return None

    def list_artifacts(self, branch_id: str, include_parent: bool = True) -> list[ResearchArtifact]:
        branch = self._branches.get(branch_id)
        if not branch:
            return []
        artifacts = list(branch.artifacts.values())
        if include_parent and branch.parent_id:
            artifacts.extend(self.list_artifacts(branch.parent_id, include_parent=True))
        return artifacts

    def merge(self, source_id: str, target_id: str) -> int:
        """Merge artifacts from source branch into target. Returns count merged."""
        source = self._branches.get(source_id)
        target = self._branches.get(target_id)
        if not source or not target:
            return 0
        count = 0
        for aid, artifact in source.artifacts.items():
            if aid not in target.artifacts:
                target.artifacts[aid] = artifact
                count += 1
        source.status = "merged"
        return count

    def search(self, branch_id: str, query: str) -> list[ResearchArtifact]:
        """Search artifacts by name, content, or tags."""
        artifacts = self.list_artifacts(branch_id)
        query_lower = query.lower()
        return [
            a for a in artifacts
            if query_lower in a.name.lower()
            or query_lower in a.content.lower()
            or any(query_lower in t.lower() for t in a.tags)
        ]

    def new_artifact_id(self) -> str:
        self._counter += 1
        return f"art_{self._counter}"

    @property
    def branch_count(self) -> int:
        return len(self._branches)

    @property
    def artifact_count(self) -> int:
        return sum(len(b.artifacts) for b in self._branches.values())