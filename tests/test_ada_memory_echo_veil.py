"""Adversarial coverage for Ada's protected-memory integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import ANY

import pytest


def _supported(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    class SyntheticEmbeddingUnavailable(RuntimeError):
        pass

    monkeypatch.setattr(module, "ECHO_VEIL_AVAILABLE", True)
    monkeypatch.setattr(module, "ECHO_VEIL_IMPORT_ERROR", "")
    monkeypatch.setattr(module, "ECHO_VEIL_SOURCE_VERSION", "0.6.0")
    monkeypatch.setattr(module, "ECHO_VEIL_DISTRIBUTION_VERSION", "0.6.0")
    monkeypatch.setattr(module, "ECHO_VEIL_INSTALLATION_KIND", "registry-or-wheel")
    monkeypatch.setattr(module, "EmbeddingUnavailable", SyntheticEmbeddingUnavailable)


def test_readiness_rejects_source_distribution_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_DISTRIBUTION_VERSION", "0.4.0")

    readiness = memory_echo_veil.get_echo_veil_readiness(
        {
            "echo_veil_enabled": True,
            "echo_veil_protection": "required",
        }
    )

    assert readiness["installed"] is True
    assert readiness["version_supported"] is False
    assert readiness["crypto_initialized"] is False
    assert readiness["write_wired"] is False
    assert readiness["index_wired"] is False
    assert readiness["retrieval_wired"] is False
    assert readiness["persistence_wired"] is False
    assert readiness["restart_restored"] is False
    assert readiness["rotation_ready"] is False
    assert readiness["healthy"] is False
    assert readiness["production_ready"] is False
    assert "module_origin" not in readiness


def test_readiness_rejects_retired_echo_veil_api_even_when_metadata_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_SOURCE_VERSION", "0.5.0")
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_DISTRIBUTION_VERSION", "0.5.0")

    readiness = memory_echo_veil.get_echo_veil_readiness(
        {
            "echo_veil_enabled": True,
            "echo_veil_protection": "required",
        }
    )

    assert readiness["version_supported"] is False
    assert readiness["installation_identity"] == "registry-or-wheel-unsupported"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (None, "registry-or-wheel"),
        ('{"dir_info":{"editable":true},"url":"file:///checkout"}', "editable"),
        (
            '{"vcs_info":{"commit_id":"' + ("a" * 40) + '"},"url":"git+https://example.invalid/repo"}',
            "vcs-pinned",
        ),
        (
            '{"archive_info":{"hash":"sha256=' + ("b" * 64) + '"},"url":"file:///echo.whl"}',
            "archive-pinned",
        ),
        ('{"url":"file:///unbounded-source"}', "direct-url-unpinned"),
        ("not-json", "direct-url-unpinned"),
        ("x" * 16_385, "direct-url-unpinned"),
    ],
)
def test_distribution_installation_identity_is_fail_closed(
    document: str | None,
    expected: str,
) -> None:
    from algo_cli import memory_echo_veil

    class FakeDistribution:
        def read_text(self, name: str) -> str | None:
            assert name == "direct_url.json"
            return document

    assert memory_echo_veil._distribution_installation_kind(FakeDistribution()) == expected


def test_distribution_installation_identity_rejects_unreadable_metadata() -> None:
    from algo_cli import memory_echo_veil

    class BrokenDistribution:
        def read_text(self, _name: str) -> str:
            raise OSError("synthetic metadata read failure")

    assert memory_echo_veil._distribution_installation_kind(BrokenDistribution()) == "direct-url-unpinned"


def test_required_mode_rejects_editable_or_unpinned_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_INSTALLATION_KIND", "editable")
    config = {
        "echo_veil_enabled": True,
        "echo_veil_protection": "required",
    }

    readiness = memory_echo_veil.get_echo_veil_readiness(config)
    assert readiness["version_supported"] is False
    assert readiness["installation_identity"] == "editable-unsupported"
    with pytest.raises(RuntimeError, match="writes are blocked"):
        memory_echo_veil.create_echo_veil_layer(config)


def test_authoritative_layer_constructs_agent_memory_without_raw_key_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    captured: dict[str, object] = {}

    class FakeEmbedder:
        def __init__(self, **kwargs: object) -> None:
            captured["embedder"] = kwargs

    class FakeMemory:
        def __init__(self, state_dir, **kwargs: object) -> None:
            captured["state_dir"] = state_dir
            captured["memory"] = kwargs

        def close(self) -> None:
            return None

        def doctor(self):
            return {
                "security_schema": "scoped-v2",
                "scope_bound": True,
                "readiness": {
                    "crypto_initialized": True,
                    "write_wired": True,
                    "index_wired": True,
                    "retrieval_wired": True,
                    "persistence_wired": True,
                    "restart_restored": True,
                },
                "rotation": {},
            }

    monkeypatch.setattr(memory_echo_veil, "OllamaTextEmbedder", FakeEmbedder)
    monkeypatch.setattr(memory_echo_veil, "AgentMemory", FakeMemory)
    config = {
        "echo_veil_enabled": True,
        "echo_veil_protection": "required",
        "echo_veil_profile": "algo-test",
        "echo_veil_scope": "algo-cli:test",
        "echo_veil_state_dir": str(tmp_path),
        "echo_veil_capacity": 27,
        "harness_embed_model": "qwen3-embedding:latest",
        "echo_veil_embedding_dimension": 4096,
        "host": "http://localhost:11434",
        "echo_veil_crypto_key_path": "/must/not/be/read.json",
    }

    layer = memory_echo_veil.create_echo_veil_layer(config)

    assert layer is not None
    assert captured["state_dir"] == tmp_path
    assert captured["memory"] == {
        "profile": "algo-test",
        "scope": "algo-cli:test",
        "capacity": 27,
        "embed": ANY,
    }
    assert captured["embedder"] == {
        "model": "qwen3-embedding:latest",
        "base_url": "http://127.0.0.1:11434",
        "dimension": 4096,
    }


def test_required_mode_blocks_write_when_crypto_cannot_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)

    class BrokenEmbedder:
        def __init__(self, **_kwargs: object) -> None:
            raise RuntimeError("synthetic initialization failure")

    monkeypatch.setattr(memory_echo_veil, "OllamaTextEmbedder", BrokenEmbedder)
    config = {
        "echo_veil_enabled": True,
        "echo_veil_protection": "required",
    }

    with pytest.raises(RuntimeError, match="writes are blocked"):
        memory_echo_veil.create_echo_veil_layer(config)
    with pytest.raises(RuntimeError, match="write"):
        memory_echo_veil.remember_with_echo_veil(config, "never plaintext")


def test_required_mode_rejects_legacy_or_incomplete_echo_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    closed: list[bool] = []

    class FakeEmbedder:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class LegacyMemory:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def doctor(self):
            return {
                "security_schema": "legacy-v1",
                "scope_bound": False,
                "readiness": {
                    "crypto_initialized": True,
                    "write_wired": True,
                    "index_wired": True,
                    "retrieval_wired": True,
                    "persistence_wired": True,
                    "restart_restored": True,
                },
            }

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(memory_echo_veil, "OllamaTextEmbedder", FakeEmbedder)
    monkeypatch.setattr(memory_echo_veil, "AgentMemory", LegacyMemory)
    config = {
        "echo_veil_enabled": True,
        "echo_veil_protection": "required",
        "echo_veil_state_dir": str(tmp_path),
    }

    with pytest.raises(RuntimeError, match="writes are blocked"):
        memory_echo_veil.create_echo_veil_layer(config)
    assert closed == [True]


def test_optional_mode_returns_none_instead_of_claiming_encryption(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)

    class BrokenEmbedder:
        def __init__(self, **_kwargs: object) -> None:
            raise RuntimeError("synthetic initialization failure")

    monkeypatch.setattr(memory_echo_veil, "OllamaTextEmbedder", BrokenEmbedder)
    assert (
        memory_echo_veil.create_echo_veil_layer(
            {
                "echo_veil_enabled": True,
                "echo_veil_protection": "optional",
            }
        )
        is None
    )
    assert memory_echo_veil._LAST_INITIALIZATION_ERROR == "initialization_failed"
    assert "initialization_failed" in caplog.text
    assert "synthetic initialization failure" not in caplog.text


def test_readiness_exposes_independent_runtime_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)

    class FakeLayer:
        degraded = False

        def doctor(self):
            return {
                "readiness": {
                    "crypto_initialized": True,
                    "write_wired": True,
                    "index_wired": True,
                    "retrieval_wired": True,
                    "persistence_wired": True,
                    "restart_restored": True,
                    "rotation_ready": True,
                    "healthy": True,
                },
                "degraded": False,
                "key_id": "ev-0123456789abcdef",
                "security_schema": "scoped-v2",
                "quarantined_records": 0,
                "rotation": {"state": "idle"},
            }

    monkeypatch.setattr(
        memory_echo_veil,
        "create_echo_veil_layer",
        lambda _config: FakeLayer(),
    )
    readiness = memory_echo_veil.get_echo_veil_readiness(
        {
            "echo_veil_enabled": True,
            "echo_veil_protection": "required",
        }
    )

    assert readiness["version_supported"] is True
    assert readiness["crypto_initialized"] is True
    assert readiness["write_wired"] is True
    assert readiness["index_wired"] is True
    assert readiness["retrieval_wired"] is True
    assert readiness["persistence_wired"] is True
    assert readiness["restart_restored"] is True
    assert readiness["rotation_ready"] is True
    assert readiness["healthy"] is True
    assert readiness["local_protection_ready"] is True
    assert readiness["production_ready"] is False
    assert readiness["key_id"] == "ev-0123456789abcdef"
    assert "key_path" not in readiness
    assert "module_origin" not in readiness


def test_required_config_memory_write_uses_echo_without_plaintext_shadow(
    config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil
    from algo_cli.config import Config

    calls: list[tuple[str, str]] = []

    def fake_remember(config, fact: str, *, source: str) -> bool:
        calls.append((fact, source))
        config.memories.append(fact)
        return True

    monkeypatch.setattr(
        memory_echo_veil,
        "remember_with_echo_veil",
        fake_remember,
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    assert cfg.remember_fact("The protected fact.") is True

    assert calls == [("The protected fact.", "user_explicit")]
    assert cfg.memories == ["The protected fact."]
    assert not (config_dir / "memory.json").exists()
    assert not (config_dir / "system_memory.json").exists()


def test_required_runtime_remember_bypasses_legacy_catalog(
    config_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime, memory_echo_veil
    from algo_cli.config import Config

    calls: list[tuple[str, str]] = []

    def fake_remember(config, fact: str, *, source: str) -> bool:
        calls.append((fact, source))
        config.memories.append(fact)
        return True

    monkeypatch.setattr(
        memory_echo_veil,
        "remember_with_echo_veil",
        fake_remember,
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    assert julia_memory_runtime.remember_fact(
        cfg,
        "The ordinary path is protected.",
        source="auto_capture",
    )

    assert calls == [("The ordinary path is protected.", "auto_capture")]
    assert not (config_dir / "memory.json").exists()
    assert not (config_dir / "system_memory.json").exists()


def test_required_automatic_capture_routes_to_echo_without_plaintext_state(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime, memory_echo_veil
    from algo_cli.config import Config

    calls: list[tuple[str, str]] = []

    def fake_remember(config, fact: str, *, source: str) -> bool:
        calls.append((fact, source))
        config.memories.append(fact)
        return True

    monkeypatch.setattr(
        memory_echo_veil,
        "remember_with_echo_veil",
        fake_remember,
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    result = julia_memory_runtime.capture_completed_user_turn(
        cfg,
        "Remember that our standard shell is zsh.",
        completed=True,
    )

    assert result["status"] == "stored"
    assert calls == [("our standard shell is zsh.", "auto_capture")]
    assert not (config_dir / "memory.json").exists()
    assert not (config_dir / "system_memory.json").exists()
    for path in config_dir.rglob("*"):
        if path.is_file():
            assert b"standard shell" not in path.read_bytes()


def test_required_slash_and_tool_writes_do_not_create_intuition_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime, main, tools
    from algo_cli.config import Config

    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )
    captured: list[str] = []
    infos: list[str] = []
    monkeypatch.setattr(
        julia_memory_runtime,
        "remember_fact",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        main,
        "capture_intuition_block",
        lambda _cfg, _type, text, **_kwargs: captured.append(text),
    )
    monkeypatch.setattr(main, "show_info", infos.append)
    monkeypatch.setattr(
        julia_memory_runtime,
        "forget_memory_index",
        lambda *_args, **_kwargs: "protected forgotten payload",
    )

    handled, _client = main.handle_command(
        "/remember protected slash fact",
        cfg,
        None,
    )
    tool_result = tools.remember("protected tool fact", cfg)
    forgot_handled, _client = main.handle_command("/forget 1", cfg, None)

    assert handled is True
    assert forgot_handled is True
    assert tool_result == "Protected memory saved."
    assert "protected tool fact" not in tool_result
    assert captured == []
    assert infos == ["Memory saved.", "Protected memory forgotten."]
    assert "protected forgotten payload" not in " ".join(infos)


def test_required_context_never_falls_back_to_plaintext_memories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import context_budget, memory_echo_veil
    from algo_cli.config import Config

    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
        memories=["legacy plaintext fallback"],
        messages=[{"role": "user", "content": "What is the answer?"}],
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "recall_with_echo_veil",
        lambda *_args, **_kwargs: [],
    )

    assert context_budget._echo_veil_memory_items(cfg) == []


def test_required_legacy_memory_command_is_rejected_before_catalog_write(
    config_dir: Path,
) -> None:
    from algo_cli import julia_memory_runtime
    from algo_cli.config import Config

    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
        memories=["protected in-process fact"],
    )

    with pytest.raises(julia_memory_runtime.MemorySystemError, match="prohibited"):
        julia_memory_runtime.command_text(
            "add --tier history never write this shadow",
            cfg,
        )

    assert not (config_dir / "system_memory.json").exists()
    assert not (config_dir / "memory.json").exists()


def test_protection_policy_rejects_ambiguous_values() -> None:
    from algo_cli.ada_memory_echo_veil import protection_policy

    with pytest.raises(ValueError, match="optional or required"):
        protection_policy(SimpleNamespace(echo_veil_protection="best-effort"))


def test_black_box_ordinary_write_encrypts_restarts_and_scope_filters(
    tmp_path: Path,
) -> None:
    """Exercise the ordinary Algo runtime in two fresh Python processes."""

    echo_source = Path(__file__).resolve().parents[2] / "echo-veil" / "src"
    if not (echo_source / "echo_veil" / "agent_memory.py").is_file():
        pytest.skip("sibling Echo Veil source is unavailable in this checkout")
    config_dir = tmp_path / "algo-config"
    state_dir = tmp_path / "echo-state"
    secret = "black box secret phrase cobalt meadow 731"
    environment = dict(os.environ)
    environment["ALGO_CLI_CONFIG_DIR"] = str(config_dir)
    environment["OLLAMA_CLI_CONFIG_DIR"] = str(config_dir)
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(Path(__file__).resolve().parents[1]),
            str(echo_source),
        ]
    )
    shared = f"""
from algo_cli import memory_echo_veil as bridge
from algo_cli.config import Config
from echo_veil.agent_memory import HashingTextEmbedder
bridge.ECHO_VEIL_AVAILABLE = True
bridge.ECHO_VEIL_SOURCE_VERSION = "0.6.0"
bridge.ECHO_VEIL_DISTRIBUTION_VERSION = "0.6.0"
bridge.ECHO_VEIL_INSTALLATION_KIND = "registry-or-wheel"
bridge.EmbeddingUnavailable = RuntimeError
bridge.OllamaTextEmbedder = lambda **kwargs: HashingTextEmbedder()
cfg = Config(
    echo_veil_enabled=True,
    echo_veil_protection="required",
    echo_veil_profile="black-box",
    echo_veil_scope="algo-cli:black-box",
    echo_veil_state_dir={str(state_dir)!r},
)
"""
    write_script = (
        shared
        + f"""
from algo_cli.julia_memory_runtime import remember_fact
created = remember_fact(cfg, {secret!r}, source="user_explicit")
print(__import__("json").dumps({{"created": created}}))
"""
    )
    written = subprocess.run(
        [sys.executable, "-c", write_script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert written.returncode == 0, written.stderr
    assert json.loads(written.stdout)["created"] is True
    assert not (config_dir / "memory.json").exists()
    assert not (config_dir / "system_memory.json").exists()
    for path in state_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()

    recall_script = (
        shared
        + f"""
layer = bridge.create_echo_veil_layer(cfg)
response = layer.recall({secret!r}, top_k=2)
print(__import__("json").dumps(response))
"""
    )
    recalled = subprocess.run(
        [sys.executable, "-c", recall_script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert recalled.returncode == 0, recalled.stderr
    response = json.loads(recalled.stdout)
    assert response["results"][0]["payload"] == secret
    assert response["results"][0]["topic_protected"] is True

    wrong_scope = shared.replace(
        'echo_veil_scope="algo-cli:black-box"',
        'echo_veil_scope="algo-cli:wrong-scope"',
    )
    refused = subprocess.run(
        [sys.executable, "-c", wrong_scope + "\nbridge.create_echo_veil_layer(cfg)\n"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert refused.returncode != 0
    assert secret not in refused.stderr
