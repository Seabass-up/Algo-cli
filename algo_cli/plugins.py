"""Plugin system for Algo CLI.

Discovers and loads plugins from ~/.algo_cli/plugins/. Each plugin is a
directory containing:
  - plugin.json  (required manifest with metadata)
  - __init__.py  (Python module with optional entry points)

Entry points a plugin may export:
  - register_actions() -> tuple[ActionSpec, ...]
  - register_slash_commands() -> list[tuple[str, str]]
  - register_tools() -> dict[str, Callable]
  - on_load(config) -> None

The plugin manager is defensive: a broken plugin never crashes the CLI.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG_DIR

logger = logging.getLogger(__name__)

PLUGINS_DIR = CONFIG_DIR / "plugins"


@dataclass(frozen=True)
class PluginManifest:
    """Metadata for a discovered plugin."""
    name: str
    version: str
    description: str
    author: str = ""
    entry_points: tuple[str, ...] = ()
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LoadedPlugin:
    """A plugin that has been discovered and optionally loaded."""
    manifest: PluginManifest
    module: Any = None
    path: Path = field(default_factory=Path)
    load_error: str = ""
    loaded: bool = False

    @property
    def name(self) -> str:
        return self.manifest.name

    def as_dict(self) -> dict[str, Any]:
        logical_path = f"plugins/{self.path.name}" if self.path.name else "plugins"
        return {
            "name": self.manifest.name,
            "version": self.manifest.version,
            "description": self.manifest.description,
            "author": self.manifest.author,
            "enabled": self.manifest.enabled,
            "loaded": self.loaded,
            "load_error": self.load_error,
            "path": logical_path,
            "entry_points": list(self.manifest.entry_points),
        }


def _parse_manifest(manifest_path: Path) -> PluginManifest | None:
    """Parse a plugin.json manifest file. Returns None on failure."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to parse plugin manifest %s: %s", manifest_path, exc)
        return None

    name = data.get("name", "")
    version = data.get("version", "0.0.0")
    description = data.get("description", "")
    if not name:
        logger.warning("Plugin manifest at %s has no 'name' field", manifest_path)
        return None

    entry_points = tuple(data.get("entry_points", []))
    enabled = data.get("enabled", True)
    author = data.get("author", "")

    return PluginManifest(
        name=name,
        version=version,
        description=description,
        author=author,
        entry_points=entry_points,
        enabled=enabled,
    )


def discover_plugins(plugins_dir: Path | None = None) -> list[PluginManifest]:
    """Discover all plugin manifests in the plugins directory.

    Returns a list of parsed manifests. Does not load plugin code.
    """
    root = plugins_dir or PLUGINS_DIR
    if not root.is_dir():
        return []

    manifests: list[PluginManifest] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "plugin.json"
        if not manifest_path.exists():
            continue
        parsed = _parse_manifest(manifest_path)
        if parsed is not None:
            manifests.append(parsed)
    return manifests


def load_plugin(manifest: PluginManifest, plugins_dir: Path | None = None) -> LoadedPlugin:
    """Load a single plugin module by its manifest.

    Returns a LoadedPlugin. If loading fails, loaded=False and load_error is set.
    """
    root = plugins_dir or PLUGINS_DIR
    plugin_path = root / manifest.name
    init_file = plugin_path / "__init__.py"

    loaded = LoadedPlugin(manifest=manifest, path=plugin_path)

    if not manifest.enabled:
        loaded.load_error = "Plugin is disabled in manifest"
        return loaded

    if not init_file.exists():
        loaded.load_error = f"No __init__.py found at {init_file}"
        return loaded

    try:
        spec = importlib.util.spec_from_file_location(
            f"algo_cli_plugin_{manifest.name}",
            init_file,
        )
        if spec is None or spec.loader is None:
            loaded.load_error = "Could not create import spec"
            return loaded

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.module = module
        loaded.loaded = True
    except Exception as exc:
        loaded.load_error = str(exc)
        logger.warning("Failed to load plugin '%s': %s", manifest.name, exc)

    return loaded


def load_all_plugins(plugins_dir: Path | None = None) -> list[LoadedPlugin]:
    """Discover and load all enabled plugins.

    Returns a list of LoadedPlugin objects (both successful and failed).
    """
    manifests = discover_plugins(plugins_dir)
    return [load_plugin(m, plugins_dir) for m in manifests]


def collect_plugin_actions(plugins: list[LoadedPlugin]) -> list[Any]:
    """Call register_actions() on each loaded plugin and collect results."""
    actions: list[Any] = []
    for plugin in plugins:
        if not plugin.loaded or plugin.module is None:
            continue
        register_fn = getattr(plugin.module, "register_actions", None)
        if register_fn is None or not callable(register_fn):
            continue
        try:
            result = register_fn()
            if isinstance(result, (list, tuple)):
                actions.extend(result)
        except Exception as exc:
            logger.warning("Plugin '%s' register_actions() failed: %s", plugin.name, exc)
    return actions


def collect_plugin_slash_commands(plugins: list[LoadedPlugin]) -> list[tuple[str, str]]:
    """Call register_slash_commands() on each loaded plugin and collect results."""
    commands: list[tuple[str, str]] = []
    for plugin in plugins:
        if not plugin.loaded or plugin.module is None:
            continue
        register_fn = getattr(plugin.module, "register_slash_commands", None)
        if register_fn is None or not callable(register_fn):
            continue
        try:
            result = register_fn()
            if isinstance(result, (list, tuple)):
                commands.extend(result)
        except Exception as exc:
            logger.warning("Plugin '%s' register_slash_commands() failed: %s", plugin.name, exc)
    return commands


def collect_plugin_tools(plugins: list[LoadedPlugin]) -> dict[str, Callable]:
    """Call register_tools() on each loaded plugin and collect results into a dict."""
    tools: dict[str, Callable] = {}
    for plugin in plugins:
        if not plugin.loaded or plugin.module is None:
            continue
        register_fn = getattr(plugin.module, "register_tools", None)
        if register_fn is None or not callable(register_fn):
            continue
        try:
            result = register_fn()
            if isinstance(result, dict):
                tools.update(result)
        except Exception as exc:
            logger.warning("Plugin '%s' register_tools() failed: %s", plugin.name, exc)
    return tools


def plugin_status(plugins_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return manifest status without importing or executing plugin code."""
    root = plugins_dir or PLUGINS_DIR
    return [
        {
            **manifest.as_dict(),
            "loaded": False,
            "load_error": "",
            "path": f"plugins/{manifest.name}",
            "state": "discovered" if manifest.enabled else "disabled",
        }
        for manifest in discover_plugins(root)
    ]


def ensure_plugins_dir() -> Path:
    """Create the plugins directory if it doesn't exist."""
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    return PLUGINS_DIR
