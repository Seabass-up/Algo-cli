"""Jev contract transport, dispatch and authority regression tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from algo_cli import jev_kernel as jev
from algo_cli.config import Config


@pytest.fixture
def contract():
    return {
        "schema_version": "jev.question-contract.v1",
        "goal": "Classify the supplied issue.",
        "decision_use": "Advisory triage only.",
        "state": {"text": "Login is failing."}, "source_revision": "test-r1",
        "items": {"login": {
            "answer_shape": "yes_no", "evidence_paths": ["/text"],
            "missing_evidence": "not_mentioned_is_false",
            "question": {"type": "noul", "instructions": "Does the text mention login failure?"},
        }},
    }


def result(contract, mode="run"):
    return {
        "schema_version": jev.RESULT_SCHEMA, "ok": True, "advisory_only": True,
        "mode": mode, "status": "answered" if mode != "lint" else "structurally_valid",
        "contract_sha256": hashlib.sha256(jev._encoded(contract)).hexdigest(),
        "source_revision": contract["source_revision"], "freshness": "not_verified",
        "answers": {} if mode == "lint" else {"login": {"status": "answered", "value": True}},
        "inference": {"model": jev.MODEL},
    }


def test_disabled_inference_never_starts_companion(monkeypatch, contract):
    monkeypatch.setattr(jev, "_invoke", lambda *_a: pytest.fail("companion invoked"))
    for enabled in (False, "true", 1, None):
        cfg = Config(jev_kernel_enabled=enabled)
        assert jev.question_contract(cfg, contract, "run")["error"] == "jev_kernel_disabled"


def test_local_lint_allowed_when_inference_disabled(monkeypatch, contract):
    monkeypatch.setattr(jev, "_invoke", lambda *_a: result(contract, "lint"))
    assert jev.question_contract(Config(), contract)["status"] == "structurally_valid"


@pytest.mark.parametrize("field,value", [
    ("advisory_only", False), ("contract_sha256", "wrong"), ("mode", "lint"),
    ("source_revision", "different"), ("answers", {}), ("freshness", "verified"),
    ("inference", {"model": "unapproved-model"}),
])
def test_rejects_mismatched_contract_response(monkeypatch, contract, field, value):
    response = result(contract)
    response[field] = value
    monkeypatch.setattr(jev, "_invoke", lambda *_a: response)
    actual = jev.question_contract(Config(jev_kernel_enabled=True), contract, "run")
    assert actual["ok"] is False
    assert actual["answers"] == {}


def test_uncertain_answer_remains_unresolved(monkeypatch, contract):
    response = result(contract)
    response.update(status="needs_evidence", answers={"login": {"status": "needs_evidence", "value": None}})
    monkeypatch.setattr(jev, "_invoke", lambda *_a: response)
    assert jev.question_contract(Config(jev_kernel_enabled=True), contract, "run") == response


@pytest.mark.parametrize("answer", [
    None, "true", {}, {"status": "answered", "value": "false"},
    {"status": "answered", "value": 1}, {"status": "needs_evidence", "value": False},
    {"status": "needs_review"}, {"status": "executed", "value": True},
])
def test_rejects_malformed_answer_values(monkeypatch, contract, answer):
    response = result(contract)
    response["answers"]["login"] = answer
    monkeypatch.setattr(jev, "_invoke", lambda *_a: response)
    assert not jev.question_contract(Config(jev_kernel_enabled=True), contract, "run")["ok"]


def test_review_uses_axis_judgments_not_business_answers(monkeypatch, contract):
    response = result(contract, "review")
    response["answers"]["login"] = {
        axis: {"type": "choice", "choice": "satisfied", "confidence": 0.9,
               "probabilities": {"satisfied": 0.9, "problem": 0.05, "unknown": 0.05}}
        for axis in ("goal_alignment", "focused_judgment", "answer_shape", "criteria_meaning", "evidence_meaning")
    }
    monkeypatch.setattr(jev, "_invoke", lambda *_a: response)
    assert jev.question_contract(Config(jev_kernel_enabled=True), contract, "review")["ok"]
    response["answers"]["login"]["goal_alignment"]["choice"] = "not-an-option"
    assert not jev.question_contract(Config(jev_kernel_enabled=True), contract, "review")["ok"]


@pytest.mark.parametrize("mode,parent", [("execute", None), ("followup", None), ("run", {}), ("followup", [])])
def test_invalid_mode_or_parent_never_executes(monkeypatch, contract, mode, parent):
    monkeypatch.setattr(jev, "_invoke", lambda *_a: pytest.fail("companion invoked"))
    assert not jev.question_contract(Config(jev_kernel_enabled=True), contract, mode, parent)["ok"]


def test_companion_status_checks_contract_support_without_inference(monkeypatch):
    calls = []

    def invoke(_cfg, operation, payload):
        calls.append((operation, payload))
        if operation == "status":
            return {"ok": True, "mode": "advisory_only", "model": jev.MODEL}
        return {"ok": False, "schema_version": jev.RESULT_SCHEMA, "status": "invalid_contract"}

    monkeypatch.setattr(jev, "_invoke", invoke)
    assert jev.kernel_status(Config())["ok"]
    assert calls == [("status", {}), ("contract", {"mode": "lint", "contract": {}})]


def test_old_companion_is_not_ready(monkeypatch):
    monkeypatch.setattr(jev, "_invoke", lambda *_a: {"ok": True, "mode": "advisory_only", "model": jev.MODEL})
    assert jev.kernel_status(Config())["error"] == "jev_contract_unavailable"


@pytest.mark.parametrize("cli", ["", "jev-workflows", "./jev-workflows"])
def test_requires_explicit_absolute_executable(cli):
    with pytest.raises(jev.JevKernelError):
        jev.companion_path(Config(jev_kernel_cli=cli))


def _child(monkeypatch, source):
    real_popen = subprocess.Popen
    monkeypatch.setattr(jev, "companion_path", lambda _cfg: Path(sys.executable))

    def popen(argv, **kwargs):
        assert argv[1] == "contract"
        assert "PYTHONPATH" not in kwargs["env"]
        assert "PYTHONHOME" not in kwargs["env"]
        assert kwargs.get("shell", False) is False
        return real_popen([sys.executable, "-I", "-c", source], **kwargs)

    monkeypatch.setattr(jev.subprocess, "Popen", popen)


def test_real_child_receives_json_on_stdin_and_returns_it(monkeypatch):
    # Read bytes like the real companion; text-mode stdin uses the Windows code page.
    _child(monkeypatch, 'import json,sys; print(json.dumps({"ok": True, "received": json.load(sys.stdin.buffer)}))')
    assert jev._invoke(Config(), "contract", {"text": "unicode \u2603"})["received"] == {"text": "unicode \u2603"}


def test_real_child_output_overflow_is_bounded(monkeypatch):
    _child(monkeypatch, 'import sys; sys.stdout.write("x" * 200000); sys.stdout.flush()')
    with pytest.raises(jev.JevKernelError, match="jev_response_too_large"):
        jev._invoke(Config(), "contract", {})


def test_real_child_timeout_is_reaped(monkeypatch):
    _child(monkeypatch, "import time; time.sleep(20)")
    monkeypatch.setattr(jev, "TIMEOUT_SECONDS", 0.05)
    with pytest.raises(jev.JevKernelError, match="jev_companion_timeout"):
        jev._invoke(Config(), "contract", {})


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group lifecycle")
def test_exited_companion_cannot_leave_inherited_pipe_hanging(monkeypatch):
    _child(monkeypatch, 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(20)"]); print(\'{"ok":true}\', flush=True)')
    started = time.monotonic()
    assert jev._invoke(Config(), "contract", {}) == {"ok": True}
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("raw", ['{"ok":true,"ok":false}', '{"ok":true,"value":NaN}'])
def test_duplicate_fields_and_non_json_numbers_are_rejected(monkeypatch, raw):
    _child(monkeypatch, f"print({raw!r})")
    with pytest.raises(jev.JevKernelError, match="jev_invalid_response"):
        jev._invoke(Config(), "contract", {})


def test_oversized_request_does_not_launch(monkeypatch):
    monkeypatch.setattr(jev, "companion_path", lambda *_a: pytest.fail("companion invoked"))
    with pytest.raises(jev.JevKernelError, match="jev_request_too_large"):
        jev._invoke(Config(), "contract", {"text": "x" * jev.MAX_REQUEST_BYTES})


def test_non_json_stderr_is_never_returned(monkeypatch, contract):
    _child(monkeypatch, 'import sys; print("secret", file=sys.stderr); print("invalid"); sys.exit(1)')
    response = jev.question_contract(Config(jev_kernel_enabled=True), contract, "run")
    assert response["ok"] is False
    assert "secret" not in json.dumps(response)


def test_runtime_injects_configuration_and_hides_it_from_model(monkeypatch, contract):
    from algo_cli.nathan_runtime import run_tool
    from algo_cli.tools import TOOL_MAP
    import inspect

    cfg = Config(jev_kernel_enabled=True)
    seen = []
    monkeypatch.setattr(jev, "question_contract", lambda actual, *_a: seen.append(actual) or {"ok": True})
    assert "cfg" not in inspect.signature(TOOL_MAP["jev_question_contract"]).parameters
    assert json.loads(run_tool("jev_question_contract", {"contract": contract, "mode": "run"}, cfg))["ok"]
    assert seen == [cfg]


def test_inference_requires_egress_authority_but_lint_is_local():
    from algo_cli.marcus_authority import Capability, ConfirmationMode, IdempotencyClass
    from algo_cli.samuel_policy_engine import resolve_action

    lint = resolve_action("jev_question_contract", {}, cwd=os.getcwd())
    run = resolve_action("jev_question_contract", {"mode": "run"}, cwd=os.getcwd())
    assert lint.capability_mask == Capability.READ.value
    assert lint.confirmation_mode is ConfirmationMode.NONE
    assert run.capability_mask & Capability.DATA_EGRESS.value
    assert run.confirmation_mode is ConfirmationMode.SESSION_PREAPPROVAL
    assert run.idempotency is IdempotencyClass.NON_IDEMPOTENT
    assert run.target == "provider:typesafe:jev"


def test_kernel_and_tool_are_discoverable():
    from algo_cli.kernels.manifest import audit_kernels
    from algo_cli.tool_context import select_tools_for_prompt
    from algo_cli.tools import ALL_TOOLS

    assert audit_kernels("jev")[0].health == "ready"
    selected = select_tools_for_prompt("Allowed tool classes: jev", ALL_TOOLS)
    assert {fn.__name__ for fn in selected} == {"jev_kernel_status", "jev_question_contract"}
