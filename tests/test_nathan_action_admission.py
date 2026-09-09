"""Known pre-dispatch refusals must not request approval or invent uncertain effects."""

import json

import pytest

from algo_cli import action_registry, cobalt_browser_service, config, execution_guardrails, nathan_runtime, tools
from algo_cli.arthur_outcomes import OutcomeStatus
from algo_cli.config import Config
from algo_cli.james_dispatch import dispatch_action
from algo_cli.marcus_authority import ConfirmationMode, EffectClass, IdempotencyClass, OutcomeModel
from algo_cli.nathan_program_runtime import authorization_for_actions
from test_james_dispatch import _dependencies


def test_discovery_schema_is_emitted_without_runtime_configuration():
    from algo_cli.chatgpt_client import _build_responses_tools
    from algo_cli.tool_schema import serialized_tool_schemas

    neutral = json.loads(serialized_tool_schemas([tools.action_search]))
    responses = _build_responses_tools([tools.action_search])
    assert len(neutral) == len(responses or []) == 1
    expected = {"query", "limit"}
    assert set(neutral[0]["function"]["parameters"]["properties"]) == expected
    assert set(responses[0]["parameters"]["properties"]) == expected


def test_discovery_offers_only_program_composable_actions(monkeypatch):
    seen = []

    def rank(_query, candidates):
        seen.extend(candidates)
        return candidates

    monkeypatch.setattr("algo_cli.tool_context.rank_tools_for_prompt", rank)
    result = json.loads(tools.action_search("available actions and capabilities", limit=12))
    allowed = authorization_for_actions(tuple(tools.TOOL_MAP)).allowed_actions
    assert {fn.__name__ for fn in seen} <= allowed
    assert result["count"] == len(result["actions"]) == 12
    assert all(row["name"] in allowed for row in result["actions"])
    assert all(row["schema"]["function"]["name"] == row["name"] for row in result["actions"])


def test_discovery_respects_runtime_program_ceiling_and_echo_policy(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), echo_veil_enabled=True, echo_veil_protection="required")
    cfg._algo_program_authorization = authorization_for_actions(("run_shell", "read_file", "update_user_profile"))
    monkeypatch.setattr("algo_cli.tool_context.rank_tools_for_prompt", lambda _query, candidates: candidates)
    result = json.loads(nathan_runtime.run_tool("action_search", {"query": "verification"}, cfg))
    assert [row["name"] for row in result["actions"]] == ["read_file"]


def test_empty_discovery_does_not_advise_an_unavailable_program(tmp_path):
    result = json.loads(nathan_runtime.run_tool("action_search", {"query": "verification"}, Config(cwd=str(tmp_path))))
    assert result["count"] == 0 and result["actions"] == []
    assert "Call action_program" not in result["next"]
    assert "unavailable" in result["next"]


@pytest.mark.parametrize("enabled,protection", [(True, "required"), (True, "optional"), (False, "required")])
@pytest.mark.parametrize(
    "name,args", [("run_shell", {"command": "python -c 'assert True'"}), ("update_user_profile", {"content": "canary"})]
)
def test_protected_globally_disabled_actions_are_denied_before_approval(tmp_path, enabled, protection, name, args):
    cfg = Config(cwd=str(tmp_path), echo_veil_enabled=enabled, echo_veil_protection=protection)
    approvals, invocations = [], []
    deps = _dependencies(tmp_path, lambda *_args: invocations.append(True) or "must not execute")
    deps.approve = lambda *_args, **_kwargs: approvals.append(True) or False
    result = dispatch_action(name, args, cfg, dependencies=deps, render=False)
    assert approvals == invocations == []
    assert result.outcome.status is OutcomeStatus.DENIED
    assert not result.outcome.invoked and not result.outcome.retry_allowed
    assert "Unknown outcome" not in result.result
    assert "Echo Veil" in result.result


def test_protected_path_refusal_precedes_approval_and_mutation_evidence(tmp_path, monkeypatch):
    protected = tmp_path / ".algo_cli"
    protected.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", protected)
    cfg = Config(cwd=str(tmp_path), echo_veil_enabled=True, echo_veil_protection="required")
    approvals = []
    deps = _dependencies(tmp_path, lambda *_args: pytest.fail("protected path invoked"))
    deps.approve = lambda *_args, **_kwargs: approvals.append(True) or False
    scope = execution_guardrails.begin_execution_scope(tmp_path)
    try:
        result = dispatch_action(
            "write_file", {"path": ".algo_cli/memory.json", "content": "canary"}, cfg, dependencies=deps, render=False
        )
        assert not approvals
        assert result.outcome.status is OutcomeStatus.DENIED
        assert not result.outcome.invoked
        assert execution_guardrails.evidence_snapshot() == ()
    finally:
        execution_guardrails.end_execution_scope(scope)


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param({"version": 1, "steps": []}, id="empty"),
        pytest.param(
            {"version": 1, "steps": [{"id": "probe", "kind": "action", "action": "available_actions", "args": {}}]},
            id="meta-action",
        ),
        pytest.param(
            {"version": 1, "steps": [{"id": "probe", "kind": "transform", "op": "filter_eq", "input": [], "args": {}}]},
            id="invalid-transform",
        ),
    ],
)
def test_program_validation_is_a_known_pre_dispatch_denial(tmp_path, plan):
    cfg = Config(cwd=str(tmp_path))
    cfg._algo_program_authorization = authorization_for_actions(("read_file",))
    approvals, invocations = [], []
    deps = _dependencies(tmp_path, lambda *_args: invocations.append(True) or "must not execute")
    deps.approve = lambda *_args, **_kwargs: approvals.append(True) or False
    result = dispatch_action("action_program", {"plan": plan}, cfg, dependencies=deps, render=False)
    assert approvals == invocations == []
    assert result.outcome.status is OutcomeStatus.DENIED
    assert not result.outcome.invoked
    assert "Unknown outcome" not in result.result
    assert "ProgramValidationError" in result.result


def test_program_with_prohibited_echo_shell_is_denied_before_outer_dispatch(tmp_path):
    cfg = Config(cwd=str(tmp_path), echo_veil_enabled=True, echo_veil_protection="required")
    cfg._algo_program_authorization = authorization_for_actions(("run_shell",))
    plan = {
        "version": 1,
        "steps": [
            {"id": "verify", "kind": "action", "action": "run_shell", "args": {"command": "python -c 'assert True'"}}
        ],
    }
    deps = _dependencies(tmp_path, lambda *_args: pytest.fail("program invoked"))
    approvals = []
    deps.approve = lambda *_args, **_kwargs: approvals.append(True) or False
    result = dispatch_action("action_program", {"plan": plan}, cfg, dependencies=deps, render=False)
    assert approvals == []
    assert result.outcome.status is OutcomeStatus.DENIED and not result.outcome.invoked
    assert "Echo Veil" in result.result


def test_unavailable_browser_does_not_report_success(monkeypatch):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: False)
    monkeypatch.setattr(cobalt_browser_service, "open_tab", lambda *_args: pytest.fail("browser called"))
    result = tools.cobalt_open("https://example.com")
    assert nathan_runtime.classify_tool_status(result, name="cobalt_open") == "failed"


@pytest.mark.parametrize(
    "name,args",
    [
        ("cobalt_open", {"url": "https://example.com"}),
        ("cobalt_click", {"tab_id": "test", "ref": "e1"}),
        ("cobalt_snapshot", {"tab_id": "test"}),
    ],
)
def test_unavailable_browser_is_rejected_before_dispatch(monkeypatch, tmp_path, name, args):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: False)
    cfg = Config(cwd=str(tmp_path))
    approvals, invocations = [], []
    deps = _dependencies(tmp_path, lambda *_args: invocations.append(True) or "must not execute")
    deps.approve = lambda *_args, **_kwargs: approvals.append(True) or False
    result = dispatch_action(name, args, cfg, dependencies=deps, render=False)
    assert approvals == invocations == []
    assert result.outcome.status is OutcomeStatus.DENIED and not result.outcome.invoked
    assert "not available" in result.result


@pytest.mark.parametrize(
    "name,method,args",
    [
        ("cobalt_open", "open_tab", {"url": "https://example.com"}),
        ("cobalt_snapshot", "snapshot", {"tab_id": "test"}),
        ("cobalt_screenshot", "screenshot", {"tab_id": "test"}),
        ("cobalt_navigate", "navigate", {"tab_id": "test", "url": "https://example.com"}),
        ("cobalt_click", "click", {"tab_id": "test", "ref": "e1"}),
        ("cobalt_type", "type_text", {"tab_id": "test", "ref": "e1", "text": "canary"}),
        ("cobalt_scroll", "scroll", {"tab_id": "test"}),
        ("cobalt_close", "close_tab", {"tab_id": "test"}),
    ],
)
def test_browser_adapter_errors_do_not_report_success(monkeypatch, name, method, args):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(cobalt_browser_service, method, lambda *_args, **_kwargs: {"error": "response lost"})
    result = tools.TOOL_MAP[name](**args)
    assert nathan_runtime.classify_tool_status(result, name=name) == "failed"


def test_attempted_browser_mutation_retains_unknown_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    calls = []
    deps = _dependencies(tmp_path, lambda *_args: calls.append(True) or "Error: response lost after click")
    deps.approve = lambda *_args, **_kwargs: True
    result = dispatch_action(
        "cobalt_click",
        {"tab_id": "test", "ref": "e1"},
        Config(cwd=str(tmp_path)),
        tool_call_id="click-1",
        dependencies=deps,
        render=False,
    )
    assert calls == [True] and result.outcome.invoked
    assert result.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert not result.outcome.retry_allowed


def test_browser_success_without_independent_verifier_is_not_completion(monkeypatch, tmp_path):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    calls = []
    deps = _dependencies(tmp_path, lambda *_args: calls.append(True) or "Clicked e1.")
    deps.approve = lambda *_args, **_kwargs: True
    cfg = Config(cwd=str(tmp_path))
    first = dispatch_action(
        "cobalt_click",
        {"tab_id": "test", "ref": "e1"},
        cfg,
        tool_call_id="click-success",
        dependencies=deps,
        render=False,
    )
    assert first.outcome.invoked and not first.outcome.worked
    assert first.outcome.status is OutcomeStatus.UNKNOWN_OUTCOME
    assert not first.outcome.retry_allowed
    second = dispatch_action(
        "cobalt_click",
        {"tab_id": "test", "ref": "e1"},
        cfg,
        tool_call_id="click-repeated",
        dependencies=deps,
        render=False,
    )
    assert not second.outcome.invoked and not second.outcome.worked
    assert calls == [True]


@pytest.mark.parametrize(
    "name,method,args",
    [
        ("cobalt_click", "click", {"tab_id": "test", "ref": "e1"}),
        ("cobalt_type", "type_text", {"tab_id": "test", "ref": "e1", "text": "canary"}),
    ],
)
def test_service_retry_hint_does_not_authorize_repeated_external_effects(monkeypatch, name, method, args):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: True)
    monkeypatch.setattr(
        cobalt_browser_service, method, lambda *_args, **_kwargs: {"error": "ambiguous", "retryable": True}
    )
    result = tools.TOOL_MAP[name](**args)
    assert "do not repeat the action until its outcome is reconciled" in result
    assert "and retry" not in result


@pytest.mark.parametrize(
    "name", ["cobalt_open", "cobalt_navigate", "cobalt_click", "cobalt_type", "cobalt_scroll", "cobalt_close"]
)
def test_browser_mutations_are_not_read_only_or_retryable(name):
    spec = action_registry.get_action_spec(name)
    assert spec.effect_class is EffectClass.EXTERNAL_MUTATION
    assert spec.confirmation_mode is ConfirmationMode.ACTION_TIME
    assert spec.outcome_model is OutcomeModel.UNKNOWN_POSSIBLE
    assert spec.idempotency is IdempotencyClass.AT_MOST_ONCE
    assert spec.mutates_state and not spec.safe_retry


@pytest.mark.parametrize(
    "name",
    [
        "cobalt_open",
        "cobalt_snapshot",
        "cobalt_screenshot",
        "cobalt_navigate",
        "cobalt_click",
        "cobalt_type",
        "cobalt_scroll",
        "cobalt_close",
    ],
)
def test_unqualified_browser_cannot_cross_echo_memory_boundary(monkeypatch, tmp_path, name):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: pytest.fail("must refuse before browser probe"))
    cfg = Config(cwd=str(tmp_path), echo_veil_enabled=True, echo_veil_protection="required")
    args = {"url": "file:///unread/private/memory.txt"} if name == "cobalt_open" else {"tab_id": "untrusted-tab"}
    decision = nathan_runtime.preflight_runtime_tool(name, args, cfg)
    assert not decision.allowed
    assert "Echo Veil" in decision.blocked_result and "unqualified" in decision.blocked_result


def test_policy_ceiling_does_not_probe_browser_readiness(monkeypatch, tmp_path):
    monkeypatch.setattr(cobalt_browser_service, "is_available", lambda: pytest.fail("unneeded readiness probe"))
    result = dispatch_action(
        "cobalt_open",
        {"url": "https://example.com"},
        Config(cwd=str(tmp_path)),
        policy_ceiling_code="batch_quarantined",
        render=False,
    )
    assert result.outcome.status is OutcomeStatus.DENIED and not result.outcome.invoked
