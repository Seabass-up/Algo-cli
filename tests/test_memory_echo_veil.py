from __future__ import annotations

import json
import os
import stat
import sys
from types import SimpleNamespace


def test_get_report_handles_count_only_gardeners_report() -> None:
    from algo_cli.memory_echo_veil import EchoVeilMemoryLayer

    class _Oracle:
        def report(self):
            return SimpleNamespace(
                thriving_vines=2,
                twilight_grove=1,
                memory_pressure=0.5,
            )

    layer = EchoVeilMemoryLayer.__new__(EchoVeilMemoryLayer)
    layer.oracle = _Oracle()

    report = layer.get_report()

    assert report["active_memories"] == []
    assert report["compressed_memories"] == []
    assert report["active_count"] == 2
    assert report["compressed_count"] == 1
    assert report["memory_pressure"] == 0.5


def test_create_echo_veil_layer_loads_crypto_key_path_from_config(tmp_path, monkeypatch) -> None:
    from algo_cli import memory_echo_veil

    key = bytes(range(32))
    key_path = tmp_path / "echo_key.json"
    key_path.write_text(json.dumps({"key_hex": key.hex()}), encoding="utf-8")
    if os.name == "posix":
        key_path.chmod(0o600)
    captured: dict[str, object] = {}

    class _Shield:
        def __init__(self, crypto_key: bytes) -> None:
            captured["crypto_key"] = crypto_key

    class _WorkspaceConfig:
        def __init__(self, capacity: int) -> None:
            captured["capacity"] = capacity

    class _Oracle:
        def __init__(self, config, shield) -> None:
            captured["oracle_config"] = config
            captured["shield"] = shield

        def report(self):
            return SimpleNamespace(thriving_vines=[], twilight_grove=[], memory_pressure=None)

    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_AVAILABLE", True)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_IMPORT_ERROR", "")
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_MODULE_ORIGIN", "/runtime/echo_veil.py")
    monkeypatch.setattr(memory_echo_veil, "AesGcmCryptoShield", _Shield)
    monkeypatch.setattr(memory_echo_veil, "WorkspaceConfig", _WorkspaceConfig)
    monkeypatch.setattr(memory_echo_veil, "Oracle", _Oracle)

    layer = memory_echo_veil.create_echo_veil_layer(
        embed_fn=lambda texts: [[0.0] for _text in texts],
        config={
            "echo_veil_enabled": True,
            "echo_veil_production": True,
            "echo_veil_capacity": 12,
            "echo_veil_crypto_key_path": str(key_path),
        },
    )

    assert layer is not None
    assert captured["crypto_key"] == key
    assert captured["capacity"] == 12


def test_production_echo_key_rejects_broad_permissions(tmp_path, monkeypatch) -> None:
    if os.name != "posix":
        return
    from algo_cli import memory_echo_veil

    key_path = tmp_path / "echo_key.json"
    key_path.write_text(json.dumps({"key_hex": bytes(range(32)).hex()}), encoding="utf-8")
    key_path.chmod(0o644)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_AVAILABLE", True)

    try:
        memory_echo_veil.create_echo_veil_layer(
            embed_fn=lambda texts: [[0.0] for _text in texts],
            config={
                "echo_veil_enabled": True,
                "echo_veil_production": True,
                "echo_veil_crypto_key_path": str(key_path),
            },
        )
    except PermissionError as exc:
        assert "0600" in str(exc)
    else:  # pragma: no cover - mandatory fail-closed behavior
        raise AssertionError("broad production key permissions were accepted")


def test_harness_echo_veil_embed_fn_handles_ollama_failure(monkeypatch, config_dir) -> None:
    from algo_cli import harness
    from algo_cli import memory_echo_veil

    captured: dict[str, object] = {}
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "echo_veil_enabled": True,
                "host": "http://127.0.0.1:11434",
                "harness_embed_model": "embed-model",
            }
        ),
        encoding="utf-8",
    )

    def fake_create_echo_veil_layer(*, embed_fn, config, crypto_key_path=None):
        captured["embed_fn"] = embed_fn
        captured["config"] = config
        captured["crypto_key_path"] = crypto_key_path
        return object()

    class _Client:
        def __init__(self, host: str) -> None:
            self.host = host

        def embed(self, **_kwargs):
            raise RuntimeError("ollama unavailable")

    monkeypatch.setattr(harness, "_echo_veil_layer", None)
    monkeypatch.setattr(memory_echo_veil, "create_echo_veil_layer", fake_create_echo_veil_layer)
    monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(Client=_Client))

    layer = harness.get_echo_veil_layer()

    assert layer is not None
    assert captured["config"]["echo_veil_enabled"] is True
    assert captured["embed_fn"](["hello"]) == []


def test_save_state_uses_datetime_and_persists_memory_text(tmp_path) -> None:
    from algo_cli.memory_echo_veil import EchoVeilMemoryLayer

    class _Oracle:
        def report(self):
            return SimpleNamespace(thriving_vines=[], twilight_grove=[], memory_pressure=None)

    layer = EchoVeilMemoryLayer.__new__(EchoVeilMemoryLayer)
    layer.capacity = 7
    layer.environment = "development"
    layer.persist_path = tmp_path / "echo_state.json"
    layer.oracle = _Oracle()
    layer._memory_text_by_title = {"topic": "full body"}

    layer._save_state()

    state = json.loads(layer.persist_path.read_text(encoding="utf-8"))
    assert state["capacity"] == 7
    assert state["memory_text_by_title"] == {"topic": "full body"}
    assert "T" in state["last_updated"]
    if os.name == "posix":
        assert stat.S_IMODE(layer.persist_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(layer.persist_path.parent.stat().st_mode) == 0o700


def test_save_state_preserves_count_only_gardeners_report(tmp_path) -> None:
    from algo_cli.memory_echo_veil import EchoVeilMemoryLayer

    class _Oracle:
        def report(self):
            return SimpleNamespace(thriving_vines=3, twilight_grove=2, memory_pressure=0.5)

    layer = EchoVeilMemoryLayer.__new__(EchoVeilMemoryLayer)
    layer.capacity = 7
    layer.environment = "development"
    layer.persist_path = tmp_path / "echo_state.json"
    layer.oracle = _Oracle()
    layer._memory_text_by_title = {}

    layer._save_state()

    state = json.loads(layer.persist_path.read_text(encoding="utf-8"))
    assert state["active_count"] == 3
    assert state["compressed_count"] == 2


def test_readiness_does_not_overstate_runtime_wiring(monkeypatch) -> None:
    from algo_cli import memory_echo_veil

    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_AVAILABLE", True)
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_IMPORT_ERROR", "")
    monkeypatch.setattr(memory_echo_veil, "ECHO_VEIL_MODULE_ORIGIN", "/runtime/echo_veil.py")

    readiness = memory_echo_veil.get_echo_veil_readiness({"echo_veil_enabled": True})

    assert readiness["installed"] is True
    assert readiness["enabled"] is True
    assert readiness["write_wired"] is False
    assert readiness["retrieval_wired"] is False
    assert readiness["persistence_wired"] is False
    assert readiness["readiness_source"] == "algo_cli.memory_echo_veil.get_echo_veil_readiness"
    assert readiness["module_origin"] == "/runtime/echo_veil.py"
    assert readiness["import_error"] is None


def test_load_state_returns_none_and_restores_memory_text(tmp_path) -> None:
    from algo_cli.memory_echo_veil import EchoVeilMemoryLayer

    state_path = tmp_path / "echo_state.json"
    state_path.write_text(json.dumps({"memory_text_by_title": {"topic": "full body"}}), encoding="utf-8")

    layer = EchoVeilMemoryLayer.__new__(EchoVeilMemoryLayer)
    layer.persist_path = state_path
    layer._loaded_state = None
    layer._memory_text_by_title = {}

    assert layer._load_state() is None
    assert layer._loaded_state == {"memory_text_by_title": {"topic": "full body"}}
    assert layer._memory_text_by_title == {"topic": "full body"}


def test_observe_does_not_report_synthetic_proximity_or_drift() -> None:
    from algo_cli.memory_echo_veil import EchoVeilMemoryLayer

    class _Workspace:
        vines = [SimpleNamespace(topic="topic")]

    class _Oracle:
        workspace = _Workspace()

        def observe(self, _embedding):
            return SimpleNamespace()

        def report(self):
            return SimpleNamespace(thriving_vines=1, twilight_grove=0, memory_pressure=None)

    layer = EchoVeilMemoryLayer.__new__(EchoVeilMemoryLayer)
    layer.oracle = _Oracle()
    layer._memory_text_by_title = {"topic": "full body"}

    observed = layer.observe([0.1])

    assert observed["active"] == [
        {
            "title": "topic",
            "text": "full body",
            "proximity": None,
            "proximity_available": False,
        }
    ]
    assert observed["drift_detected"] is None
    assert observed["drift_available"] is False
