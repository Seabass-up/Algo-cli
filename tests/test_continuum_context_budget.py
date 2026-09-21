"""Required Continuum state can grow without crashing the terminal UI."""

import json

import pytest

from algo_cli import context_budget, continuum_memory as memory, main
from algo_cli.config import Config


def refusal(budget=12000, required=12477):
    return {"ok": False, "decision": "REFUSE_BUDGET", "error": "budget",
            "details": {"budget_bytes": budget, "required_bytes": required}}


def test_budget_retry_preserves_scopes_query_and_validates_packets(monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "recall_facts", lambda _cfg: [])

    def invoke(cfg, operation, payload, *, scope):
        calls.append((operation, scope, payload))
        if operation == "validate_context":
            assert payload["packet"]["context"]["items"] == ["required-record"]
            return {"ok": True}
        if scope == "shared" and payload["budget_bytes"] == 12000:
            return refusal()
        return {"ok": True, "budget_bytes": payload["budget_bytes"], "context": {"items": ["required-record"]}}

    monkeypatch.setattr(memory, "invoke_continuum", invoke)
    value = json.loads(memory.prompt_context(Config(continuum_enabled=True), "current task"))
    assert value["contexts"]["shared"]["budget_bytes"] == 14336
    assert value["contexts"]["private"]["budget_bytes"] == 6000
    assert calls == [
        ("context", "shared", {"query": "current task", "budget_bytes": 12000}),
        ("context", "shared", {"query": "current task", "budget_bytes": 14336}),
        ("validate_context", "shared", {"packet": value["contexts"]["shared"]}),
        ("context", "private", {"query": "current task", "budget_bytes": 6000}),
        ("validate_context", "private", {"packet": value["contexts"]["private"]}),
    ]


@pytest.mark.parametrize("packet", [
    refusal(required=32769), refusal(required=True), refusal(required="12477"),
    refusal(required=12000), refusal(budget=6000),
    {"ok": False, "error": "integrity", "decision": "REFUSE_INTEGRITY"},
    {"ok": False, "error": "critical_unavailable", "decision": "REFUSE_CRITICAL_STATE"},
])
def test_non_budget_or_malformed_or_over_cap_refusals_are_not_retried(monkeypatch, packet):
    calls = []
    monkeypatch.setattr(memory, "invoke_continuum", lambda *a, **k: calls.append(k) or packet)
    with pytest.raises(memory.ContinuumMemoryError):
        memory._context_packet(Config(continuum_enabled=True), "q", scope="shared", budget=12000)
    assert len(calls) == 1


def test_growth_retry_is_bounded_and_capped(monkeypatch):
    budgets = []

    def invoke(_cfg, _operation, payload, **_kwargs):
        budgets.append(payload["budget_bytes"])
        return refusal(budget=payload["budget_bytes"], required=32768)

    monkeypatch.setattr(memory, "invoke_continuum", invoke)
    with pytest.raises(memory.ContinuumMemoryError, match="continuum_context_budget_exceeded"):
        memory._context_packet(Config(continuum_enabled=True), "q", scope="shared", budget=12000)
    assert budgets == [12000, 32768]


def test_retry_packet_must_still_pass_native_validation(monkeypatch):
    monkeypatch.setattr(memory, "recall_facts", lambda _cfg: [])
    replies = iter([refusal(), {"ok": True}, {"ok": False, "error": "stale"}])
    monkeypatch.setattr(memory, "invoke_continuum", lambda *a, **k: next(replies))
    with pytest.raises(memory.ContinuumMemoryError, match="could not be verified"):
        memory.prompt_context(Config(continuum_enabled=True), "q")


def test_footer_survives_refusal_and_recovers_without_false_usage(monkeypatch):
    monkeypatch.setattr(main, "RUNTIME_STATUS", {})
    monkeypatch.setattr(main, "local_model_names", lambda cfg: [])
    monkeypatch.setattr(main._model_info_module, "resolve_model_info", lambda *a: {"context_length": 8192})

    def unavailable(*a, **k):
        raise memory.ContinuumMemoryError("private backend detail", reason_code="continuum_context_refused")

    monkeypatch.setattr(main, "context_status", unavailable)
    cfg = Config(continuum_enabled=True)
    main.refresh_runtime_status(cfg, force=True)
    assert main.RUNTIME_STATUS["context"] == "unknown"
    assert main.RUNTIME_STATUS["context_pct_left"] is None
    assert main.RUNTIME_STATUS["context_error"] == "continuum_context_refused"
    assert "ctx unavailable" in str(main.build_status_toolbar(cfg))
    assert "private backend detail" not in str(main.RUNTIME_STATUS)
    monkeypatch.setattr(main, "context_status", lambda *a, **k: (100, 8192, 8092, 8192, 8192))
    main.refresh_runtime_status(cfg, force=True)
    assert main.RUNTIME_STATUS["context_error"] == ""
    assert main.RUNTIME_STATUS["context_used"] == 100


def test_prompt_assembly_still_refuses_unavailable_required_memory(monkeypatch):
    monkeypatch.setattr(memory, "recall_facts", lambda cfg: [])
    monkeypatch.setattr(memory, "invoke_continuum", lambda *a, **k: refusal(required=40000))
    with pytest.raises(memory.ContinuumMemoryError, match="no fallback"):
        context_budget.build_system_prompt(Config(continuum_enabled=True), user_message="task")
