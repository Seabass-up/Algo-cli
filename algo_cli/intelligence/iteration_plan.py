"""B39. Iteration Plan + Endgame Checklist Cadence (VS Code Project Pattern).

Manages monthly iteration plans and release endgame checklists so
development stays disciplined as features accumulate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class CheckState(Enum):
    UNCHECKED = "unchecked"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class ChecklistItem:
    name: str
    description: str = ""
    state: CheckState = CheckState.UNCHECKED
    blocker: bool = False  # if True, failed state blocks release


@dataclass
class IterationPlan:
    month: str  # "YYYY-MM"
    goals: list[str] = field(default_factory=list)
    work_items: list[dict[str, str]] = field(default_factory=list)  # {label, kind, status}
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class EndgameChecklist:
    release_version: str
    items: list[ChecklistItem] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def add_item(self, name: str, description: str = "", blocker: bool = False) -> None:
        self.items.append(ChecklistItem(name=name, description=description, blocker=blocker))

    def mark(self, name: str, state: CheckState) -> bool:
        for item in self.items:
            if item.name == name:
                item.state = state
                return True
        return False

    def release_ready(self) -> bool:
        """True if no blocker item is failed or unchecked."""
        for item in self.items:
            if item.blocker and item.state != CheckState.PASSED:
                return False
        return True

    def blockers(self) -> list[ChecklistItem]:
        """Return unresolved blocker items."""
        return [i for i in self.items if i.blocker and i.state != CheckState.PASSED]

    def summary(self) -> dict[str, Any]:
        total = len(self.items)
        passed = sum(1 for i in self.items if i.state == CheckState.PASSED)
        failed = sum(1 for i in self.items if i.state == CheckState.FAILED)
        unchecked = sum(1 for i in self.items if i.state == CheckState.UNCHECKED)
        blocked = sum(1 for i in self.items if i.state == CheckState.BLOCKED)
        return {
            "release": self.release_version,
            "total": total,
            "passed": passed,
            "failed": failed,
            "unchecked": unchecked,
            "blocked": blocked,
            "release_ready": self.release_ready(),
            "blockers": [b.name for b in self.blockers()],
        }


def default_endgame_checklist(version: str) -> EndgameChecklist:
    """Create a standard release endgame checklist."""
    cl = EndgameChecklist(release_version=version)
    cl.add_item("tests_pass", "All tests pass (pytest -q)", blocker=True)
    cl.add_item("no_skip_markers", "No new pytest.skip markers without justification", blocker=False)
    cl.add_item("help_updated", "/help text updated for new commands", blocker=True)
    cl.add_item("docs_updated", "ALGO.md and README updated", blocker=False)
    cl.add_item("migration_notes", "Migration notes for breaking changes", blocker=True)
    cl.add_item("safety_gates", "Safety gates verified (external send, delete, payments)", blocker=True)
    cl.add_item("rollback_plan", "Rollback plan documented", blocker=False)
    cl.add_item("changelog", "Changelog/changefiles written", blocker=False)
    return cl


class IterationPlanManager:
    """Manages iteration plans and endgame checklists."""

    def __init__(self) -> None:
        self.plans: dict[str, IterationPlan] = {}
        self.checklists: dict[str, EndgameChecklist] = {}

    def create_plan(self, month: str, goals: list[str] | None = None) -> IterationPlan:
        plan = IterationPlan(month=month, goals=goals or [])
        self.plans[month] = plan
        return plan

    def add_work_item(self, month: str, label: str, kind: str, status: str = "open") -> bool:
        plan = self.plans.get(month)
        if not plan:
            return False
        plan.work_items.append({"label": label, "kind": kind, "status": status})
        return True

    def create_checklist(self, version: str) -> EndgameChecklist:
        cl = default_endgame_checklist(version)
        self.checklists[version] = cl
        return cl

    def current_plan(self) -> IterationPlan | None:
        if not self.plans:
            return None
        return self.plans[max(self.plans.keys())]

    def roadmap_summary(self) -> dict[str, Any]:
        current = self.current_plan()
        return {
            "current_month": current.month if current else None,
            "goals": current.goals if current else [],
            "open_items": sum(1 for w in (current.work_items if current else []) if w.get("status") == "open"),
            "checklists": {v: cl.summary() for v, cl in self.checklists.items()},
        }