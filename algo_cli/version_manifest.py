"""Version manifest for Algo CLI.

Tracks the full system state — not just the CLI binary version, but the
harness index version, embedding model, knowledge graph version, and any
loaded plugin versions. Inspired by Docker's componentsVersion.json.

The manifest may be written to ~/.algo_cli/versions.json. `algo-cli --version`
only reads state that already exists; it never builds an index or scaffolds files.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import CONFIG_DIR
from . import __version__ as _cli_version

VERSIONS_FILE = CONFIG_DIR / "versions.json"
EXTENSIONS_VERSION_FILE = CONFIG_DIR / "extensions_version.json"


@dataclass
class VersionManifest:
    """Full system version state."""
    cli_version: str = _cli_version
    python_version: str = ""
    platform: str = ""
    harness_index_version: str = ""
    harness_record_count: int = 0
    harness_embed_model: str = ""
    knowledge_graph_version: str = ""
    knowledge_graph_node_count: int = 0
    plugins: dict[str, str] = field(default_factory=dict)
    config_dir: str = ""
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


def _safe_get_harness_info() -> dict[str, Any]:
    """Read an existing harness index without building or refreshing it."""
    try:
        from . import harness
        if not harness.INDEX_PATH.is_file():
            return {}
        index = json.loads(harness.INDEX_PATH.read_text(encoding="utf-8"))
        record_count = len(index.get("records", []))
        embed_model = index.get("embed_model", "")
        index_version = index.get("version", "")
        return {
            "harness_index_version": index_version,
            "harness_record_count": record_count,
            "harness_embed_model": embed_model,
        }
    except Exception:
        return {}


def _safe_get_kg_info() -> dict[str, Any]:
    """Read existing knowledge-graph metadata without running the graph."""
    try:
        from . import index_compute_lab
        assets = index_compute_lab.lab_assets()
        if assets is not None:
            _root, graph_path, _aliases = assets
            data = json.loads(graph_path.read_text(encoding="utf-8"))
            nodes = data.get("nodes")
            index = data.get("index")
            node_count = len(nodes) if isinstance(nodes, list) else len(index) if isinstance(index, dict) else 0
            version = data.get("version") or data.get("schema_version") or ""
            return {
                "knowledge_graph_version": version,
                "knowledge_graph_node_count": node_count,
            }
    except Exception:
        pass
    return {}


def _safe_get_plugin_versions() -> dict[str, str]:
    """Safely query loaded plugins for their versions."""
    try:
        from . import plugins as plugins_module
        manifests = plugins_module.discover_plugins()
        return {m.name: m.version for m in manifests}
    except Exception:
        return {}


def build_manifest() -> VersionManifest:
    """Build a version manifest from the current system state."""
    import platform as _platform
    import sys

    harness_info = _safe_get_harness_info()
    kg_info = _safe_get_knowledge_graph_info()
    plugin_versions = _safe_get_plugin_versions()

    return VersionManifest(
        cli_version=_cli_version,
        python_version=sys.version.split()[0] if sys.version else "",
        platform=f"{_platform.system()} {_platform.machine()}",
        harness_index_version=harness_info.get("harness_index_version", ""),
        harness_record_count=harness_info.get("harness_record_count", 0),
        harness_embed_model=harness_info.get("harness_embed_model", ""),
        knowledge_graph_version=kg_info.get("knowledge_graph_version", ""),
        knowledge_graph_node_count=kg_info.get("knowledge_graph_node_count", 0),
        plugins=plugin_versions,
        config_dir="~/.algo_cli (or ALGO_CLI_CONFIG_DIR)",
        updated_at=time.time(),
    )


def _safe_get_knowledge_graph_info() -> dict[str, Any]:
    """Alias for _safe_get_kg_info to avoid name collision."""
    return _safe_get_kg_info()


def save_manifest() -> VersionManifest:
    """Build and persist the version manifest to disk."""
    manifest = build_manifest()
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        VERSIONS_FILE.write_text(manifest.to_json(), encoding="utf-8")
    except OSError as exc:
        # Don't crash if we can't write — just log
        import logging
        logging.getLogger(__name__).warning("Could not save version manifest: %s", exc)
    return manifest


def load_manifest() -> VersionManifest | None:
    """Load a previously saved version manifest from disk."""
    try:
        data = json.loads(VERSIONS_FILE.read_text(encoding="utf-8"))
        return VersionManifest(**data)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


def format_version_string(manifest: VersionManifest | None = None) -> str:
    """Format a human-readable version string showing full system state."""
    if manifest is None:
        manifest = build_manifest()

    lines = [
        f"Algo CLI v{manifest.cli_version}",
        f"  Python:      {manifest.python_version}",
        f"  Platform:    {manifest.platform}",
        f"  Config dir:   {manifest.config_dir}",
    ]

    if manifest.harness_record_count > 0:
        lines.append(
            f"  Harness:     {manifest.harness_record_count} records"
            f" (index v{manifest.harness_index_version or 'unknown'})"
        )
        if manifest.harness_embed_model:
            lines.append(f"  Embed model: {manifest.harness_embed_model}")

    if manifest.knowledge_graph_node_count > 0:
        lines.append(
            f"  KG:          {manifest.knowledge_graph_node_count} nodes"
            f" (v{manifest.knowledge_graph_version or 'unknown'})"
        )

    if manifest.plugins:
        lines.append("  Plugins:")
        for name, version in sorted(manifest.plugins.items()):
            lines.append(f"    {name} v{version}")

    return "\n".join(lines)
