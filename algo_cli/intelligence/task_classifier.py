"""B84. Task Classifier + Agent Delegator.

Classify tasks by complexity/domain.  Route to appropriate agents.
Source: CCASP pattern.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class TaskComplexity(Enum):
    TRIVIAL = auto()     # read, list, search — cheap model
    SIMPLE = auto()     # single edit, single test — standard model
    MODERATE = auto()    # multi-file edit, fix bug — standard model
    COMPLEX = auto()    # refactor, new feature — expensive model
    CRITICAL = auto()   # architecture, security — expensive model + review


class TaskDomain(Enum):
    CODE = auto()
    RESEARCH = auto()
    MAINTENANCE = auto()
    DOCUMENTATION = auto()
    TESTING = auto()
    DEPLOYMENT = auto()
    UNKNOWN = auto()


@dataclass
class TaskClassification:
    complexity: TaskComplexity
    domain: TaskDomain
    confidence: float = 0.5
    suggested_model: str = ""
    suggested_agents: list[str] = field(default_factory=list)
    estimated_steps: int = 1


COMPLEXITY_KEYWORDS: dict[TaskComplexity, list[str]] = {
    TaskComplexity.TRIVIAL: ["read", "list", "show", "find", "search", "grep", "where"],
    TaskComplexity.SIMPLE: ["fix", "update", "rename", "add", "remove", "change"],
    TaskComplexity.MODERATE: ["refactor", "migrate", "convert", "split", "merge"],
    TaskComplexity.COMPLEX: ["implement", "design", "architect", "create new", "build"],
    TaskComplexity.CRITICAL: ["security", "deploy", "release", "migration", "rewrite"],
}

DOMAIN_KEYWORDS: dict[TaskDomain, list[str]] = {
    TaskDomain.CODE: ["function", "class", "method", "variable", "import", "module"],
    TaskDomain.RESEARCH: ["research", "analyze", "compare", "investigate", "survey"],
    TaskDomain.MAINTENANCE: ["maintain", "clean", "update", "upgrade", "fix"],
    TaskDomain.DOCUMENTATION: ["document", "readme", "doc", "comment", "explain"],
    TaskDomain.TESTING: ["test", "spec", "coverage", "mock", "fixture"],
    TaskDomain.DEPLOYMENT: ["deploy", "release", "publish", "ci", "cd", "build"],
}

MODEL_BY_COMPLEXITY: dict[TaskComplexity, str] = {
    TaskComplexity.TRIVIAL: "qwen3:4b",
    TaskComplexity.SIMPLE: "glm-5.2",
    TaskComplexity.MODERATE: "glm-5.2",
    TaskComplexity.COMPLEX: "gpt-5.5",
    TaskComplexity.CRITICAL: "gpt-5.5",
}


class TaskClassifier:
    """Classify tasks by complexity and domain."""

    def classify(self, task_description: str) -> TaskClassification:
        desc_lower = task_description.lower()

        # Determine complexity
        complexity_scores: dict[TaskComplexity, int] = {}
        for comp, keywords in COMPLEXITY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in desc_lower)
            complexity_scores[comp] = score

        complexity = max(complexity_scores, key=complexity_scores.get)
        if complexity_scores[complexity] == 0:
            complexity = TaskComplexity.SIMPLE  # default

        # Determine domain
        domain_scores: dict[TaskDomain, int] = {}
        for domain, keywords in DOMAIN_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in desc_lower)
            domain_scores[domain] = score

        domain = max(domain_scores, key=domain_scores.get)
        if domain_scores[domain] == 0:
            domain = TaskDomain.UNKNOWN

        # Estimate steps
        step_count = desc_lower.count(" and ") + desc_lower.count(" then ") + 1
        step_count = min(step_count, 10)

        # Confidence based on keyword match strength
        total_matches = sum(complexity_scores.values()) + sum(domain_scores.values())
        confidence = min(1.0, total_matches / 5.0)

        return TaskClassification(
            complexity=complexity,
            domain=domain,
            confidence=confidence,
            suggested_model=MODEL_BY_COMPLEXITY[complexity],
            estimated_steps=step_count,
        )


class AgentDelegator:
    """Route classified tasks to appropriate agents."""

    def __init__(self) -> None:
        self._handlers: dict[TaskDomain, Callable[[str, TaskClassification], str]] = {}
        self._classifier = TaskClassifier()

    def register_handler(self, domain: TaskDomain,
                          handler: Callable[[str, TaskClassification], str]) -> None:
        self._handlers[domain] = handler

    def delegate(self, task: str) -> tuple[TaskClassification, str]:
        """Classify and delegate a task. Returns (classification, result)."""
        classification = self._classifier.classify(task)
        handler = self._handlers.get(classification.domain)
        if handler:
            result = handler(task, classification)
        else:
            result = f"No handler for domain {classification.domain.name}"
        return classification, result