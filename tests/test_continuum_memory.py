from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess

import pytest

from algo_cli import continuum_memory as memory
from algo_cli import config, context_budget, julia_memory_runtime, main, tools
from algo_cli.config import Config


def test_reconciliation_scan_covers_rows_beyond_old_tail_limit(tmp_path):
    row = {"operation": "memory_remember", "mutates_state": True, "status": "worked"}
    unknown = {**row, "status": "unknown_outcome", "effect_identity": "a" * 64}
    (tmp_path / "attempts.jsonl").write_text(
        "\n".join(json.dumps(item) for item in [unknown, *[row] * 200]), encoding="utf-8"
    )
    result = memory._reconciliation_counts(Config(), receipt_root=tmp_path)
    assert result["unresolved_unknown_outcomes"] == 1
    assert result["receipt_scan_incomplete"] is False


def test_reconciliation_scan_reports_unreadable_or_malformed_receipts(tmp_path):
    (tmp_path / "attempts.jsonl").write_text("{invalid json\n", encoding="utf-8")
    result = memory._reconciliation_counts(Config(), receipt_root=tmp_path)
    assert result["receipt_scan_incomplete"] is True
    assert result["malformed_receipt_files_or_rows"] == 1


@pytest.mark.parametrize("namespace", [{}, {"owner": "other", "project": "legacy-memory", "scope": "private"}])
def test_status_cannot_claim_counts_from_a_different_namespace(monkeypatch, namespace):
    monkeypatch.setattr(memory, "recall_facts", lambda cfg: [])
    monkeypatch.setattr(memory, "invoke_continuum", lambda *a, **k: {
        "ok": True, "namespace": namespace, "counts": {"usable": 5}, "records": 5,
    })
    with pytest.raises(memory.ContinuumMemoryError, match="invalid status"):
        memory.memory_status_report(Config(continuum_enabled=True))


@pytest.fixture
def backend(monkeypatch, config_dir):
    state = {"facts": [], "calls": [], "revision": 1}

    def invoke(cfg, operation, payload, *, scope):
        state["calls"].append(operation)
        if operation == "verify":
            return {
                "ok": True, "encrypted_at_rest": True,
                "head": {"sequence": state["revision"], "mac": "f" * 64},
                "namespace": {"project": "legacy-memory", "scope": scope,
                              "owner": "algo" if scope == "private" else None},
            }
        if operation == "context":
            return {"ok": True, "context": {"items": [], "omitted_optional": 0}}
        if operation == "validate_context":
            return {"ok": True}
        if operation == "remember":
            assert payload["expected_revision"] == state["revision"]
            assert payload["request_id"].startswith("algo-facts:")
            state["facts"] = payload["data"]["facts"]
            state["revision"] += 1
            return {"ok": True, "revision": state["revision"]}
        assert operation == "get"
        return {"ok": True, "memory": {
            "id": memory._facts_record_id(), "revision": state["revision"], "kind": "fact",
            "data": {"schema": "algo-cli-facts-v1", "facts": list(state["facts"])},
        }}

    monkeypatch.setattr(memory, "invoke_continuum", invoke)
    return state


def test_selection_flags_survive_save_restart_without_loading_legacy_memory(config_dir):
    config.MEMORY_FILE.write_text(json.dumps(["legacy fact"]), encoding="utf-8")
    cfg = Config(continuum_enabled=True)
    cfg.memories = ["transient fact"]
    cfg.save()
    loaded = Config.load()
    assert loaded.continuum_enabled is True
    assert not hasattr(loaded, "d057_enabled")
    assert not hasattr(loaded, "echo_veil_enabled")
    assert loaded.memories == []
    assert json.loads(config.MEMORY_FILE.read_text()) == ["legacy fact"]


@pytest.mark.parametrize("field", ["echo_veil_enabled", "echo_veil_protection", "d057_enabled", "d057_cli"])
def test_retired_authority_fields_are_not_active_config_fields(field):
    with pytest.raises(TypeError, match="unexpected keyword"):
        Config(**{field: True})


def test_invalid_saved_authority_flag_cannot_reactivate_plaintext(config_dir):
    config.MEMORY_FILE.write_text(json.dumps(["plaintext canary"]), encoding="utf-8")
    config.CONFIG_FILE.write_text('{"continuum_enabled":"false"}', encoding="utf-8")
    loaded = Config.load()
    assert loaded.continuum_enabled is False
    assert loaded.memory_config_error
    assert loaded.memories == []
    with pytest.raises(memory.ContinuumMemoryError, match="requires repair"):
        memory.selected(loaded)


def test_native_remember_and_prompt_recall_use_only_continuum(backend, monkeypatch):
    cfg = Config(continuum_enabled=True, memories=["stale host memory"])
    monkeypatch.setattr(julia_memory_runtime, "MemoryCatalog", lambda: pytest.fail("plaintext catalog"))
    monkeypatch.setattr(main, "capture_intuition_block", lambda *a, **k: None)
    assert tools.remember("Use verified receipts.", cfg=cfg) == "Continuum memory saved."
    assert tools.remember("Use verified receipts.", cfg=cfg) == "Continuum memory already stored."
    assert Config(continuum_enabled=True).remember_fact("Preserve unrelated changes.")
    section = context_budget._memory_prompt_section(cfg)
    assert "Continuum Memory" in section
    assert "Use verified receipts." in section
    assert "Preserve unrelated changes." in section
    assert "stale host memory" not in section
    assert "system_memory.json" not in section
    assert backend["calls"].count("remember") == 2


def test_ordinary_memory_refuses_backend_failure_without_plaintext_writes(config_dir, monkeypatch):
    cfg = Config(continuum_enabled=True, memories=["old host fact"])

    def unavailable(*args, **kwargs):
        raise memory.ContinuumMemoryError("unavailable")

    monkeypatch.setattr(memory, "invoke_continuum", unavailable)
    assert tools.remember("A concise verified fact.", cfg=cfg) == "Error: unavailable"
    with pytest.raises(memory.ContinuumMemoryError):
        context_budget._memory_prompt_section(cfg)
    assert not config.MEMORY_FILE.exists()
    assert not julia_memory_runtime.catalog_path().exists()


@pytest.mark.parametrize("method,args", [
    ("save_memories", ()), ("forget_memory_index", (0,)), ("reconcile_memory_facts", ()),
])
def test_legacy_mutations_stay_unavailable_when_continuum_selected(config_dir, method, args):
    cfg = Config(continuum_enabled=True)
    with pytest.raises(RuntimeError, match="Continuum Memory|Continuum memory_revoke"):
        getattr(cfg, method)(*args)
    assert not config.MEMORY_FILE.exists()


def test_append_only_catalog_operations_do_not_create_plaintext(backend, config_dir):
    cfg = Config(continuum_enabled=True)
    assert "Continuum memory authority" in julia_memory_runtime.command_text("status", cfg)
    for command in ("add a fact", "reindex", "archive mem_1234567890abcdef"):
        with pytest.raises(julia_memory_runtime.MemorySystemError, match="Continuum Memory"):
            julia_memory_runtime.command_text(command, cfg)
    with pytest.raises(julia_memory_runtime.MemorySystemError, match="memory_revoke"):
        julia_memory_runtime.forget_memory_index(cfg, 0)
    assert not julia_memory_runtime.catalog_path().exists()


def test_concurrent_snapshot_writes_preserve_every_fact(backend):
    facts = [f"Verified fixture fact {i}." for i in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda fact: memory.remember_fact(Config(continuum_enabled=True), fact), facts))
    assert all(results)
    assert set(backend["facts"]) == set(facts)
    assert len(backend["facts"]) == len(facts)


def test_memory_capacity_refuses_without_discarding_existing_facts(backend):
    backend["facts"] = [str(i) + "x" * 3990 for i in range(5)]
    before = list(backend["facts"])
    with pytest.raises(memory.ContinuumMemoryError, match="full"):
        memory.remember_fact(Config(continuum_enabled=True), "y" * 200)
    assert backend["facts"] == before
    assert "remember" not in backend["calls"]


def test_unverified_readback_does_not_retry_append(backend, monkeypatch):
    original = memory.invoke_continuum

    def run(cfg, operation, payload, *, scope):
        result = original(cfg, operation, payload, scope=scope)
        if operation == "remember":
            backend["facts"] = []
        return result

    monkeypatch.setattr(memory, "invoke_continuum", run)
    with pytest.raises(memory.ContinuumMemoryError, match="automatically retry"):
        memory.remember_fact(Config(continuum_enabled=True), "Keep mutation receipts.")
    assert backend["calls"].count("remember") == 1


def test_private_content_is_rejected_before_append(backend):
    with pytest.raises(julia_memory_runtime.MemorySafetyError):
        memory.remember_fact(Config(continuum_enabled=True), "My API key is synthetic-placeholder")
    assert not backend["calls"]


def test_continuum_memory_arguments_and_results_are_not_persisted_as_history(backend):
    cfg = Config(continuum_enabled=True, session_summary="untrusted memory summary")
    assert config.persisted_session_summary(cfg) == ""
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "m1", "function": {
            "name": "remember", "arguments": '{"fact":"private fixture content"}',
        }}]},
        {"role": "tool", "tool_call_id": "m1", "content": "private fixture content"},
    ]
    projected = config.project_messages_for_persistence(
        messages,
        protected_memory=config.protected_memory_selected_for_persistence(cfg),
    )
    assert "private fixture content" not in json.dumps(projected)


@pytest.mark.parametrize("output,code", [('{"ok":true}', 1), ('{"ok":true}', 2), ("not-json secret", 0)])
def test_invalid_transport_replies_are_sanitized(tmp_path, monkeypatch, output, code):
    monkeypatch.setattr(memory, "_native_cli", lambda: tmp_path / "continuum-memory")
    monkeypatch.setattr(memory.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, output, "secret"))
    with pytest.raises(memory.ContinuumMemoryError) as caught:
        memory.doctor(Config(continuum_enabled=True))
    assert "secret" not in str(caught.value)


def test_native_transport_binds_scope_and_keeps_payload_out_of_argv(tmp_path, monkeypatch):
    cli = tmp_path / "continuum-memory"
    monkeypatch.setattr(memory, "_native_cli", lambda: cli)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")

    monkeypatch.setattr(memory.subprocess, "run", run)
    memory.invoke_continuum(Config(continuum_enabled=True), "capture", {"text": "PRIVATE_CANARY"}, scope="private")
    argv, kwargs = calls[0]
    assert argv == [str(cli), "--root", str(memory.storage_root()), "--project", "legacy-memory",
                    "--harness", "algo", "--scope", "private", "capture"]
    assert "PRIVATE_CANARY" not in str(argv)
    assert json.loads(kwargs["input"]) == {"text": "PRIVATE_CANARY"}
    assert "PYTHONPATH" not in kwargs["env"]
    with pytest.raises(memory.ContinuumMemoryError, match="one explicit scope"):
        memory.invoke_continuum(Config(continuum_enabled=True), "verify", {"scope": "shared"}, scope="private")
    assert len(calls) == 1


def test_native_lessons_route_continuum_and_profile_shadows_are_unavailable(backend, monkeypatch):
    monkeypatch.setattr(main, "capture_intuition_block", lambda *a, **k: None)
    cfg = Config(continuum_enabled=True)
    assert tools.append_lesson("Preserve verified negative results.", cfg=cfg) == "Continuum memory saved."
    assert "Preserve verified negative results." in backend["facts"]
    assert "unavailable with Continuum Memory" in tools.update_user_profile("a profile", cfg=cfg)
    assert main._intuition_engine_for(cfg) is None
    assert "unavailable with Continuum Memory" in tools.write_knowledge_graph_note("a title", "a body", cfg=cfg)


def test_failed_continuum_preflight_stops_before_any_model_call(config_dir, monkeypatch):
    from algo_cli import protected_memory_preflight

    cfg = Config(continuum_enabled=True)
    monkeypatch.setattr(protected_memory_preflight, "prepare_protected_auxiliary_state", lambda *a, **k: {})

    def refuse(*args):
        raise memory.ContinuumMemoryError("unavailable")

    monkeypatch.setattr(memory, "doctor", refuse)
    errors = []
    monkeypatch.setattr(main, "show_error", errors.append)
    main.agent_loop(object(), cfg, "Inspect source")
    assert errors and "before model execution" in errors[-1]


def test_unknown_empty_reply_is_not_a_missing_record(backend, monkeypatch):
    original = memory.invoke_continuum

    def invoke(cfg, operation, payload, *, scope):
        if operation == "get":
            return {"ok": False, "error": "empty"}
        return original(cfg, operation, payload, scope=scope)

    monkeypatch.setattr(memory, "invoke_continuum", invoke)
    with pytest.raises(memory.ContinuumMemoryError, match="refused recall"):
        memory.recall_facts(Config(continuum_enabled=True))


@pytest.mark.parametrize("payload", [
    {"d057_enabled": True}, {"echo_veil_protection": "required"},
])
def test_retired_selection_never_selects_a_different_backend(config_dir, payload):
    config.MEMORY_FILE.write_text('["plaintext canary"]', encoding="utf-8")
    config.CONFIG_FILE.write_text(json.dumps(payload), encoding="utf-8")
    original = config.CONFIG_FILE.read_bytes()
    cfg = Config.load()
    assert cfg.continuum_enabled is False
    assert cfg.memory_config_error and cfg.memories == []
    with pytest.raises(memory.ContinuumMemoryError, match="requires repair"):
        memory.selected(cfg)
    with pytest.raises(RuntimeError, match="requires repair"):
        cfg.save()
    assert config.CONFIG_FILE.read_bytes() == original


def test_explicit_continuum_selection_drops_retired_keys_on_save(config_dir):
    config.CONFIG_FILE.write_text(json.dumps({
        "continuum_enabled": True, "d057_enabled": True, "d057_cli": "/unrelated/retired-cli",
        "echo_veil_enabled": False,
    }), encoding="utf-8")
    cfg = Config.load()
    assert memory.selected(cfg)
    cfg.save()
    saved = config.CONFIG_FILE.read_text()
    assert "d057" not in saved and "echo_veil" not in saved


def test_malformed_projection_is_not_injected(backend):
    backend["facts"] = [123]
    with pytest.raises(memory.ContinuumMemoryError, match="invalid memory projection"):
        context_budget._memory_prompt_section(Config(continuum_enabled=True))
