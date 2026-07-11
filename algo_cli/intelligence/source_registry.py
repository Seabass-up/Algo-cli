"""B42. Source Registry + Policy-Aware Resolution (WinGet Pattern).

Manages multiple harness/plugin/model/document sources with priority,
trust levels, enabled flags, and policy locks.  Higher-priority trusted
sources win resolution conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class SourceType:
    HARNESS = "harness"
    PLUGIN = "plugin"
    MODEL = "model"
    DOCUMENT = "document"
    PROJECT = "project"
    KNOWLEDGE_GRAPH = "knowledge_graph"


@dataclass
class Source:
    """A registered content source."""

    source_id: str
    source_type: str
    path: str = ""
    priority: int = 100  # lower = higher priority
    trust_level: str = "trusted"  # "trusted", "untrusted", "blocked"
    enabled: bool = True
    last_refresh: str = ""
    policy_locked: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def can_resolve(self) -> bool:
        return self.enabled and self.trust_level != "blocked"


@dataclass
class SourceRecord:
    """A record retrieved from a source, with provenance."""
    source_id: str
    record_id: str
    content: Any
    trust_level: str = "trusted"


class SourceRegistry:
    """Policy-aware source registry with priority resolution."""

    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}

    def add(self, source: Source) -> None:
        self._sources[source.source_id] = source

    def remove(self, source_id: str) -> bool:
        return self._sources.pop(source_id, None) is not None

    def get(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)

    def list_all(self) -> list[Source]:
        return list(self._sources.values())

    def list_enabled(self) -> list[Source]:
        return [s for s in self._sources.values() if s.enabled]

    def list_by_type(self, source_type: str) -> list[Source]:
        return [s for s in self._sources.values() if s.source_type == source_type]

    def resolve(self, record_id: str, candidates: list[SourceRecord]) -> SourceRecord | None:
        """Resolve a record from multiple sources by priority/trust.

        Higher-priority (lower number) trusted sources win.
        """
        enabled_sources = {s.source_id: s for s in self._sources.values() if s.can_resolve()}
        best: SourceRecord | None = None
        best_priority = 999
        for rec in candidates:
            src = enabled_sources.get(rec.source_id)
            if not src:
                continue
            if src.priority < best_priority:
                best = rec
                best_priority = src.priority
        return best

    def disable(self, source_id: str) -> bool:
        src = self._sources.get(source_id)
        if src and not src.policy_locked:
            src.enabled = False
            return True
        return False

    def enable(self, source_id: str) -> bool:
        src = self._sources.get(source_id)
        if src:
            src.enabled = True
            return True
        return False

    def lock_policy(self, source_id: str) -> bool:
        src = self._sources.get(source_id)
        if src:
            src.policy_locked = True
            return True
        return False

    def refresh(self, source_id: str) -> bool:
        src = self._sources.get(source_id)
        if src:
            src.last_refresh = datetime.now().isoformat()
            return True
        return False

    def health(self) -> list[dict[str, Any]]:
        return [
            {
                "source_id": s.source_id,
                "type": s.source_type,
                "enabled": s.enabled,
                "trust": s.trust_level,
                "priority": s.priority,
                "last_refresh": s.last_refresh,
                "policy_locked": s.policy_locked,
            }
            for s in self._sources.values()
        ]

    def conflicts(self) -> list[dict[str, Any]]:
        """Detect sources with same type and priority."""
        by_key: dict[tuple[str, int], list[str]] = {}
        for s in self._sources.values():
            key = (s.source_type, s.priority)
            by_key.setdefault(key, []).append(s.source_id)
        return [
            {"type": k[0], "priority": k[1], "sources": v}
            for k, v in by_key.items() if len(v) > 1
        ]