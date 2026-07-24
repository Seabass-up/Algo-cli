from __future__ import annotations

from dataclasses import replace
import json

import pytest

from algo_cli import action_registry, tools
from algo_cli.marcus_authority import (
    Capability,
    CapabilityMask,
    ConfirmationMode,
    ConsentGrant,
    CURATED_TOOL_POLICIES,
    CuratedToolRegistry,
    EffectClass,
    IdempotencyClass,
    OutcomeModel,
    policy_for_action,
)


def test_every_runtime_tool_has_a_curated_authority_policy() -> None:
    assert set(CURATED_TOOL_POLICIES) == set(tools.TOOL_MAP)
    specs = tuple(spec for spec in action_registry.effective_action_specs() if spec.kind == "tool")
    assert len(specs) == len(tools.TOOL_MAP)
    assert all(spec.curated for spec in specs)


def test_unknown_tool_policy_is_fail_closed() -> None:
    policy = policy_for_action("browser_click")
    assert policy.curated is False
    assert policy.effect_class is EffectClass.UNCLASSIFIED
    assert policy.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED
    assert policy.capability_mask.has(Capability.UNCLASSIFIED)
    assert policy.mutates_state is True
    assert policy.requires_approval is True
    assert policy.safe_retry is False
    assert action_registry.action_requires_approval("browser_click") is True


def test_generated_unknown_spec_cannot_claim_read_only_or_retry_safe() -> None:
    spec = action_registry._generated_tool_spec("browser_click", lambda: None)
    assert spec.curated is False
    assert spec.risk_level == "high"
    assert spec.mutates_state is True
    assert spec.requires_approval is True
    assert spec.safe_retry is False
    assert spec.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED
    assert Capability.UNCLASSIFIED in spec.capabilities


def test_runtime_registry_rejects_an_unclassified_tool() -> None:
    registry = CuratedToolRegistry(tools.TOOL_MAP)
    try:
        registry["browser_click"] = lambda: "clicked"
    except ValueError as exc:
        assert "no curated authority policy" in str(exc)
    else:  # pragma: no cover - explicit failure message
        raise AssertionError("unclassified tool was accepted")


def test_curated_policy_fields_are_internally_consistent() -> None:
    for name, policy in CURATED_TOOL_POLICIES.items():
        assert policy.maximum_risk in {"low", "medium", "high"}, name
        assert policy.capabilities, name
        assert policy.data_classes, name
        assert policy.requires_approval is (policy.confirmation_mode is not ConfirmationMode.NONE), name
        expected_retry = (
            policy.idempotency in {IdempotencyClass.PURE, IdempotencyClass.IDEMPOTENT}
            and policy.outcome_model is not OutcomeModel.UNKNOWN_POSSIBLE
        )
        assert policy.safe_retry is expected_retry, name
        assert policy.estimated_cost > 0, name
        spec = action_registry.get_action_spec(name)
        assert spec.runtime_class is policy.runtime_class, name
        assert spec.estimated_cost == policy.estimated_cost, name
        assert spec.log_suppression is policy.suppress_logs, name
        assert spec.compensation_action == policy.compensation_action, name


def test_compensation_is_never_implicit_or_falsely_advertised() -> None:
    assert all(
        policy.idempotency is not IdempotencyClass.COMPENSATABLE
        for policy in CURATED_TOOL_POLICIES.values()
    )
    write_policy = policy_for_action("write_file")
    with pytest.raises(ValueError, match="explicit compensation action"):
        replace(write_policy, idempotency=IdempotencyClass.COMPENSATABLE)

    explicit = replace(
        write_policy,
        idempotency=IdempotencyClass.COMPENSATABLE,
        compensation_action="restore_file_snapshot",
    )
    assert explicit.compensation_action == "restore_file_snapshot"


def test_action_spec_dict_is_json_serializable_and_exposes_authority() -> None:
    value = action_registry.get_action_spec("write_file").as_dict()
    assert value["effect_class"] == "local_mutation"
    assert value["confirmation_mode"] == "action_time"
    assert "write" in value["capabilities"]
    json.dumps(value)


def test_memory_writes_require_action_time_confirmation() -> None:
    for name in ("remember", "append_lesson", "write_knowledge_graph_note", "update_user_profile"):
        assert action_registry.action_confirmation_mode(name) is ConfirmationMode.ACTION_TIME


def test_session_wrappers_declare_dynamic_exact_action_resolution() -> None:
    for name in ("session_command", "session_slash"):
        policy = policy_for_action(name)
        spec = action_registry.get_action_spec(name)
        assert policy.dynamic_resolution is True
        assert spec.dynamic_resolution is True
        assert policy.safe_retry is False

    assert all(
        not policy.dynamic_resolution
        for name, policy in CURATED_TOOL_POLICIES.items()
        if name not in {"session_command", "session_slash"}
    )


def test_capability_grant_checks_scope_expiry_and_required_mask() -> None:
    grant = ConsentGrant(
        grant_id="grant-1",
        capability_mask=Capability.READ.value | Capability.WRITE.value,
        allowed_actions=frozenset({"write_file"}),
        allowed_targets=frozenset({"workspace:/repo"}),
        expires_at=20.0,
        maximum_action_count=1,
    )
    required = CapabilityMask(Capability.READ.value | Capability.WRITE.value)
    assert grant.permits("write_file", required, "workspace:/repo", 10.0) is True
    assert grant.permits("run_shell", required, "workspace:/repo", 10.0) is False
    assert grant.permits("write_file", required, "workspace:/other", 10.0) is False
    assert grant.permits("write_file", required, "workspace:/repo", 20.0) is False
    assert grant.permits(
        "write_file",
        CapabilityMask(required.value | Capability.SHELL.value),
        "workspace:/repo",
        10.0,
    ) is False
