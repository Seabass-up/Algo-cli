"""Fail-closed Irene path authority for protected mutable memory."""

from __future__ import annotations

import os
import stat
import json
from dataclasses import dataclass
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
_PROTECTED_MODEL_SESSION_COMMANDS = frozenset({
    "/cwd", "/pwd", "/cd", "/ls", "/read", "/status", "/info", "/help",
    "/mode", "/memory", "/memories", "/remember", "/lesson", "/lessons",
    "/harness", "/hsearch", "/hread",
})
_MAX_PATH_BYTES = 16_384
UNQUALIFIED_BROWSER_ACTIONS = frozenset({
    "cobalt_open", "cobalt_snapshot", "cobalt_screenshot", "cobalt_navigate",
    "cobalt_click", "cobalt_type", "cobalt_scroll", "cobalt_close",
})
GLOBALLY_DISABLED_PROTECTED_ACTIONS = UNQUALIFIED_BROWSER_ACTIONS | {"run_shell", "update_user_profile"}


def _known_protected_roots() -> tuple[Path, ...]:
    """Return closed, content-free mutable-memory root identities."""

    from . import harness
    from . import config
    from .config import CONFIG_DIR, LEGACY_CONFIG_DIR
    from .index_compute_lab import resolve_lab_root
    from .continuum_memory import storage_root

    roots = {
        storage_root(),
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
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate

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
    # Collapse parent traversal only after inspecting the components it would
    # erase. `link/../file` must not validate a different path from the reader.
    return Path(os.path.abspath(os.fspath(candidate)))


def _path_within(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.abspath(os.fspath(candidate)).casefold()
    root_text = os.path.abspath(os.fspath(root)).casefold()
    try:
        return os.path.commonpath((candidate_text, root_text)) == root_text
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class ProtectedPathRules:
    roots: tuple[str, ...]
    residue_parent: str
    residue_prefixes: tuple[str, ...]
    identities: tuple[tuple[int, int, int], ...] = ()

    def denies_identity(self, information: os.stat_result) -> bool:
        return (information.st_dev, information.st_ino, stat.S_IFMT(information.st_mode)) in self.identities

    def denies(self, candidate: Path) -> bool:
        candidate_text = os.path.abspath(os.fspath(candidate)).casefold()
        try:
            relative_text = os.path.relpath(candidate_text, self.residue_parent)
        except ValueError:
            relative_text = ""
        first_component = relative_text.split(os.sep, 1)[0]
        residue_match = first_component.startswith(self.residue_prefixes)
        return residue_match or any(_path_within(candidate, Path(root)) for root in self.roots)


def protected_path_rules() -> ProtectedPathRules:
    """Capture immutable deny definitions for a bounded descendant operation."""
    from .config import CONFIG_DIR, get_legacy_backup_dir

    config_root = Path(CONFIG_DIR).expanduser()
    backup_root = get_legacy_backup_dir().expanduser()
    roots = tuple(sorted(os.path.abspath(os.fspath(root)) for root in _known_protected_roots()))
    identities = set()
    for root in roots:
        try:
            info = Path(root).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProtectedMemoryPathError("protected root identity is unavailable") from exc
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            identities.add((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))
    return ProtectedPathRules(
        roots,
        os.path.abspath(os.fspath(config_root.parent)).casefold(),
        (f"{config_root.name}.migration-".casefold(), f"{backup_root.name}.migration-".casefold()),
        tuple(sorted(identities)),
    )


def require_allowed_path(raw: object, *, cwd: object, rules: ProtectedPathRules | None = None) -> Path:
    """Validate one model-controlled path without following aliases."""

    candidate = _absolute_nofollow_path(raw, cwd=cwd)
    rules = rules or protected_path_rules()
    if rules.denies(candidate):
        raise ProtectedMemoryPathError("protected memory paths are unavailable to model-callable filesystem tools")
    # Firmlinks and mount aliases may share identity without being symlinks.
    for current in (candidate, *candidate.parents):
        try:
            if rules.denies_identity(current.lstat()):
                raise ProtectedMemoryPathError("protected path authority rejects aliased root ancestry")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProtectedMemoryPathError("protected path identity is unavailable") from exc
    return candidate


def require_disjoint_git_worktree(cwd: object) -> Path:
    """Refuse aggregate Git reads when the repository overlaps protected state."""
    rules = protected_path_rules()
    candidate = require_allowed_path(".", cwd=cwd, rules=rules)
    root = candidate
    for ancestor in (candidate, *candidate.parents):
        marker = ancestor / ".git"
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProtectedMemoryPathError("repository boundary is unavailable") from exc
        require_allowed_path(marker, cwd=ancestor, rules=rules)
        root = require_allowed_path(ancestor, cwd=ancestor, rules=rules)
        break
    try:
        root_info = root.lstat()
        root_identity = (root_info.st_dev, root_info.st_ino, stat.S_IFMT(root_info.st_mode))
        for protected in rules.roots:
            if _path_within(Path(protected), root):
                raise ProtectedMemoryPathError("repository encloses protected memory")
            for parent in (Path(protected), *Path(protected).parents):
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) == root_identity:
                    raise ProtectedMemoryPathError("repository aliases an ancestor of protected memory")
    except OSError as exc:
        raise ProtectedMemoryPathError("repository boundary is unavailable") from exc
    from .git_evidence import _run_git

    rc, metadata = _run_git(
        ["rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"],
        str(candidate),
        errors="surrogateescape",
    )
    if rc != 0:
        # Let the ordinary handler report a non-repository, but never accept
        # unreadable metadata from a repository marker found above.
        if (root / ".git").exists():
            raise ProtectedMemoryPathError("repository metadata is unavailable")
        return root
    locations = metadata.splitlines()
    if len(locations) != 3:
        raise ProtectedMemoryPathError("repository metadata is invalid")
    actual_root = require_allowed_path(locations[0], cwd=root, rules=rules)
    try:
        same_root = actual_root.samefile(root)
    except OSError as exc:
        raise ProtectedMemoryPathError("repository identity changed") from exc
    if not same_root:
        raise ProtectedMemoryPathError("repository worktree is redirected")
    for location in locations[1:]:
        require_allowed_path(location, cwd=root, rules=rules)
    rc, inventory = _run_git(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], str(root), errors="surrogateescape"
    )
    paths = inventory.split("\0")
    if rc != 0 or len(paths) > 20_001 or len(inventory) > 4 * 1024 * 1024:
        raise ProtectedMemoryPathError("repository file inventory is unavailable or oversized")
    for relative in paths:
        if not relative:
            continue
        path = require_allowed_path(relative, cwd=root, rules=rules)
        if not _path_within(path, root):
            raise ProtectedMemoryPathError("repository file inventory escaped its worktree")
    if rules != protected_path_rules():
        raise ProtectedMemoryPathError("protected repository policy changed")
    return root


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

    from .continuum_memory import ContinuumMemoryError, selected

    try:
        protected = selected(cfg)
    except ContinuumMemoryError:
        return "Error: memory configuration requires repair before tool execution."
    if not protected:
        return None
    if name in UNQUALIFIED_BROWSER_ACTIONS:
        return (
            "Error: the unqualified browser service is disabled while Continuum "
            "Memory is authoritative; browser access requires a "
            "qualified containment boundary."
        )
    if name == "update_user_profile":
        return (
            "Error: update_user_profile is unavailable while Continuum Memory is "
            "authoritative; use an explicit scoped memory_remember action instead."
        )
    if name == "run_shell":
        return (
            "Error: run_shell is disabled while Continuum Memory is the protected "
            "authority; use typed filesystem tools on non-memory paths."
        )
    cwd = args.get("cwd") or getattr(cfg, "cwd", None) or os.getcwd()
    try:
        if name in _GIT_PATH_ACTIONS:
            require_disjoint_git_worktree(cwd)
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
            parts = str(args.get("command") or "").strip().split(maxsplit=1)
            command_name = parts[0].casefold() if parts else ""
            if command_name and not command_name.startswith("/"):
                command_name = "/" + command_name
            if name == "session_command":
                if command_name not in _PROTECTED_MODEL_SESSION_COMMANDS:
                    return "Error: protected memory paths are unavailable through this broad session command; it is not qualified. Use an admitted typed tool."
                # Broad slash dispatch includes implicit-cwd readers such as
                # repository intelligence and code/diff helpers. Validate its
                # workspace even when the command has no explicit path token.
                require_allowed_path(".", cwd=cwd)
            if command_name.casefold() in _SESSION_DENIED_PATH_COMMANDS:
                raise ProtectedMemoryPathError("protected path-bearing session command is unavailable")
            session_path = _session_path(args.get("command"))
            if session_path is not None:
                require_allowed_path(session_path, cwd=cwd)
    except ProtectedMemoryPathError:
        return (
            "Error: protected memory paths are unavailable to model-callable "
            "filesystem tools while Continuum Memory is selected."
        )
    return None


__all__ = [
    "ProtectedMemoryPathError",
    "protected_tool_policy_error",
    "require_allowed_path",
]
