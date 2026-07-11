"""H1 — Algorithm Finding Record.

Structured append-only findings with provenance, mined from T3MP3ST Evidence Vault §8.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FindingStatus(str, Enum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    RETRACTED = "retracted"


@dataclass
class Finding:
    """A single algorithm finding with provenance."""

    id: str
    title: str
    description: str
    source_repo: str
    source_section: str
    status: FindingStatus = FindingStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    provenance: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "source_repo": self.source_repo,
            "source_section": self.source_section,
            "status": self.status.value,
            "created_at": self.created_at,
            "provenance": dict(self.provenance),
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            id=data["id"],
            title=data["title"],
            description=data["description"],
            source_repo=data["source_repo"],
            source_section=data["source_section"],
            status=FindingStatus(data.get("status", "proposed")),
            created_at=data.get("created_at", time.time()),
            provenance=dict(data.get("provenance", {})),
            evidence=list(data.get("evidence", [])),
        )


class FindingRecord:
    """Append-only store for algorithm findings."""

    def __init__(self) -> None:
        self._findings: list[Finding] = []

    def create(
        self,
        id: str,
        title: str,
        description: str,
        source_repo: str,
        source_section: str,
        provenance: dict[str, Any] | None = None,
        evidence: list[str] | None = None,
    ) -> Finding:
        if self.get(id) is not None:
            raise ValueError(f"Finding {id!r} already exists — append-only store")
        finding = Finding(
            id=id,
            title=title,
            description=description,
            source_repo=source_repo,
            source_section=source_section,
            provenance=provenance or {},
            evidence=evidence or [],
        )
        self._findings.append(finding)
        return finding

    def get(self, id: str) -> Finding | None:
        for f in self._findings:
            if f.id == id:
                return f
        return None

    def query(
        self,
        status: FindingStatus | None = None,
        source_repo: str | None = None,
    ) -> list[Finding]:
        results = self._findings
        if status is not None:
            results = [f for f in results if f.status == status]
        if source_repo is not None:
            results = [f for f in results if f.source_repo == source_repo]
        return list(results)

    def update_status(self, id: str, status: FindingStatus) -> Finding:
        f = self.get(id)
        if f is None:
            raise KeyError(f"Finding {id!r} not found")
        f.status = status
        return f

    def all(self) -> list[Finding]:
        return list(self._findings)

    def count(self) -> int:
        return len(self._findings)