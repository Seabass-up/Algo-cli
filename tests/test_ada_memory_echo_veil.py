"""Adversarial coverage for Ada's protected-memory integration."""

from __future__ import annotations

import json
import inspect
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
    monkeypatch.setattr(module, "ECHO_VEIL_SOURCE_VERSION", "0.7.0")
    monkeypatch.setattr(module, "ECHO_VEIL_DISTRIBUTION_VERSION", "0.7.0")
    monkeypatch.setattr(module, "ECHO_VEIL_INSTALLATION_KIND", "vcs-pinned")
    monkeypatch.setattr(
        module,
        "ECHO_VEIL_SOURCE_COMMIT",
        module.QUALIFIED_ECHO_VEIL_COMMIT,
    )
    monkeypatch.setattr(module, "EmbeddingUnavailable", SyntheticEmbeddingUnavailable)


def _shielded_memory_layers() -> dict[str, object]:
    return {
        "contract": "shielded-four-layer-v1",
        "all_records_shielded": True,
        "context_trace": "bounded-authenticated-outgoing-v1",
        "content_policy": "bounded-seed-crystal-v1",
        "live_refresh": "protected-supersession-or-renewal-v1",
    }


def _complete_readiness() -> dict[str, bool]:
    return {
        "crypto_initialized": True,
        "write_wired": True,
        "index_wired": True,
        "retrieval_wired": True,
        "persistence_wired": True,
        "restart_restored": True,
        "layer_contract_wired": True,
        "context_trace_wired": True,
        "competing_memory_wired": True,
        "content_policy_wired": True,
        "live_refresh_wired": True,
    }


def test_algo_defaults_to_the_shared_local_echo_authority() -> None:
    from algo_cli import memory_echo_veil
    from algo_cli.config import Config

    config = Config()

    assert memory_echo_veil.DEFAULT_ECHO_PROFILE == "echo-universal-qwen3-v1"
    assert memory_echo_veil.DEFAULT_ECHO_SCOPE == "local-user"
    assert memory_echo_veil.DEFAULT_ECHO_DIMENSION == 1024
    assert config.echo_veil_profile == "echo-universal-qwen3-v1"
    assert config.echo_veil_scope == "local-user"
    assert config.echo_veil_state_dir == ""
    assert config.echo_veil_embedding_dimension == 1024
    assert config.echo_veil_embedding_keep_alive_seconds == 0
    assert config.echo_veil_embedding_context_length == 16_384
    assert config.echo_veil_embedding_gpu_layers == 0


def test_algo_tool_registry_exposes_the_full_governed_echo_contract() -> None:
    from algo_cli import action_registry, tools
    from algo_cli.marcus_authority import EffectClass

    expected = {
        "echo_veil_remember",
        "echo_veil_refresh_live",
        "echo_veil_promote",
        "echo_veil_recall",
        "echo_veil_context",
        "echo_veil_list",
        "echo_veil_forget",
        "echo_veil_doctor",
        "echo_veil_reindex",
    }

    assert expected.issubset(tools.TOOL_MAP)
    for name in expected:
        assert "cfg" not in inspect.signature(tools.TOOL_MAP[name]).parameters

    assert action_registry.action_requires_approval("echo_veil_list") is False
    assert action_registry.action_requires_approval("echo_veil_doctor") is False
    for name in {
        "echo_veil_remember",
        "echo_veil_refresh_live",
        "echo_veil_promote",
        "echo_veil_recall",
        "echo_veil_context",
        "echo_veil_forget",
        "echo_veil_reindex",
    }:
        assert action_registry.action_requires_approval(name) is True

    assert (
        action_registry.get_action_spec("echo_veil_recall").effect_class
        is EffectClass.LOCAL_MUTATION
    )
    assert (
        action_registry.get_action_spec("echo_veil_context").effect_class
        is EffectClass.LOCAL_MUTATION
    )


def test_echo_observations_receive_the_read_only_runtime_baseline(
    tmp_path: Path,
) -> None:
    from algo_cli.config import Config
    from algo_cli.nathan_runtime import preflight_runtime_tool

    cfg = Config(
        cwd=str(tmp_path),
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    for name in ("echo_veil_doctor", "echo_veil_list"):
        preflight = preflight_runtime_tool(name, {}, cfg)
        assert preflight.policy.disposition.value == "allow"
        assert preflight.policy.grant_id.startswith("grant-")


def test_algo_echo_tools_fail_closed_and_preserve_operation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil, tools
    from algo_cli.config import Config

    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    closed_leases: list[bool] = []

    class FakeMemory:
        def remember(self, *args: object, **kwargs: object) -> dict[str, object]:
            calls.append(("remember", args, kwargs))
            return {
                "created": True,
                "vine_id": "live-id",
                "memory_layer": kwargs["layer"],
            }

        def forget(self, vine_id: str) -> dict[str, object]:
            calls.append(("forget", (vine_id,), {}))
            return {"vine_id": vine_id, "forgotten": True}

        def reindex(self) -> dict[str, object]:
            calls.append(("reindex", (), {}))
            return {"reindexed": 3}

        def close(self) -> None:
            closed_leases.append(True)

    memory = FakeMemory()
    monkeypatch.setattr(
        memory_echo_veil,
        "create_echo_veil_layer",
        lambda _cfg: memory,
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "refresh_live_with_echo_veil",
        lambda *args, **kwargs: (
            calls.append(("refresh", args, kwargs))
            or {"refreshed": True, "vine_id": args[1]}
        ),
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "promote_with_echo_veil",
        lambda *args, **kwargs: (
            calls.append(("promote", args, kwargs))
            or {"promoted": True, "vine_id": args[1]}
        ),
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "recall_response_with_echo_veil",
        lambda *args, **kwargs: {
            "results": [{"vine_id": "short-id", "memory_layer": "short_term"}],
            "degraded": False,
            "semantic_available": True,
            "lifecycle_mutated": True,
        },
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "context_with_echo_veil",
        lambda *args, **kwargs: {
            "logic_roots": [{"vine_id": "logic-id"}],
            "evidence": [{"vine_id": "long-id"}],
            "lifecycle_mutated": True,
        },
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "list_echo_veil_memories",
        lambda *args, **kwargs: [
            {
                "vine_id": "active-id",
                "memory_layer": "short_term",
                "superseded_by": None,
            },
            {
                "vine_id": "old-id",
                "memory_layer": "short_term",
                "superseded_by": "active-id",
            },
        ],
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "get_echo_veil_readiness",
        lambda _cfg: {
            "healthy": True,
            "all_records_shielded": True,
            "production_ready": False,
        },
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    remembered = json.loads(
        tools.echo_veil_remember(
            "active state",
            "Algo / active",
            layer="live",
            expires_in_seconds=600,
            cfg=cfg,
        )
    )
    refreshed = json.loads(
        tools.echo_veil_refresh_live(
            "live-id",
            "updated state",
            expires_in_seconds=600,
            cfg=cfg,
        )
    )
    promoted = json.loads(
        tools.echo_veil_promote(
            "live-id",
            "short_term",
            "needed tomorrow",
            cfg=cfg,
        )
    )
    recalled = json.loads(
        tools.echo_veil_recall(
            "what state is active?",
            layers="live,short_term",
            cfg=cfg,
        )
    )
    context = json.loads(
        tools.echo_veil_context("why was it selected?", cfg=cfg)
    )
    inventory = json.loads(tools.echo_veil_list(cfg=cfg))
    forgotten = json.loads(tools.echo_veil_forget("active-id", cfg=cfg))
    doctor = json.loads(tools.echo_veil_doctor(cfg=cfg))
    reindexed = json.loads(tools.echo_veil_reindex(cfg=cfg))

    for result in {
        "remember": remembered,
        "refresh_live": refreshed,
        "promote": promoted,
        "recall": recalled,
        "context": context,
        "list": inventory,
        "forget": forgotten,
        "doctor": doctor,
        "reindex": reindexed,
    }.values():
        assert result["memory_authority"] == "echo_veil"
        assert result["plaintext_fallback"] is False
    assert recalled["semantic_available"] is True
    assert recalled["lifecycle_mutated"] is True
    assert context["evidence"] == [{"vine_id": "long-id"}]
    assert inventory["inventory_only"] is True
    assert inventory["semantic_retrieval_performed"] is False
    assert inventory["lifecycle_mutated"] is False
    assert inventory["records"] == [
        {
            "memory_layer": "short_term",
            "superseded_by": None,
            "vine_id": "active-id",
        }
    ]
    assert forgotten["forgotten"] is True
    assert doctor["all_records_shielded"] is True
    assert doctor["production_ready"] is False
    assert reindexed["reindexed"] == 3
    assert calls[0][0] == "remember"
    assert calls[0][2]["provenance"] == ["algo-cli:model_tool"]
    assert calls[0][2]["layer"] == "live"
    assert calls[1][0] == "refresh"
    assert calls[2][0] == "promote"
    assert len(closed_leases) == 3

    optional_cfg = Config(
        echo_veil_enabled=False,
        echo_veil_protection="optional",
        memories=["must not be returned"],
    )
    with pytest.raises(RuntimeError, match="no legacy memory fallback"):
        tools.echo_veil_recall("legacy?", cfg=optional_cfg)
    with pytest.raises(ValueError, match="Long-Term requires"):
        tools.echo_veil_remember(
            "invalid direct durable write",
            "Algo / invalid",
            layer="long_term",
            cfg=cfg,
        )


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
    assert readiness["installation_identity"] == "vcs-pinned-unsupported"


def test_readiness_rejects_unqualified_pinned_source_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_SOURCE_COMMIT", "a" * 40)

    readiness = memory_echo_veil.get_echo_veil_readiness(
        {
            "echo_veil_enabled": True,
            "echo_veil_protection": "required",
        }
    )

    assert readiness["version_supported"] is False
    assert readiness["qualified_source_revision"] is False
    assert readiness["healthy"] is False


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
                "readiness": _complete_readiness(),
                "memory_layers": _shielded_memory_layers(),
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
        "echo_veil_embedding_keep_alive_seconds": 0,
        "echo_veil_embedding_context_length": 16_384,
        "echo_veil_embedding_gpu_layers": 0,
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
        "keep_alive_seconds": 0,
        "context_length": 16_384,
        "gpu_layers": 0,
    }
    layer.close()


def test_normal_operations_release_distinct_profile_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    opened: list[int] = []
    closed: list[int] = []

    class FakeLayer:
        def __init__(self, _config: object) -> None:
            self.lease_id = len(opened) + 1
            opened.append(self.lease_id)

        def recall(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"results": [], "lease_id": self.lease_id}

        def close(self) -> None:
            closed.append(self.lease_id)

    monkeypatch.setattr(memory_echo_veil, "EchoVeilMemoryLayer", FakeLayer)
    monkeypatch.setattr(memory_echo_veil, "_version_supported", lambda: True)
    config = {
        "echo_veil_enabled": True,
        "echo_veil_protection": "required",
    }

    first = memory_echo_veil.recall_response_with_echo_veil(config, "first")
    second = memory_echo_veil.recall_response_with_echo_veil(config, "second")

    assert first["lease_id"] == 1
    assert second["lease_id"] == 2
    assert opened == [1, 2]
    assert closed == [1, 2]


def test_normal_operation_releases_profile_lease_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    closed: list[bool] = []

    class BrokenLayer:
        def recall(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("synthetic recall failure")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        memory_echo_veil,
        "create_echo_veil_layer",
        lambda _config: BrokenLayer(),
    )

    with pytest.raises(RuntimeError, match="synthetic recall failure"):
        memory_echo_veil.recall_response_with_echo_veil(
            {
                "echo_veil_enabled": True,
                "echo_veil_protection": "required",
            },
            "fail safely",
        )

    assert closed == [True]


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


def test_required_mode_rejects_scoped_profile_without_four_layer_shield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    closed: list[bool] = []

    class FakeEmbedder:
        def __init__(self, **_kwargs: object) -> None:
            pass

    class IncompleteMemory:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def doctor(self):
            return {
                "security_schema": "scoped-v2",
                "scope_bound": True,
                "readiness": _complete_readiness(),
            }

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(memory_echo_veil, "OllamaTextEmbedder", FakeEmbedder)
    monkeypatch.setattr(memory_echo_veil, "AgentMemory", IncompleteMemory)

    with pytest.raises(RuntimeError, match="writes are blocked"):
        memory_echo_veil.create_echo_veil_layer(
            {
                "echo_veil_enabled": True,
                "echo_veil_protection": "required",
                "echo_veil_state_dir": str(tmp_path),
            }
        )
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


def test_required_mode_rejects_disabled_adapter() -> None:
    from algo_cli import memory_echo_veil

    config = {
        "echo_veil_enabled": False,
        "echo_veil_protection": "required",
    }

    with pytest.raises(RuntimeError, match="protection is disabled"):
        memory_echo_veil.create_echo_veil_layer(config)
    with pytest.raises(RuntimeError, match="protection is disabled"):
        memory_echo_veil.recall_response_with_echo_veil(
            config,
            "do not continue without protected memory",
        )
    assert memory_echo_veil._LAST_INITIALIZATION_ERROR == (
        "disabled_by_configuration"
    )


def test_required_mode_accepts_only_shielded_fail_closed_availability_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    captured: dict[str, object] = {}

    class UnavailableEmbedder:
        def __init__(self, **_kwargs: object) -> None:
            raise memory_echo_veil.EmbeddingUnavailable("offline")

    class FakeAvailability:
        def __init__(self, state_dir, **kwargs: object) -> None:
            captured["state_dir"] = state_dir
            captured["availability"] = kwargs

        def doctor(self):
            memory_layers = _shielded_memory_layers()
            memory_layers["live_refresh"] = "unavailable-read-only"
            return {
                "security_schema": "scoped-v2",
                "scope_bound": True,
                "writes_available": False,
                "lifecycle_mutation_available": False,
                "semantic_available": False,
                "memory_layers": memory_layers,
            }

        def recall(self, *_args: object, **_kwargs: object):
            return {
                "degraded": True,
                "semantic_available": False,
                "results": [],
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        memory_echo_veil,
        "OllamaTextEmbedder",
        UnavailableEmbedder,
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "AlwaysAvailableMemory",
        FakeAvailability,
    )
    layer = memory_echo_veil.create_echo_veil_layer(
        {
            "echo_veil_enabled": True,
            "echo_veil_protection": "required",
            "echo_veil_state_dir": str(tmp_path),
            "echo_veil_profile": "availability-test",
            "echo_veil_scope": "algo-cli:test",
        }
    )

    assert layer is not None
    assert layer.degraded is True
    assert layer.writes_available is False
    assert layer.recall("known keyword")["semantic_available"] is False
    with pytest.raises(RuntimeError, match="writes are blocked"):
        layer.remember("blocked")
    assert captured["state_dir"] == tmp_path
    assert captured["availability"] == {
        "profile": "availability-test",
        "scope": "algo-cli:test",
        "reason": "algo_cli_embedding_service_unavailable",
    }
    layer.close()


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
                    **_complete_readiness(),
                    "rotation_ready": True,
                    "healthy": True,
                },
                "memory_layers": _shielded_memory_layers(),
                "degraded": False,
                "key_id": "ev-0123456789abcdef",
                "security_schema": "scoped-v2",
                "quarantined_records": 0,
                "rotation": {"state": "idle"},
            }

        def close(self) -> None:
            return None

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
    assert readiness["layer_contract_wired"] is True
    assert readiness["context_trace_wired"] is True
    assert readiness["competing_memory_wired"] is True
    assert readiness["content_policy_wired"] is True
    assert readiness["live_refresh_wired"] is True
    assert readiness["all_records_shielded"] is True
    assert readiness["rotation_ready"] is True
    assert readiness["healthy"] is True
    assert readiness["local_protection_ready"] is True
    assert readiness["production_ready"] is False
    assert readiness["host_profile_lease"] == "per-operation"
    assert readiness["shared_profile_safe"] is True
    assert readiness["key_id"] == "ev-0123456789abcdef"
    assert "key_path" not in readiness
    assert "module_origin" not in readiness


def test_bridge_forwards_full_protected_lifecycle_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeMemory:
        def remember(self, *args: object, **kwargs: object):
            calls.append(("remember", args, kwargs))
            return {"created": True}

        def refresh_live(self, *args: object, **kwargs: object):
            calls.append(("refresh_live", args, kwargs))
            return {"refreshed": True}

        def promote(self, *args: object, **kwargs: object):
            calls.append(("promote", args, kwargs))
            return {"promoted": True}

        def recall(self, *args: object, **kwargs: object):
            calls.append(("recall", args, kwargs))
            return {"results": []}

        def context(self, *args: object, **kwargs: object):
            calls.append(("context", args, kwargs))
            return {"records": []}

        def reindex(self):
            calls.append(("reindex", (), {}))
            return {"reindexed": 1}

    layer = object.__new__(memory_echo_veil.EchoVeilMemoryLayer)
    layer.degraded = False
    layer.memory = FakeMemory()

    layer.remember(
        "active state",
        topic="current task",
        layer="live",
        provenance=["algo-cli:user_explicit"],
        expires_at=123.0,
    )
    layer.refresh_live(
        "live-id",
        "updated state",
        provenance=["algo-cli:user_explicit"],
        expires_at=456.0,
    )
    layer.promote(
        "live-id",
        "short_term",
        reason="needed tomorrow",
        provenance=["algo-cli:user_explicit"],
    )
    layer.recall("what is active?", top_k=3, layers=["live", "short_term"])
    layer.context("why?", max_depth=2, max_records=5)
    layer.reindex()

    assert calls == [
        (
            "remember",
            ("current task", "active state"),
            {
                "layer": "live",
                "provenance": [
                    "caller:algo-cli",
                    "algo-cli:user_explicit",
                ],
                "promotion_reason": None,
                "expires_at": 123.0,
                "logic_kind": None,
                "related_ids": None,
            },
        ),
        (
            "refresh_live",
            ("live-id", "updated state"),
            {
                "provenance": [
                    "caller:algo-cli",
                    "algo-cli:user_explicit",
                ],
                "expires_at": 456.0,
            },
        ),
        (
            "promote",
            ("live-id", "short_term"),
            {
                "reason": "needed tomorrow",
                "provenance": [
                    "caller:algo-cli",
                    "algo-cli:user_explicit",
                ],
            },
        ),
        (
            "recall",
            ("what is active?",),
            {
                "top_k": 3,
                "min_score": None,
                "allow_inferential": False,
                "as_of": None,
                "layers": ["live", "short_term"],
            },
        ),
        (
            "context",
            ("why?",),
            {
                "min_score": None,
                "allow_inferential": False,
                "as_of": None,
                "max_depth": 2,
                "max_records": 5,
            },
        ),
        ("reindex", (), {}),
    ]


def test_real_required_write_has_explicit_provenance_and_no_ram_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil
    from algo_cli.config import Config

    captured: dict[str, object] = {}

    class FakeLayer:
        def remember(self, payload: str, **kwargs: object):
            captured["payload"] = payload
            captured.update(kwargs)
            return {"created": True}

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(
        memory_echo_veil,
        "create_echo_veil_layer",
        lambda _config: FakeLayer(),
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    assert memory_echo_veil.remember_with_echo_veil(
        cfg,
        "shielded only",
        source="user_explicit",
    )
    assert cfg.memories == []
    assert captured == {
        "payload": "shielded only",
        "topic": "algo memory · user_explicit",
        "layer": "short_term",
        "provenance": ["algo-cli:user_explicit"],
        "closed": True,
    }


def test_algo_transport_identity_is_separate_from_memory_evidence() -> None:
    from algo_cli import memory_echo_veil

    assert memory_echo_veil._caller_bound_provenance(None) == [
        "caller:algo-cli"
    ]
    assert memory_echo_veil._caller_bound_provenance(
        ["algo-cli:user_explicit"]
    ) == [
        "caller:algo-cli",
        "algo-cli:user_explicit",
    ]
    with pytest.raises(ValueError, match="at most 3 supplied"):
        memory_echo_veil._caller_bound_provenance(
            ["source:one", "source:two", "source:three", "source:four"]
        )


def test_protected_prompt_context_preserves_layer_provenance_and_degraded_label() -> None:
    from algo_cli import memory_echo_veil

    rendered = memory_echo_veil.format_protected_prompt_context(
        {
            "degraded": True,
            "ranking_ambiguous": True,
            "results": [
                {
                    "vine_id": "abc",
                    "memory_layer": "short_term",
                    "confidence_band": "high_confidence",
                    "score": 0.81,
                    "provenance": ["algo-cli:user_explicit"],
                    "possible_conflict": True,
                    "temporal_status": "current",
                    "gated": False,
                    "payload": "Use the verified release gate.",
                }
            ],
        }
    )

    assert "degraded keyed read-only" in rendered
    assert "non-authoritative" in rendered
    assert "Ranking is ambiguous" in rendered
    assert "layer=short_term" in rendered
    assert "provenance=algo-cli:user_explicit" in rendered
    assert "possible_conflict" in rendered
    assert 'payload="Use the verified release gate."' in rendered


def test_required_system_prompt_injects_the_four_layer_memory_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import context_budget, memory_echo_veil
    from algo_cli.config import Config

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        memory_echo_veil,
        "protected_prompt_context",
        lambda _cfg, query, *, top_k: (
            calls.append((query, top_k))
            or "Recall mode: semantic with answerability verification."
        ),
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
        messages=[{"role": "user", "content": "Why was this design selected?"}],
        memories=["plaintext fallback must stay unused"],
    )

    prompt = context_budget.build_system_prompt(cfg)

    assert calls == [("Why was this design selected?", 3)]
    assert "Echo Veil is the exclusive mutable agent-memory authority" in prompt
    assert "Every\n  substantive task receives" in prompt
    assert "echo_veil_context" in prompt
    assert "ranking_ambiguous=true" in prompt
    assert "competing_memory_detected=true" in prompt
    assert "degraded=true" in prompt
    assert "Long-Term requires reviewed Short-Term promotion" in prompt
    assert "Never consult or write a host plaintext fallback" in prompt
    assert "plaintext fallback must stay unused" not in prompt


def test_exact_response_keeps_doctor_preflight_but_skips_semantic_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import context_budget, memory_echo_veil
    from algo_cli.config import Config

    monkeypatch.setattr(
        memory_echo_veil,
        "get_echo_veil_readiness",
        lambda _cfg: {
            "healthy": True,
            "all_records_shielded": True,
            "local_protection_ready": True,
            "protection_policy": "required",
        },
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "protected_prompt_context",
        lambda *_args, **_kwargs: pytest.fail(
            "closed-form response must not retrieve semantic payloads"
        ),
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
        messages=[
            {
                "role": "user",
                "content": "Reply with exactly: ALGO_QOS_OK",
            }
        ],
    )

    prompt = context_budget.build_system_prompt(cfg)

    assert "Doctor-backed shield preflight passed" in prompt
    assert "wholly self-contained response" in prompt


def test_required_system_prompt_fails_closed_when_echo_is_disabled() -> None:
    from algo_cli import context_budget
    from algo_cli.config import Config

    cfg = Config(
        echo_veil_enabled=False,
        echo_veil_protection="required",
        messages=[{"role": "user", "content": "Use prior context."}],
        memories=["legacy plaintext fallback"],
    )

    assert context_budget._echo_veil_memory_items(cfg) == []
    with pytest.raises(
        RuntimeError,
        match="required protected memory context is unavailable",
    ):
        context_budget.build_system_prompt(cfg)


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


def test_required_memory_reads_never_construct_legacy_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime, memory_echo_veil
    from algo_cli.config import Config

    class ForbiddenCatalog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("legacy catalog must not be constructed")

    monkeypatch.setattr(julia_memory_runtime, "MemoryCatalog", ForbiddenCatalog)
    monkeypatch.setattr(
        memory_echo_veil,
        "recall_response_with_echo_veil",
        lambda *_args, **_kwargs: {
            "degraded": False,
            "results": [
                {
                    "vine_id": "protected-id",
                    "memory_layer": "short_term",
                    "confidence_band": "high_confidence",
                    "score": 0.9,
                    "provenance": ["algo-cli:user_explicit"],
                    "gated": False,
                    "payload": "protected result",
                }
            ],
        },
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    rendered = julia_memory_runtime.command_text("search protected", cfg)
    assert "protected result" in rendered
    assert "layer=short_term" in rendered


def test_required_memory_commands_route_refresh_promotion_and_context_to_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime, memory_echo_veil
    from algo_cli.config import Config

    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class ForbiddenCatalog:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("legacy catalog must not be constructed")

    monkeypatch.setattr(julia_memory_runtime, "MemoryCatalog", ForbiddenCatalog)
    monkeypatch.setattr(
        memory_echo_veil,
        "refresh_live_with_echo_veil",
        lambda *args, **kwargs: (
            calls.append(("refresh", args, kwargs))
            or {"refreshed": True, "memory_layer": "live"}
        ),
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "promote_with_echo_veil",
        lambda *args, **kwargs: (
            calls.append(("promote", args, kwargs))
            or {"promoted": True, "memory_layer": "short_term"}
        ),
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "context_with_echo_veil",
        lambda *args, **kwargs: (
            calls.append(("context", args, kwargs))
            or {"records": [], "edges": []}
        ),
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )

    refreshed = julia_memory_runtime.command_text(
        "refresh live-id updated active state",
        cfg,
    )
    promoted = julia_memory_runtime.command_text(
        "promote live-id --to short_term --reason needed tomorrow",
        cfg,
    )
    context = julia_memory_runtime.command_text(
        "context why was this selected",
        cfg,
    )

    assert '"refreshed": true' in refreshed
    assert '"promoted": true' in promoted
    assert '"records": []' in context
    assert calls == [
        (
            "refresh",
            (cfg, "live-id", "updated active state"),
            {"source": "user_explicit"},
        ),
        (
            "promote",
            (cfg, "live-id", "short_term"),
            {"reason": "needed tomorrow", "source": "user_explicit"},
        ),
        (
            "context",
            (cfg, "why was this selected"),
            {},
        ),
    ]


def test_required_config_load_does_not_read_legacy_memory_into_ram(
    config_dir: Path,
) -> None:
    from algo_cli.config import Config

    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "echo_veil_enabled": True,
                "echo_veil_protection": "required",
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "memory.json").write_text(
        json.dumps(["legacy plaintext must remain untouched on disk"]),
        encoding="utf-8",
    )

    cfg = Config.load()

    assert cfg.memories == []
    assert (config_dir / "memory.json").exists()


def test_required_harness_tools_filter_legacy_memory_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import tools
    from algo_cli.config import Config

    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
    )
    monkeypatch.setattr(
        tools.harness,
        "search_index",
        lambda *_args, **_kwargs: [
            {
                "id": "legacy-memory",
                "kind": "memory",
                "title": "Legacy memory",
                "path": "memory.md",
            },
            {
                "id": "safe-skill",
                "kind": "skill",
                "title": "Safe skill",
                "path": "SKILL.md",
            },
        ],
    )

    rendered = tools.harness_search("memory", cfg=cfg)

    assert "safe-skill" in rendered
    assert "legacy-memory" not in rendered
    assert (
        "only through Echo Veil"
        in tools.harness_search("memory", kind="memory", cfg=cfg)
    )

    monkeypatch.setattr(
        tools.harness,
        "get_record",
        lambda _record_id: {"kind": "memory"},
    )
    monkeypatch.setattr(
        tools.harness,
        "read_record",
        lambda *_args, **_kwargs: pytest.fail("legacy record must not be read"),
    )
    assert "only through Echo Veil" in tools.harness_read(
        "legacy-memory",
        cfg=cfg,
    )


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
bridge.ECHO_VEIL_SOURCE_VERSION = "0.7.0"
bridge.ECHO_VEIL_DISTRIBUTION_VERSION = "0.7.0"
bridge.ECHO_VEIL_INSTALLATION_KIND = "vcs-pinned"
bridge.ECHO_VEIL_SOURCE_COMMIT = bridge.QUALIFIED_ECHO_VEIL_COMMIT
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
print(__import__("json").dumps({{"created": created, "ram_shadow": cfg.memories}}))
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
    write_result = json.loads(written.stdout)
    assert write_result["created"] is True
    assert write_result["ram_shadow"] == []
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
    assert response["results"][0]["memory_layer"] == "short_term"
    assert response["results"][0]["provenance"] == [
        "caller:algo-cli",
        "algo-cli:user_explicit",
    ]
    assert response["results"][0]["layer_contract_protected"] is True
    assert response["results"][0]["content_policy"]["policy_compliant"] is True

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
