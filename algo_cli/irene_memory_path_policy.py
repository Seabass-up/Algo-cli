"""Fail-closed Irene path authority while Echo owns mutable memory."""

from __future__ import annotations

import os
import stat
import json
from pathlib import Path
from typing import Any, Mapping


class ProtectedMemoryPathError(RuntimeError):
    """A model-callable path could cross the protected memory boundary."""


_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "read_pdf": ("path",),
    "render_pdf_pages": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "find_unique_anchor": ("path",),
    "batch_edit": ("path",),
    "list_directory": ("path",),
    "search_files": ("path",),
    "vision_describe": ("image_path",),
}
_GIT_PATH_ACTIONS = frozenset({"git_status", "git_diff"})
_SESSION_PATH_COMMANDS = frozenset({"/cd", "/ls", "/read"})
_SESSION_DENIED_PATH_COMMANDS = frozenset({"/embed", "/identity", "/pdf", "/vision"})
_MAX_PATH_BYTES = 16_384


def _known_protected_roots() -> tuple[Path, ...]:
    """Return closed, content-free mutable-memory root identities."""

    from . import harness
    from . import config
    from .config import CONFIG_DIR, LEGACY_CONFIG_DIR
    from .index_compute_lab import resolve_lab_root

    roots = {
        Path(CONFIG_DIR).expanduser(),
        Path(LEGACY_CONFIG_DIR).expanduser(),
        config.get_legacy_backup_dir().expanduser(),
        resolve_lab_root().expanduser(),
        harness.CODEX_DIR,
        harness.CLAUDE_DIR,
        harness.OPENCLAW_DIR,
        harness.AGENTS_DIR,
        harness.MERCURY_DIR,
        harness.CLI_AGENT_DIR,
    }
    roots.update(
        root.root for root in harness.built_in_source_roots(include_external=True) if root.harness != "algo-cli"
    )
    extra_roots_path = Path(CONFIG_DIR) / "harness_roots.json"
    try:
        payload = config._state_descriptor_payload(
            extra_roots_path,
            max_bytes=1024 * 1024,
        )
    except FileNotFoundError:
        payload = None
    except OSError as exc:
        raise ProtectedMemoryPathError("protected extra-root policy is unavailable") from exc
    if payload is not None:
        try:
            decoded = json.loads(payload.decode("utf-8", errors="strict"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtectedMemoryPathError("protected extra-root policy is invalid") from exc
        configured, rejected = harness._parse_extra_source_roots_payload(decoded)
        if rejected:
            raise ProtectedMemoryPathError("protected extra-root policy is invalid")
        roots.update(root.root for root in configured)
    # Protected-root aliases are deny definitions, not authorities to consume.
    # Include both their lexical spelling and current resolved target so an
    # operator-configured symlink cannot be bypassed through its target path.
    expanded_roots = set(roots)
    for root in roots:
        try:
            expanded_roots.add(Path(os.path.realpath(os.fspath(root))))
        except (OSError, ValueError):
            raise ProtectedMemoryPathError("protected root identity is unavailable") from None
    return tuple(sorted(expanded_roots, key=lambda item: os.fspath(item).casefold()))


def _absolute_nofollow_path(raw: object, *, cwd: object) -> Path:
    value = os.fspath(raw) if isinstance(raw, os.PathLike) else str(raw or "")
    if not value or "\x00" in value or len(value.encode("utf-8", errors="replace")) > _MAX_PATH_BYTES:
        raise ProtectedMemoryPathError("protected path authority rejected an invalid path")
    base_value = os.fspath(cwd) if isinstance(cwd, os.PathLike) else str(cwd or os.getcwd())
    if not base_value or "\x00" in base_value:
        raise ProtectedMemoryPathError("protected path authority rejected an invalid workspace")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base_value).expanduser() / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))

    # Do not resolve aliases. Reject every symlink in the existing prefix so
    # neither a final link nor an ancestor can redirect a tool into memory.
    current = Path(candidate.anchor)
    parts = candidate.parts[1:] if candidate.anchor else candidate.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ProtectedMemoryPathError("protected path authority could not validate path ancestry") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ProtectedMemoryPathError("protected path authority rejects symlinked path ancestry")
        if index == len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ProtectedMemoryPathError("protected path authority rejects aliased or special files")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ProtectedMemoryPathError("protected path authority rejects invalid path ancestry")
    return candidate


def _path_within(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.abspath(os.fspath(candidate)).casefold()
    root_text = os.path.abspath(os.fspath(root)).casefold()
    try:
        return os.path.commonpath((candidate_text, root_text)) == root_text
    except (OSError, ValueError):
        return False


def require_allowed_path(raw: object, *, cwd: object) -> Path:
    """Validate one model-controlled path without following aliases."""

    candidate = _absolute_nofollow_path(raw, cwd=cwd)
    from .config import CONFIG_DIR, get_legacy_backup_dir

    config_root = Path(CONFIG_DIR).expanduser()
    backup_root = get_legacy_backup_dir().expanduser()
    residue_prefixes = (
        f"{config_root.name}.migration-".casefold(),
        f"{backup_root.name}.migration-".casefold(),
    )
    residue_parent = os.path.abspath(os.fspath(config_root.parent)).casefold()
    candidate_text = os.path.abspath(os.fspath(candidate)).casefold()
    residue_match = False
    try:
        relative_text = os.path.relpath(candidate_text, residue_parent)
    except ValueError:
        relative_text = ""
    if relative_text and relative_text not in {".", ".."}:
        first_component = relative_text.split(os.sep, 1)[0]
        residue_match = first_component.startswith(residue_prefixes)
    if residue_match or any(_path_within(candidate, root) for root in _known_protected_roots()):
        raise ProtectedMemoryPathError("protected memory paths are unavailable to model-callable filesystem tools")
    return candidate


def _session_path(command_line: object) -> str | None:
    from .workspace_resolver import parse_path_arg

    stripped = str(command_line or "").strip()
    if not stripped:
        return None
    head, _, remainder = stripped.partition(" ")
    if not head.startswith("/"):
        head = f"/{head}"
    if head.casefold() not in _SESSION_PATH_COMMANDS:
        return None
    if head.casefold() in {"/ls", "/cd"} and not remainder.strip():
        return "."
    return parse_path_arg(remainder.strip()) or None


def protected_tool_policy_error(
    name: str,
    args: Mapping[str, Any],
    cfg: Any,
) -> str | None:
    """Return a fixed refusal for one protected model-callable action."""

    from .ada_memory_echo_veil import echo_veil_authority_selected

    if not echo_veil_authority_selected(cfg):
        return None
    if name == "update_user_profile":
        return (
            "Error: update_user_profile is unavailable while Echo Veil is the "
            "exclusive memory authority; use an explicit reviewed Echo memory "
            "action instead."
        )
    if name == "run_shell":
        return (
            "Error: run_shell is disabled while Echo Veil is the exclusive memory "
            "authority; use typed filesystem tools on non-memory paths."
        )
    cwd = args.get("cwd") or getattr(cfg, "cwd", None) or os.getcwd()
    try:
        if name in _GIT_PATH_ACTIONS:
            require_allowed_path(".", cwd=cwd)
            if name == "git_diff" and args.get("path") is not None:
                require_allowed_path(args.get("path"), cwd=cwd)
        for field in _PATH_FIELDS.get(name, ()):
            if (
                name == "vision_describe"
                and field == "image_path"
                and args.get("artifact_id") is not None
                and args.get("artifact_page") is not None
                and args.get("artifact_receipt") is not None
                and not args.get("image_path")
                and args.get("cwd") is None
            ):
                # The typed PDF-artifact resolver independently authenticates
                # the ID, manifest, TTL, and page bytes. It is not a raw path
                # exception into CONFIG_DIR.
                continue
            raw = args.get(field, "." if field == "path" else None)
            require_allowed_path(raw, cwd=cwd)
        if name in {"session_slash", "session_command"}:
            if name == "session_command":
                # Broad slash dispatch includes implicit-cwd readers such as
                # repository intelligence and code/diff helpers. Validate its
                # workspace even when the command has no explicit path token.
                require_allowed_path(".", cwd=cwd)
            command_name = str(args.get("command") or "").strip().split(maxsplit=1)[0]
            if command_name.casefold() in _SESSION_DENIED_PATH_COMMANDS:
                raise ProtectedMemoryPathError("protected path-bearing session command is unavailable")
            session_path = _session_path(args.get("command"))
            if session_path is not None:
                require_allowed_path(session_path, cwd=cwd)
    except ProtectedMemoryPathError:
        return (
            "Error: protected memory paths are unavailable to model-callable "
            "filesystem tools while Echo Veil is selected."
        )
    return None


__all__ = [
    "ProtectedMemoryPathError",
    "protected_tool_policy_error",
    "require_allowed_path",
]
