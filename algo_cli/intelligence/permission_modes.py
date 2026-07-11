"""B51. Declarative Permission Modes + Structural Spawn Safety.

Config-level tool sets and path restrictions.  Prevent permission
escalation structurally.  Source: aloop pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class PermissionLevel(Enum):
    READ_ONLY = auto()
    STANDARD = auto()
    ELEVATED = auto()
    UNRESTRICTED = auto()


# Tool sets per permission level
DEFAULT_TOOL_SETS: dict[PermissionLevel, set[str]] = {
    PermissionLevel.READ_ONLY: {
        "read_file", "search_files", "list_directory", "harness_search",
        "harness_read", "web_search", "web_fetch", "read_pdf",
    },
    PermissionLevel.STANDARD: {
        "read_file", "search_files", "list_directory", "write_file",
        "edit_file", "run_shell", "harness_search", "harness_read",
        "web_search", "web_fetch", "read_pdf", "embed_text",
    },
    PermissionLevel.ELEVATED: {
        "read_file", "search_files", "list_directory", "write_file",
        "edit_file", "run_shell", "harness_search", "harness_read",
        "web_search", "web_fetch", "read_pdf", "embed_text",
        "model_create", "model_delete", "git_status", "git_diff",
    },
    PermissionLevel.UNRESTRICTED: {"*"},
}

# Which levels can spawn which levels (prevent escalation)
SPAWN_MATRIX: dict[PermissionLevel, set[PermissionLevel]] = {
    PermissionLevel.READ_ONLY: set(),
    PermissionLevel.STANDARD: {PermissionLevel.READ_ONLY},
    PermissionLevel.ELEVATED: {PermissionLevel.READ_ONLY, PermissionLevel.STANDARD},
    PermissionLevel.UNRESTRICTED: {
        PermissionLevel.READ_ONLY, PermissionLevel.STANDARD,
        PermissionLevel.ELEVATED, PermissionLevel.UNRESTRICTED,
    },
}


@dataclass
class PermissionMode:
    level: PermissionLevel
    allowed_tools: set[str] = field(default_factory=set)
    allowed_paths: list[str] = field(default_factory=list)  # glob patterns
    denied_paths: list[str] = field(default_factory=list)
    spawnable_modes: set[PermissionLevel] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not self.allowed_tools:
            self.allowed_tools = DEFAULT_TOOL_SETS.get(self.level, set()).copy()
        if not self.spawnable_modes:
            self.spawnable_modes = SPAWN_MATRIX.get(self.level, set()).copy()

    def can_use_tool(self, tool_name: str) -> bool:
        return "*" in self.allowed_tools or tool_name in self.allowed_tools

    def can_access_path(self, path: str) -> bool:
        import fnmatch
        for denied in self.denied_paths:
            if fnmatch.fnmatch(path, denied):
                return False
        if not self.allowed_paths:
            return True
        return any(fnmatch.fnmatch(path, p) for p in self.allowed_paths)

    def can_spawn(self, target_level: PermissionLevel) -> bool:
        return target_level in self.spawnable_modes


class PermissionManager:
    """Manage permission modes and enforce structural spawn safety."""

    def __init__(self, default_level: PermissionLevel = PermissionLevel.STANDARD) -> None:
        self._modes: dict[str, PermissionMode] = {}
        self._default = PermissionMode(level=default_level)

    def register(self, name: str, mode: PermissionMode) -> None:
        self._modes[name] = mode

    def get(self, name: str | None) -> PermissionMode:
        if name and name in self._modes:
            return self._modes[name]
        return self._default

    def validate_spawn(
        self,
        parent_name: str | None,
        child_level: PermissionLevel,
    ) -> bool:
        parent = self.get(parent_name)
        return parent.can_spawn(child_level)

    def list_modes(self) -> dict[str, PermissionMode]:
        return dict(self._modes)