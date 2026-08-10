"""Config load/save and runtime-env parsing."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from algo_cli import config
from algo_cli.config import (
    CODE_RAG_CONSENT_VERSION,
    DEFAULT_CHAT_STREAM_TIMEOUT_SECONDS,
    MEMORY_AUTO_CAPTURE_CONSENT_VERSION,
    Config,
    code_rag_consent_granted,
    load_runtime_env,
    memory_auto_capture_consent_granted,
)


def test_defaults():
    cfg = Config()
    assert cfg.cloud is False
    assert cfg.safe_mode is True
    assert cfg.num_ctx > 0
    assert cfg.chat_stream_timeout_seconds == DEFAULT_CHAT_STREAM_TIMEOUT_SECONDS
    assert cfg.skill_crystallize_enabled is False
    assert cfg.skill_crystallize_every >= 1
    assert cfg.runs_since_crystallize == 0
    assert cfg.algorithmic_tool_policy_enabled is True
    assert cfg.echo_veil_capacity == 400
    assert cfg.echo_veil_production is False
    assert cfg.memory_auto_capture_enabled is False
    assert cfg.memory_auto_capture_consent_version == 0
    assert memory_auto_capture_consent_granted(cfg) is False
    assert cfg.memory_auto_daily_limit == 5
    assert cfg.memory_auto_entry_limit == 64
    assert cfg.memory_auto_char_limit == 12_000
    assert cfg.external_harness_sources_enabled is False
    assert cfg.index_compute_lab_auto_inject is False
    assert cfg.code_rag_enabled is False
    assert cfg.code_rag_consent_version == 0
    assert code_rag_consent_granted(cfg) is False


def test_save_load_roundtrip():
    cfg = Config()
    cfg.model = "test-model:latest"
    cfg.num_ctx = 12345
    cfg.cloud = True
    cfg.chat_stream_timeout_seconds = 45.0
    cfg.skill_crystallize_every = 7
    cfg.algorithmic_tool_policy_enabled = True
    cfg.echo_veil_capacity = 12
    cfg.echo_veil_production = True
    cfg.memory_auto_capture_enabled = True
    cfg.memory_auto_capture_consent_version = MEMORY_AUTO_CAPTURE_CONSENT_VERSION
    cfg.memory_auto_daily_limit = 3
    cfg.memory_auto_entry_limit = 24
    cfg.memory_auto_char_limit = 8_000
    cfg.code_rag_enabled = True
    cfg.code_rag_consent_version = CODE_RAG_CONSENT_VERSION
    cfg.save()

    reloaded = Config.load()
    assert reloaded.model == "test-model:latest"
    assert reloaded.num_ctx == 12345
    assert reloaded.cloud is True
    assert reloaded.chat_stream_timeout_seconds == 45.0
    assert reloaded.skill_crystallize_every == 7
    assert reloaded.algorithmic_tool_policy_enabled is True
    assert reloaded.echo_veil_capacity == 12
    assert reloaded.echo_veil_production is True
    assert reloaded.memory_auto_capture_enabled is True
    assert memory_auto_capture_consent_granted(reloaded) is True
    assert reloaded.memory_auto_daily_limit == 3
    assert reloaded.memory_auto_entry_limit == 24
    assert reloaded.memory_auto_char_limit == 8_000
    assert reloaded.code_rag_enabled is True
    assert reloaded.code_rag_consent_version == CODE_RAG_CONSENT_VERSION
    assert code_rag_consent_granted(reloaded) is True


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("sol", "gpt-5.6-sol"),
        ("terra", "gpt-5.6-terra"),
        ("luna", "gpt-5.6-luna"),
        ("lunna", "gpt-5.6-luna"),
    ],
)
def test_load_canonicalizes_persisted_codex_alias(alias, canonical):
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({"model": alias}), encoding="utf-8")

    assert Config.load().model == canonical


def test_legacy_code_rag_true_does_not_migrate_as_consent() -> None:
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        json.dumps({"code_rag_enabled": True}),
        encoding="utf-8",
    )

    loaded = Config.load()

    assert loaded.code_rag_enabled is False
    assert loaded.code_rag_consent_version == 0
    assert code_rag_consent_granted(loaded) is False


def test_outdated_code_rag_consent_version_fails_closed() -> None:
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        json.dumps({"code_rag_enabled": True, "code_rag_consent_version": 99}),
        encoding="utf-8",
    )

    loaded = Config.load()

    assert loaded.code_rag_enabled is False
    assert code_rag_consent_granted(loaded) is False


def test_legacy_memory_auto_capture_boolean_does_not_migrate_as_consent() -> None:
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        json.dumps({"memory_auto_capture_enabled": True}),
        encoding="utf-8",
    )

    loaded = Config.load()

    assert loaded.memory_auto_capture_enabled is False
    assert loaded.memory_auto_capture_consent_version == 0
    assert memory_auto_capture_consent_granted(loaded) is False


def test_outdated_memory_auto_capture_consent_fails_closed() -> None:
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        json.dumps(
            {
                "memory_auto_capture_enabled": True,
                "memory_auto_capture_consent_version": 99,
            }
        ),
        encoding="utf-8",
    )

    loaded = Config.load()

    assert loaded.memory_auto_capture_enabled is False
    assert memory_auto_capture_consent_granted(loaded) is False


def test_reconcile_memory_facts_is_atomic_normalized_and_idempotent():
    cfg = Config(memories=["Keep  exact spacing", "Retire Echo report bug", "Mixed Case Fact"])
    cfg.save_memories()

    result = cfg.reconcile_memory_facts(
        additions=[" mixed case fact ", "New durable fact", "new   durable FACT"],
        remove_if=lambda fact: "echo report bug" in fact.casefold(),
    )

    assert result == {"changed": True, "removed": 1, "added": 1, "total": 3}
    assert cfg.memories == ["Keep  exact spacing", "Mixed Case Fact", "New durable fact"]
    assert config.MEMORY_FILE.with_suffix(".json.reconcile.bak").exists()

    second = cfg.reconcile_memory_facts(
        additions=["  NEW durable   fact  "],
        remove_if=lambda fact: "echo report bug" in fact.casefold(),
    )
    assert second == {"changed": False, "removed": 0, "added": 0, "total": 3}


def test_reconcile_memory_facts_rolls_back_when_predicate_fails():
    cfg = Config(memories=["preserve me"])
    cfg.save_memories()
    before = config.MEMORY_FILE.read_bytes()

    def fail(_fact: str) -> bool:
        raise RuntimeError("migration failed")

    with pytest.raises(RuntimeError, match="migration failed"):
        cfg.reconcile_memory_facts(additions=["not written"], remove_if=fail)

    assert config.MEMORY_FILE.read_bytes() == before


def test_load_coerces_or_ignores_bad_persisted_types():
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        '{"num_ctx": "8192", "temperature": null, "safe_mode": "false", "messages": [{"role": "user"}]}',
        encoding="utf-8",
    )

    reloaded = Config.load()

    assert reloaded.num_ctx == 8192
    assert reloaded.temperature == Config().temperature
    assert reloaded.safe_mode is False
    assert reloaded.messages == []


def test_messages_and_memories_not_in_config_file():
    cfg = Config()
    cfg.messages = [{"role": "user", "content": "hello"}]
    cfg.memories = ["a fact"]
    cfg.save()
    # messages/memories are intentionally excluded from config.json
    reloaded = Config.load()
    assert reloaded.messages == []


def test_echo_config_load_save_drops_legacy_plaintext_attempts_and_summary() -> None:
    canary = "LEGACY_RAW_ECHO_PAYLOAD_CANARY"
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        json.dumps(
            {
                "echo_veil_enabled": True,
                "echo_veil_protection": "required",
                "session_summary": canary,
                "attempt_ledger": [
                    {
                        "status": "failed",
                        "tool": "echo_veil_recall",
                        "args_preview": canary,
                        "summary": canary,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = Config.load()
    loaded.save()
    persisted = config.CONFIG_FILE.read_text(encoding="utf-8")

    assert loaded.session_summary == ""
    assert loaded.attempt_ledger == []
    assert canary not in persisted


def test_echo_conversation_persistence_projects_direct_wrapped_and_unpaired_tools() -> None:
    args_canary = "PROTECTED_ARGUMENT_CANARY"
    result_canary = "PROTECTED_RESULT_WITHOUT_PROVIDER_MARKER"
    wrapped_canary = "WRAPPED_MEMORY_QUERY_CANARY"
    unpaired_canary = "UNPAIRED_TOOL_RESULT_CANARY"
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
        session_summary=result_canary,
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "echo-call",
                        "function": {
                            "name": "echo_veil_recall",
                            "arguments": json.dumps({"query": args_canary}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "echo-call",
                "content": json.dumps({"records": [{"payload": result_canary}]}),
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "wrapped-call",
                        "function": {
                            "name": "session_command",
                            "arguments": json.dumps({"command": f"/memory search {wrapped_canary}"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "wrapped-call",
                "content": wrapped_canary,
            },
            {
                "role": "tool",
                "tool_call_id": "legacy-unpaired",
                "content": unpaired_canary,
            },
        ],
    )

    path = cfg.save_conversation("protected")
    persisted = path.read_text(encoding="utf-8")

    assert args_canary not in persisted
    assert result_canary not in persisted
    assert wrapped_canary not in persisted
    assert unpaired_canary not in persisted
    assert args_canary in json.dumps(cfg.messages)
    assert cfg.session_summary == result_canary

    legacy = json.loads(persisted)
    legacy["messages"][-1]["content"] = unpaired_canary
    legacy["session_summary"] = result_canary
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )
    loaded.load_conversation("protected")
    migrated = path.read_text(encoding="utf-8")

    assert result_canary not in migrated
    assert unpaired_canary not in migrated
    assert result_canary not in json.dumps(loaded.messages)
    assert unpaired_canary not in json.dumps(loaded.messages)
    assert loaded.session_summary == ""


def test_memory_save_is_atomic_and_forget_does_not_readd_existing_file_state():
    cfg = Config()
    cfg.memories = ["keep", "remove"]
    cfg.save_memories()

    cfg.memories.pop(1)
    cfg.save_memories()

    reloaded = Config.load()
    assert reloaded.memories == ["keep"]


def test_remember_fact_reloads_current_file_to_reduce_lost_updates():
    a = Config()
    b = Config()
    a.remember_fact("from a")
    b.remember_fact("from b")

    reloaded = Config.load()
    assert reloaded.memories == ["from a", "from b"]


def test_remember_fact_preserves_concurrent_writes():
    facts = [f"fact {i}" for i in range(25)]

    def write_fact(fact: str) -> None:
        Config().remember_fact(fact)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_fact, facts))

    reloaded = Config.load()
    assert set(reloaded.memories) == set(facts)
    assert len(reloaded.memories) == len(facts)


def test_remember_fact_preserves_concurrent_process_writes(tmp_path):
    facts = [f"process fact {i}" for i in range(12)]
    script = (
        "import sys; "
        "from algo_cli.config import Config; "
        "ok = Config().remember_fact(sys.argv[1]); "
        "sys.exit(0 if ok else 2)"
    )
    env = dict(os.environ)
    env["ALGO_CLI_CONFIG_DIR"] = str(tmp_path)
    env["OLLAMA_CLI_CONFIG_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")

    processes = [subprocess.Popen([sys.executable, "-c", script, fact], env=env) for fact in facts]
    failures = [proc.wait(timeout=15) for proc in processes]

    assert failures == [0] * len(facts)
    loaded = config._load_json_file(tmp_path / "memory.json", [])
    assert set(loaded) == set(facts)
    assert len(loaded) == len(facts)


def test_corrupt_memory_file_is_preserved_and_not_silently_deleted():
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.MEMORY_FILE.write_text('["ok",', encoding="utf-8")

    loaded = Config.load()

    assert loaded.memories == []
    assert config.MEMORY_FILE.with_suffix(config.MEMORY_FILE.suffix + ".corrupt").exists()


def test_corrupt_config_is_not_duplicated_before_echo_authority_is_known() -> None:
    canary = "CORRUPT_CONFIG_PROTECTED_CANARY"
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        '{"echo_veil_protection":"required","payload":"' + canary,
        encoding="utf-8",
    )
    config.MEMORY_FILE.write_text('["' + canary + '"]', encoding="utf-8")

    loaded = Config.load()

    assert loaded.echo_veil_enabled is True
    assert loaded.echo_veil_protection == "required"
    assert loaded.memories == []
    assert canary in config.CONFIG_FILE.read_text(encoding="utf-8")
    assert not config.CONFIG_FILE.with_suffix(".json.corrupt").exists()


def test_unsafe_or_oversize_config_fails_closed_without_loading_plaintext_memory(
    tmp_path,
) -> None:
    canary = "UNSAFE_CONFIG_MEMORY_CANARY"
    config.MEMORY_FILE.write_text(json.dumps([canary]), encoding="utf-8")
    outside = tmp_path / "outside-config.json"
    outside.write_text(
        '{"echo_veil_enabled":false,"echo_veil_protection":"optional"}',
        encoding="utf-8",
    )
    config.CONFIG_FILE.symlink_to(outside)

    symlinked = Config.load()
    assert symlinked.echo_veil_enabled is True
    assert symlinked.echo_veil_protection == "required"
    assert symlinked.memories == []

    config.CONFIG_FILE.unlink()
    config.CONFIG_FILE.write_bytes(
        b'{"echo_veil_enabled":false,"padding":"' + (b"x" * (config.MAX_JSON_STATE_BYTES + 1)) + b'"}'
    )
    oversized = Config.load()
    assert oversized.echo_veil_enabled is True
    assert oversized.echo_veil_protection == "required"
    assert oversized.memories == []


def test_json_state_loader_rejects_symlink_and_oversize_inputs(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"poison":true}', encoding="utf-8")
    config.CONFIG_FILE.symlink_to(outside)

    assert config._load_json_file(config.CONFIG_FILE, {"safe": True}) == {"safe": True}
    config.CONFIG_FILE.unlink()
    config.CONFIG_FILE.write_text('{"value":"' + ("x" * 128) + '"}', encoding="utf-8")
    assert config._load_json_file(
        config.CONFIG_FILE,
        {"safe": True},
        max_bytes=64,
    ) == {"safe": True}


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO identity contract")
def test_json_state_loader_rejects_fifo_without_blocking() -> None:
    os.mkfifo(config.CONFIG_FILE, 0o600)

    assert config._load_json_file(config.CONFIG_FILE, {"safe": True}) == {"safe": True}


def test_json_state_loader_rejects_path_replacement_during_descriptor_read(
    monkeypatch,
) -> None:
    original_document = {"value": "a" * 70_000}
    config.CONFIG_FILE.write_text(json.dumps(original_document), encoding="utf-8")
    displaced = config.CONFIG_FILE.with_suffix(".original")
    original_read = config.os.read
    swapped = False

    def swapping_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        chunk = original_read(descriptor, size)
        if chunk and not swapped:
            swapped = True
            config.CONFIG_FILE.rename(displaced)
            config.CONFIG_FILE.write_text('{"value":"replacement"}', encoding="utf-8")
        return chunk

    monkeypatch.setattr(config.os, "read", swapping_read)

    assert config._load_json_file(config.CONFIG_FILE, {"safe": True}) == {"safe": True}
    assert swapped is True
    assert not config.CONFIG_FILE.with_suffix(".json.corrupt").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner-only mode contract")
def test_config_writes_force_owner_only_directories_and_files(config_dir) -> None:
    os.chmod(config_dir, 0o755)
    previous = os.umask(0o022)
    try:
        cfg = Config(messages=[{"role": "user", "content": "safe"}])
        cfg.save()
        conversation = cfg.save_conversation("private")
    finally:
        os.umask(previous)

    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.CONFIG_FILE.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.HISTORY_DIR.stat().st_mode) == 0o700
    assert stat.S_IMODE(conversation.stat().st_mode) == 0o600


def test_json_loader_handles_invalid_utf8_and_unreadable_paths(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"{\xff}")
    directory = tmp_path / "directory.json"
    directory.mkdir()

    assert config._load_json_file(invalid, {"safe": True}) == {"safe": True}
    assert invalid.with_suffix(".json.corrupt").is_file()
    assert config._load_json_file(directory, []) == []


def test_state_lock_file_does_not_grow_per_acquisition(tmp_path):
    target = tmp_path / "state.json"

    for _ in range(50):
        with config._exclusive_state_lock(target):
            pass

    assert target.with_suffix(".json.lock").read_bytes() == b"x"


def test_default_system_points_algo_pattern_updates_to_reviewed_doc():
    assert "docs/ALGO.md" in config.DEFAULT_SYSTEM
    assert "update" in config.DEFAULT_SYSTEM


def test_load_refreshes_only_the_stock_legacy_system_prompt():
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(
        json.dumps({"system": config.LEGACY_DEFAULT_SYSTEM}),
        encoding="utf-8",
    )

    assert Config.load().system == config.DEFAULT_SYSTEM


def test_load_preserves_custom_system_prompt():
    custom = "You are my customized Algo CLI runtime."
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({"system": custom}), encoding="utf-8")

    assert Config.load().system == custom


def test_conversation_roundtrip():
    cfg = Config()
    cfg.messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    cfg.session_summary = "a summary"
    path = cfg.save_conversation("my-session")
    assert path.exists()

    fresh = Config()
    count = fresh.load_conversation("my-session")
    assert count == 2
    assert fresh.session_summary == "a summary"


def test_load_conversation_rejects_path_traversal(config_dir):
    config.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    outside = config.HISTORY_DIR.parent / "outside.json"
    outside.write_text('[{"role": "user", "content": "leaked"}]', encoding="utf-8")

    cfg = Config()

    with pytest.raises(FileNotFoundError, match="outside"):
        cfg.load_conversation("../outside")

    assert cfg.messages == []


def test_save_and_load_use_same_sanitized_conversation_name():
    cfg = Config()
    cfg.messages = [{"role": "user", "content": "saved"}]
    saved_path = cfg.save_conversation("my../session")

    fresh = Config()
    count = fresh.load_conversation("my../session")

    assert saved_path.name == "mysession.json"
    assert count == 1
    assert fresh.messages == [{"role": "user", "content": "saved"}]


def test_save_conversation_rejects_empty_name():
    cfg = Config()
    try:
        cfg.save_conversation("!!!")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-alphanumeric name")


def test_load_runtime_env(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text(
        '# a comment\nexport OLLAMA_CLI_TEST_KEY=value1\nQUOTED="value two"\nEMPTY=\n',
        encoding="utf-8",
    )
    loaded = load_runtime_env(env_file, override=True)
    assert loaded["OLLAMA_CLI_TEST_KEY"] == "value1"
    assert loaded["QUOTED"] == "value two"


def test_load_runtime_env_does_not_strip_single_quote_character(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text('ODD="\n', encoding="utf-8")

    loaded = load_runtime_env(env_file, override=True)

    assert loaded["ODD"] == '"'


def test_load_runtime_env_skips_invalid_keys_and_nul_values(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_bytes(b"VALID_RUNTIME_KEY=ok\nBAD-KEY=no\nNUL_VALUE=bad\x00value\n")
    monkeypatch.delenv("VALID_RUNTIME_KEY", raising=False)
    monkeypatch.delenv("NUL_VALUE", raising=False)

    loaded = load_runtime_env(env_file, override=True)

    assert loaded == {"VALID_RUNTIME_KEY": "ok"}
    assert os.environ["VALID_RUNTIME_KEY"] == "ok"
    assert "NUL_VALUE" not in os.environ


def test_load_runtime_env_falls_back_to_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / "env"
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("OLLAMA_CLI_DOTENV_FALLBACK=loaded\n", encoding="utf-8")
    monkeypatch.delenv("OLLAMA_CLI_ENV_FILE", raising=False)
    monkeypatch.delenv("OLLAMA_CLI_DOTENV_FALLBACK", raising=False)
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_file)
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", dotenv_file)

    loaded = config.load_runtime_env(override=True)

    assert loaded["OLLAMA_CLI_DOTENV_FALLBACK"] == "loaded"


# --- Rebrand dual-support tests (ALGO_CLI_* + ~/.algo_cli preference) ---


def test_new_env_prefix_takes_precedence(monkeypatch, tmp_path):
    new_dir = tmp_path / "algo_new"
    monkeypatch.setenv("ALGO_CLI_CONFIG_DIR", str(new_dir))
    # Even if old env is also set, new wins
    monkeypatch.setenv("OLLAMA_CLI_CONFIG_DIR", str(tmp_path / "ollama_old"))

    # Re-import to pick up env changes (config resolves at import time)
    import importlib
    import algo_cli.config as cfgmod

    importlib.reload(cfgmod)

    assert cfgmod.CONFIG_DIR == new_dir


def test_old_env_prefix_still_works_when_no_new(monkeypatch, tmp_path):
    old_dir = tmp_path / "legacy_only"
    monkeypatch.delenv("ALGO_CLI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("OLLAMA_CLI_CONFIG_DIR", str(old_dir))

    import importlib
    import algo_cli.config as cfgmod

    importlib.reload(cfgmod)

    assert cfgmod.CONFIG_DIR == old_dir


def test_has_legacy_data_and_migration_helpers(tmp_path, monkeypatch):
    legacy = tmp_path / ".ollama_cli"
    (legacy / "identity").mkdir(parents=True)
    (legacy / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)

    assert config.has_legacy_data() is True

    backup = config.get_legacy_backup_dir()
    # In this test we don't actually call perform_legacy_migration (it would write to real home)
    # We just verify the helper functions exist and the logic doesn't explode
    assert ".ollama_cli.backup" in str(backup)


def test_echo_selected_legacy_migration_projects_only_safe_configuration(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    backup = tmp_path / ".ollama_cli.backup"
    canary = "LEGACY_ECHO_MIGRATION_CANARY"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        json.dumps(
            {
                "model": "qwen3",
                "theme": "tokyo-night",
                "echo_veil_enabled": True,
                "echo_veil_protection": "required",
                "skill_crystallize_enabled": True,
                "session_summary": canary,
                "attempt_ledger": [{"raw": canary}],
                "messages": [{"role": "tool", "content": canary}],
            }
        ),
        encoding="utf-8",
    )
    (legacy / "memory.json").write_text(canary, encoding="utf-8")
    (legacy / "run_history.jsonl").write_text(canary, encoding="utf-8")
    (legacy / "prompt_history.txt").write_text(canary, encoding="utf-8")
    (legacy / "skills").mkdir()
    (legacy / "skills" / "derived.md").write_text(canary, encoding="utf-8")
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)
    monkeypatch.setattr(config, "get_legacy_backup_dir", lambda: backup)

    assert config.perform_legacy_migration() is True

    migrated = json.loads((current / "config.json").read_text(encoding="utf-8"))
    assert migrated["echo_veil_enabled"] is True
    assert migrated["echo_veil_protection"] == "required"
    assert migrated["skill_crystallize_enabled"] is False
    assert "session_summary" not in migrated
    assert "attempt_ledger" not in migrated
    assert "messages" not in migrated
    assert canary not in "".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in current.rglob("*") if path.is_file()
    )
    assert not backup.exists()
    assert canary in (legacy / "memory.json").read_text(encoding="utf-8")
    receipt = json.loads((current / ".legacy_migration_receipt.json").read_text(encoding="utf-8"))
    assert receipt["echo_authority_selected"] is True
    assert receipt["original_retained"] is True
    assert receipt["backup_created"] is False
    assert receipt["explicit_review_required"] is True
    assert receipt["inventory_truncated"] is False
    assert receipt["blocked_artifact_counts"]["memory"] >= 2
    assert all("/" not in key and "\\" not in key for key in receipt["artifact_counts"])
    if os.name == "posix":
        assert stat.S_IMODE(current.stat().st_mode) == 0o700
        assert stat.S_IMODE((current / "config.json").stat().st_mode) == 0o600


def test_echo_selected_sidecar_migration_refuses_plaintext_shadow_copy(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    canary = "LEGACY_ECHO_SIDECAR_CANARY"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        '{"echo_veil_enabled":true,"echo_veil_protection":"required"}',
        encoding="utf-8",
    )
    (legacy / "chatgpt_auth.json").write_text(canary, encoding="utf-8")
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)

    assert config.migrate_legacy_sidecar_files() == []
    assert not current.exists()


def test_malformed_legacy_config_fails_closed_without_partial_migration(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"echo_veil_enabled":', encoding="utf-8")
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)
    monkeypatch.setattr(
        config,
        "get_legacy_backup_dir",
        lambda: tmp_path / ".ollama_cli.backup",
    )

    with pytest.raises(config.LegacyMigrationError) as raised:
        config.perform_legacy_migration()
    assert "echo_veil" not in str(raised.value)
    assert not current.exists()


@pytest.mark.parametrize(
    "failed_name",
    [
        config.LEGACY_MIGRATION_INCOMPLETE_FILE,
        ".legacy_migration_receipt.json",
        "config.json",
        config.LEGACY_MIGRATION_COMPLETE_FILE,
    ],
)
def test_echo_legacy_migration_partial_publication_never_downgrades_startup(
    tmp_path,
    monkeypatch,
    failed_name: str,
) -> None:
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        '{"echo_veil_enabled":true,"echo_veil_protection":"required"}',
        encoding="utf-8",
    )
    (legacy / "memory.json").write_text("PARTIAL_MIGRATION_CANARY", encoding="utf-8")
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)
    monkeypatch.setattr(
        config,
        "get_legacy_backup_dir",
        lambda: tmp_path / ".ollama_cli.backup",
    )
    original_link = config.os.link

    def fail_selected(source, destination, **kwargs):
        if Path(destination).name == failed_name:
            raise OSError("simulated publication failure")
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(config.os, "link", fail_selected)

    with pytest.raises(config.LegacyMigrationError):
        config.perform_legacy_migration()
    if current.exists():
        assert not (current / config.LEGACY_MIGRATION_COMPLETE_FILE).exists()

    with pytest.raises(config.LegacyMigrationError):
        config.perform_legacy_migration()


def test_echo_legacy_migration_post_completion_fsync_failure_remains_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        '{"echo_veil_enabled":true,"echo_veil_protection":"required"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)
    monkeypatch.setattr(
        config,
        "get_legacy_backup_dir",
        lambda: tmp_path / ".ollama_cli.backup",
    )
    original_link = config.os.link
    original_fsync = config.os.fsync
    completion_linked = False

    def track_completion(source, destination, **kwargs):
        nonlocal completion_linked
        result = original_link(source, destination, **kwargs)
        if Path(destination).name == config.LEGACY_MIGRATION_COMPLETE_FILE:
            completion_linked = True
        return result

    def fail_after_completion(descriptor: int) -> None:
        if completion_linked:
            raise OSError("simulated post-completion durability failure")
        original_fsync(descriptor)

    monkeypatch.setattr(config.os, "link", track_completion)
    monkeypatch.setattr(config.os, "fsync", fail_after_completion)

    with pytest.raises(config.LegacyMigrationError):
        config.perform_legacy_migration()

    assert (current / config.LEGACY_MIGRATION_COMPLETE_FILE).is_file()
    assert (current / config.LEGACY_MIGRATION_INCOMPLETE_FILE).is_file()
    with pytest.raises(config.LegacyMigrationError):
        config.perform_legacy_migration()


@pytest.mark.parametrize("with_incomplete", [False, True])
def test_completed_legacy_migration_without_config_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_incomplete: bool,
) -> None:
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        '{"echo_veil_enabled":true,"echo_veil_protection":"required"}',
        encoding="utf-8",
    )
    current.mkdir(mode=0o700)
    completion = current / config.LEGACY_MIGRATION_COMPLETE_FILE
    completion.write_bytes(b"Legacy configuration migrated by Algo CLI.\n")
    completion.chmod(0o600)
    if with_incomplete:
        incomplete = current / config.LEGACY_MIGRATION_INCOMPLETE_FILE
        incomplete.write_bytes(b"Algo CLI legacy migration publication is incomplete.\n")
        incomplete.chmod(0o600)
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)

    with pytest.raises(config.LegacyMigrationError):
        config.perform_legacy_migration()

    with pytest.raises(config.LegacyMigrationError):
        config.perform_legacy_migration()


def test_sidecar_migration_includes_provider_credentials(tmp_path, monkeypatch):
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        '{"echo_veil_enabled":false,"echo_veil_protection":"optional"}',
        encoding="utf-8",
    )
    for name in (
        "chatgpt_auth.json",
        "google_workspace_auth.json",
        "google_workspace_pending_login.json",
        "env",
    ):
        (legacy / name).write_text("{}", encoding="utf-8")
    (legacy / "codex-chatgpt").mkdir()
    (legacy / "codex-chatgpt" / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)

    moved = config.migrate_legacy_sidecar_files()

    assert set(moved) == {
        "chatgpt_auth.json",
        "google_workspace_auth.json",
        "google_workspace_pending_login.json",
        "env",
    }
    assert not (current / "codex-chatgpt").exists()
    if os.name == "posix":
        assert stat.S_IMODE(current.stat().st_mode) == 0o700
        for name in moved:
            assert stat.S_IMODE((current / name).stat().st_mode) == 0o600


def test_sidecar_migration_rejects_symlink_source(tmp_path, monkeypatch) -> None:
    legacy = tmp_path / ".ollama_cli"
    current = tmp_path / ".algo_cli"
    outside = tmp_path / "outside-auth.json"
    legacy.mkdir()
    (legacy / "config.json").write_text(
        '{"echo_veil_enabled":false,"echo_veil_protection":"optional"}',
        encoding="utf-8",
    )
    outside.write_text("SIDECAR_SYMLINK_CANARY", encoding="utf-8")
    (legacy / "chatgpt_auth.json").symlink_to(outside)
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", legacy)
    monkeypatch.setattr(config, "CONFIG_DIR", current)

    assert config.migrate_legacy_sidecar_files() == []
    assert not current.exists()
