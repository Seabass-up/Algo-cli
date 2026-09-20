from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess

import pytest

from algo_cli import ada_memory_d057 as d057
from algo_cli import config, context_budget, julia_memory_runtime, main, tools
from algo_cli.config import Config


@pytest.fixture
def backend(monkeypatch, config_dir):
    state = {"facts": [], "calls": []}

    def run(cfg, args):
        state["calls"].append(args[0])
        if args[0] == "doctor":
            return {"ok": True, "verify": True, "tip_sequence": 1}
        if args[0] == "remember":
            state["facts"] = json.loads(args[-1])["facts"]
            return {"ok": True}
        return {
            "ok": True,
            "projection": {
                "capsule": {
                    "task_id": d057._task_id(),
                    "summary": {"schema": "algo-cli-facts-v1", "facts": list(state["facts"])},
                },
            },
        }

    monkeypatch.setattr(d057, "_run", run)
    return state


def test_selection_flags_survive_save_restart_without_loading_legacy_memory(config_dir):
    config.MEMORY_FILE.write_text(json.dumps(["legacy fact"]), encoding="utf-8")
    cfg = Config(d057_enabled=True, d057_package_root="/package", d057_cli="/bridge")
    cfg.memories = ["transient fact"]
    cfg.save()
    loaded = Config.load()
    assert loaded.d057_enabled is True
    assert loaded.d057_cli == "/bridge"
    assert loaded.d057_package_root == "/package"
    assert loaded.d057_adapter == "d057-algo"
    assert loaded.echo_veil_enabled is False
    assert loaded.memories == []
    assert json.loads(config.MEMORY_FILE.read_text()) == ["legacy fact"]


@pytest.mark.parametrize("flags", [{"echo_veil_enabled": True}, {"echo_veil_protection": "required"}])
def test_competing_authorities_refuse_without_selecting_a_winner(flags):
    with pytest.raises(d057.D057MemoryError, match="Conflicting"):
        d057.selected(Config(d057_enabled=True, **flags))


def test_invalid_saved_authority_flag_cannot_reactivate_plaintext(config_dir):
    config.CONFIG_FILE.write_text('{"d057_enabled":"false"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="Invalid D-57"):
        Config.load()


def test_native_remember_and_prompt_recall_use_only_d057(backend, monkeypatch):
    cfg = Config(d057_enabled=True, memories=["stale host memory"])
    monkeypatch.setattr(julia_memory_runtime, "MemoryCatalog", lambda: pytest.fail("plaintext catalog"))
    monkeypatch.setattr(main, "capture_intuition_block", lambda *a, **k: None)
    assert tools.remember("Use verified receipts.", cfg=cfg) == "Remembered: Use verified receipts."
    assert tools.remember("Use verified receipts.", cfg=cfg).startswith("Fact already")
    assert Config(d057_enabled=True).remember_fact("Preserve unrelated changes.")
    section = context_budget._memory_prompt_section(cfg)
    assert "D-57 Memory Authority" in section
    assert "Use verified receipts." in section
    assert "Preserve unrelated changes." in section
    assert "stale host memory" not in section
    assert "system_memory.json" not in section
    assert backend["calls"].count("remember") == 2


def test_ordinary_memory_refuses_backend_failure_without_plaintext_writes(config_dir, monkeypatch):
    cfg = Config(d057_enabled=True, memories=["old host fact"])

    def unavailable(*args):
        raise d057.D057MemoryError("unavailable")

    monkeypatch.setattr(d057, "_run", unavailable)
    assert tools.remember("A concise verified fact.", cfg=cfg) == "Error: unavailable"
    with pytest.raises(d057.D057MemoryError):
        context_budget._memory_prompt_section(cfg)
    assert not config.MEMORY_FILE.exists()
    assert not julia_memory_runtime.catalog_path().exists()


@pytest.mark.parametrize("method,args", [
    ("save_memories", ()), ("forget_memory_index", (0,)), ("reconcile_memory_facts", ()),
])
def test_legacy_mutations_stay_unavailable_when_d057_selected(config_dir, method, args):
    cfg = Config(d057_enabled=True)
    with pytest.raises(RuntimeError, match="D-57"):
        getattr(cfg, method)(*args)
    assert not config.MEMORY_FILE.exists()


def test_append_only_catalog_operations_do_not_create_plaintext(backend, config_dir):
    cfg = Config(d057_enabled=True)
    assert "D-57 memory authority" in julia_memory_runtime.command_text("status", cfg)
    for command in ("add a fact", "reindex", "archive mem_1234567890abcdef"):
        with pytest.raises(julia_memory_runtime.MemorySystemError, match="D-57"):
            julia_memory_runtime.command_text(command, cfg)
    with pytest.raises(julia_memory_runtime.MemorySystemError, match="append-only"):
        julia_memory_runtime.forget_memory_index(cfg, 0)
    assert not julia_memory_runtime.catalog_path().exists()


def test_concurrent_snapshot_writes_preserve_every_fact(backend):
    facts = [f"Verified fixture fact {i}." for i in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda fact: d057.remember_fact(Config(d057_enabled=True), fact), facts))
    assert all(results)
    assert set(backend["facts"]) == set(facts)
    assert len(backend["facts"]) == len(facts)


def test_memory_capacity_refuses_without_discarding_existing_facts(backend):
    backend["facts"] = [str(i) + "x" * 3990 for i in range(5)]
    before = list(backend["facts"])
    with pytest.raises(d057.D057MemoryError, match="full"):
        d057.remember_fact(Config(d057_enabled=True), "y" * 200)
    assert backend["facts"] == before
    assert "remember" not in backend["calls"]


def test_unverified_readback_does_not_retry_append(backend, monkeypatch):
    original = d057._run

    def run(cfg, args):
        result = original(cfg, args)
        if args[0] == "remember":
            backend["facts"] = []
        return result

    monkeypatch.setattr(d057, "_run", run)
    with pytest.raises(d057.D057MemoryError, match="automatically retry"):
        d057.remember_fact(Config(d057_enabled=True), "Keep mutation receipts.")
    assert backend["calls"].count("remember") == 1


def test_private_content_is_rejected_before_append(backend):
    with pytest.raises(julia_memory_runtime.MemorySafetyError):
        d057.remember_fact(Config(d057_enabled=True), "My API key is synthetic-placeholder")
    assert not backend["calls"]


def test_d057_memory_arguments_and_results_are_not_persisted_as_history(backend):
    cfg = Config(d057_enabled=True, session_summary="untrusted memory summary")
    assert config.persisted_session_summary(cfg) == ""
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "m1", "function": {
            "name": "remember", "arguments": '{"fact":"private fixture content"}',
        }}]},
        {"role": "tool", "tool_call_id": "m1", "content": "private fixture content"},
    ]
    projected = config.project_messages_for_persistence(
        messages, echo_authority=config.echo_authority_selected_for_persistence(cfg),
    )
    assert "private fixture content" not in json.dumps(projected)


def test_nonzero_success_payload_and_malformed_json_are_not_accepted(tmp_path, monkeypatch):
    root = tmp_path / "package"
    cli = root / "algo-package" / "bin" / "d057_algo_cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# fixture\n", encoding="utf-8")
    cfg = Config(d057_enabled=True, d057_package_root=str(root), d057_cli=str(cli))
    for output, code in [('{"ok":true,"verify":true}', 1), ("not-json secret", 0)]:
        monkeypatch.setattr(d057.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, output, "secret"))
        with pytest.raises(d057.D057MemoryError) as caught:
            d057.doctor(cfg)
        assert "secret" not in str(caught.value)


def test_alternate_executable_cannot_be_selected_as_the_memory_bridge(tmp_path):
    cfg = Config(d057_enabled=True, d057_package_root=str(tmp_path), d057_cli="/bin/sh")
    with pytest.raises(d057.D057MemoryError, match="unavailable"):
        d057.doctor(cfg)


def test_native_lessons_route_d057_and_profile_shadows_are_unavailable(backend, monkeypatch):
    monkeypatch.setattr(main, "capture_intuition_block", lambda *a, **k: None)
    cfg = Config(d057_enabled=True)
    assert tools.append_lesson("Preserve verified negative results.", cfg=cfg).startswith("Remembered:")
    assert "Preserve verified negative results." in backend["facts"]
    assert "unavailable with D-57" in tools.update_user_profile("a profile", cfg=cfg)
    assert main._intuition_engine_for(cfg) is None
    assert "unavailable with D-57" in tools.write_knowledge_graph_note("a title", "a body", cfg=cfg)


def test_failed_d057_preflight_stops_before_any_model_call(config_dir, monkeypatch):
    from algo_cli import elsie_echo_preflight

    cfg = Config(d057_enabled=True)
    monkeypatch.setattr(elsie_echo_preflight, "prepare_echo_auxiliary_state", lambda *a, **k: {})

    def refuse(*args):
        raise d057.D057MemoryError("unavailable")

    monkeypatch.setattr(d057, "doctor", refuse)
    errors = []
    monkeypatch.setattr(main, "show_error", errors.append)
    main.agent_loop(object(), cfg, "Inspect source")
    assert errors and "before model execution" in errors[-1]


def test_empty_reply_requires_verified_empty_tip(config_dir, monkeypatch):
    def run(cfg, args):
        return {"ok": True, "verify": True, "tip_sequence": 1} if args[0] == "doctor" else {
            "ok": False, "error": "empty",
        }

    monkeypatch.setattr(d057, "_run", run)
    with pytest.raises(d057.D057MemoryError, match="refused memory recall"):
        d057.recall_facts(Config(d057_enabled=True))


def test_malformed_projection_is_not_injected(backend):
    backend["facts"] = [123]
    with pytest.raises(d057.D057MemoryError, match="invalid memory projection"):
        context_budget._memory_prompt_section(Config(d057_enabled=True))
