"""Exercise local Jev authorization through the chat dispatcher, not raw tools."""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from algo_cli import jev_kernel as jev
from algo_cli.arthur_outcomes import OutcomeStatus
from algo_cli.config import Config
from algo_cli.james_dispatch import dispatch_action
from algo_cli.marcus_authority import Capability, ConfirmationMode, TargetScope
from algo_cli.nathan_runtime import (
    ask_approval,
    authority_session_for,
    preflight_runtime_tool,
)
from algo_cli.samuel_policy_engine import PolicyDisposition


LOCAL_CALLS = [
    pytest.param("jev_kernel_status", {}, "runtime:jev_kernel_status", id="status"),
    pytest.param("jev_question_contract", {"contract": {}}, "runtime:jev:lint", id="default-lint"),
    pytest.param("jev_question_contract", {"contract": {}, "mode": "lint"}, "runtime:jev:lint", id="lint"),
]


@pytest.mark.parametrize("name,args,target", LOCAL_CALLS)
def test_local_jev_reaches_companion_without_prompt(monkeypatch, tmp_path, name, args, target):
    cfg = Config(cwd=str(tmp_path), jev_kernel_enabled=False)
    cfg._nathan_approval_mode = "never"
    calls = []

    def invoke(actual_cfg, operation, payload):
        assert actual_cfg is cfg
        calls.append((operation, payload))
        if operation == "status":
            return {"ok": True, "mode": "advisory_only", "model": jev.MODEL}
        assert operation == "contract" and payload["mode"] == "lint"
        return {
            "schema_version": jev.RESULT_SCHEMA, "ok": False,
            "advisory_only": True, "mode": "lint", "status": "invalid_contract",
            "contract_sha256": hashlib.sha256(jev._encoded({})).hexdigest(),
            "answers": {}, "freshness": "not_verified",
        }

    monkeypatch.setattr(jev, "_invoke", invoke)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("unexpected approval prompt"))
    dispatched = dispatch_action(name, args, cfg, render=False)

    assert dispatched.outcome.invoked is True
    assert dispatched.preflight.policy.disposition is PolicyDisposition.ALLOW
    action = dispatched.preflight.policy.action
    assert action.target == target
    assert action.target_scope is TargetScope.RUNTIME
    assert action.capability_mask == Capability.READ.value
    assert action.confirmation_mode is ConfirmationMode.NONE
    if name == "jev_kernel_status":
        assert dispatched.outcome.status is OutcomeStatus.SUCCEEDED
        assert json.loads(dispatched.result)["api_connectivity"] == "not_probed_by_status"
        assert len(calls) == 2
    else:
        # Invalid input reaches the local validator instead of being denied by authority.
        assert json.loads(dispatched.result)["status"] == "invalid_contract"
        assert len(calls) == 1


@pytest.mark.parametrize("name,args,target", LOCAL_CALLS)
def test_local_jev_still_respects_caller_ceiling(monkeypatch, tmp_path, name, args, target):
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr(jev, "_invoke", lambda *_: pytest.fail("companion invoked"))
    dispatched = dispatch_action(
        name, args, cfg, policy_ceiling_code="agent_tool_not_allowed", render=False,
    )
    assert dispatched.outcome.invoked is False
    assert not dispatched.preflight.allowed
    assert not dispatched.preflight.policy.grant_id


@pytest.mark.parametrize("mode", ["run", "review", "followup", "unknown", "LINT", None, 1])
def test_nonlocal_modes_never_inherit_local_grant(monkeypatch, tmp_path, mode):
    cfg = Config(cwd=str(tmp_path), jev_kernel_enabled=True)
    cfg._nathan_approval_mode = "never"
    monkeypatch.setattr(jev, "_invoke", lambda *_: pytest.fail("companion invoked"))
    dispatched = dispatch_action(
        "jev_question_contract", {"contract": {}, "mode": mode}, cfg, render=False,
    )
    action = dispatched.preflight.policy.action
    assert action.target == "provider:typesafe:jev"
    assert action.target_scope is TargetScope.PROVIDER
    assert action.capability_mask & Capability.DATA_EGRESS.value
    assert action.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL
    assert not authority_session_for(cfg).baseline_allows(action)
    assert dispatched.outcome.invoked is False
    assert not dispatched.preflight.policy.grant_id


@pytest.mark.parametrize("name,args,target", LOCAL_CALLS)
@pytest.mark.parametrize("unavailable", ["consumed", "revoked", "expired"])
def test_local_jev_grants_are_one_use_and_revocable(
    monkeypatch, tmp_path, name, args, target, unavailable,
):
    cfg = Config(cwd=str(tmp_path))
    preflight = preflight_runtime_tool(name, args, cfg)
    assert preflight.policy.disposition is PolicyDisposition.ALLOW
    session = authority_session_for(cfg)
    if unavailable == "consumed":
        assert ask_approval(name, args, cfg, preflight=preflight)
    elif unavailable == "revoked":
        session.revoke_all()
    else:
        expired_now = time.time() + 31
        monkeypatch.setattr("algo_cli.nathan_runtime.time.time", lambda: expired_now)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("unexpected approval prompt"))
    assert not ask_approval(name, args, cfg, preflight=preflight)


def test_lint_grant_cannot_authorize_inference_or_changed_input(tmp_path):
    cfg = Config(cwd=str(tmp_path), jev_kernel_enabled=True)
    args = {"contract": {}, "mode": "lint"}
    preflight = preflight_runtime_tool("jev_question_contract", args, cfg)
    assert preflight.policy.disposition is PolicyDisposition.ALLOW
    for changed in ({"contract": {}, "mode": "run"}, {"contract": {"goal": "changed"}, "mode": "lint"}):
        assert not ask_approval("jev_question_contract", changed, cfg, preflight=preflight)


@pytest.mark.parametrize("approval,expected_invoked", [("y", True), ("n", False)])
def test_paid_run_still_uses_explicit_approval(monkeypatch, tmp_path, approval, expected_invoked):
    cfg = Config(cwd=str(tmp_path), jev_kernel_enabled=True)
    cfg._nathan_approval_mode = "interactive"
    contract = {
        "schema_version": "jev.question-contract.v1",
        "goal": "Classify the supplied issue.", "decision_use": "Advisory only.",
        "state": {"text": "Login is failing."}, "source_revision": "test-r1",
        "items": {"login": {
            "answer_shape": "yes_no", "evidence_paths": ["/text"],
            "missing_evidence": "not_mentioned_is_false",
            "question": {"type": "noul", "instructions": "Does the text mention login failure?"},
        }},
    }
    prompts, calls = [], []

    def invoke(actual_cfg, operation, payload):
        assert actual_cfg is cfg
        calls.append((operation, payload))
        assert operation == "contract" and payload["mode"] == "run"
        return {
            "schema_version": jev.RESULT_SCHEMA, "ok": True, "advisory_only": True,
            "mode": "run", "status": "answered", "freshness": "not_verified",
            "source_revision": "test-r1",
            "contract_sha256": hashlib.sha256(jev._encoded(contract)).hexdigest(),
            "answers": {"login": {"status": "answered", "value": True}},
            "inference": {"model": jev.MODEL},
        }

    monkeypatch.setattr(jev, "_invoke", invoke)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or approval)
    dispatched = dispatch_action(
        "jev_question_contract", {"contract": contract, "mode": "run"}, cfg, render=False,
    )
    assert len(prompts) == 1
    assert dispatched.outcome.invoked is expected_invoked
    assert len(calls) == int(expected_invoked)
    if expected_invoked:
        assert dispatched.outcome.status is OutcomeStatus.SUCCEEDED
        assert json.loads(dispatched.result)["answers"]["login"]["value"] is True
