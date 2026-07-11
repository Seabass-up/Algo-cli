"""H3 — Retraction Ledger.

Append-only retraction records, never silent deletes.
Mined from T3MP3ST INTEGRITY_LEDGER.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetractionEntry:
    """A single retraction record."""

    id: str
    target_id: str
    reason: str
    retracted_at: float = field(default_factory=time.time)
    retracted_by: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_id": self.target_id,
            "reason": self.reason,
            "retracted_at": self.retracted_at,
            "retracted_by": self.retracted_by,
            "metadata": dict(self.metadata),
        }


class RetractionLedger:
    """Append-only ledger — entries can never be deleted, only added."""

    def __init__(self) -> None:
        self._entries: list[RetractionEntry] = []

    def add(
        self,
        target_id: str,
        reason: str,
        retracted_by: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RetractionEntry:
        entry = RetractionEntry(
            id=f"retraction-{len(self._entries) + 1:06d}",
            target_id=target_id,
            reason=reason,
            retracted_by=retracted_by,
            metadata=metadata or {},
        )
        self._entries.append(entry)
        return entry

    def is_retracted(self, target_id: str) -> bool:
        return any(e.target_id == target_id for e in self._entries)

    def get_retractions(self, target_id: str) -> list[RetractionEntry]:
        return [e for e in self._entries if e.target_id == target_id]

    def all(self) -> list[RetractionEntry]:
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def delete(self, id: str) -> bool:
        """Always returns False — retractions are permanent."""
        return False