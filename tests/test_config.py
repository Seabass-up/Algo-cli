"""Config load/save and runtime-env parsing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from algo_cli import config
from algo_cli.config import (
    CODE_RAG_CONSENT_VERSION,
    DEFAULT_CHAT_STREAM_TIMEOUT_SECONDS,
    Config,
    code_rag_consent_granted,
    load_runtime_env,
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
    assert cfg.algorithmic_tool_policy_enabled is False
    assert cfg.echo_veil_capacity == 400
    assert cfg.echo_veil_production is False
    assert cfg.memory_auto_capture_enabled is True
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
    cfg.memory_auto_capture_enabled = False
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
    assert reloaded.memory_auto_capture_enabled is False
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
    config.MEMORY_FILE.write_text("[\"ok\",", encoding="utf-8")

    loaded = Config.load()

    assert loaded.memories == []
    assert config.MEMORY_FILE.with_suffix(config.MEMORY_FILE.suffix + ".corrupt").exists()


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
        "# a comment\n"
        "export OLLAMA_CLI_TEST_KEY=value1\n"
        'QUOTED="value two"\n'
        "EMPTY=\n",
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
