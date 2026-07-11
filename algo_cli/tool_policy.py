"""Advisory tool policy selection for routed Agent Block runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import tools as tools_module
from ._internal.policy_chain import CheckResult, Control, evaluate_chain
from .capability_mask import Capability, CapabilityMask, tier_mask
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
    """Composable preflight policy for every model-invoked runtime tool."""

    tool_name: str
    tier: str
    capability_mask: int
    capability_names: tuple[str, ...]
    allowed: bool
    reasons: tuple[str, ...]
    fired_rules: tuple[str, ...]


_NETWORK_TOOLS = frozenset({
    "web_search",
    "web_fetch",
    "x_search",
    "x_account_status",
    "x_account_draft_post",
    "x_account_draft_reply",
    "x_account_post",
    "x_account_reply",
    "x_account_post_action",
    "model_pull",
    "model_create",
})
_MODEL_TOOLS = frozenset({
    "model_show",
    "model_pull",
    "model_copy",
    "model_create",
    "model_delete",
    "embed_text",
    "vision_describe",
})
_CREDENTIAL_TOOLS = frozenset({"credential_helpers_get", "credential_helpers_store"})
_MEMORY_TOOLS = frozenset({"remember", "append_lesson", "write_knowledge_graph_note"})
_EXTERNAL_PUBLISH_TOOLS = frozenset({"x_account_post", "x_account_reply", "x_account_post_action"})
_DESTRUCTIVE_TOOLS = frozenset({"model_delete"})
_STATE_WRITE_TOOLS = WRITE_TOOLS | frozenset({
    "update_user_profile",
    "plugins_load",
    "credential_helpers_store",
    "session_command",
    "model_pull",
    "model_copy",
    "model_create",
    "model_delete",
    "harness_refresh",
    "reindex_knowledge_graph",
    "write_knowledge_graph_note",
    "remember",
    "append_lesson",
    "x_account_post",
    "x_account_reply",
    "x_account_post_action",
})


def tool_capability_mask(tool_name: str) -> CapabilityMask:
    """Return the stable structural capabilities required by a runtime tool."""

    mask = CapabilityMask().add(Capability.READ)
    if tool_name in _STATE_WRITE_TOOLS:
        mask = mask.add(Capability.WRITE)
    if tool_name == "run_shell":
        mask = mask.add(Capability.SHELL)
    if tool_name in _NETWORK_TOOLS:
        mask = mask.add(Capability.NETWORK)
    if tool_name in _MODEL_TOOLS:
        mask = mask.add(Capability.MODEL)
    if tool_name in _CREDENTIAL_TOOLS:
        mask = mask.add(Capability.CREDENTIAL)
    if tool_name in _MEMORY_TOOLS:
        mask = mask.add(Capability.MEMORY)
    if tool_name in _EXTERNAL_PUBLISH_TOOLS:
        mask = mask.add(Capability.EXTERNAL_PUBLISH)
    if tool_name in _DESTRUCTIVE_TOOLS:
        mask = mask.add(Capability.DESTRUCTIVE)
    return mask


def tool_capability_tier(mask: CapabilityMask) -> str:
    """Choose the least-privileged tier that contains every required capability."""

    for tier in ("tier0", "tier1", "tier2", "tier3"):
        if mask.value & ~tier_mask(tier) == 0:
            return tier
    return "tier3"


def evaluate_runtime_tool_policy(
    tool_name: str,
    args: dict[str, Any],
    *,
    safe_mode: bool,
) -> RuntimeToolPolicyDecision:
    """Run the policy-chain and capability-mask preflight for a tool call."""

    mask = tool_capability_mask(tool_name)
    tier = tool_capability_tier(mask)

    def registered_check(call: dict[str, Any], _context: dict[str, Any]) -> CheckResult:
        registered = call["tool"] in tools_module.TOOL_MAP
        return CheckResult(
            name="tool_registered",
            passed=registered,
            reason="" if registered else f"unknown runtime tool: {call['tool']}",
        )

    def capability_check(call: dict[str, Any], _context: dict[str, Any]) -> CheckResult:
        required = int(call["capability_mask"])
        ceiling = tier_mask(str(call["tier"]))
        passed = required & ~ceiling == 0
        return CheckResult(
            name="capability_tier",
            passed=passed,
            reason="" if passed else f"capability mask {required} exceeds {call['tier']}",
        )

    def safe_shell_check(call: dict[str, Any], context: dict[str, Any]) -> CheckResult:
        blocked = (
            call["tool"] == "run_shell"
            and bool(context.get("safe_mode"))
            and tools_module.shell_is_dangerous(str(call.get("args", {}).get("command", "")))
        )
        return CheckResult(
            name="safe_shell",
            passed=not blocked,
            reason="safe mode blocks destructive commands and shell mutations" if blocked else "",
        )

    chain = (
        (Control.REQUISITE, registered_check),
        (Control.REQUIRED, capability_check),
        (Control.REQUISITE, safe_shell_check),
    )
    chain_decision = evaluate_chain(
        "runtime-tool",
        chain,
        {
            "tool": tool_name,
            "args": dict(args),
            "tier": tier,
            "capability_mask": mask.value,
        },
        {"safe_mode": safe_mode},
    )
    return RuntimeToolPolicyDecision(
        tool_name=tool_name,
        tier=tier,
        capability_mask=mask.value,
        capability_names=mask.names(),
        allowed=chain_decision.passed,
        reasons=chain_decision.reasons,
        fired_rules=tuple(chain_decision.fired_rules()),
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

    approval_required = frozenset()
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
