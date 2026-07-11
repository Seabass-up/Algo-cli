"""B38. Extension Host Isolation + Declarative Contribution Points (VS Code Pattern).

Extensions declare what they contribute (commands, tools, converters,
maintenance checks) via a manifest.  Activation events control when an
extension's code is loaded.  Extensions run behind a boundary and cannot
directly mutate files or send externally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ── contribution schema ───────────────────────────────────────────────


@dataclass
class CommandContribution:
    id: str
    title: str
    handler: Callable[[], str] | None = None


@dataclass
class ToolContribution:
    id: str
    description: str
    handler: Callable[[dict], str] | None = None


@dataclass
class ConverterContribution:
    id: str
    suffixes: list[str]
    handler: Callable[[str], str] | None = None


@dataclass
class MaintenanceCheckContribution:
    id: str
    name: str
    handler: Callable[[], dict] | None = None


@dataclass
class ExtensionManifest:
    """Declarative manifest for an Algo CLI extension."""
    id: str
    name: str
    version: str = "0.1.0"
    enabled: bool = True
    activation_events: list[str] = field(default_factory=list)
    contributes_commands: list[CommandContribution] = field(default_factory=list)
    contributes_tools: list[ToolContribution] = field(default_factory=list)
    contributes_converters: list[ConverterContribution] = field(default_factory=list)
    contributes_maintenance_checks: list[MaintenanceCheckContribution] = field(default_factory=list)


# ── activation events ─────────────────────────────────────────────────


def matches_activation(event: str, context: dict[str, Any]) -> bool:
    """Check if an activation event matches the current context."""
    if event == "*":
        return True
    if event.startswith("onCommand:"):
        return context.get("command") == event.split(":", 1)[1]
    if event.startswith("onFileType:"):
        return context.get("file_suffix") == event.split(":", 1)[1]
    if event.startswith("onProject:"):
        return context.get("project") == event.split(":", 1)[1]
    if event.startswith("onSchedule:"):
        return context.get("schedule") == event.split(":", 1)[1]
    return False


# ── extension host ────────────────────────────────────────────────────


class ExtensionHost:
    """Manages extensions with activation and contribution resolution."""

    def __init__(self) -> None:
        self._manifests: dict[str, ExtensionManifest] = {}
        self._activated: set[str] = set()

    def register(self, manifest: ExtensionManifest) -> None:
        self._manifests[manifest.id] = manifest

    def activate(self, ext_id: str, context: dict[str, Any] | None = None) -> bool:
        """Activate an extension if its activation events match."""
        manifest = self._manifests.get(ext_id)
        if not manifest or not manifest.enabled:
            return False
        ctx = context or {}
        if any(matches_activation(ev, ctx) for ev in manifest.activation_events):
            self._activated.add(ext_id)
            return True
        return False

    def is_activated(self, ext_id: str) -> bool:
        return ext_id in self._activated

    def disable(self, ext_id: str) -> None:
        manifest = self._manifests.get(ext_id)
        if manifest:
            manifest.enabled = False
            self._activated.discard(ext_id)

    def all_commands(self) -> list[CommandContribution]:
        """Get commands from all activated extensions."""
        commands: list[CommandContribution] = []
        for ext_id in self._activated:
            manifest = self._manifests[ext_id]
            commands.extend(manifest.contributes_commands)
        return commands

    def all_tools(self) -> list[ToolContribution]:
        tools: list[ToolContribution] = []
        for ext_id in self._activated:
            manifest = self._manifests[ext_id]
            tools.extend(manifest.contributes_tools)
        return tools

    def all_converters(self) -> list[ConverterContribution]:
        converters: list[ConverterContribution] = []
        for ext_id in self._activated:
            manifest = self._manifests[ext_id]
            converters.extend(manifest.contributes_converters)
        return converters

    def all_maintenance_checks(self) -> list[MaintenanceCheckContribution]:
        checks: list[MaintenanceCheckContribution] = []
        for ext_id in self._activated:
            manifest = self._manifests[ext_id]
            checks.extend(manifest.contributes_maintenance_checks)
        return checks

    def execute_command(self, command_id: str) -> str:
        """Execute a contributed command (gated by activation)."""
        for ext_id in self._activated:
            manifest = self._manifests[ext_id]
            for cmd in manifest.contributes_commands:
                if cmd.id == command_id and cmd.handler:
                    return cmd.handler()
        return f"Command {command_id} not found or not activated"

    def validate_manifest(self, ext_id: str) -> list[str]:
        """Validate a manifest and return list of issues."""
        manifest = self._manifests.get(ext_id)
        if not manifest:
            return ["manifest not found"]
        issues: list[str] = []
        if not manifest.id:
            issues.append("missing id")
        if not manifest.name:
            issues.append("missing name")
        for cmd in manifest.contributes_commands:
            if not cmd.id:
                issues.append("command missing id")
        return issues