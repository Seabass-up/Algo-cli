"""B57. Extension Manifest Schema + Catalog.

YAML manifest with commands/config/hooks.  Extension registry with
multi-source catalog and priority.  Source: spec-kit pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class ExtensionStatus(Enum):
    DISCOVERED = auto()
    INSTALLED = auto()
    ENABLED = auto()
    DISABLED = auto()
    ERROR = auto()


@dataclass
class ExtensionContribution:
    type: str  # "command", "tool", "hook", "contextProvider", "fileConverter"
    id: str
    handler: str = ""  # module:function or path
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtensionManifest:
    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    priority: int = 100  # lower = higher priority
    activation_events: list[str] = field(default_factory=list)
    contributes: list[ExtensionContribution] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    status: ExtensionStatus = ExtensionStatus.DISCOVERED

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "priority": self.priority,
            "activation_events": self.activation_events,
            "contributes": [
                {"type": c.type, "id": c.id, "handler": c.handler, "config": c.config}
                for c in self.contributes
            ],
        }


@dataclass
class CatalogSource:
    name: str
    url: str = ""
    trust_level: str = "local"  # local, trusted, community
    priority: int = 100
    enabled: bool = True


class ExtensionCatalog:
    """Multi-source extension catalog with priority-based resolution."""

    def __init__(self) -> None:
        self._sources: dict[str, CatalogSource] = {}
        self._extensions: dict[str, ExtensionManifest] = {}

    def add_source(self, source: CatalogSource) -> None:
        self._sources[source.name] = source

    def register(self, manifest: ExtensionManifest, source_name: str = "local") -> None:
        self._extensions[manifest.id] = manifest

    def get(self, ext_id: str) -> ExtensionManifest | None:
        return self._extensions.get(ext_id)

    def list_extensions(self, status: ExtensionStatus | None = None) -> list[ExtensionManifest]:
        exts = list(self._extensions.values())
        if status:
            exts = [e for e in exts if e.status == status]
        return sorted(exts, key=lambda e: e.priority)

    def find_contribution(self, contrib_type: str, contrib_id: str) -> ExtensionManifest | None:
        for ext in self._extensions.values():
            for c in ext.contributes:
                if c.type == contrib_type and c.id == contrib_id:
                    return ext
        return None

    def enable(self, ext_id: str) -> bool:
        ext = self._extensions.get(ext_id)
        if ext:
            ext.status = ExtensionStatus.ENABLED
            return True
        return False

    def disable(self, ext_id: str) -> bool:
        ext = self._extensions.get(ext_id)
        if ext:
            ext.status = ExtensionStatus.DISABLED
            return True
        return False

    def match_activation(self, event: str) -> list[ExtensionManifest]:
        """Find extensions whose activation events match."""
        return [
            e for e in self._extensions.values()
            if e.status == ExtensionStatus.ENABLED and event in e.activation_events
        ]