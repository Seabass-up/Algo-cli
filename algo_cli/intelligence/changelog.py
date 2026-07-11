"""B44. Changefile-Driven Release Notes (Microsoft Beachball Pattern).

Requires a changefile for every user-visible code change.  Generates
release notes grouped by kind (feature, fix, maintenance, safety, docs,
breaking).  Validates that no release is missing changefiles.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


class ChangeKind:
    FEATURE = "feature"
    FIX = "fix"
    MAINTENANCE = "maintenance"
    SAFETY = "safety"
    DOCS = "docs"
    BREAKING = "breaking"


@dataclass
class ChangeFile:
    """A single change file (changes/*.json)."""
    filename: str
    kind: str
    description: str
    component: str = ""
    author: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    version_bump: str = "patch"  # "patch", "minor", "major"


@dataclass
class ReleaseNotes:
    version: str
    date: str
    sections: dict[str, list[str]] = field(default_factory=dict)
    breaking_changes: list[str] = field(default_factory=list)
    total_changes: int = 0

    def to_markdown(self) -> str:
        lines = [f"# Release {self.version}", f"Date: {self.date}", ""]
        if self.breaking_changes:
            lines.append("## ⚠️ Breaking Changes")
            for bc in self.breaking_changes:
                lines.append(f"- {bc}")
            lines.append("")
        kind_order = [ChangeKind.FEATURE, ChangeKind.FIX, ChangeKind.SAFETY, ChangeKind.MAINTENANCE, ChangeKind.DOCS]
        kind_labels = {
            ChangeKind.FEATURE: "✨ Features",
            ChangeKind.FIX: "🐛 Fixes",
            ChangeKind.SAFETY: "🛡️ Safety",
            ChangeKind.MAINTENANCE: "🔧 Maintenance",
            ChangeKind.DOCS: "📚 Documentation",
        }
        for kind in kind_order:
            entries = self.sections.get(kind, [])
            if entries:
                lines.append(f"## {kind_labels.get(kind, kind)}")
                for entry in entries:
                    lines.append(f"- {entry}")
                lines.append("")
        return "\n".join(lines)


class ChangelogManager:
    """Manages change files and release note generation."""

    def __init__(self, changes_dir: str = "changes"):
        self.changes_dir = Path(changes_dir)
        self._changes: list[ChangeFile] = []

    def add_change(self, change: ChangeFile) -> None:
        self._changes.append(change)

    def load_from_dir(self, directory: Path | None = None) -> list[ChangeFile]:
        """Load change files from a directory."""
        d = directory or self.changes_dir
        if not d.exists():
            return []
        loaded: list[ChangeFile] = []
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                loaded.append(ChangeFile(
                    filename=f.name,
                    kind=data.get("kind", ChangeKind.MAINTENANCE),
                    description=data.get("description", ""),
                    component=data.get("component", ""),
                    author=data.get("author", ""),
                    created_at=data.get("created_at", datetime.now().isoformat()),
                    version_bump=data.get("version_bump", "patch"),
                ))
            except (json.JSONDecodeError, KeyError):
                continue
        self._changes.extend(loaded)
        return loaded

    def pending_changes(self) -> list[ChangeFile]:
        """Return all loaded changes (not yet released)."""
        return list(self._changes)

    def has_changes(self) -> bool:
        return len(self._changes) > 0

    def required_version_bump(self) -> str:
        """Determine the minimum version bump from pending changes."""
        if any(c.version_bump == "major" or c.kind == ChangeKind.BREAKING for c in self._changes):
            return "major"
        if any(c.version_bump == "minor" or c.kind == ChangeKind.FEATURE for c in self._changes):
            return "minor"
        return "patch"

    def generate_release_notes(self, version: str, date: str | None = None) -> ReleaseNotes:
        """Generate release notes from pending changes."""
        date = date or datetime.now().strftime("%Y-%m-%d")
        sections: dict[str, list[str]] = {}
        breaking: list[str] = []
        for change in self._changes:
            kind = change.kind
            if kind == ChangeKind.BREAKING:
                breaking.append(change.description)
                kind = ChangeKind.FIX  # also list under fixes
            sections.setdefault(kind, []).append(
                f"{change.description}" + (f" ({change.component})" if change.component else "")
            )
        notes = ReleaseNotes(
            version=version,
            date=date,
            sections=sections,
            breaking_changes=breaking,
            total_changes=len(self._changes),
        )
        return notes

    def release_check(self) -> dict[str, Any]:
        """Validate release readiness."""
        issues: list[str] = []
        if not self._changes:
            issues.append("no changefiles found — user-visible changes require a changefile")
        for c in self._changes:
            if not c.description:
                issues.append(f"changefile {c.filename} has empty description")
            if c.kind not in [ChangeKind.FEATURE, ChangeKind.FIX, ChangeKind.MAINTENANCE, ChangeKind.SAFETY, ChangeKind.DOCS, ChangeKind.BREAKING]:
                issues.append(f"changefile {c.filename} has unknown kind: {c.kind}")
        return {
            "ready": len(issues) == 0,
            "issues": issues,
            "change_count": len(self._changes),
            "required_bump": self.required_version_bump(),
        }

    def clear_released(self) -> None:
        """Clear changes after generating release notes."""
        self._changes.clear()


def write_change_file(directory: Path, name: str, kind: str, description: str, component: str = "", author: str = "") -> Path:
    """Write a change file to disk."""
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "kind": kind,
        "description": description,
        "component": component,
        "author": author,
        "created_at": datetime.now().isoformat(),
        "version_bump": "major" if kind == ChangeKind.BREAKING else "minor" if kind == ChangeKind.FEATURE else "patch",
    }
    path = directory / f"{name}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path