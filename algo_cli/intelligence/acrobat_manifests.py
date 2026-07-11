"""B154-B156, B162, B168, B171: Acrobat-derived manifest/tool/dependency patterns.

- B154: Declarative Tool/App Manifest UI
- B155: Edition / Entitlement Overlay Manifests
- B156: Plugin Silo + Host Adapter Tree
- B162: Web Plugin Version Map and Resource Provenance
- B168: Explicit Dependency Manifest
- B171: Declarative Task/Filter Registry
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ── B154: Declarative Tool/App Manifest UI ─────────────────────────────


@dataclass
class UIComponent:
    """A single UI component in a declarative tool manifest."""
    type: str
    name: str = ""
    label: str = ""
    children: list["UIComponent"] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCommand:
    """A command declared in a tool manifest."""
    name: str
    execute_after_layout: bool = False
    prompt_user: bool = False


@dataclass
class ToolManifest:
    """Declarative tool/app manifest (B154).

    Defines tools, panels, buttons, commands, and layouts without
    hardcoding UI in Python.
    """
    id: str
    title: str
    version: str = "1.0"
    requires_context: bool = False
    commands: list[ToolCommand] = field(default_factory=list)
    layouts: dict[str, list[UIComponent]] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)

    def validate(self, command_registry: set[str], component_registry: set[str]) -> list[str]:
        """Validate manifest against registries. Returns list of errors (empty = valid)."""
        errors: list[str] = []
        for cmd in self.commands:
            if cmd.name not in command_registry:
                errors.append(f"unknown command: {cmd.name}")
        for layout_name, components in self.layouts.items():
            for comp in components:
                if comp.name and comp.name not in component_registry:
                    errors.append(f"unbound component '{comp.name}' in layout '{layout_name}'")
        return errors


class ToolManifestLoader:
    """Loads and validates tool manifests."""

    def __init__(self) -> None:
        self._manifests: dict[str, ToolManifest] = {}
        self._command_registry: set[str] = set()
        self._component_registry: set[str] = set()

    def register_command(self, name: str) -> None:
        self._command_registry.add(name)

    def register_component(self, name: str) -> None:
        self._component_registry.add(name)

    def load(self, manifest: ToolManifest) -> list[str]:
        """Load a manifest, returning validation errors. Empty list = success."""
        errors = manifest.validate(self._command_registry, self._component_registry)
        if not errors:
            self._manifests[manifest.id] = manifest
        return errors

    def get(self, manifest_id: str) -> ToolManifest | None:
        return self._manifests.get(manifest_id)

    def all_manifests(self) -> list[ToolManifest]:
        return list(self._manifests.values())


# ── B155: Edition / Entitlement Overlay Manifests ──────────────────────


@dataclass
class EditionOverlay:
    """An edition overlay for a tool (B155)."""
    edition: str  # "reader", "pro", "enterprise", "local", "cloud"
    disabled_commands: list[str] = field(default_factory=list)
    hidden_components: list[str] = field(default_factory=list)
    metadata_overrides: dict[str, str] = field(default_factory=dict)
    paywall_message: str = ""


@dataclass
class ResolvedManifest:
    """The effective manifest after overlay resolution."""
    manifest: ToolManifest
    disabled_commands: set[str] = field(default_factory=set)
    hidden_components: set[str] = field(default_factory=set)
    paywall_message: str = ""
    edition: str = ""


class EditionResolver:
    """Resolves base tool manifest + edition overlay into effective manifest."""

    def __init__(self) -> None:
        self._overlays: dict[tuple[str, str], EditionOverlay] = {}

    def register_overlay(self, tool_id: str, overlay: EditionOverlay) -> None:
        self._overlays[(tool_id, overlay.edition)] = overlay

    def resolve(self, tool_id: str, edition: str, entitled: bool = True) -> ResolvedManifest | None:
        """Resolve a tool manifest with edition overlay."""
        # In a real system, this would look up the base manifest
        # For testing, we accept a simple resolution
        overlay = self._overlays.get((tool_id, edition))
        if overlay is None:
            return None
        if not entitled:
            # Return a paywall-only manifest
            return ResolvedManifest(
                manifest=ToolManifest(id=tool_id, title=""),
                disabled_commands=set(overlay.disabled_commands),
                hidden_components=set(overlay.hidden_components),
                paywall_message=overlay.paywall_message or "Not entitled",
                edition=edition,
            )
        return ResolvedManifest(
            manifest=ToolManifest(id=tool_id, title=""),
            disabled_commands=set(overlay.disabled_commands),
            hidden_components=set(overlay.hidden_components),
            paywall_message="",
            edition=edition,
        )


# ── B156: Plugin Silo + Host Adapter Tree ─────────────────────────────


@dataclass
class HostAdapter:
    """A host-specific adapter for a plugin (B156)."""
    host: str  # "word", "outlook", "autocad", etc.
    version: str  # "2019", "365", etc.
    platform: str  # "win", "mac", "linux"
    path: str = ""
    available: bool = True


@dataclass
class PluginSilo:
    """A plugin silo with core capability and host adapters (B156)."""
    id: str
    name: str
    core_api_version: str = "1.0"
    adapters: list[HostAdapter] = field(default_factory=list)
    shared_common: str = ""  # path to shared common utilities

    def select_adapter(self, host: str, version: str, platform: str) -> HostAdapter | None:
        """Select the best adapter for the given host/version/platform."""
        # Try exact match first
        for adapter in self.adapters:
            if (
                adapter.host == host
                and adapter.version == version
                and adapter.platform == platform
                and adapter.available
            ):
                return adapter
        # Try host + platform, any version
        for adapter in self.adapters:
            if adapter.host == host and adapter.platform == platform and adapter.available:
                return adapter
        # Try host only
        for adapter in self.adapters:
            if adapter.host == host and adapter.available:
                return adapter
        return None

    def unsupported_hosts(self) -> list[str]:
        """List hosts that have no available adapters."""
        hosts_with_adapters = {a.host for a in self.adapters if a.available}
        return sorted(set(a.host for a in self.adapters) - hosts_with_adapters)


class PluginSiloRegistry:
    """Registry of plugin silos."""

    def __init__(self) -> None:
        self._silos: dict[str, PluginSilo] = {}

    def register(self, silo: PluginSilo) -> None:
        self._silos[silo.id] = silo

    def get(self, silo_id: str) -> PluginSilo | None:
        return self._silos.get(silo_id)

    def all_silos(self) -> list[PluginSilo]:
        return list(self._silos.values())


# ── B162: Web Plugin Version Map and Resource Provenance ───────────────


@dataclass
class ResourceProvenance:
    """Provenance for a web resource (B162)."""
    plugin_ids: list[str]
    schema_version: int = 1
    git_path: str = ""
    git_commit: str = ""
    git_repo: str = ""


@dataclass
class PluginGroup:
    """A group of plugins with shared provenance (B162)."""
    name: str
    provenance: ResourceProvenance

    def resolves(self, plugin_id: str) -> bool:
        return plugin_id in self.provenance.plugin_ids


class WebPluginVersionMap:
    """Maps plugin IDs to code provenance, resource bundles, and versions."""

    def __init__(self) -> None:
        self._groups: dict[str, PluginGroup] = {}

    def register(self, group: PluginGroup) -> None:
        self._groups[group.name] = group

    def resolve(self, plugin_id: str) -> PluginGroup | None:
        """Resolve a plugin ID to its group."""
        matches = [g for g in self._groups.values() if g.resolves(plugin_id)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Ambiguous - return first but caller should check
            return matches[0]
        return None

    def provenance(self, plugin_id: str) -> ResourceProvenance | None:
        group = self.resolve(plugin_id)
        return group.provenance if group else None

    def all_groups(self) -> list[PluginGroup]:
        return list(self._groups.values())


# ── B168: Explicit Dependency Manifest ─────────────────────────────────


@dataclass
class DependencyEntry:
    """A single dependency declaration (B168)."""
    name: str
    required: bool = True
    version: str = ""


@dataclass
class DependencyManifest:
    """Explicit dependency manifest (B168)."""
    identity_name: str
    version: str = "1.0.0"
    arch: str = "amd64"
    files: list[DependencyEntry] = field(default_factory=list)
    external_dependencies: list[str] = field(default_factory=list)

    def check(self, existing_files: set[str]) -> tuple[list[str], list[str]]:
        """Check dependencies against existing files.
        Returns (missing_required, missing_optional).
        """
        missing_required: list[str] = []
        missing_optional: list[str] = []
        for dep in self.files:
            if dep.name not in existing_files:
                if dep.required:
                    missing_required.append(dep.name)
                else:
                    missing_optional.append(dep.name)
        return missing_required, missing_optional


# ── B171: Declarative Task/Filter Registry ─────────────────────────────


@dataclass
class TaskFilter:
    """A task/filter mapping (B171)."""
    menu_item_id: str
    filter_id: str
    handler: Callable[[dict], Any] | None = None


class TaskFilterRegistry:
    """Maps user-facing menu items to format handlers via filter IDs (B171)."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskFilter] = {}
        self._handlers: dict[str, Callable[[dict], Any]] = {}

    def register(self, task: TaskFilter) -> None:
        self._tasks[task.menu_item_id] = task
        if task.handler:
            self._handlers[task.filter_id] = task.handler

    def register_handler(self, filter_id: str, handler: Callable[[dict], Any]) -> None:
        self._handlers[filter_id] = handler

    def dispatch(self, menu_item_id: str, context: dict | None = None) -> Any:
        """Dispatch a menu item to its handler."""
        task = self._tasks.get(menu_item_id)
        if not task:
            raise KeyError(f"unknown task: {menu_item_id}")
        handler = self._handlers.get(task.filter_id)
        if not handler:
            raise KeyError(f"no handler for filter: {task.filter_id}")
        return handler(context or {})

    def available_tasks(self) -> list[str]:
        return list(self._tasks.keys())