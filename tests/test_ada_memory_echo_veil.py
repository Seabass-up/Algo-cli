"""Adversarial coverage for Ada's protected-memory integration."""

from __future__ import annotations

import base64
import csv
import hashlib
from importlib import metadata
import json
import inspect
import os
from pathlib import Path
import shutil
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
    monkeypatch.setattr(module, "ECHO_VEIL_MODULE_IDENTITY_VERIFIED", True)
    monkeypatch.setattr(module, "ECHO_VEIL_SOURCE_VERSION", "0.7.0")
    monkeypatch.setattr(module, "ECHO_VEIL_DISTRIBUTION_VERSION", "0.7.0")
    monkeypatch.setattr(module, "ECHO_VEIL_INSTALLATION_KIND", "vcs-pinned")
    monkeypatch.setattr(
        module,
        "ECHO_VEIL_INSTALLATION_REPOSITORY",
        module.QUALIFIED_ECHO_VEIL_REPOSITORY,
    )
    monkeypatch.setattr(
        module,
        "ECHO_VEIL_INSTALLATION_COMMIT",
        module.QUALIFIED_ECHO_VEIL_COMMIT,
    )
    monkeypatch.setattr(
        module,
        "ECHO_VEIL_REQUESTED_REVISION",
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


def test_static_echo_config_loader_fails_closed_for_symlink_and_oversize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import ada_memory_echo_veil as bridge

    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"echo_veil_enabled":false,"echo_veil_protection":"optional"}',
        encoding="utf-8",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").symlink_to(outside)
    monkeypatch.setattr(bridge, "CONFIG_DIR", config_dir)

    symlinked = bridge._load_config_mapping()
    assert symlinked["echo_veil_enabled"] is True
    assert symlinked["echo_veil_protection"] == "required"

    (config_dir / "config.json").unlink()
    (config_dir / "config.json").write_bytes(b'{"echo_veil_enabled":false,"padding":"' + (b"x" * 1_048_577) + b'"}')
    oversized = bridge._load_config_mapping()
    assert oversized["echo_veil_enabled"] is True
    assert oversized["echo_veil_protection"] == "required"


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO identity contract")
def test_static_echo_config_loader_rejects_fifo_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import ada_memory_echo_veil as bridge

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    os.mkfifo(config_dir / "config.json", 0o600)
    monkeypatch.setattr(bridge, "CONFIG_DIR", config_dir)

    loaded = bridge._load_config_mapping()
    assert loaded["echo_veil_enabled"] is True
    assert loaded["echo_veil_protection"] == "required"


def test_echo_dependency_is_commit_pinned_and_exercised_in_ci() -> None:
    root = Path(__file__).resolve().parents[1]
    pin = "aaf8497ddbe33dac2f79e7f02cbce2cb26f706eb"
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    workflow = (root / ".github/workflows/oliver-ci.yml").read_text(encoding="utf-8")

    assert pin in project
    assert pin in lock
    assert (
        "uv sync --frozen --no-editable --extra dev --extra supply-chain "
        "--extra echo-veil --reinstall-package algo-cli-runtime --link-mode copy"
    ) in workflow
    assert (
        "uv run --frozen --no-editable --extra dev --extra supply-chain --extra echo-veil --link-mode copy pytest tests"
    ) in workflow
    assert (
        "uv sync --frozen --no-editable --extra dev --extra echo-veil "
        "--reinstall-package algo-cli-runtime --link-mode copy"
    ) in workflow
    assert ("uv run --frozen --no-editable --extra dev --extra echo-veil --link-mode copy pytest tests") in workflow


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
        assert action_registry.get_action_spec(name).log_suppression is True

    assert action_registry.action_requires_approval("echo_veil_list") is True
    assert action_registry.action_requires_approval("echo_veil_doctor") is True
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

    assert action_registry.get_action_spec("echo_veil_recall").effect_class is EffectClass.LOCAL_MUTATION
    assert action_registry.get_action_spec("echo_veil_context").effect_class is EffectClass.LOCAL_MUTATION
    assert action_registry.get_action_spec("echo_veil_list").effect_class is EffectClass.LOCAL_MUTATION
    assert action_registry.get_action_spec("echo_veil_doctor").effect_class is EffectClass.LOCAL_MUTATION


def test_echo_lifecycle_observations_require_runtime_approval(
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
        assert preflight.policy.disposition.value == "confirm"
        assert preflight.policy.grant_id == ""


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
        lambda *args, **kwargs: calls.append(("refresh", args, kwargs)) or {"refreshed": True, "vine_id": args[1]},
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "promote_with_echo_veil",
        lambda *args, **kwargs: calls.append(("promote", args, kwargs)) or {"promoted": True, "vine_id": args[1]},
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
        lambda _cfg, **_kwargs: {
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
    context = json.loads(tools.echo_veil_context("why was it selected?", cfg=cfg))
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
    assert inventory["lifecycle_mutated"] is True
    assert doctor["lifecycle_mutated"] is True
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


def test_readiness_rejects_unreviewed_future_echo_veil_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_SOURCE_VERSION", "0.8.0")
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_DISTRIBUTION_VERSION", "0.8.0")

    readiness = memory_echo_veil.get_echo_veil_readiness(
        {
            "echo_veil_enabled": True,
            "echo_veil_protection": "required",
        }
    )

    assert readiness["version_supported"] is False
    assert readiness["installation_identity"] == "vcs-pinned-unsupported"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (None, "registry-or-wheel"),
        ('{"dir_info":{"editable":true},"url":"file:///checkout"}', "editable"),
        (
            '{"vcs_info":{"vcs":"git","commit_id":"' + ("a" * 40) + '"},"url":"git+https://example.invalid/repo"}',
            "vcs-pinned",
        ),
        (
            '{"vcs_info":{"vcs":"hg","commit_id":"' + ("a" * 40) + '"},"url":"https://example.invalid/repo"}',
            "direct-url-unpinned",
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


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("ECHO_VEIL_INSTALLATION_KIND", "registry-or-wheel"),
        ("ECHO_VEIL_INSTALLATION_REPOSITORY", "https://example.invalid/echo.git"),
        ("ECHO_VEIL_INSTALLATION_COMMIT", "0" * 40),
        ("ECHO_VEIL_REQUESTED_REVISION", "0" * 40),
    ],
)
def test_required_mode_rejects_same_version_without_exact_qualified_identity(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    value: str,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(memory_echo_veil, attribute, value)
    config = {
        "echo_veil_enabled": True,
        "echo_veil_protection": "required",
    }

    assert memory_echo_veil._version_supported() is True
    assert memory_echo_veil._qualified_runtime_identity() is False
    readiness = memory_echo_veil.get_echo_veil_readiness(config)
    assert readiness["qualified_runtime_identity"] is False
    assert readiness["import_error"] == "qualified_runtime_identity_mismatch"
    with pytest.raises(RuntimeError, match="writes are blocked"):
        memory_echo_veil.create_echo_veil_layer(config)


def test_optional_mode_remains_bounded_to_supported_noneditable_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(
        memory_echo_veil,
        "ECHO_VEIL_INSTALLATION_KIND",
        "registry-or-wheel",
    )
    assert memory_echo_veil._version_supported() is True
    assert memory_echo_veil._qualified_runtime_identity() is False


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
    assert memory_echo_veil._LAST_INITIALIZATION_ERROR == ("disabled_by_configuration")


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
        },
        live_probe=True,
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


def test_static_readiness_never_constructs_the_lifecycle_mutating_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil

    _supported(monkeypatch, memory_echo_veil)
    monkeypatch.setattr(
        memory_echo_veil,
        "create_echo_veil_layer",
        lambda *_args, **_kwargs: pytest.fail("static readiness must not construct Echo"),
    )

    readiness = memory_echo_veil.get_echo_veil_readiness(
        {
            "echo_veil_enabled": True,
            "echo_veil_protection": "required",
        }
    )

    assert readiness["live_probe_performed"] is False
    assert readiness["healthy"] is False
    assert readiness["qualified_runtime_identity"] is True


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

    assert memory_echo_veil._caller_bound_provenance(None) == ["caller:algo-cli"]
    assert memory_echo_veil._caller_bound_provenance(["algo-cli:user_explicit"]) == [
        "caller:algo-cli",
        "algo-cli:user_explicit",
    ]
    with pytest.raises(ValueError, match="at most 3 supplied"):
        memory_echo_veil._caller_bound_provenance(["source:one", "source:two", "source:three", "source:four"])


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
            calls.append((query, top_k)) or "Recall mode: semantic with answerability verification."
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
        lambda _cfg, **_kwargs: {
            "healthy": True,
            "all_records_shielded": True,
            "local_protection_ready": True,
            "protection_policy": "required",
        },
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "protected_prompt_context",
        lambda *_args, **_kwargs: pytest.fail("closed-form response must not retrieve semantic payloads"),
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


def test_optional_enabled_config_write_uses_echo_without_plaintext_shadow(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import memory_echo_veil
    from algo_cli.config import Config

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        memory_echo_veil,
        "remember_with_echo_veil",
        lambda _config, fact, *, source: calls.append((fact, source)) or True,
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="optional",
        memories=["stale legacy fact"],
    )

    assert cfg.remember_fact("The Echo-owned fact.") is True
    assert calls == [("The Echo-owned fact.", "user_explicit")]
    assert cfg.memories == ["stale legacy fact"]
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
    from algo_cli.config import MEMORY_AUTO_CAPTURE_CONSENT_VERSION, Config
    from algo_cli.grace_memory_receipts import ElsieReceiptAuthority
    from algo_cli.grace_key_store import StaticKeyStore
    from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL

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
    key_store = StaticKeyStore({PRIVACY_KEY_LABEL: b"j" * 32})
    receipt_authority = ElsieReceiptAuthority.from_key_store(store=key_store)
    monkeypatch.setattr(
        julia_memory_runtime.memory_candidates.ElsieReceiptAuthority,
        "from_key_store",
        classmethod(lambda cls, **_kwargs: receipt_authority),
    )
    monkeypatch.setattr(
        julia_memory_runtime.memory_candidates.ElsieReceiptAuthority,
        "from_existing_key_store",
        classmethod(lambda cls, **_kwargs: receipt_authority),
    )
    monkeypatch.setattr(
        julia_memory_runtime.memory_candidates.ElsieReceiptAuthority,
        "from_optional_existing_key_store",
        classmethod(lambda cls, **_kwargs: receipt_authority),
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="required",
        memory_auto_capture_enabled=True,
        memory_auto_capture_consent_version=MEMORY_AUTO_CAPTURE_CONSENT_VERSION,
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


def test_optional_enabled_automatic_capture_routes_to_echo_without_legacy_shadow(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime, memory_echo_veil
    from algo_cli.config import MEMORY_AUTO_CAPTURE_CONSENT_VERSION, Config
    from algo_cli.grace_memory_receipts import ElsieReceiptAuthority
    from algo_cli.grace_key_store import StaticKeyStore
    from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        memory_echo_veil,
        "remember_with_echo_veil",
        lambda _config, fact, *, source: calls.append((fact, source)) or True,
    )
    key_store = StaticKeyStore({PRIVACY_KEY_LABEL: b"j" * 32})
    receipt_authority = ElsieReceiptAuthority.from_key_store(store=key_store)
    monkeypatch.setattr(
        julia_memory_runtime.memory_candidates.ElsieReceiptAuthority,
        "from_key_store",
        classmethod(lambda cls, **_kwargs: receipt_authority),
    )
    monkeypatch.setattr(
        julia_memory_runtime.memory_candidates.ElsieReceiptAuthority,
        "from_existing_key_store",
        classmethod(lambda cls, **_kwargs: receipt_authority),
    )
    monkeypatch.setattr(
        julia_memory_runtime.memory_candidates.ElsieReceiptAuthority,
        "from_optional_existing_key_store",
        classmethod(lambda cls, **_kwargs: receipt_authority),
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="optional",
        memory_auto_capture_enabled=True,
        memory_auto_capture_consent_version=MEMORY_AUTO_CAPTURE_CONSENT_VERSION,
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


def test_echo_auto_capture_does_not_consult_in_process_legacy_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime
    from algo_cli.config import MEMORY_AUTO_CAPTURE_CONSENT_VERSION, Config

    captured: dict[str, object] = {}

    def fake_process(_text, existing, *_args, **_kwargs):
        captured["existing"] = existing
        return {
            "status": "rejected",
            "reason": "synthetic",
            "counts": {},
            "reason_counts": {},
            "state": {},
        }

    monkeypatch.setattr(
        julia_memory_runtime.memory_candidates,
        "process_memory_candidates",
        fake_process,
    )
    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="optional",
        memory_auto_capture_enabled=True,
        memory_auto_capture_consent_version=MEMORY_AUTO_CAPTURE_CONSENT_VERSION,
        memories=["legacy plaintext must not be consulted"],
    )

    julia_memory_runtime.capture_completed_user_turn(
        cfg,
        "Remember that our standard shell is zsh.",
        completed=True,
    )

    assert captured["existing"] == ()


def test_optional_enabled_write_failure_never_falls_back_to_legacy(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import julia_memory_runtime, memory_echo_veil
    from algo_cli.config import Config

    monkeypatch.setattr(
        memory_echo_veil,
        "remember_with_echo_veil",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic Echo outage")),
    )
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="optional")

    with pytest.raises(
        julia_memory_runtime.MemorySystemError,
        match="Enabled Echo Veil is unavailable",
    ):
        julia_memory_runtime.remember_fact(cfg, "Never shadow this fact.")

    assert cfg.memories == []
    assert not (config_dir / "memory.json").exists()
    assert not (config_dir / "system_memory.json").exists()


@pytest.mark.parametrize("policy", ["optional", "required"])
def test_echo_authority_slash_and_tool_writes_do_not_create_intuition_shadow(
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    from algo_cli import julia_memory_runtime, main, tools
    from algo_cli.config import Config

    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection=policy,
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


def test_optional_enabled_context_never_falls_back_to_plaintext_memories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algo_cli import context_budget, memory_echo_veil
    from algo_cli.config import Config

    cfg = Config(
        echo_veil_enabled=True,
        echo_veil_protection="optional",
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


@pytest.mark.parametrize("policy", ["optional", "required"])
def test_echo_authority_memory_reads_never_construct_legacy_catalog(
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
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
        echo_veil_protection=policy,
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
        lambda *args, **kwargs: calls.append(("refresh", args, kwargs)) or {"refreshed": True, "memory_layer": "live"},
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "promote_with_echo_veil",
        lambda *args, **kwargs: (
            calls.append(("promote", args, kwargs)) or {"promoted": True, "memory_layer": "short_term"}
        ),
    )
    monkeypatch.setattr(
        memory_echo_veil,
        "context_with_echo_veil",
        lambda *args, **kwargs: calls.append(("context", args, kwargs)) or {"records": [], "edges": []},
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


@pytest.mark.parametrize("policy", ["optional", "required"])
def test_echo_authority_blocks_direct_legacy_forget_and_load(
    config_dir: Path,
    policy: str,
) -> None:
    from algo_cli.config import Config

    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "echo_veil_enabled": True,
                "echo_veil_protection": policy,
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "memory.json").write_text(
        json.dumps(["legacy plaintext must remain untouched"]),
        encoding="utf-8",
    )

    cfg = Config.load()

    assert cfg.memories == []
    with pytest.raises(RuntimeError, match="persistence is disabled"):
        cfg.save_memories()
    with pytest.raises(RuntimeError, match="reconciliation is prohibited"):
        cfg.reconcile_memory_facts(additions=["never shadow this"])
    with pytest.raises(RuntimeError, match="plaintext deletion is prohibited"):
        cfg.forget_memory_index(0)
    assert json.loads((config_dir / "memory.json").read_text(encoding="utf-8")) == [
        "legacy plaintext must remain untouched"
    ]


def test_attempt_ledger_persists_only_content_free_echo_result_receipt(
    config_dir: Path,
) -> None:
    from algo_cli.config import CONFIG_FILE, Config
    from algo_cli.nathan_runtime import record_tool_attempt

    canary = "raw-echo-payload-must-never-enter-the-ledger"
    cfg = Config()
    record_tool_attempt(
        cfg,
        name="echo_veil_list",
        args={"topic_prefix": canary},
        result=json.dumps({"records": [{"payload": canary}]}),
        status="worked",
    )

    encoded = json.dumps(cfg.attempt_ledger, sort_keys=True)
    assert canary not in encoded
    assert "status=worked" in cfg.attempt_ledger[-1]["summary"]
    assert "digest=hmac-sha256:" in cfg.attempt_ledger[-1]["summary"]
    cfg.save()
    assert canary not in CONFIG_FILE.read_text(encoding="utf-8")
    assert CONFIG_FILE.parent == config_dir


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
    assert "only through Echo Veil" in tools.harness_search("memory", kind="memory", cfg=cfg)

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


def test_preloaded_echo_modules_cannot_borrow_genuine_file_origins() -> None:
    script = r"""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys
import types

distribution = importlib.metadata.distribution("echo-veil")
package_file = Path(distribution.locate_file("echo_veil/__init__.py")).resolve()
agent_file = Path(distribution.locate_file("echo_veil/agent_memory.py")).resolve()
package = types.ModuleType("echo_veil")
package.__file__ = str(package_file)
package.__path__ = [str(package_file.parent)]
package.__spec__ = importlib.util.spec_from_file_location(
    "echo_veil", package_file, submodule_search_locations=[str(package_file.parent)]
)
package.__version__ = "0.7.0"
agent = types.ModuleType("echo_veil.agent_memory")
agent.__file__ = str(agent_file)
agent.__spec__ = importlib.util.spec_from_file_location("echo_veil.agent_memory", agent_file)
agent.AgentMemory = object
agent.AlwaysAvailableMemory = object
agent.EmbeddingUnavailable = RuntimeError
agent.OllamaTextEmbedder = object
sys.modules["echo_veil"] = package
sys.modules["echo_veil.agent_memory"] = agent

from algo_cli import ada_memory_echo_veil as bridge
print(json.dumps({
    "available": bridge.ECHO_VEIL_AVAILABLE,
    "qualified": bridge._qualified_runtime_identity(),
    "module_identity": bridge.ECHO_VEIL_MODULE_IDENTITY_VERIFIED,
    "poison_selected": bridge.AgentMemory is object,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "available": False,
        "qualified": False,
        "module_identity": False,
        "poison_selected": False,
    }


def test_tampered_transitive_echo_source_is_rejected_before_import(
    tmp_path: Path,
) -> None:
    distribution = metadata.distribution("echo-veil")
    site = tmp_path / "site"
    package = site / "echo_veil"
    origin_package = site / "echo_veil_origin"
    dist_info = site / Path(str(getattr(distribution, "_path"))).name
    shutil.copytree(Path(distribution.locate_file("echo_veil")), package)
    shutil.copytree(
        Path(distribution.locate_file("echo_veil_origin")),
        origin_package,
    )
    shutil.copytree(Path(str(getattr(distribution, "_path"))), dist_info)
    confidence = package / "confidence.py"
    confidence.write_text(
        confidence.read_text(encoding="utf-8") + "\nimport builtins\nbuiltins.ALGO_ECHO_TRANSITIVE_POISON = True\n",
        encoding="utf-8",
    )
    digest = base64.urlsafe_b64encode(hashlib.sha256(confidence.read_bytes()).digest()).rstrip(b"=").decode("ascii")
    record = dist_info / "RECORD"
    rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
    for row in rows:
        if row and row[0] == "echo_veil/confidence.py":
            row[1] = f"sha256={digest}"
            row[2] = str(confidence.stat().st_size)
    with record.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([str(site), str(Path(__file__).resolve().parents[1])])
    script = r"""
import builtins
import json
from algo_cli import ada_memory_echo_veil as bridge
print(json.dumps({
    "available": bridge.ECHO_VEIL_AVAILABLE,
    "qualified": bridge._qualified_runtime_identity(),
    "poison_executed": bool(getattr(builtins, "ALGO_ECHO_TRANSITIVE_POISON", False)),
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "available": False,
        "qualified": False,
        "poison_executed": False,
    }


def test_tampered_echo_origin_source_is_rejected_before_import(
    tmp_path: Path,
) -> None:
    distribution = metadata.distribution("echo-veil")
    site = tmp_path / "site"
    package = site / "echo_veil"
    origin_package = site / "echo_veil_origin"
    dist_info = site / Path(str(getattr(distribution, "_path"))).name
    shutil.copytree(Path(distribution.locate_file("echo_veil")), package)
    shutil.copytree(
        Path(distribution.locate_file("echo_veil_origin")),
        origin_package,
    )
    shutil.copytree(Path(str(getattr(distribution, "_path"))), dist_info)
    engine = origin_package / "openfhe_engine.py"
    engine.write_text(
        engine.read_text(encoding="utf-8") + "\nimport builtins\nbuiltins.ALGO_ECHO_ORIGIN_POISON = True\n",
        encoding="utf-8",
    )
    digest = base64.urlsafe_b64encode(hashlib.sha256(engine.read_bytes()).digest()).rstrip(b"=").decode("ascii")
    record = dist_info / "RECORD"
    rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
    for row in rows:
        if row and row[0] == "echo_veil_origin/openfhe_engine.py":
            row[1] = f"sha256={digest}"
            row[2] = str(engine.stat().st_size)
    with record.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([str(site), str(Path(__file__).resolve().parents[1])])
    script = r"""
import builtins
import json
from algo_cli import ada_memory_echo_veil as bridge
print(json.dumps({
    "available": bridge.ECHO_VEIL_AVAILABLE,
    "qualified": bridge._qualified_runtime_identity(),
    "poison_executed": bool(getattr(builtins, "ALGO_ECHO_ORIGIN_POISON", False)),
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "available": False,
        "qualified": False,
        "poison_executed": False,
    }


def test_verified_echo_snapshot_consumes_captured_bytes_after_path_mutation(
    tmp_path: Path,
) -> None:
    distribution = metadata.distribution("echo-veil")
    package = tmp_path / "echo_veil"
    shutil.copytree(Path(distribution.locate_file("echo_veil")), package)
    shutil.copytree(
        Path(distribution.locate_file("echo_veil_origin")),
        tmp_path / "echo_veil_origin",
    )
    script = r"""
import builtins
import importlib
import json
from pathlib import Path
import sys
from algo_cli.ada_echo_veil_identity import (
    QualifiedEchoSnapshotFinder,
    capture_qualified_echo_source_tree,
)

root = Path(sys.argv[1])
snapshot = capture_qualified_echo_source_tree(root)
target = root / "echo_veil" / "confidence.py"
target.write_text(
    target.read_text(encoding="utf-8")
    + "\nimport builtins\nbuiltins.ALGO_ECHO_PATH_SWAP_POISON = True\n",
    encoding="utf-8",
)
finder = QualifiedEchoSnapshotFinder(snapshot)
sys.meta_path.insert(0, finder)
package = importlib.import_module("echo_veil")
owned = all(
    finder.owns_module(module)
    for name, module in sys.modules.items()
    if (name == "echo_veil" or name.startswith("echo_veil.")) and module is not None
)
print(json.dumps({
    "owned": owned,
    "poison_executed": bool(getattr(builtins, "ALGO_ECHO_PATH_SWAP_POISON", False)),
    "version": package.__version__,
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "owned": True,
        "poison_executed": False,
        "version": "0.7.0",
    }


def test_verified_echo_snapshot_rejects_unknown_module_added_after_capture(
    tmp_path: Path,
) -> None:
    distribution = metadata.distribution("echo-veil")
    package = tmp_path / "echo_veil"
    shutil.copytree(Path(distribution.locate_file("echo_veil")), package)
    shutil.copytree(
        Path(distribution.locate_file("echo_veil_origin")),
        tmp_path / "echo_veil_origin",
    )
    script = r"""
import builtins
import importlib
import json
from pathlib import Path
import sys
from algo_cli.ada_echo_veil_identity import (
    QualifiedEchoSnapshotFinder,
    capture_qualified_echo_source_tree,
)

root = Path(sys.argv[1])
snapshot = capture_qualified_echo_source_tree(root)
finder = QualifiedEchoSnapshotFinder(snapshot)
sys.meta_path.insert(0, finder)
importlib.import_module("echo_veil")
(root / "echo_veil" / "post_capture_poison.py").write_text(
    "import builtins\nbuiltins.ALGO_ECHO_UNKNOWN_MODULE_POISON = True\n",
    encoding="utf-8",
)
rejected = False
try:
    importlib.import_module("echo_veil.post_capture_poison")
except ModuleNotFoundError:
    rejected = True
(root / "echo_veil_origin" / "post_capture_poison.py").write_text(
    "import builtins\nbuiltins.ALGO_ECHO_UNKNOWN_ORIGIN_POISON = True\n",
    encoding="utf-8",
)
origin_rejected = False
try:
    importlib.import_module("echo_veil_origin.post_capture_poison")
except ModuleNotFoundError:
    origin_rejected = True
print(json.dumps({
    "rejected": rejected,
    "origin_rejected": origin_rejected,
    "poison_executed": bool(
        getattr(builtins, "ALGO_ECHO_UNKNOWN_MODULE_POISON", False)
    ),
    "origin_poison_executed": bool(
        getattr(builtins, "ALGO_ECHO_UNKNOWN_ORIGIN_POISON", False)
    ),
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "rejected": True,
        "origin_rejected": True,
        "poison_executed": False,
        "origin_poison_executed": False,
    }


def test_black_box_ordinary_write_encrypts_restarts_and_scope_filters(
    tmp_path: Path,
) -> None:
    """Exercise the ordinary Algo runtime in two fresh Python processes."""

    config_dir = tmp_path / "algo-config"
    state_dir = tmp_path / "echo-state"
    secret = "black box secret phrase cobalt meadow 731"
    environment = dict(os.environ)
    environment["ALGO_CLI_CONFIG_DIR"] = str(config_dir)
    environment["OLLAMA_CLI_CONFIG_DIR"] = str(config_dir)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    shared = f"""
from algo_cli import memory_echo_veil as bridge
from algo_cli.config import Config
from echo_veil.agent_memory import HashingTextEmbedder
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
