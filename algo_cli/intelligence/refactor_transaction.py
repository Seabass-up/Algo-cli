"""B83. Refactor Transaction with Savepoint/Rollback.

Atomic refactoring with savepoints and rollback.
Source: CCASP pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path


class TransactionStatus(Enum):
    ACTIVE = auto()
    COMMITTED = auto()
    ROLLED_BACK = auto()


@dataclass
class Savepoint:
    name: str
    file_snapshots: dict[str, str] = field(default_factory=dict)  # path → content


@dataclass
class TransactionResult:
    status: TransactionStatus
    savepoints: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    error: str = ""


class RefactorTransaction:
    """Atomic refactoring with savepoint/rollback for multi-file changes."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._savepoints: list[Savepoint] = []
        self._modified: set[str] = set()
        self._status: TransactionStatus = TransactionStatus.ACTIVE

    def savepoint(self, name: str, files: list[str]) -> Savepoint:
        """Create a savepoint with snapshots of the given files."""
        if self._status != TransactionStatus.ACTIVE:
            raise RuntimeError("Transaction not active")

        sp = Savepoint(name=name)
        for filepath in files:
            try:
                content = Path(filepath).read_text(encoding="utf-8")
                sp.file_snapshots[filepath] = content
            except FileNotFoundError:
                sp.file_snapshots[filepath] = ""
        self._savepoints.append(sp)
        return sp

    def rollback_to(self, savepoint_name: str) -> None:
        """Rollback to a named savepoint."""
        sp = next((s for s in self._savepoints if s.name == savepoint_name), None)
        if not sp:
            raise KeyError(f"Savepoint '{savepoint_name}' not found")

        for filepath, content in sp.file_snapshots.items():
            Path(filepath).write_text(content, encoding="utf-8")

        # Remove savepoints after this one
        idx = self._savepoints.index(sp)
        self._savepoints = self._savepoints[:idx + 1]

    def rollback_all(self) -> None:
        """Rollback to the first savepoint (undo everything)."""
        if self._savepoints:
            self.rollback_to(self._savepoints[0].name)
        self._status = TransactionStatus.ROLLED_BACK

    def commit(self) -> TransactionResult:
        """Commit the transaction — changes are permanent."""
        self._status = TransactionStatus.COMMITTED
        return TransactionResult(
            status=self._status,
            savepoints=[sp.name for sp in self._savepoints],
            files_modified=list(self._modified),
        )

    def record_modification(self, filepath: str) -> None:
        self._modified.add(filepath)

    @property
    def status(self) -> TransactionStatus:
        return self._status

    @property
    def savepoint_names(self) -> list[str]:
        return [sp.name for sp in self._savepoints]