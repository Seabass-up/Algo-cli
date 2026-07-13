"""Offline usability/routing tests for local Ollama, Ollama Cloud, xAI, and ChatGPT.

These tests exercise the boundaries that decide which provider receives chat,
which credential source is required, and whether local Ollama startup is needed.
No network calls are made: clients/auth helpers are monkeypatched.
"""
from __future__ import annotations

import pytest

from algo_cli import model_info, model_routing, runtime_services
from algo_cli.config import Config


class _FakeOllamaClient:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).calls.append(kwargs)


def test_local_ollama_client_uses_configured_host(monkeypatch):
    monkeypatch.setattr(runtime_services, "Client", _FakeOllamaClient)
    _FakeOllamaClient.calls.clear()
    cfg = Config(host="http://127.0.0.1:11434", cloud=False, model="qwen3:latest")

    client = runtime_services.create_client(cfg)

    assert isinstance(client, _FakeOllamaClient)
    assert client.kwargs["host"] == "http://127.0.0.1:11434"
    assert "headers" not in client.kwargs


def test_ollama_cloud_client_requires_key_and_uses_bearer(monkeypatch):
    monkeypatch.setattr(runtime_services, "Client", _FakeOllamaClient)
    monkeypatch.setenv("OLLAMA_API_KEY", "CLOUD_TOKEN")
    cfg = Config(cloud=True, model="qwen3:cloud")

    client = runtime_services.create_client(cfg)

    assert isinstance(client, _FakeOllamaClient)
    assert client.kwargs["host"] == "https://ollama.com"
    assert client.kwargs["headers"] == {"Authorization": "Bearer CLOUD_TOKEN"}


def test_cloud_tag_model_can_route_through_local_ollama_without_key(monkeypatch):
    monkeypatch.setattr(runtime_services, "Client", _FakeOllamaClient)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    _FakeOllamaClient.calls.clear()
    cfg = Config(host="http://127.0.0.1:11434", cloud=False, model="qwen3:cloud")

    client = runtime_services.create_client(cfg)

    assert isinstance(client, _FakeOllamaClient)
    assert client.kwargs["host"] == "http://127.0.0.1:11434"
    assert "headers" not in client.kwargs
    assert model_routing.effective_runtime_host(cfg) == "http://127.0.0.1:11434"


def test_direct_cloud_mode_without_key_falls_back_to_local_ollama(monkeypatch):
    monkeypatch.setattr(runtime_services, "Client", _FakeOllamaClient)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    _FakeOllamaClient.calls.clear()
    cfg = Config(host="http://127.0.0.1:11434", cloud=True, model="qwen3:cloud")

    client = runtime_services.create_client(cfg)

    assert isinstance(client, _FakeOllamaClient)
    assert client.kwargs["host"] == "http://127.0.0.1:11434"
    assert "headers" not in client.kwargs
    assert model_routing.uses_ollama_cloud(cfg) is False


def test_xai_model_routes_to_xai_client_without_ollama_key(monkeypatch):
    fake = object()
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    from algo_cli import xai_client

    monkeypatch.setattr(xai_client, "active_xai_client", lambda: fake)
    cfg = Config(model="grok-4-latest", cloud=False)

    assert runtime_services.create_client(cfg) is fake
    assert model_routing.effective_runtime_host(cfg) == "xai"


def test_chatgpt_model_routes_to_chatgpt_client_without_ollama_key(monkeypatch):
    fake = object()
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    from algo_cli import chatgpt_client

    monkeypatch.setattr(chatgpt_client, "active_chatgpt_client", lambda: fake)
    cfg = Config(model="gpt-5.1", cloud=False)

    assert runtime_services.create_client(cfg) is fake
    assert model_routing.effective_runtime_host(cfg) == "chatgpt"


@pytest.mark.parametrize("alias", ["sol", "terra", "luna", "lunna"])
def test_codex_alias_routes_to_chatgpt_client(monkeypatch, alias):
    fake = object()
    from algo_cli import chatgpt_client

    monkeypatch.setattr(chatgpt_client, "active_chatgpt_client", lambda: fake)
    cfg = Config(model=alias, cloud=False)

    assert runtime_services.create_client(cfg) is fake
    assert model_routing.effective_runtime_host(cfg) == "chatgpt"


def test_chatgpt_detection_does_not_steal_gpt_oss_ollama_models():
    assert model_info.is_chatgpt_model("gpt-5.1") is True
    assert model_info.is_chatgpt_model("chatgpt-4o-latest") is True
    assert model_info.is_chatgpt_model("o3-mini") is True
    assert model_info.is_chatgpt_model("gpt-oss:120b-cloud") is False


def test_provider_models_do_not_start_local_ollama(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(runtime_services, "start_local_ollama_host", lambda host: calls.append(host) or True)

    assert runtime_services.start_ollama_server(Config(model="grok-4-latest")) is True
    assert runtime_services.start_ollama_server(Config(model="gpt-5.1")) is True
    assert calls == []


def test_agent_block_xai_falls_back_when_not_authenticated(monkeypatch):
    active = object()
    monkeypatch.setattr(runtime_services.xai_auth, "get_valid_token", lambda: None)
    messages: list[str] = []
    monkeypatch.setattr(runtime_services, "show_info", lambda msg: messages.append(msg))
    cfg = Config(model="qwen3:latest")

    assert runtime_services.client_for_model("grok-4-latest", cfg, active) is active
    assert any("xAI OAuth" in msg for msg in messages)


def test_agent_block_chatgpt_falls_back_when_not_authenticated(monkeypatch):
    active = object()
    from algo_cli import chatgpt_auth

    monkeypatch.setattr(chatgpt_auth, "get_valid_token", lambda: None)
    messages: list[str] = []
    monkeypatch.setattr(runtime_services, "show_info", lambda msg: messages.append(msg))
    cfg = Config(model="qwen3:latest")

    assert runtime_services.client_for_model("gpt-5.1", cfg, active) is active
    assert any("ChatGPT OAuth" in msg for msg in messages)
