"""B50. Hash-First Deduplication + Orphan Pruning.

Detect moved/renamed files by content hash.  Retarget existing embeddings
instead of re-embedding.  Prune orphaned chunks.  Source: 0k-rag pattern.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class ChangeKind(str, Enum):
    """Shared change-kind enum for hash diffs and changelog-style changes.

    The package-level ``algo_cli.intelligence.ChangeKind`` is used by both
    B50 hash-dedup tests (NEW/MOVED/MODIFIED/DELETED/UNCHANGED) and B44
    changefile tests (FEATURE/FIX/MAINTENANCE/SAFETY/DOCS/BREAKING).  Making
    this a ``str`` enum keeps JSON serialization and string comparisons
    compatible with the Beachball-style changelog helpers while preserving the
    hash-diff members.
    """

    NEW = "new"
    MOVED = "moved"
    MODIFIED = "modified"
    DELETED = "deleted"
    UNCHANGED = "unchanged"
    FEATURE = "feature"
    FIX = "fix"
    MAINTENANCE = "maintenance"
    SAFETY = "safety"
    DOCS = "docs"
    BREAKING = "breaking"


@dataclass
class FileRecord:
    path: str
    content_hash: str
    size: int
    mtime: float


@dataclass
class HashChange:
    kind: ChangeKind
    path: str
    old_path: str | None = None  # for MOVED
    content_hash: str = ""
    old_hash: str = ""


class HashDeduplicator:
    """Track files by content hash to detect moves and deduplicate."""

    def __init__(self) -> None:
        self._by_path: dict[str, FileRecord] = {}
        self._by_hash: dict[str, list[str]] = {}

    def snapshot(self, files: Iterable[tuple[str, bytes, float]]) -> None:
        """Take a snapshot of current files (path, content, mtime)."""
        self._by_path.clear()
        self._by_hash.clear()
        for path, content, mtime in files:
            h = hashlib.sha256(content).hexdigest()
            rec = FileRecord(path=path, content_hash=h, size=len(content), mtime=mtime)
            self._by_path[path] = rec
            self._by_hash.setdefault(h, []).append(path)

    def diff(
        self,
        old: "HashDeduplicator",
    ) -> list[HashChange]:
        """Compute changes between old and current snapshots."""
        changes: list[HashChange] = []
        old_paths = set(old._by_path.keys())
        new_paths = set(self._by_path.keys())

        # Deleted
        for path in old_paths - new_paths:
            old_rec = old._by_path[path]
            # Check if content moved to a new path
            new_locs = self._by_hash.get(old_rec.content_hash, [])
            if new_locs:
                changes.append(HashChange(
                    kind=ChangeKind.MOVED,
                    path=new_locs[0],
                    old_path=path,
                    content_hash=old_rec.content_hash,
                    old_hash=old_rec.content_hash,
                ))
            else:
                changes.append(HashChange(
                    kind=ChangeKind.DELETED,
                    path=path,
                    content_hash=old_rec.content_hash,
                    old_hash=old_rec.content_hash,
                ))

        # New or modified
        for path in new_paths - old_paths:
            rec = self._by_path[path]
            # Check if content existed at old path (already handled as MOVED)
            old_locs = old._by_hash.get(rec.content_hash, [])
            if not old_locs:
                changes.append(HashChange(
                    kind=ChangeKind.NEW,
                    path=path,
                    content_hash=rec.content_hash,
                ))

        for path in new_paths & old_paths:
            old_rec = old._by_path[path]
            new_rec = self._by_path[path]
            if old_rec.content_hash != new_rec.content_hash:
                changes.append(HashChange(
                    kind=ChangeKind.MODIFIED,
                    path=path,
                    content_hash=new_rec.content_hash,
                    old_hash=old_rec.content_hash,
                ))

        return changes

    def find_duplicates(self) -> dict[str, list[str]]:
        """Find files with identical content hashes."""
        return {h: paths for h, paths in self._by_hash.items() if len(paths) > 1}

    def prune_orphans(self, valid_paths: set[str]) -> list[str]:
        """Remove records for paths no longer in valid set. Return pruned paths."""
        orphans = [p for p in self._by_path if p not in valid_paths]
        for p in orphans:
            rec = self._by_path.pop(p)
            bucket = self._by_hash.get(rec.content_hash, [])
            if p in bucket:
                bucket.remove(p)
                if not bucket:
                    del self._by_hash[rec.content_hash]
        return orphans


def compute_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()