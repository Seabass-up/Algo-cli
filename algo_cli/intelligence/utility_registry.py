"""B40. Modular Utility Registry + Per-Utility Settings (PowerToys Pattern).

Each Algo CLI feature is an independently enabled utility with its own
config, slash commands, health check, telemetry channel, and permissions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Utility:
    """A single Algo CLI utility/feature module."""

    id: str
    name: str
    description: str = ""
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    slash_commands: list[str] = field(default_factory=list)
    health_check: Callable[[], dict[str, Any]] | None = None
    telemetry_channel: str = ""
    permissions: list[str] = field(default_factory=list)

    def effective_config(self) -> dict[str, Any]:
        """Merge defaults with user overrides."""
        merged = dict(self.defaults)
        merged.update(self.config)
        return merged

    def run_health_check(self) -> dict[str, Any]:
        if self.health_check:
            try:
                return self.health_check()
            except Exception as e:
                return {"healthy": False, "error": str(e)}
        return {"healthy": True, "note": "no health check defined"}


class UtilityRegistry:
    """Registry of independently enabled utilities."""

    def __init__(self) -> None:
        self._utilities: dict[str, Utility] = {}

    def register(self, util: Utility) -> None:
        self._utilities[util.id] = util

    def get(self, util_id: str) -> Utility | None:
        return self._utilities.get(util_id)

    def enable(self, util_id: str) -> bool:
        util = self._utilities.get(util_id)
        if util:
            util.enabled = True
            return True
        return False

    def disable(self, util_id: str) -> bool:
        util = self._utilities.get(util_id)
        if util:
            util.enabled = False
            return True
        return False

    def list_all(self) -> list[Utility]:
        return list(self._utilities.values())

    def list_enabled(self) -> list[Utility]:
        return [u for u in self._utilities.values() if u.enabled]

    def list_disabled(self) -> list[Utility]:
        return [u for u in self._utilities.values() if not u.enabled]

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": u.id,
                "name": u.name,
                "enabled": u.enabled,
                "commands": u.slash_commands,
                "config": u.effective_config(),
                "health": u.run_health_check() if u.enabled else {"healthy": False, "reason": "disabled"},
            }
            for u in self._utilities.values()
        ]

    def find_by_command(self, command: str) -> Utility | None:
        """Find which utility owns a slash command."""
        for u in self._utilities.values():
            if command in u.slash_commands:
                return u
        return None

    def update_config(self, util_id: str, key: str, value: Any) -> bool:
        util = self._utilities.get(util_id)
        if util:
            util.config[key] = value
            return True
        return False


def default_registry() -> UtilityRegistry:
    """Build a registry with known Algo CLI utilities."""
    reg = UtilityRegistry()
    reg.register(Utility(
        id="maintenance",
        name="Maintenance Center",
        description="System health, diagnostics, and repair",
        slash_commands=["/maintenance", "/maint", "/doctor"],
        telemetry_channel="system",
        permissions=["read", "diagnose"],
        defaults={"auto_crystallize": True, "crystallize_interval": 3},
    ))
    reg.register(Utility(
        id="sms_project_logger",
        name="SMS Project Logger",
        description="Log iMessages to project folders with contact routing",
        slash_commands=[],
        telemetry_channel="tool_calls",
        permissions=["read", "log"],
        defaults={"db_path": "~/.algo_cli/sms_project_log.db"},
    ))
    reg.register(Utility(
        id="document_ingest",
        name="Document Ingest",
        description="Convert files to Markdown for RAG/analysis",
        slash_commands=["/ingest"],
        telemetry_channel="tool_calls",
        permissions=["read"],
    ))
    reg.register(Utility(
        id="pdf_ocr",
        name="PDF OCR",
        description="Extract text from scanned PDFs via vision models",
        slash_commands=["/pdf"],
        telemetry_channel="tool_calls",
        permissions=["read"],
    ))
    reg.register(Utility(
        id="bid_compare",
        name="Bid Comparison",
        description="Compare competing electrical bids",
        slash_commands=[],
        telemetry_channel="operational",
        permissions=["read", "analyze"],
    ))
    reg.register(Utility(
        id="memory_hygiene",
        name="Memory Hygiene",
        description="Detect duplicate/stale memories and lessons",
        slash_commands=["/memories", "/lessons"],
        telemetry_channel="system",
        permissions=["read", "propose"],
    ))
    return reg