"""B80. Optimistic Concurrency Control for Edits.

SHA-256 version hashes prevent conflicting writes and stale-data overwrites.
Source: Pathfinder pattern.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WriteResult:
    success: bool
    version: str = ""
    error: str = ""
    current_version: str = ""
    message: str = ""


class OCCEditor:
    """Optimistic concurrency control for file edits."""

    def __init__(self) -> None:
        self._versions: dict[str, str] = {}

    def read(self, path: str) -> tuple[str, str]:
        """Read file and return (content, version_hash)."""
        content = Path(path).read_text(encoding="utf-8")
        version = self._hash(content)
        self._versions[path] = version
        return content, version

    def write(self, path: str, content: str, expected_version: str) -> WriteResult:
        """Write with OCC — fails if file changed since read."""
        try:
            current = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        current_version = self._hash(current)

        if current_version != expected_version:
            return WriteResult(
                success=False,
                error="VERSION_MISMATCH",
                current_version=current_version,
                message=f"File changed since you read it. Expected {expected_version[:8]}, got {current_version[:8]}.",
            )

        Path(path).write_text(content, encoding="utf-8")
        new_version = self._hash(content)
        self._versions[path] = new_version
        return WriteResult(success=True, version=new_version)

    def check_version(self, path: str, expected_version: str) -> bool:
        """Check if file still matches expected version."""
        try:
            content = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        return self._hash(content) == expected_version

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()