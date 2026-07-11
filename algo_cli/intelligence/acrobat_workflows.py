"""B157-B158: Acrobat-derived workflow and policy profile patterns.

- B157: Declarative Workflow Sequence Engine
- B158: Named Policy Profile Packs
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


# ── B157: Declarative Workflow Sequence Engine ────────────────────────


@dataclass
class WorkflowItem:
    """A typed parameter for a workflow command."""
    name: str
    item_type: str  # "boolean", "integer", "text"
    value: Any = None


@dataclass
class WorkflowInstruction:
    """An instruction step in a workflow."""
    label: str


@dataclass
class WorkflowCommand:
    """A command step in a workflow."""
    name: str
    prompt_user: bool = False
    pause_before: bool = False
    items: list[WorkflowItem] = field(default_factory=list)


@dataclass
class WorkflowGroup:
    """A group of steps in a workflow."""
    label: str
    steps: list[WorkflowInstruction | WorkflowCommand | "WorkflowSeparator"] = field(default_factory=list)


@dataclass
class WorkflowSeparator:
    """A visual separator in a workflow."""
    pass


@dataclass
class WorkflowDefinition:
    """A declarative workflow definition (B157)."""
    title: str
    description: str = ""
    major_version: int = 1
    minor_version: int = 0
    groups: list[WorkflowGroup] = field(default_factory=list)

    def all_command_names(self) -> list[str]:
        """Extract all command names from the workflow."""
        names: list[str] = []
        for group in self.groups:
            for step in group.steps:
                if isinstance(step, WorkflowCommand):
                    names.append(step.name)
        return names

    def validate(self, command_registry: set[str]) -> list[str]:
        """Validate workflow against a command registry. Returns errors."""
        errors: list[str] = []
        for name in self.all_command_names():
            if name not in command_registry:
                errors.append(f"unknown command: {name}")
        # Validate typed items
        for group in self.groups:
            for step in group.steps:
                if isinstance(step, WorkflowCommand):
                    for item in step.items:
                        if item.item_type == "boolean" and not isinstance(item.value, bool | type(None)):
                            if item.value is not None:
                                errors.append(f"bad boolean value for {item.name}: {item.value}")
                        elif item.item_type == "integer" and item.value is not None:
                            if not isinstance(item.value, int):
                                errors.append(f"bad integer value for {item.name}: {item.value}")
        return errors


@dataclass
class WorkflowStepResult:
    """Result of executing a single workflow step."""
    step_name: str
    success: bool = True
    output: Any = None
    skipped: bool = False
    error: str = ""


@dataclass
class WorkflowExecutionResult:
    """Result of executing an entire workflow."""
    title: str
    steps: list[WorkflowStepResult] = field(default_factory=list)
    completed: bool = False
    paused_at: str = ""

    @property
    def failed_steps(self) -> list[WorkflowStepResult]:
        return [s for s in self.steps if not s.success and not s.skipped]


class WorkflowExecutor:
    """Executes declarative workflows (B157)."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[dict], Any]] = {}

    def register_command(self, name: str, handler: Callable[[dict], Any]) -> None:
        self._handlers[name] = handler

    def validate(self, workflow: WorkflowDefinition) -> list[str]:
        """Validate a workflow before execution."""
        return workflow.validate(set(self._handlers.keys()))

    def execute(self, workflow: WorkflowDefinition, context: dict | None = None) -> WorkflowExecutionResult:
        """Execute a workflow step by step."""
        ctx = context or {}
        result = WorkflowExecutionResult(title=workflow.title)
        errors = self.validate(workflow)
        if errors:
            result.steps.append(WorkflowStepResult(
                step_name="validation",
                success=False,
                error="; ".join(errors),
            ))
            return result

        for group in workflow.groups:
            for step in group.steps:
                if isinstance(step, WorkflowInstruction | WorkflowSeparator):
                    continue
                if isinstance(step, WorkflowCommand):
                    if step.pause_before:
                        result.paused_at = step.name
                        return result
                    handler = self._handlers.get(step.name)
                    if not handler:
                        result.steps.append(WorkflowStepResult(
                            step_name=step.name,
                            success=False,
                            error=f"no handler: {step.name}",
                        ))
                        return result
                    try:
                        item_dict = {item.name: item.value for item in step.items}
                        output = handler({**ctx, **item_dict})
                        result.steps.append(WorkflowStepResult(
                            step_name=step.name,
                            success=True,
                            output=output,
                        ))
                    except Exception as e:
                        result.steps.append(WorkflowStepResult(
                            step_name=step.name,
                            success=False,
                            error=str(e),
                        ))
                        return result
        result.completed = True
        return result


# ── B158: Named Policy Profile Packs ──────────────────────────────────


@dataclass
class PolicyProfile:
    """A named policy profile (B158)."""
    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] = field(default_factory=dict)
    source: str = "shipped"  # "shipped" or "user"

    def resolve(self, defaults: dict[str, Any]) -> dict[str, Any]:
        """Resolve profile against defaults, applying overrides."""
        result = dict(defaults)
        result.update(self.parameters)
        result.update(self.overrides)
        return result


class PolicyProfilePack:
    """A collection of named policy profiles (B158)."""

    def __init__(self) -> None:
        self._profiles: dict[str, PolicyProfile] = {}
        self._defaults: dict[str, Any] = {}

    def set_defaults(self, defaults: dict[str, Any]) -> None:
        self._defaults = dict(defaults)

    def register(self, profile: PolicyProfile) -> None:
        self._profiles[profile.name] = profile

    def get(self, name: str) -> PolicyProfile | None:
        return self._profiles.get(name)

    def resolve(self, name: str) -> dict[str, Any] | None:
        """Resolve a profile by name."""
        profile = self._profiles.get(name)
        if not profile:
            return None
        return profile.resolve(self._defaults)

    def available_profiles(self) -> list[str]:
        return list(self._profiles.keys())

    def add_user_override(self, profile_name: str, key: str, value: Any) -> bool:
        """Add a user override to a profile."""
        profile = self._profiles.get(profile_name)
        if not profile:
            return False
        profile.overrides[key] = value
        return True