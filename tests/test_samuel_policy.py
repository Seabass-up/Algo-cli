from __future__ import annotations

from algo_cli import agent_blocks, samuel_policy as tool_policy, task_router
from algo_cli.marcus_authority import ConfirmationMode, ConfirmationReceipt, ConsentGrant
from algo_cli.samuel_policy_engine import PolicyDisposition, resolve_action


def _grant(action, *, now: float = 1.0) -> ConsentGrant:
    return ConsentGrant(
        grant_id="test-grant",
        capability_mask=action.capability_mask,
        allowed_actions=frozenset({action.name}),
        allowed_targets=frozenset({action.target}),
        expires_at=now + 60.0,
        maximum_action_count=1,
        issued_at=now,
        source="test",
    )


def _confirmation(action, *, now: float = 1.0) -> ConfirmationReceipt:
    return ConfirmationReceipt(
        receipt_id="test-confirmation",
        action_digest=action.action_digest,
        confirmation_mode=ConfirmationMode.ACTION_TIME,
        confirmed_at=now,
        expires_at=now + 60.0,
    )


def test_tool_groups_expand_only_registered_policy_tools():
    tools = tool_policy.expand_tool_groups(["read", "web", "write", "shell"])

    assert "read_file" in tools
    assert "git_status" in tools
    assert "git_diff" in tools
    assert "web_search" in tools
    assert "write_file" in tools
    assert "run_shell" in tools
    assert "delete_file" not in tools


def test_read_group_excludes_knowledge_graph_writers():
    tools = tool_policy.expand_tool_groups(["read"])

    assert "write_knowledge_graph_note" not in tools
    assert "reindex_knowledge_graph" not in tools


def test_default_policy_preserves_block_tools():
    route = task_router.TaskRoute(
        task_type="general",
        complexity="low",
        recommended_mode="chat",
        suggested_pipeline="default",
        allowed_tool_groups=(),
        risk="low",
        reason="test",
    )

    decision = tool_policy.compute_policy(route, "implement", agent_blocks.IMPLEMENT_TOOLS, False, False)

    assert decision.allowed_tools == agent_blocks.IMPLEMENT_TOOLS
    assert decision.denied_tools == frozenset()
    assert decision.approval_required == frozenset()


def test_high_risk_policy_denies_write_and_shell():
    route = task_router.route_task("Fix credential deletion handling")

    decision = tool_policy.compute_policy(route, "implement", agent_blocks.IMPLEMENT_TOOLS, False, False)

    assert "write_file" not in decision.allowed_tools
    assert "run_shell" not in decision.allowed_tools
    assert {"write_file", "run_shell"} <= decision.denied_tools


def test_medium_risk_requires_mutating_approval_when_auto_is_off():
    route = task_router.route_task("Fix the failing test")

    decision = tool_policy.compute_policy(route, "implement", agent_blocks.IMPLEMENT_TOOLS, False, False)

    assert decision.approval_required == frozenset({"write_file", "edit_file", "batch_edit", "run_shell"})


def test_auto_mode_skips_medium_risk_extra_approval():
    route = task_router.route_task("Fix the failing test")

    decision = tool_policy.compute_policy(route, "implement", agent_blocks.IMPLEMENT_TOOLS, False, True)

    assert decision.approval_required == frozenset()


def test_safe_mode_preserves_medium_risk_approval_and_shell_guards():
    route = task_router.route_task("Fix the failing test")

    decision = tool_policy.compute_policy(route, "implement", agent_blocks.IMPLEMENT_TOOLS, True, False)

    assert {"write_file", "edit_file", "batch_edit", "run_shell"} <= decision.allowed_tools
    assert decision.approval_required == frozenset({"write_file", "edit_file", "batch_edit", "run_shell"})
    assert any("safe mode remains active" in reason for reason in decision.reasons)


def test_review_task_denies_mutating_tools_even_if_a_block_exposes_them():
    route = task_router.route_task("Review this implementation")

    decision = tool_policy.compute_policy(route, "review", agent_blocks.IMPLEMENT_TOOLS, False, False)

    assert "write_file" not in decision.allowed_tools
    assert "run_shell" not in decision.allowed_tools


def test_allowed_tools_never_exceed_original_block_tools():
    route = task_router.route_task("Research the latest framework release")
    block_tools = frozenset({"read_file", "web_search", "write_file", "run_shell"})

    decision = tool_policy.compute_policy(route, "research", block_tools, False, False)

    assert decision.allowed_tools <= block_tools
    assert "write_file" not in decision.allowed_tools


def test_shell_policy_ignores_non_change_blocks():
    decision = tool_policy.evaluate_shell_command("git add file.py", requires_change=False, safe_mode=True)

    assert decision.is_mutation is False
    assert decision.blocked is False
    assert decision.force_approval is False


def test_shell_policy_blocks_change_block_mutation_in_safe_mode():
    decision = tool_policy.evaluate_shell_command("git add file.py", requires_change=True, safe_mode=True)

    assert decision.is_mutation is True
    assert decision.blocked is True
    assert decision.force_approval is False
    assert "write_file" in str(decision.reason)


def test_shell_policy_requires_approval_for_mutation_outside_safe_mode():
    decision = tool_policy.evaluate_shell_command("Set-Content x y", requires_change=True, safe_mode=False)

    assert decision.is_mutation is True
    assert decision.blocked is False
    assert decision.force_approval is True


def test_shell_policy_allows_verification_in_change_block():
    decision = tool_policy.evaluate_shell_command("python -m pytest -q", requires_change=True, safe_mode=True)

    assert decision.is_mutation is False
    assert decision.blocked is False
    assert decision.force_approval is False


def test_explicit_approval_applies_enforced_block_policy():
    route = task_router.route_task("Fix the failing test")
    block_policy = tool_policy.compute_policy(route, "implement", agent_blocks.IMPLEMENT_TOOLS, False, False)
    shell_decision = tool_policy.evaluate_shell_command("python -m pytest -q", requires_change=True, safe_mode=False)

    assert tool_policy.requires_explicit_approval(
        "write_file",
        block_policy=block_policy,
        shell_decision=shell_decision,
        policy_enforced=True,
    )


def test_explicit_approval_applies_shell_mutation_even_without_policy_enforcement():
    route = task_router.route_task("Fix the failing test")
    block_policy = tool_policy.compute_policy(route, "implement", agent_blocks.IMPLEMENT_TOOLS, False, True)
    shell_decision = tool_policy.evaluate_shell_command("git add file.py", requires_change=True, safe_mode=False)

    assert tool_policy.requires_explicit_approval(
        "run_shell",
        block_policy=block_policy,
        shell_decision=shell_decision,
        policy_enforced=False,
    )


def test_mutation_audit_capability_and_action_descriptions():
    assert tool_policy.supports_mutation_audit(agent_blocks.REVIEW_TOOLS)
    assert not tool_policy.supports_mutation_audit(agent_blocks.READ_TOOLS)
    assert tool_policy.describes_mutation_action("write_file", {"path": "main.py"}) == "write_file: main.py"
    assert tool_policy.describes_mutation_action("run_shell", {"command": "git add main.py"}) == "run_shell: git add main.py"
    assert tool_policy.describes_mutation_action("run_shell", {"command": "git status --short"}) is None


def test_runtime_policy_requires_external_scoped_authority(tmp_path):
    cwd = str(tmp_path)
    read_action = resolve_action("read_file", {"path": "README.md"}, cwd=cwd)
    no_grant = tool_policy.evaluate_runtime_tool_policy(
        "read_file", {"path": "README.md"}, safe_mode=True, cwd=cwd, now=1.0
    )
    read = tool_policy.evaluate_runtime_tool_policy(
        "read_file",
        {"path": "README.md"},
        safe_mode=True,
        cwd=cwd,
        grant=_grant(read_action),
        now=1.0,
    )
    write_action = resolve_action("write_file", {"path": "x", "content": "y"}, cwd=cwd)
    write = tool_policy.evaluate_runtime_tool_policy(
        "write_file",
        {"path": "x", "content": "y"},
        safe_mode=True,
        cwd=cwd,
        grant=_grant(write_action),
        confirmation=_confirmation(write_action),
        now=1.0,
    )

    assert no_grant.disposition is PolicyDisposition.DENY
    assert read.allowed is True
    assert read.tier == "scoped"
    assert read.capability_names == ("read",)
    assert write.allowed is True
    assert {"read", "write"} <= set(write.capability_names)


def test_runtime_policy_blocks_safe_shell_mutation_before_authority(tmp_path):
    cwd = str(tmp_path)
    blocked = tool_policy.evaluate_runtime_tool_policy(
        "run_shell",
        {"command": "git add main.py"},
        safe_mode=True,
        cwd=cwd,
    )
    needs_confirmation = tool_policy.evaluate_runtime_tool_policy(
        "run_shell",
        {"command": "git status --short"},
        safe_mode=True,
        cwd=cwd,
    )

    assert blocked.allowed is False
    assert "safe_shell" in blocked.fired_rules
    assert any("safe mode blocks" in reason for reason in blocked.reasons)
    assert needs_confirmation.disposition is PolicyDisposition.CONFIRM
