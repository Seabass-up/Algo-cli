"""Applicability declarations preserve native authority and verified outcomes."""

import hashlib
import json

import pytest
from ollama import Tool

from algo_cli import chatgpt_client, continuum_memory, james_dispatch, tools
from algo_cli.config import Config
from algo_cli.continuum_tools import memory_set_applicability
from algo_cli.tool_schema import provider_tools, serialized_tool_schemas


def arguments(**changes):
    return {"memory_id": "fact:fixture", "memory_revision": 4, "projects": ["beta", "alpha"],
            "reason": "reviewed fixture", "scope": "private", "cfg": Config(continuum_enabled=True), **changes}


def fixtures(projects):
    result = {"ok": True, "id": "@applicability:" + hashlib.sha256(b"fact:fixture").hexdigest(),
              "revision": 5, "memory_id": "fact:fixture", "memory_revision": 4, "projects": projects}
    observed = {"ok": True, "id": "fact:fixture", "revision": 4,
                "status": {"decision": "PRESENT", "usable": True},
                "project_applicability": {"declaration_id": result["id"], "declaration_revision": 5,
                    "projects": projects, "state": "reviewed_global" if projects is None else "reviewed_projects"}}
    return result, observed


@pytest.mark.parametrize("scope", ["shared", "private"])
@pytest.mark.parametrize("projects", [None, ["beta", "alpha"]])
def test_write_and_replay_require_exact_readback(monkeypatch, scope, projects):
    result, observed = fixtures(None if projects is None else sorted(projects))
    calls = []
    def invoke(_cfg, operation, payload, *, scope):
        calls.append((operation, payload, scope))
        return {**result, "replayed_request": len(calls) > 1} if operation == "set_applicability" else observed
    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    first = json.loads(memory_set_applicability(**arguments(scope=scope, projects=projects)))
    replay = json.loads(memory_set_applicability(**arguments(scope=scope, projects=projects)))
    assert first["status"] == "worked" and replay["status"] == "skipped"
    assert first["authoritative_postcondition"]["memory_revision"] == 4
    assert first["authoritative_postcondition"]["declaration_revision"] == 5
    assert calls[0][1]["request_id"] == calls[2][1]["request_id"]
    assert all(call[2] == scope for call in calls)


@pytest.mark.parametrize("changes", [
    {"projects": []}, {"projects": ["alpha", "alpha"]}, {"projects": [True]},
    {"projects": "alpha"}, {"projects": ["Alpha"]}, {"memory_revision": True},
    {"memory_revision": 0}, {"expected_revision": -1}, {"reason": " "}, {"scope": "other"},
])
def test_invalid_declaration_never_calls_transport(monkeypatch, changes):
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *a, **kw: pytest.fail("Unexpected native call"))
    value = json.loads(memory_set_applicability(**arguments(**changes)))
    assert value["status"] == "denied" and value["error"] == "input"


@pytest.mark.parametrize("field,value", [
    ("declaration_revision", 6), ("projects", None), ("state", "stale_global"),
])
def test_mismatched_postcondition_is_unknown_not_success(monkeypatch, field, value):
    result, observed = fixtures(["alpha", "beta"])
    observed["project_applicability"][field] = value
    monkeypatch.setattr(continuum_memory, "invoke_continuum",
                        lambda cfg, op, payload, **kw: result if op == "set_applicability" else observed)
    reply = json.loads(memory_set_applicability(**arguments()))
    assert reply["status"] == "unknown_outcome" and reply["requires_reconciliation"] is True
    assert reply["retry_allowed"] is False


def test_unavailable_write_does_not_retry_or_fallback(monkeypatch):
    calls = []
    def unavailable(*a, **kw):
        calls.append(1)
        raise continuum_memory.ContinuumMemoryError("offline fixture")
    monkeypatch.setattr(continuum_memory, "invoke_continuum", unavailable)
    value = json.loads(memory_set_applicability(**arguments()))
    assert value["status"] == "unknown_outcome" and calls == [1]
    assert value["retry_allowed"] is False


def test_nullable_required_projects_survives_provider_adapters():
    catalog = [tools.TOOL_MAP["memory_verify"], tools.TOOL_MAP["memory_set_applicability"]]
    prepared = provider_tools(catalog)
    expected = json.loads(serialized_tool_schemas(catalog))[1]["function"]["parameters"]
    assert "projects" in expected["required"]
    assert expected["properties"]["projects"]["type"] == ["array", "null"]
    assert "cfg" not in expected["properties"]
    # Ollama omits some bounds; the host validator above still enforces them.
    ollama = Tool.model_validate(prepared[1]).model_dump(exclude_none=True)["function"]["parameters"]
    assert ollama["required"] == expected["required"]
    assert ollama["properties"]["projects"]["type"] == ["array", "null"]
    assert chatgpt_client._build_responses_tools(prepared)[1]["parameters"] == expected
    assert james_dispatch._explicit_memory_write_effect("memory_set_applicability", {}) is True
