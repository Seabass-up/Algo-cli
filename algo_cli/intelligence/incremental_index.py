"""Incremental Index — mtime-watermark based change tracking.

Borrowed from Windows Search USN Journal change tracking
(C:\\ProgramData\\Microsoft\\Search\\Data\\Applications\\Windows\\Windows-usn.db):
Windows Search uses the NTFS USN (Update Sequence Number) journal to detect
file changes in O(1) instead of scanning the entire filesystem.  The
ChangeTracking table tracks moves, deletes, and byte-range changes with
batch-based processing.

Since Python cannot access the raw NTFS USN journal without ctypes bindings,
this module uses file mtime + size as a watermark — the same principle but
portable across filesystems.

Pattern: B27 in ALGO.md.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import IntFlag
from pathlib import Path
from typing import Any, Iterable


class ChangeType(IntFlag):
    NONE = 0
    CREATED = 1
    MODIFIED = 2
    DELETED = 4
    MOVED = 8


@dataclass
class FileWatermark:
    """Per-file watermark — mtime + size + inode."""
    path: str
    mtime: float
    size: int
    inode: int = 0  # for move detection on platforms that support it

    def matches(self, stat: os.stat_result) -> bool:
        return (self.mtime == stat.st_mtime
                and self.size == stat.st_size)


@dataclass
class ChangeRecord:
    """A single detected change."""
    path: str
    change_type: ChangeType
    old_path: str | None = None  # for moves
    watermark: FileWatermark | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class IncrementalIndex:
    """Mtime-watermark based incremental change tracker.

    Usage:
        tracker = IncrementalIndex.load(watermark_file)
        changes = tracker.scan(directory, glob="*.py")
        for change in changes:
            if change.change_type & ChangeType.MODIFIED:
                re_embed(change.path)
        tracker.save(watermark_file)
    """

    watermarks: dict[str, FileWatermark] = field(default_factory=dict)
    last_scan: float = 0.0
    scan_count: int = 0

    # --- scan -------------------------------------------------------------

    def scan_paths(self, paths: Iterable[Path]) -> list[ChangeRecord]:
        """Scan an explicit iterable of files and return changes.

        This is the safer companion to ``scan(directory)`` for callers that
        already have a filtered source list (for example, harness roots with
        include/exclude patterns).  It preserves the same watermark semantics
        while avoiding a broad recursive walk.
        """
        changes: list[ChangeRecord] = []
        current_files: dict[str, FileWatermark] = {}
        current_inodes: set[int] = set()

        for filepath in paths:
            if not filepath.is_file():
                continue
            path_str = str(filepath)
            try:
                stat = filepath.stat()
            except OSError:
                continue

            wm = FileWatermark(
                path=path_str,
                mtime=stat.st_mtime,
                size=stat.st_size,
                inode=stat.st_ino,
            )
            current_files[path_str] = wm
            if stat.st_ino:
                current_inodes.add(stat.st_ino)

            old_wm = self.watermarks.get(path_str)
            if old_wm is None:
                moved_from = self._find_by_inode(stat.st_ino, path_str)
                if moved_from:
                    changes.append(ChangeRecord(
                        path=path_str,
                        change_type=ChangeType.MOVED,
                        old_path=moved_from,
                        watermark=wm,
                    ))
                else:
                    changes.append(ChangeRecord(
                        path=path_str,
                        change_type=ChangeType.CREATED,
                        watermark=wm,
                    ))
            elif not old_wm.matches(stat):
                changes.append(ChangeRecord(
                    path=path_str,
                    change_type=ChangeType.MODIFIED,
                    watermark=wm,
                ))

        for old_path, old_wm in self.watermarks.items():
            if old_path not in current_files:
                if old_wm.inode and old_wm.inode in current_inodes:
                    continue
                changes.append(ChangeRecord(
                    path=old_path,
                    change_type=ChangeType.DELETED,
                ))

        self.watermarks = current_files
        self.last_scan = time.time()
        self.scan_count += 1
        return changes

    def scan(
        self,
        directory: Path,
        *,
        glob: str = "**/*",
        exclude_dirs: set[str] | None = None,
    ) -> list[ChangeRecord]:
        """Scan directory and return changes since last scan.

        Detects: created, modified, deleted, moved.
        """
        if exclude_dirs is None:
            exclude_dirs = {".git", "__pycache__", ".venv", "node_modules",
                            ".mypy_cache", ".pytest_cache", ".ruff_cache"}

        changes: list[ChangeRecord] = []
        current_files: dict[str, FileWatermark] = {}

        # Walk the directory
        for filepath in directory.glob(glob):
            if not filepath.is_file():
                continue
            # Skip excluded directories
            rel_parts = filepath.relative_to(directory).parts
            if any(part in exclude_dirs for part in rel_parts):
                continue

            path_str = str(filepath)
            try:
                stat = filepath.stat()
            except OSError:
                continue

            wm = FileWatermark(
                path=path_str,
                mtime=stat.st_mtime,
                size=stat.st_size,
                inode=stat.st_ino,
            )
            current_files[path_str] = wm

            old_wm = self.watermarks.get(path_str)
            if old_wm is None:
                # Check if this file was moved (same inode, different path)
                moved_from = self._find_by_inode(stat.st_ino, path_str)
                if moved_from:
                    changes.append(ChangeRecord(
                        path=path_str,
                        change_type=ChangeType.MOVED,
                        old_path=moved_from,
                        watermark=wm,
                    ))
                else:
                    changes.append(ChangeRecord(
                        path=path_str,
                        change_type=ChangeType.CREATED,
                        watermark=wm,
                    ))
            elif not old_wm.matches(stat):
                changes.append(ChangeRecord(
                    path=path_str,
                    change_type=ChangeType.MODIFIED,
                    watermark=wm,
                ))

        # Detect deletions (files in watermarks but not in current_files)
        for old_path, old_wm in self.watermarks.items():
            if old_path not in current_files:
                # Check if it was moved (inode exists under new path)
                if not self._inode_exists_elsewhere(old_wm.inode, old_path):
                    changes.append(ChangeRecord(
                        path=old_path,
                        change_type=ChangeType.DELETED,
                    ))

        # Update watermarks
        self.watermarks = current_files
        self.last_scan = time.time()
        self.scan_count += 1

        return changes

    def _find_by_inode(self, inode: int, exclude_path: str) -> str | None:
        """Find a watermark with the same inode (for move detection)."""
        if inode == 0:
            return None
        for path, wm in self.watermarks.items():
            if path != exclude_path and wm.inode == inode:
                return path
        return None

    def _inode_exists_elsewhere(self, inode: int, exclude_path: str) -> bool:
        """Check if inode exists in current scan under a different path."""
        return self._find_by_inode(inode, exclude_path) is not None

    # --- query ------------------------------------------------------------

    def needs_update(self, path: str) -> bool:
        """Check if a specific file has changed since last scan."""
        filepath = Path(path)
        if not filepath.exists():
            return path in self.watermarks  # was deleted
        try:
            stat = filepath.stat()
        except OSError:
            return False
        wm = self.watermarks.get(path)
        if wm is None:
            return True  # new file
        return not wm.matches(stat)

    def get_changed_files(self, directory: Path, **kwargs: Any) -> list[str]:
        """Return only the paths of changed files (convenience)."""
        changes = self.scan(directory, **kwargs)
        return [c.path for c in changes if c.change_type != ChangeType.NONE]

    # --- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "watermarks": {
                path: {"mtime": wm.mtime, "size": wm.size, "inode": wm.inode}
                for path, wm in self.watermarks.items()
            },
            "last_scan": self.last_scan,
            "scan_count": self.scan_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IncrementalIndex:
        tracker = cls()
        for path, wm_data in data.get("watermarks", {}).items():
            tracker.watermarks[path] = FileWatermark(
                path=path,
                mtime=wm_data["mtime"],
                size=wm_data["size"],
                inode=wm_data.get("inode", 0),
            )
        tracker.last_scan = data.get("last_scan", 0.0)
        tracker.scan_count = data.get("scan_count", 0)
        return tracker

    def save(self, path: Path) -> None:
        """Save watermarks to a JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> IncrementalIndex:
        """Load watermarks from a JSON file.

        Returns empty tracker if file doesn't exist (first scan).
        """
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls.from_dict(data)

    # --- stats ------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "tracked_files": len(self.watermarks),
            "last_scan": self.last_scan,
            "scan_count": self.scan_count,
        }