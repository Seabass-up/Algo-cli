"""Scoped tool policy selection for routed Agent Block runs."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from . import tools as tools_module
from .marcus_authority import Capability, ConfirmationReceipt, ConsentGrant, ResolvedAction
from .samuel_policy_engine import PolicyDisposition, evaluate_action, resolve_action
from .task_router import TaskRoute


MUTATING_TOOLS = frozenset({"write_file", "edit_file", "batch_edit", "run_shell"})
WRITE_TOOLS = frozenset({"write_file", "edit_file", "batch_edit"})
SHELL_TOOLS = frozenset({"run_shell"})

TOOL_GROUP_MAP: dict[str, frozenset[str]] = {
    "read": frozenset(
        {
            "read_file",
            "read_pdf",
            "render_pdf_pages",
            "list_directory",
            "search_files",
            "git_status",
            "git_diff",
            "available_actions",
            "session_slash",
            "harness_stats",
            "harness_search",
            "harness_read",
            "query_knowledge_graph",
            "model_show",
        }
    ),
    "web": frozenset({"web_search", "web_fetch", "x_search"}),
    "write": WRITE_TOOLS,
    "shell": SHELL_TOOLS,
}


def expand_tool_groups(groups: list[str] | tuple[str, ...]) -> frozenset[str]:
    tools: set[str] = set()
    for group in groups:
        tools.update(TOOL_GROUP_MAP[group])
    return frozenset(tools)


@dataclass(frozen=True)
class ToolPolicyDecision:
    """Prospective policy decision; advisory until enforcement is enabled."""

    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    approval_required: frozenset[str]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ShellMutationDecision:
    """Mutation-policy decision for a shell command attempted by an Agent Block."""

    is_mutation: bool
    blocked: bool
    force_approval: bool
    reason: str | None = None


@dataclass(frozen=True)
class RuntimeToolPolicyDecision:
    """Target-bound policy result for a model-invoked runtime tool."""

    tool_name: str
    action: ResolvedAction
    disposition: PolicyDisposition
    capability_mask: int
    capability_names: tuple[str, ...]
    reasons: tuple[str, ...]
    fired_rules: tuple[str, ...]
    grant_id: str = ""
    confirmation_receipt_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.disposition is PolicyDisposition.ALLOW

    @property
    def eligible(self) -> bool:
        return self.disposition in {PolicyDisposition.ALLOW, PolicyDisposition.CONFIRM}

    @property
    def tier(self) -> str:
        """Compatibility label; tiers no longer grant runtime authority."""

        return "scoped"


def evaluate_runtime_tool_policy(
    tool_name: str,
    args: dict[str, Any],
    *,
    safe_mode: bool,
    cwd: str = ".",
    grant: ConsentGrant | None = None,
    confirmation: ConfirmationReceipt | None = None,
    now: float | None = None,
    auto_approve: bool = False,
) -> RuntimeToolPolicyDecision:
    """Evaluate registration, safe shell, and externally supplied authority."""

    action = resolve_action(tool_name, args, cwd=cwd)
    fired_rules: list[str] = []
    reasons: list[str] = []
    if tool_name not in tools_module.TOOL_MAP:
        fired_rules.append("tool_registered")
        reasons.append(f"unknown runtime tool: {tool_name}")
        disposition = PolicyDisposition.DENY
    elif (
        tool_name == "run_shell"
        and safe_mode
        and tools_module.shell_is_dangerous(str(args.get("command") or ""))
    ):
        fired_rules.append("safe_shell")
        reasons.append("safe mode blocks destructive commands and shell mutations")
        disposition = PolicyDisposition.DENY
    else:
        decision = evaluate_action(
            action,
            grant=grant,
            confirmation=confirmation,
            now=time.time() if now is None else now,
            auto_approve=auto_approve,
        )
        disposition = decision.disposition
        reasons.append(decision.reason)
        if disposition is not PolicyDisposition.ALLOW:
            fired_rules.append("scoped_authority")
    return RuntimeToolPolicyDecision(
        tool_name=tool_name,
        action=action,
        disposition=disposition,
        capability_mask=action.capability_mask,
        capability_names=tuple(
            capability.name.casefold()
            for capability in Capability
            if action.capability_mask & capability.value
        ),
        reasons=tuple(reasons),
        fired_rules=tuple(fired_rules),
        grant_id=grant.grant_id if grant is not None else "",
        confirmation_receipt_id=confirmation.receipt_id if confirmation is not None else "",
    )


def evaluate_shell_command(
    command: str,
    *,
    requires_change: bool,
    safe_mode: bool,
) -> ShellMutationDecision:
    """Apply required-change shell mutation policy to one command."""

    if not requires_change:
        return ShellMutationDecision(False, False, False)
    if not tools_module.shell_mutates_workspace(command):
        return ShellMutationDecision(False, False, False)
    reason = (
        "file edits must use edit_file or write_file; direct file or Git mutations through "
        "run_shell require explicit approval"
    )
    return ShellMutationDecision(
        is_mutation=True,
        blocked=safe_mode,
        force_approval=not safe_mode,
        reason=reason,
    )


def requires_explicit_approval(
    tool_name: str,
    *,
    block_policy: ToolPolicyDecision,
    shell_decision: ShellMutationDecision,
    policy_enforced: bool,
) -> bool:
    """Resolve all Agent Block policy sources that require an approval prompt."""

    return shell_decision.force_approval or (
        policy_enforced and tool_name in block_policy.approval_required
    )


def supports_mutation_audit(block_tools: frozenset[str]) -> bool:
    """Return whether a block can execute a workspace mutation tool."""

    return bool(block_tools & MUTATING_TOOLS)


def describes_mutation_action(tool_name: str, args: dict[str, object]) -> str | None:
    """Describe a model-invoked action whose effects should be audited."""

    if tool_name == "write_file":
        path = str(args.get("path", "")).strip() or "(unspecified path)"
        return f"write_file: {path}"
    if tool_name == "edit_file":
        path = str(args.get("path", "")).strip() or "(unspecified path)"
        return f"edit_file: {path}"
    if tool_name == "batch_edit":
        path = str(args.get("path", "")).strip() or "(unspecified path)"
        return f"batch_edit: {path}"
    if tool_name == "run_shell":
        command = str(args.get("command", "")).strip()
        if tools_module.shell_mutates_workspace(command):
            compact = " ".join(command.split())
            return f"run_shell: {compact[:160]}"
    return None


def _route_ceiling(route: TaskRoute, block_tools: frozenset[str]) -> frozenset[str]:
    if route.recommended_mode != "agent" or not route.allowed_tool_groups:
        return block_tools
    return expand_tool_groups(route.allowed_tool_groups)


def compute_policy(
    route: TaskRoute,
    block_role: str,
    block_tools: frozenset[str],
    safe_mode: bool,
    auto_mode: bool,
) -> ToolPolicyDecision:
    """Return the advisory effective tool policy for one block."""
    allowed = block_tools & _route_ceiling(route, block_tools)
    reasons: list[str] = []

    denied_by_route = block_tools - allowed
    if denied_by_route:
        reasons.append("route tool groups restrict this block")

    if route.risk == "high":
        denied = allowed & MUTATING_TOOLS
        allowed -= denied
        if denied:
            reasons.append("high risk denies write and shell tools")

    if route.task_type == "review":
        denied = allowed & MUTATING_TOOLS
        allowed -= denied
        if denied:
            reasons.append("review tasks are read-only")
    elif route.task_type == "research":
        denied = allowed & WRITE_TOOLS
        allowed -= denied
        if denied:
            reasons.append("research tasks deny writes")

    approval_required: frozenset[str] = frozenset()
    if route.risk == "medium" and not auto_mode:
        approval_required = allowed & MUTATING_TOOLS
        if approval_required:
            reasons.append("medium risk requires approval for mutating tools")
    if safe_mode and "run_shell" in allowed:
        reasons.append("safe mode remains active inside shell execution")

    denied_tools = block_tools - allowed
    if not reasons:
        reasons.append(f"{block_role} block policy unchanged")
    return ToolPolicyDecision(
        allowed_tools=allowed,
        denied_tools=denied_tools,
        approval_required=approval_required,
        reasons=tuple(reasons),
    )


def format_policy_summary(decision: ToolPolicyDecision) -> str:
    allowed = ", ".join(sorted(decision.allowed_tools)) or "none"
    text = f"tools: {allowed}"
    if decision.approval_required:
        text += f" (approval: {', '.join(sorted(decision.approval_required))})"
    if decision.denied_tools:
        text += f"; denied: {', '.join(sorted(decision.denied_tools))}"
    return text
