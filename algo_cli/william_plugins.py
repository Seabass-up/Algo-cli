"""Manifest-only, fail-closed plugin discovery for Algo CLI.

Python import executes module code in the Algo CLI process. It is therefore not
a plugin security boundary. During the hardening freeze, local plugin manifests
may be discovered as untrusted metadata, but Python modules and callable
contributions are never imported or registered. No local plugin execution route
is enabled; a future reviewed, allowlisted gateway adapter would be required.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import CONFIG_DIR

PLUGINS_DIR = CONFIG_DIR / "plugins"
PLUGIN_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_VERSION_RE = re.compile(
    r"^(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){1,3}"
    r"(?:-[a-z0-9]+(?:[.-][a-z0-9]+)*)?$"
)
_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "version",
        "description",
        "author",
        "enabled",
        "entry_points",
    }
)
_PRIVILEGED_FIELDS = frozenset(
    {
        "actions",
        "capabilities",
        "commands",
        "effect_class",
        "permissions",
        "register_actions",
        "register_slash_commands",
        "register_tools",
        "slash_commands",
        "tools",
    }
)
_CODE_LOADING_DISABLED = "In-process Python plugin loading is disabled; no local plugin execution route is enabled."


class PluginValidationError(ValueError):
    """A content-free, stable plugin validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _DuplicateManifestKey(ValueError):
    pass


@dataclass(frozen=True)
class PluginManifest:
    """Validated manifest metadata; never proof that code is trustworthy."""

    name: str
    version: str
    description: str
    author: str = ""
    entry_points: tuple[str, ...] = ()
    enabled: bool = True
    schema_version: int = PLUGIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema_version != PLUGIN_SCHEMA_VERSION
            or not isinstance(self.name, str)
            or not _NAME_RE.fullmatch(self.name)
            or unicodedata.normalize("NFKC", self.name) != self.name
            or not isinstance(self.version, str)
            or not _VERSION_RE.fullmatch(self.version)
            or not isinstance(self.enabled, bool)
            or not isinstance(self.entry_points, tuple)
            or self.entry_points
        ):
            raise PluginValidationError("manifest_object", "plugin manifest object is invalid")
        _bounded_text(self.description, field_name="description", max_bytes=4096)
        _bounded_text(self.author, field_name="author", max_bytes=512)

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "metadata_trust": "untrusted",
            "code_loading": False,
        }


@dataclass(frozen=True)
class PluginRejection:
    """A stable rejection that does not disclose a local filesystem path."""

    logical_name: str
    error_code: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PluginScan:
    manifests: tuple[PluginManifest, ...]
    rejections: tuple[PluginRejection, ...]
    root_ready: bool


@dataclass
class LoadedPlugin:
    """Compatibility result for a plugin whose code remains unexecuted."""

    manifest: PluginManifest
    module: Any = None
    path: Path = field(default_factory=Path)
    load_error: str = ""
    loaded: bool = False
    error_code: str = "code_loading_disabled"

    @property
    def name(self) -> str:
        return self.manifest.name if _manifest_object_is_valid(self.manifest) else "invalid-plugin"

    def as_dict(self) -> dict[str, Any]:
        valid_manifest = _manifest_object_is_valid(self.manifest)
        logical_name = self.manifest.name if valid_manifest else "invalid-plugin"
        logical_path = f"plugins/{logical_name}"
        return {
            "name": logical_name,
            "version": self.manifest.version if valid_manifest else "",
            "description": self.manifest.description if valid_manifest else "",
            "author": self.manifest.author if valid_manifest else "",
            "enabled": self.manifest.enabled if valid_manifest else False,
            "loaded": False,
            "load_error": self.load_error or _CODE_LOADING_DISABLED,
            "error_code": self.error_code,
            "path": logical_path,
            "entry_points": [],
            "state": ("rejected" if not valid_manifest else "disabled" if not self.manifest.enabled else "blocked"),
            "code_loading": False,
            "security_boundary": False,
        }


def _logical_label(raw: str, index: int) -> str:
    return raw if _NAME_RE.fullmatch(raw) else f"invalid-entry-{index}"


def _bounded_text(value: Any, *, field_name: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise PluginValidationError("manifest_schema", f"{field_name} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PluginValidationError("manifest_encoding", f"{field_name} is not valid Unicode") from exc
    if len(encoded) > max_bytes:
        raise PluginValidationError("manifest_schema", f"{field_name} is too long")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise PluginValidationError("manifest_schema", f"{field_name} contains control characters")
    return value


def _manifest_object_is_valid(manifest: PluginManifest) -> bool:
    try:
        PluginManifest(
            name=manifest.name,
            version=manifest.version,
            description=manifest.description,
            author=manifest.author,
            entry_points=manifest.entry_points,
            enabled=manifest.enabled,
            schema_version=manifest.schema_version,
        )
    except (AttributeError, PluginValidationError, TypeError):
        return False
    return True


def _is_link_or_reparse(path: Path, info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0)):
        return True
    junction_check = getattr(path, "is_junction", None)
    return bool(junction_check()) if callable(junction_check) else False


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _read_regular_file_no_follow(path: Path) -> bytes:
    """Read one bounded regular file while rejecting symlink substitution."""

    try:
        before = path.lstat()
    except OSError as exc:
        raise PluginValidationError("manifest_unreadable", "plugin manifest cannot be inspected") from exc
    if _is_link_or_reparse(path, before):
        raise PluginValidationError("manifest_symlink", "plugin manifest cannot be a link or reparse point")
    if not stat.S_ISREG(before.st_mode):
        raise PluginValidationError("manifest_special", "plugin manifest must be a regular file")
    if before.st_nlink != 1:
        raise PluginValidationError("manifest_hardlink", "plugin manifest cannot have multiple hard links")
    if os.name == "posix" and before.st_mode & 0o022:
        raise PluginValidationError("manifest_permissions", "plugin manifest cannot be group/world writable")
    if before.st_size > MAX_MANIFEST_BYTES:
        raise PluginValidationError("manifest_oversize", "plugin manifest is too large")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        latest = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(before, opened)
            or not os.path.samestat(opened, latest)
        ):
            raise PluginValidationError("manifest_changed", "plugin manifest changed while being inspected")
        chunks: list[bytes] = []
        remaining = MAX_MANIFEST_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise PluginValidationError("manifest_oversize", "plugin manifest is too large")
        completed = os.fstat(descriptor)
        latest_completed = path.lstat()
        if (
            not os.path.samestat(opened, completed)
            or not os.path.samestat(completed, latest_completed)
            or len(payload) != completed.st_size
            or completed.st_size != opened.st_size
            or completed.st_mtime_ns != opened.st_mtime_ns
            or completed.st_ctime_ns != opened.st_ctime_ns
        ):
            raise PluginValidationError("manifest_changed", "plugin manifest changed while being read")
        return payload
    except PluginValidationError:
        raise
    except OSError as exc:
        raise PluginValidationError("manifest_unreadable", "plugin manifest cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parse_manifest(manifest_path: Path, *, expected_name: str) -> PluginManifest:
    payload = _read_regular_file_no_follow(manifest_path)
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PluginValidationError("manifest_encoding", "plugin manifest must be strict UTF-8") from exc
    try:
        data = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateManifestKey as exc:
        raise PluginValidationError("manifest_duplicate_key", "plugin manifest contains duplicate keys") from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PluginValidationError("manifest_json", "plugin manifest is invalid JSON") from exc
    if not isinstance(data, dict):
        raise PluginValidationError("manifest_schema", "plugin manifest must be an object")
    privileged = set(data) & _PRIVILEGED_FIELDS
    if privileged:
        raise PluginValidationError(
            "privileged_contribution",
            "plugin manifests cannot declare tools, commands, effects, or capabilities",
        )
    unknown = set(data) - _ALLOWED_FIELDS
    if unknown:
        raise PluginValidationError("manifest_schema", "plugin manifest contains unsupported fields")
    if data.get("schema_version") != PLUGIN_SCHEMA_VERSION:
        raise PluginValidationError("manifest_version", f"plugin schema_version must be {PLUGIN_SCHEMA_VERSION}")

    name = _bounded_text(data.get("name"), field_name="name", max_bytes=64)
    if not _NAME_RE.fullmatch(name) or unicodedata.normalize("NFKC", name) != name:
        raise PluginValidationError("manifest_name", "plugin name must use canonical lowercase ASCII kebab-case")
    if name != expected_name:
        raise PluginValidationError("name_mismatch", "plugin manifest name must exactly match its directory")
    version = _bounded_text(data.get("version"), field_name="version", max_bytes=64)
    if not _VERSION_RE.fullmatch(version):
        raise PluginValidationError("manifest_version", "plugin version must use the bounded canonical version format")
    description = _bounded_text(data.get("description", ""), field_name="description", max_bytes=4096)
    author = _bounded_text(data.get("author", ""), field_name="author", max_bytes=512)
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PluginValidationError("manifest_schema", "enabled must be a boolean")
    entry_points = data.get("entry_points", [])
    if not isinstance(entry_points, list) or entry_points:
        raise PluginValidationError(
            "privileged_contribution",
            "executable plugin entry points are prohibited during hardening",
        )
    return PluginManifest(
        name=name,
        version=version,
        description=description,
        author=author,
        entry_points=(),
        enabled=enabled,
    )


def _safe_root(root: Path) -> Path | None:
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PluginValidationError("root_unreadable", "plugin root cannot be inspected") from exc
    if _is_link_or_reparse(root, info) or not stat.S_ISDIR(info.st_mode):
        raise PluginValidationError("root_unsafe", "plugin root must be a real non-symlink directory")
    if os.name == "posix" and info.st_mode & 0o022:
        raise PluginValidationError("root_permissions", "plugin root cannot be group/world writable")
    try:
        return root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PluginValidationError("root_unreadable", "plugin root cannot be resolved") from exc


def scan_plugins(plugins_dir: Path | None = None) -> PluginScan:
    """Inspect manifests without importing code or following plugin symlinks."""

    configured = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    try:
        root = _safe_root(configured)
    except PluginValidationError as exc:
        return PluginScan(
            (),
            (PluginRejection("plugin-root", exc.code, str(exc)),),
            False,
        )
    if root is None:
        return PluginScan((), (), False)

    manifests: dict[str, PluginManifest] = {}
    rejections: list[PluginRejection] = []
    duplicate_names: set[str] = set()
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError:
        return PluginScan(
            (),
            (PluginRejection("plugin-root", "root_unreadable", "plugin root cannot be listed"),),
            False,
        )

    for index, entry in enumerate(entries, 1):
        logical_name = _logical_label(entry.name, index)
        try:
            if entry.is_symlink():
                raise PluginValidationError("symlink_entry", "plugin directories cannot be symbolic links")
            if not entry.is_dir(follow_symlinks=False):
                continue
            if not _NAME_RE.fullmatch(entry.name):
                raise PluginValidationError("directory_name", "plugin directory must use lowercase ASCII kebab-case")
            plugin_path = Path(entry.path)
            plugin_info = plugin_path.lstat()
            if _is_link_or_reparse(plugin_path, plugin_info):
                raise PluginValidationError("symlink_entry", "plugin directories cannot be links or junctions")
            if os.name == "posix" and plugin_info.st_mode & 0o022:
                raise PluginValidationError("directory_permissions", "plugin directory cannot be group/world writable")
            resolved_plugin = plugin_path.resolve(strict=True)
            if resolved_plugin.parent != root:
                raise PluginValidationError("path_escape", "plugin directory escapes the configured root")
            manifest_path = resolved_plugin / "plugin.json"
            try:
                manifest_path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise PluginValidationError("manifest_unreadable", "plugin manifest cannot be inspected") from exc
            manifest = _parse_manifest(manifest_path, expected_name=entry.name)
            canonical = manifest.name.casefold()
            if canonical in manifests or canonical in duplicate_names:
                manifests.pop(canonical, None)
                duplicate_names.add(canonical)
                raise PluginValidationError("duplicate_plugin", "duplicate canonical plugin names are rejected")
            manifests[canonical] = manifest
        except PluginValidationError as exc:
            rejections.append(PluginRejection(logical_name, exc.code, str(exc)))
        except (OSError, RuntimeError):
            rejections.append(
                PluginRejection(
                    logical_name,
                    "entry_unreadable",
                    "plugin entry cannot be inspected safely",
                )
            )
    return PluginScan(
        tuple(sorted(manifests.values(), key=lambda item: item.name)),
        tuple(rejections),
        True,
    )


def discover_plugins(plugins_dir: Path | None = None) -> list[PluginManifest]:
    """Return only strict manifests; invalid entries remain visible in status."""

    return list(scan_plugins(plugins_dir).manifests)


def load_plugin(manifest: PluginManifest, plugins_dir: Path | None = None) -> LoadedPlugin:
    """Revalidate the manifest and refuse all in-process code execution."""

    root = Path(plugins_dir) if plugins_dir is not None else PLUGINS_DIR
    manifest_name_valid = _manifest_object_is_valid(manifest)
    result = LoadedPlugin(
        manifest=manifest,
        path=root / manifest.name if manifest_name_valid else root,
    )
    if not manifest_name_valid:
        result.load_error = "Plugin manifest object is invalid."
        result.error_code = "manifest_revalidation_failed"
        return result
    scan = scan_plugins(root)
    current = next((item for item in scan.manifests if item.name == manifest.name), None)
    if current is None or current != manifest:
        result.load_error = "Plugin manifest is missing, stale, or invalid."
        result.error_code = "manifest_revalidation_failed"
        return result
    if not manifest.enabled:
        result.load_error = "Plugin is disabled in its manifest."
        result.error_code = "plugin_disabled"
        return result
    result.load_error = _CODE_LOADING_DISABLED
    result.error_code = "code_loading_disabled"
    return result


def load_all_plugins(plugins_dir: Path | None = None) -> list[LoadedPlugin]:
    """Return explicit blocked results without importing a Python module."""

    return [load_plugin(manifest, plugins_dir) for manifest in discover_plugins(plugins_dir)]


def collect_plugin_actions(plugins: list[LoadedPlugin]) -> list[Any]:
    """Reject callable action contributions; kept as a compatibility surface."""

    del plugins
    return []


def collect_plugin_slash_commands(plugins: list[LoadedPlugin]) -> list[tuple[str, str]]:
    """Reject callable slash-command contributions."""

    del plugins
    return []


def collect_plugin_tools(plugins: list[LoadedPlugin]) -> dict[str, Callable[..., Any]]:
    """Reject tool contributions so plugins cannot override core actions."""

    del plugins
    return {}


def plugin_status(plugins_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return strict discovery and rejection status without executing code."""

    scan = scan_plugins(plugins_dir)
    statuses: list[dict[str, Any]] = [
        {
            **manifest.as_dict(),
            "loaded": False,
            "load_error": "" if not manifest.enabled else _CODE_LOADING_DISABLED,
            "error_code": "plugin_disabled" if not manifest.enabled else "code_loading_disabled",
            "path": f"plugins/{manifest.name}",
            "state": "disabled" if not manifest.enabled else "discovered",
            "code_loading": False,
            "security_boundary": False,
        }
        for manifest in scan.manifests
    ]
    statuses.extend(
        {
            "name": rejection.logical_name,
            "version": "",
            "description": "",
            "author": "",
            "enabled": False,
            "loaded": False,
            "load_error": rejection.reason,
            "error_code": rejection.error_code,
            "path": "plugins/<rejected>",
            "entry_points": [],
            "state": "rejected",
            "code_loading": False,
            "security_boundary": False,
        }
        for rejection in scan.rejections
    )
    return sorted(statuses, key=lambda item: (str(item["name"]), str(item["state"])))


def ensure_plugins_dir() -> Path:
    """Create a private real plugin directory, never through a symlink."""

    PLUGINS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = PLUGINS_DIR.lstat()
    except OSError as exc:
        raise PluginValidationError("root_unreadable", "plugin root cannot be inspected") from exc
    if _is_link_or_reparse(PLUGINS_DIR, info) or not stat.S_ISDIR(info.st_mode):
        raise PluginValidationError("root_unsafe", "plugin root must be a real non-symlink directory")
    if os.name == "posix":
        os.chmod(PLUGINS_DIR, 0o700)
    return PLUGINS_DIR


__all__ = [
    "LoadedPlugin",
    "PLUGIN_SCHEMA_VERSION",
    "PLUGINS_DIR",
    "PluginManifest",
    "PluginRejection",
    "PluginScan",
    "PluginValidationError",
    "collect_plugin_actions",
    "collect_plugin_slash_commands",
    "collect_plugin_tools",
    "discover_plugins",
    "ensure_plugins_dir",
    "load_all_plugins",
    "load_plugin",
    "plugin_status",
    "scan_plugins",
]
