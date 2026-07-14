"""Extension version manifest (J14).

Sibling to ``version_manifest.py``. Inspired by macOS SystemVersion.plist and
Docker componentsVersion.json: one durable, queryable truth for plugin/helper
components instead of scattering version facts across status output.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

EXTENSIONS_VERSION_FILE = CONFIG_DIR / "extensions_version.json"


@dataclass(frozen=True)
class ExtensionComponent:
    name: str
    kind: str
    version: str = ""
    path: str = ""
    status: str = "unknown"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ExtensionsManifest:
    generated_at: float
    components: tuple[ExtensionComponent, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"generated_at": self.generated_at, "components": [c.as_dict() for c in self.components]}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def _plugin_components() -> list[ExtensionComponent]:
    try:
        from . import plugins
        manifests = plugins.discover_plugins()
        return [
            ExtensionComponent(
                name=m.name,
                kind="plugin",
                version=m.version,
                path=str(plugins.PLUGINS_DIR / m.name),
                status="discovered",
            )
            for m in manifests
        ]
    except Exception:
        return []


def _binary_component(name: str) -> ExtensionComponent:
    found = shutil.which(name)
    return ExtensionComponent(name=name, kind="binary", path=found or "", status="ready" if found else "missing")


def build_extensions_manifest() -> ExtensionsManifest:
    components: list[ExtensionComponent] = []
    components.extend(_plugin_components())
    for binary in ("ollama", "git", "gh", "lms"):
        components.append(_binary_component(binary))
    return ExtensionsManifest(generated_at=time.time(), components=tuple(components))


def save_extensions_manifest(path: Path | None = None) -> ExtensionsManifest:
    manifest = build_extensions_manifest()
    target = path or EXTENSIONS_VERSION_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(manifest.to_json(), encoding="utf-8")
    return manifest


__all__ = ["ExtensionComponent", "ExtensionsManifest", "build_extensions_manifest", "save_extensions_manifest", "EXTENSIONS_VERSION_FILE"]
