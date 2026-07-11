"""H30 — Delta Reporting.

Report what changed since last assessment, not full reports.
Mined from T3MP3ST VISION.md Vector 4.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeltaEntry:
    """A single change in a delta report."""

    change_type: str  # "added", "removed", "changed"
    target_id: str
    old_value: Any = None
    new_value: Any = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": self.change_type,
            "target_id": self.target_id,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "timestamp": self.timestamp,
        }


@dataclass
class DeltaReport:
    """A report of changes between two states."""

    entries: list[DeltaEntry] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)

    @property
    def added(self) -> list[DeltaEntry]:
        return [e for e in self.entries if e.change_type == "added"]

    @property
    def removed(self) -> list[DeltaEntry]:
        return [e for e in self.entries if e.change_type == "removed"]

    @property
    def changed(self) -> list[DeltaEntry]:
        return [e for e in self.entries if e.change_type == "changed"]

    @property
    def is_empty(self) -> bool:
        return len(self.entries) == 0

    def summary(self) -> str:
        return (
            f"Delta: +{len(self.added)} added, -{len(self.removed)} removed, "
            f"~{len(self.changed)} changed"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "generated_at": self.generated_at,
            "summary": self.summary(),
        }


class DeltaReporter:
    """Compute deltas between two state snapshots."""

    @staticmethod
    def compute(
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> DeltaReport:
        """Compute delta between two state dicts keyed by ID."""
        entries: list[DeltaEntry] = []
        prev_keys = set(previous.keys())
        curr_keys = set(current.keys())
        # Added
        for key in sorted(curr_keys - prev_keys):
            entries.append(DeltaEntry("added", key, new_value=current[key]))
        # Removed
        for key in sorted(prev_keys - curr_keys):
            entries.append(DeltaEntry("removed", key, old_value=previous[key]))
        # Changed
        for key in sorted(prev_keys & curr_keys):
            if previous[key] != current[key]:
                entries.append(DeltaEntry("changed", key, previous[key], current[key]))
        return DeltaReport(entries=entries)