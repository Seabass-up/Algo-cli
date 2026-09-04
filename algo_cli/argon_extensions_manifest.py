"""Argon-isolated extension version manifest (J14).

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

# Curated optional helpers. Presence is discovered without executing Homebrew or
# the helper itself; catalog membership never grants subprocess authority.
# ``probe`` is a command name for formulae/binary casks, an absolute app path
# for GUI casks, and intentionally empty for font-only casks.
HOMEBREW_HELPERS: tuple[tuple[str, str, str], ...] = (
    ("awww", "homebrew-formula", "awww"),
    ("b4n", "homebrew-formula", "b4n"),
    ("bluetuith", "homebrew-formula", "bluetuith"),
    ("cliphist", "homebrew-formula", "cliphist"),
    ("cuttlefish", "homebrew-formula", "cuttlefish"),
    ("evnx", "homebrew-formula", "evnx"),
    ("fluxcd", "homebrew-formula", "flux"),
    ("fusesoc", "homebrew-formula", "fusesoc"),
    ("inshellisense", "homebrew-formula", "is"),
    ("kata", "homebrew-formula", "kata"),
    ("lld@22", "homebrew-formula", "ld.lld"),
    ("llvm@22", "homebrew-formula", "clang"),
    ("nats", "homebrew-formula", "nats"),
    ("rammap", "homebrew-formula", "rammap"),
    ("svlang", "homebrew-formula", "slang"),
    ("systemd-lsp", "homebrew-formula", "systemd-lsp"),
    ("tracy-genomics", "homebrew-formula", "tracy"),
    ("vapoursynth-vszip", "homebrew-formula", ""),
    ("vi-sql", "homebrew-formula", "vi-sql"),
    ("xmedcon", "homebrew-formula", "medcon"),
    ("bb", "homebrew-cask", "/Applications/bb.app"),
    ("ds4-control", "homebrew-cask", "/Applications/DS4 Control.app"),
    ("font-jetendard", "homebrew-font", ""),
    ("font-nexon-football-gothic", "homebrew-font", ""),
    ("font-nexon-kart-gothic", "homebrew-font", ""),
    ("glean", "homebrew-cask", "/Applications/Glean.app"),
    ("grok-bot", "homebrew-cask", "/Applications/Grok Bot.app"),
    ("mongrel", "homebrew-cask", "/Applications/Mongrel.app"),
    ("muse-code", "homebrew-cask", "muse"),
    ("owlocr", "homebrew-cask", "/Applications/OwlOCR.app"),
    ("petdex", "homebrew-cask", "/Applications/Petdex.app"),
    ("sentry-cli", "homebrew-cask", "sentry-cli"),
    ("sina-finance", "homebrew-cask", "/Applications/新浪财经APP.app"),
    ("subtitle-edit", "homebrew-cask", "/Applications/Subtitle Edit.app"),
    ("warp-agent-cli", "homebrew-cask", "warp"),
)


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
        from . import william_plugins as plugins
        manifests = plugins.discover_plugins()
        return [
            ExtensionComponent(
                name=m.name,
                kind="plugin",
                version=m.version,
                path=f"plugins/{m.name}",
                status="discovered",
            )
            for m in manifests
        ]
    except Exception:
        return []


def _binary_component(name: str) -> ExtensionComponent:
    found = shutil.which(name)
    return ExtensionComponent(name=name, kind="binary", path=found or "", status="ready" if found else "missing")


def _homebrew_prefix() -> Path | None:
    """Return Homebrew's prefix without invoking Homebrew."""
    brew = shutil.which("brew")
    if not brew:
        return None
    binary = Path(brew).expanduser()
    if binary.parent.name not in {"bin", "sbin"}:
        return None
    return binary.parent.parent


def _installed_version(path: Path) -> str:
    """Infer a version from an opt link, Cellar, or Caskroom directory."""
    try:
        if path.parent.name == "opt":
            resolved = path.resolve(strict=True)
            return resolved.name if resolved.name != path.name else ""
        children = [child for child in path.iterdir() if child.is_dir()]
        if children:
            newest = max(children, key=lambda child: child.stat().st_mtime_ns)
            return newest.name
    except OSError:
        pass
    return ""


def _homebrew_component(
    name: str,
    kind: str,
    probe: str,
    *,
    prefix: Path | None,
) -> ExtensionComponent:
    if kind == "homebrew-formula":
        receipt = prefix / "Cellar" / name if prefix is not None else None
        if receipt is None or not receipt.exists():
            return ExtensionComponent(name=name, kind=kind, status="missing")
        opt_path = prefix / "opt" / name
        command = opt_path / "bin" / probe if probe else None
        command_exists = bool(command is not None and command.exists())
        return ExtensionComponent(
            name=name,
            kind=kind,
            version=_installed_version(receipt),
            path=str(command if command_exists else receipt),
            status="ready" if command_exists else "installed",
        )

    receipt = prefix / "Caskroom" / name if prefix is not None else None
    receipt_exists = bool(receipt is not None and receipt.exists())
    if not receipt_exists or receipt is None:
        return ExtensionComponent(name=name, kind=kind, status="missing")

    located = ""
    if probe.startswith("/"):
        app_path = Path(probe)
        if app_path.exists():
            located = str(app_path)
    elif probe:
        located = shutil.which(probe) or ""

    if kind == "homebrew-font":
        status = "ready"
    elif located:
        status = "ready"
    else:
        status = "installed"
    return ExtensionComponent(
        name=name,
        kind=kind,
        version=_installed_version(receipt) if receipt_exists and receipt is not None else "",
        path=located or (str(receipt) if receipt_exists and receipt is not None else ""),
        status=status,
    )


def _homebrew_components() -> list[ExtensionComponent]:
    prefix = _homebrew_prefix()
    return [
        _homebrew_component(name, kind, probe, prefix=prefix)
        for name, kind, probe in HOMEBREW_HELPERS
    ]


def build_extensions_manifest() -> ExtensionsManifest:
    components: list[ExtensionComponent] = []
    components.extend(_plugin_components())
    for binary in ("ollama", "git", "gh", "lms"):
        components.append(_binary_component(binary))
    components.extend(_homebrew_components())
    return ExtensionsManifest(generated_at=time.time(), components=tuple(components))


def save_extensions_manifest(path: Path | None = None) -> ExtensionsManifest:
    manifest = build_extensions_manifest()
    target = path or EXTENSIONS_VERSION_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(manifest.to_json(), encoding="utf-8")
    return manifest


__all__ = ["ExtensionComponent", "ExtensionsManifest", "build_extensions_manifest", "save_extensions_manifest", "EXTENSIONS_VERSION_FILE"]
