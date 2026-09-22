from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from algo_cli import continuum_memory, config, tools
from algo_cli.action_registry import get_action_spec
from algo_cli.config import Config
from algo_cli.continuum_tools import (
    CONTINUUM_MUTATION_TOOLS,
    CONTINUUM_READ_ONLY_TOOLS,
    CONTINUUM_TOOL_NAMES,
    memory_capture,
    memory_remember,
    memory_verify,
)
from algo_cli.marcus_authority import (
    Capability,
    ConfirmationMode,
    EffectClass,
    TargetScope,
    policy_for_action,
)
from algo_cli.samuel_policy_engine import resolve_action
from algo_cli.tool_context import select_tools_for_prompt
from algo_cli.tool_schema import serialized_tool_schemas


CANONICAL_REQUIRED = {
    "memory_init": {"scope"},
    "memory_verify": {"scope"},
    "memory_status": {"scope"},
    "memory_capture": {"text", "source", "scope"},
    "memory_remember": {"memory_id", "data", "scope"},
    "memory_get": {"memory_id", "scope"},
    "memory_read": {"memory_id", "scope"},
    "memory_search": {"query", "scope"},
    "memory_context": {"query", "scope"},
    "memory_validate_context": {"packet", "scope"},
    "memory_revoke": {"memory_id", "expected_revision", "reason", "scope"},
    "memory_resolve": {"slot", "winner", "expected_revisions", "reason", "scope"},
    "memory_explain": {"memory_id", "scope"},
    "memory_history": {"memory_id", "scope"},
    "memory_handoff": {"memory_id", "goal", "completed", "next_steps", "blockers", "scope"},
}


def _payload(value: str) -> dict:
    loaded = json.loads(value)
    assert isinstance(loaded, dict)
    return loaded


def test_exact_native_catalog_is_registered_and_schema_callable() -> None:
    assert len(CONTINUUM_TOOL_NAMES) == 15
    assert set(CONTINUUM_TOOL_NAMES) <= set(tools.TOOL_MAP)
    schemas = json.loads(serialized_tool_schemas([tools.TOOL_MAP[name] for name in CONTINUUM_TOOL_NAMES]))
    assert {item["function"]["name"] for item in schemas} == set(CONTINUUM_TOOL_NAMES)
    for item in schemas:
        name = item["function"]["name"]
        required = set(item["function"]["parameters"].get("required", ()))
        assert CANONICAL_REQUIRED[name] <= required
        assert "scope" in required
        assert "cfg" not in item["function"]["parameters"]["properties"]


def test_local_canonical_contract_matches_registry_when_available() -> None:
    contract = Path(os.environ.get("CONTINUUM_CHECKOUT", Path.home() / "Code" / "continuum-memory")) / "integrations/harnesses/tools.json"
    if not contract.is_file():
        pytest.skip("canonical Continuum checkout is not installed")
    upstream = json.loads(contract.read_text(encoding="utf-8"))
    by_name = {item["name"]: item["inputSchema"] for item in upstream}
    assert set(by_name) == set(CONTINUUM_TOOL_NAMES)
    for name, required in CANONICAL_REQUIRED.items():
        assert set(by_name[name]["required"]) == required
        assert by_name[name]["properties"]["scope"]["enum"] == ["shared", "private"]


def test_native_authority_classification_is_complete() -> None:
    assert len(CONTINUUM_READ_ONLY_TOOLS) == 9
    assert len(CONTINUUM_MUTATION_TOOLS) == 6
    for name in CONTINUUM_TOOL_NAMES:
        policy = policy_for_action(name)
        assert policy.curated is True
        assert policy.target_scope is TargetScope.MEMORY_STORE
        assert Capability.MEMORY in policy.capabilities
        assert policy.suppress_logs is True
        spec = get_action_spec(name)
        assert spec.mutates_state is (name in CONTINUUM_MUTATION_TOOLS)
    for name in CONTINUUM_READ_ONLY_TOOLS:
        policy = policy_for_action(name)
        assert policy.effect_class is EffectClass.OBSERVE
        assert policy.confirmation_mode is ConfirmationMode.NONE
    for name in CONTINUUM_MUTATION_TOOLS - {"memory_revoke"}:
        policy = policy_for_action(name)
        assert policy.effect_class is EffectClass.LOCAL_MUTATION
        assert policy.confirmation_mode is ConfirmationMode.ACTION_TIME
    revoke = policy_for_action("memory_revoke")
    assert revoke.effect_class is EffectClass.DESTRUCTIVE
    assert Capability.DESTRUCTIVE in revoke.capabilities
    assert revoke.confirmation_mode is ConfirmationMode.ACTION_TIME


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Continuum memory", {"memory_get", "memory_search"}),
        ("search shared memory", {"memory_search"}),
        ("verify memory integrity", {"memory_verify"}),
        ("get exact memory history", {"memory_get", "memory_history"}),
        ("build and validate context", {"memory_context", "memory_validate_context"}),
        ("revoke a memory", {"memory_revoke"}),
    ],
)
def test_action_search_discovers_native_memory_schemas(query: str, expected: set[str]) -> None:
    result = _payload(tools.action_search(query, limit=12))
    actions = {item["name"]: item for item in result["actions"]}
    assert expected <= set(actions)
    for name in expected:
        assert "scope" in actions[name]["schema"]["function"]["parameters"]["required"]


def test_memory_class_selection_is_explicit_and_bounded() -> None:
    selected = select_tools_for_prompt("Allowed tool classes: memory", tools.ALL_TOOLS)
    names = {tool.__name__ for tool in selected}
    assert set(CONTINUUM_TOOL_NAMES) <= names
    ordinary = select_tools_for_prompt("Review this parser and run its tests", tools.ALL_TOOLS)
    assert not any(tool.__name__.startswith("memory_") for tool in ordinary)


def test_native_targets_bind_exact_continuum_scope() -> None:
    shared = resolve_action("memory_search", {"query": "x", "scope": "shared"}, cwd="/")
    private = resolve_action("memory_search", {"query": "x", "scope": "private"}, cwd="/")
    missing = resolve_action("memory_search", {"query": "x"}, cwd="/")
    assert shared.target == "memory-store:continuum:shared"
    assert private.target == "memory-store:continuum:private"
    assert missing.target == "memory-store:unresolved"


def test_native_calls_preserve_explicit_distinct_scopes(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def invoke(_cfg, operation, _payload, *, scope):
        calls.append((operation, scope))
        return {"ok": True, "namespace": {"scope": scope}}

    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    cfg = Config(continuum_enabled=True)
    assert _payload(memory_verify("shared", cfg=cfg))["scope"] == "shared"
    assert _payload(memory_verify("private", cfg=cfg))["scope"] == "private"
    assert calls == [("verify", "shared"), ("verify", "private")]


def test_structured_refusal_survives_without_fallback(monkeypatch) -> None:
    def invoke(*args, **kwargs):
        del args, kwargs
        return {"ok": False, "decision": "REFUSE_INTEGRITY", "error": "integrity", "message": "bad head"}

    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    result = _payload(memory_verify("shared", cfg=Config(continuum_enabled=True)))
    assert result["decision"] == "REFUSE_INTEGRITY"
    assert result["error"] == "integrity"
    assert result["status"] == "denied"
    assert result["requires_reconciliation"] is False


def test_interrupted_mutation_is_unknown_and_not_retried(monkeypatch) -> None:
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        raise continuum_memory.ContinuumMemoryError("offline")

    monkeypatch.setattr(continuum_memory, "invoke_continuum", unavailable)
    result = _payload(memory_capture("sensitive", "fixture", "private", cfg=Config(continuum_enabled=True)))
    assert calls == 1
    assert result["status"] == "unknown_outcome"
    assert result["requires_reconciliation"] is True
    assert result["retry_allowed"] is False


def test_verified_replay_returns_exact_authoritative_postcondition(monkeypatch) -> None:
    request_ids: list[str] = []
    writes = 0

    def invoke(_cfg, operation, payload, *, scope):
        nonlocal writes
        assert scope == "private"
        if operation == "remember":
            request_ids.append(payload["request_id"])
            writes += 1
            return {
                "ok": True,
                "id": "fact:1",
                "revision": 4,
                "data_digest": "digest-1",
                "replayed_request": writes > 1,
            }
        assert operation == "get"
        return {"ok": True, "memory": {"id": "fact:1", "revision": 4, "data_digest": "digest-1"}}

    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    cfg = Config(continuum_enabled=True)
    first = _payload(memory_remember("fact:1", {"v": 1}, "private", cfg=cfg))
    second = _payload(memory_remember("fact:1", {"v": 1}, "private", cfg=cfg))
    assert request_ids[0] == request_ids[1]
    assert first["status"] == "worked"
    assert second["status"] == "skipped"
    assert second["deduplicated"] is True
    assert second["requires_reconciliation"] is False
    assert second["authoritative_postcondition"] == {
        "operation": "remember",
        "scope": "private",
        "record_id": "fact:1",
        "revision": 4,
        "data_digest": "digest-1",
    }


def test_dedup_false_positive_cannot_clear_reconciliation(monkeypatch) -> None:
    def invoke(_cfg, operation, _payload, *, scope):
        assert scope == "private"
        if operation == "remember":
            return {
                "ok": True,
                "id": "fact:1",
                "revision": 4,
                "data_digest": "claimed",
                "replayed_request": True,
            }
        return {"ok": True, "memory": {"id": "fact:1", "revision": 4, "data_digest": "different"}}

    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    result = _payload(memory_remember("fact:1", {"v": 1}, "private", cfg=Config(continuum_enabled=True)))
    assert result["status"] == "unknown_outcome"
    assert result["requires_reconciliation"] is True
    assert "authoritative_postcondition" not in result


def test_native_memory_messages_are_always_redacted_from_persistence() -> None:
    secret = "private Continuum fixture body"
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "m1",
                    "function": {
                        "name": "memory_remember",
                        "arguments": json.dumps(
                            {"memory_id": "fact:1", "data": {"text": secret}, "scope": "private"}
                        ),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "m1", "name": "memory_remember", "content": secret},
    ]
    projected = config.project_messages_for_persistence(messages, protected_memory=False)
    encoded = json.dumps(projected)
    assert secret not in encoded
    assert '"arguments": "{}"' in encoded
    assert "Protected memory tool result omitted" in encoded


def test_model_facing_signatures_require_scope_and_hide_runtime_config() -> None:
    for name in CONTINUUM_TOOL_NAMES:
        signature = inspect.signature(tools.TOOL_MAP[name])
        assert "scope" in signature.parameters
        assert signature.parameters["scope"].default is inspect.Parameter.empty
        assert "cfg" not in signature.parameters
