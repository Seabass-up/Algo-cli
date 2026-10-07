"""Native applicability preserves scope, revision checks and verified write outcomes."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path

from ollama import Tool
import pytest

from algo_cli import chatgpt_client, continuum_memory, james_dispatch, tools, xai_client
from algo_cli.config import Config
from algo_cli.continuum_tools import memory_set_applicability
from algo_cli.tool_schema import provider_tools, serialized_tool_schemas


def request(**changes):
    return dict(memory_id="fact:fixture", memory_revision=4, projects=["beta", "alpha"],
                reason="reviewed fixture", scope="private", cfg=Config(continuum_enabled=True), **changes)


def reply(projects):
    return {"ok": True, "id": "@applicability:" + hashlib.sha256(b"fact:fixture").hexdigest(),
            "revision": 5, "memory_id": "fact:fixture", "memory_revision": 4, "projects": projects}


def readback(result):
    return {"ok": True, "id": result["memory_id"], "revision": result["memory_revision"],
            "status": {"decision": "PRESENT", "usable": True},
            "project_applicability": {"declaration_id": result["id"], "declaration_revision": result["revision"],
                                      "projects": result["projects"],
                                      "state": "reviewed_global" if result["projects"] is None else "reviewed_projects"}}


@pytest.mark.parametrize("scope", ["shared", "private"])
@pytest.mark.parametrize("projects", [None, ["beta", "alpha"]])
def test_write_readback_and_replay_are_revision_bound(monkeypatch, scope, projects):
    calls = []
    result = reply(None if projects is None else sorted(projects))

    def invoke(_cfg, operation, payload, *, scope):
        calls.append((operation, payload, scope))
        if operation == "set_applicability":
            return {**result, "replayed_request": len(calls) > 1}
        assert operation == "explain" and payload == {"memory_id": "fact:fixture"}
        return readback(result)

    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    args = {**request(), "scope": scope, "projects": projects}
    first = json.loads(memory_set_applicability(**args))
    replay = json.loads(memory_set_applicability(**args))
    assert first["status"] == "worked" and replay["status"] == "skipped"
    assert all(call[2] == scope for call in calls)
    assert calls[0][1]["request_id"] == calls[2][1]["request_id"]
    assert first["authoritative_postcondition"]["memory_revision"] == 4
    assert first["authoritative_postcondition"]["declaration_revision"] == 5
    assert replay["requires_reconciliation"] is False


@pytest.mark.parametrize("field,value", [
    ("projects", []), ("projects", ["alpha", "alpha"]), ("projects", [True]),
    ("projects", "alpha"), ("projects", ["Alpha"]), ("projects", ["x" * 65]),
    ("projects", [str(i) for i in range(17)]), ("projects", [None]),
    ("memory_revision", True), ("memory_revision", 0), ("expected_revision", False),
    ("expected_revision", -1), ("memory_id", "@applicability:internal"),
    ("reason", " "), pytest.param("reason", "x" * 2049, id="oversized-reason"), ("scope", "other"),
])
def test_invalid_declaration_never_reaches_native_transport(monkeypatch, field, value):
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *_a, **_k: pytest.fail("native call"))
    result = json.loads(memory_set_applicability(**{**request(), field: value}))
    assert result["status"] == "denied" and result["error"] == "input"


@pytest.mark.parametrize("mismatch", ["target", "content_revision", "declaration_revision", "projects", "state", "usable", "missing"])
def test_readback_mismatch_cannot_report_success(monkeypatch, mismatch):
    result = reply(["alpha", "beta"])
    observed = readback(result)
    if mismatch == "target":
        observed["id"] = "other"
    elif mismatch == "content_revision":
        observed["revision"] = 6
    elif mismatch == "usable":
        observed["status"]["usable"] = False
    elif mismatch == "missing":
        observed.pop("project_applicability")
    else:
        observed["project_applicability"][mismatch] = {
            "declaration_revision": 7, "projects": None, "state": "stale_global",
        }[mismatch]
    monkeypatch.setattr(continuum_memory, "invoke_continuum",
                        lambda _c, op, _p, **_k: result if op == "set_applicability" else observed)
    value = json.loads(memory_set_applicability(**request()))
    assert value["status"] == "unknown_outcome" and value["requires_reconciliation"] is True
    assert value["retry_allowed"] is False


def test_unavailable_transport_and_native_revision_refusal_are_not_retried(monkeypatch):
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(1)
        raise continuum_memory.ContinuumMemoryError("offline")

    monkeypatch.setattr(continuum_memory, "invoke_continuum", unavailable)
    value = json.loads(memory_set_applicability(**request()))
    assert value["status"] == "unknown_outcome" and len(calls) == 1
    monkeypatch.setattr(continuum_memory, "invoke_continuum", lambda *_a, **_k: {
        "ok": False, "decision": "REFUSE_REVISION_CONFLICT", "error": "revision_conflict",
    })
    value = json.loads(memory_set_applicability(**request()))
    assert value["status"] == "denied" and value["retry_allowed"] is False


def test_native_schema_survives_all_provider_paths(monkeypatch):
    catalog = [tools.TOOL_MAP["memory_verify"], tools.TOOL_MAP["memory_set_applicability"]]
    prepared = provider_tools(catalog)
    wire = json.loads(serialized_tool_schemas(catalog))
    assert prepared == wire
    for payload in [prepared, chatgpt_client._build_openai_tools(prepared)]:
        parameters = payload[1]["function"]["parameters"]
        assert "projects" in parameters["required"]
        assert parameters["properties"]["projects"]["type"] == ["array", "null"]
        assert "cfg" not in parameters["properties"]
    ollama_parameters = Tool.model_validate(prepared[1]).model_dump(exclude_none=True)["function"]["parameters"]
    assert "projects" in ollama_parameters["required"]
    assert ollama_parameters["properties"]["projects"]["type"] == ["array", "null"]
    assert chatgpt_client._build_responses_tools(prepared)[1]["parameters"] == wire[1]["function"]["parameters"]
    requests = []
    monkeypatch.setattr(xai_client, "_post_chat", lambda payload, **_k: (
        requests.append(payload) or io.BytesIO(b'{"choices":[{"message":{"content":"fixture"}}]}')
    ))
    xai_client.XaiClient().chat(model="fixture", messages=[], tools=prepared, stream=False)
    assert requests[0]["tools"] == wire
    assert provider_tools(catalog[:1]) == catalog[:1]


def test_native_transport_receipts_and_write_detection_cover_applicability(monkeypatch, tmp_path):
    monkeypatch.setattr(continuum_memory, "_native_cli", lambda: tmp_path / "continuum-memory")
    calls = []
    monkeypatch.setattr(continuum_memory, "_run_native", lambda argv, body, **_k: (
        calls.append((argv, json.loads(body))) or (2, '{"ok":false,"error":"revision_conflict"}')
    ))
    continuum_memory.invoke_continuum(Config(continuum_enabled=True), "set_applicability",
                                     {"reason": "private review"}, scope="private")
    assert calls[0][0][-1] == "set_applicability" and "private review" not in str(calls[0][0])
    assert james_dispatch._explicit_memory_write_effect("memory_set_applicability", {}) is True
    assert "memory_set_applicability" in continuum_memory._MEMORY_MUTATION_NAMES


def test_against_native_companion_in_isolated_store(monkeypatch, tmp_path):
    root = Path(os.environ.get("CONTINUUM_CHECKOUT", Path.home() / "Code" / "continuum-memory"))
    if not (root / "continuum_memory" / "engine.py").is_file():
        pytest.skip("native companion checkout is not installed")
    monkeypatch.syspath_prepend(str(root))
    from continuum_memory import Memory, Refusal

    native = Memory(tmp_path / "store", "fixture", "algo", "private", authority_dir=tmp_path / "authority")
    native.initialize()
    original = native.remember("fact:fixture", {"text": "synthetic fixture"})

    def invoke(_cfg, operation, payload, *, scope):
        assert scope == "private"
        try:
            return getattr(native, operation)(**copy.deepcopy(payload))
        except Refusal as exc:
            return exc.result()

    monkeypatch.setattr(continuum_memory, "invoke_continuum", invoke)
    args = {**request(), "memory_revision": original["revision"]}
    value = json.loads(memory_set_applicability(**args))
    assert value["status"] == "worked"
    assert native.get("fact:fixture")["memory"]["revision"] == original["revision"]
    assert json.loads(memory_set_applicability(**args))["status"] == "skipped"
    global_args = {**args, "projects": None, "expected_revision": value["revision"]}
    assert json.loads(memory_set_applicability(**global_args))["status"] == "worked"
    assert json.loads(memory_set_applicability(**{**args, "reason": "different review"}))["status"] == "denied"
