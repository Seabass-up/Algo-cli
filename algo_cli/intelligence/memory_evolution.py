"""B56. Memory Skill Evolution.

Learn reusable memory skills from task feedback.  Evolve from hard cases.
Source: MemSkill pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto


class SkillStatus(Enum):
    CANDIDATE = auto()
    ACTIVE = auto()
    DEPRECATED = auto()
    EVOLVED = auto()


@dataclass
class MemorySkill:
    name: str
    pattern: str  # what to remember
    trigger: str  # when to apply
    confidence: float = 0.5
    uses: int = 0
    successes: int = 0
    failures: int = 0
    status: SkillStatus = SkillStatus.CANDIDATE
    created_at: float = field(default_factory=time.time)
    evolved_from: str | None = None
    hard_cases: list[str] = field(default_factory=list)


@dataclass
class TaskFeedback:
    task: str
    memory_used: list[str]  # skill names
    outcome: str  # "success", "failure", "partial"
    missing_info: str = ""  # what was missing?
    wrong_info: str = ""  # what was wrong/stale?


class MemorySkillEvolver:
    """Evolve memory skills from task feedback."""

    def __init__(self, confidence_threshold: float = 0.7,
                 deprecation_threshold: float = 0.3) -> None:
        self._skills: dict[str, MemorySkill] = {}
        self._feedback_history: list[TaskFeedback] = []
        self._confidence_threshold = confidence_threshold
        self._deprecation_threshold = deprecation_threshold

    def register(self, skill: MemorySkill) -> None:
        self._skills[skill.name] = skill

    def record_feedback(self, feedback: TaskFeedback) -> None:
        self._feedback_history.append(feedback)
        for skill_name in feedback.memory_used:
            skill = self._skills.get(skill_name)
            if not skill:
                continue
            skill.uses += 1
            if feedback.outcome == "success":
                skill.successes += 1
            elif feedback.outcome == "failure":
                skill.failures += 1
                if feedback.wrong_info:
                    skill.hard_cases.append(feedback.wrong_info)

    def _confidence(self, skill: MemorySkill) -> float:
        if skill.uses == 0:
            return skill.confidence
        return skill.successes / skill.uses

    def evolve(self) -> list[MemorySkill]:
        """Promote/deprecate/evolve skills based on feedback."""
        evolved: list[MemorySkill] = []
        for skill in list(self._skills.values()):
            conf = self._confidence(skill)
            if conf >= self._confidence_threshold and skill.status == SkillStatus.CANDIDATE:
                skill.status = SkillStatus.ACTIVE
                evolved.append(skill)
            elif conf < self._deprecation_threshold and skill.status == SkillStatus.ACTIVE:
                skill.status = SkillStatus.DEPRECATED
                evolved.append(skill)
            elif skill.hard_cases and skill.status == SkillStatus.ACTIVE:
                # Evolve: create a refined version
                evolved_name = f"{skill.name}_v2"
                if evolved_name not in self._skills:
                    evolved_skill = MemorySkill(
                        name=evolved_name,
                        pattern=skill.pattern,
                        trigger=skill.trigger,
                        confidence=0.5,
                        status=SkillStatus.CANDIDATE,
                        evolved_from=skill.name,
                        hard_cases=list(skill.hard_cases),
                    )
                    self._skills[evolved_name] = evolved_skill
                    skill.status = SkillStatus.EVOLVED
                    evolved.append(evolved_skill)
        return evolved

    def mine_hard_cases(self) -> list[dict]:
        """Find cases where memory was wrong or missing."""
        cases: list[dict] = []
        for fb in self._feedback_history:
            if fb.wrong_info:
                cases.append({"type": "wrong", "info": fb.wrong_info, "task": fb.task})
            if fb.missing_info:
                cases.append({"type": "missing", "info": fb.missing_info, "task": fb.task})
        return cases

    @property
    def skills(self) -> dict[str, MemorySkill]:
        return dict(self._skills)