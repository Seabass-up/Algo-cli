"""Offline usability/routing tests for local Ollama, Ollama Cloud, xAI, and ChatGPT.

These tests exercise the boundaries that decide which provider receives chat,
which credential source is required, and whether local Ollama startup is needed.
No network calls are made: clients/auth helpers are monkeypatched.
"""

from __future__ import annotations

from urllib.error import URLError

import pytest

from algo_cli import model_info, model_routing, theodore_runtime_services as runtime_services
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


def test_explicit_ollama_provider_keeps_local_gpt_alias_on_ollama(monkeypatch):
    monkeypatch.setattr(runtime_services, "Client", _FakeOllamaClient)
    _FakeOllamaClient.calls.clear()
    cfg = Config(
        host="http://127.0.0.1:11434",
        cloud=False,
        model="gpt-local:latest",
        model_provider="ollama",
    )

    client = runtime_services.create_client(cfg)

    assert isinstance(client, _FakeOllamaClient)
    assert client.kwargs["host"] == "http://127.0.0.1:11434"
    assert model_routing.runtime_mode_label(cfg) == "local"
    assert model_routing.effective_runtime_host(cfg) == "http://127.0.0.1:11434"


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


@pytest.mark.parametrize(
    "model",
    [
        "gpt-3.5-turbo",
        "gpt-4",
        "gpt-4.1-mini",
        "gpt-4o-realtime-preview",
        "gpt-5.1",
        "chatgpt-4o-latest",
        "o3-mini",
    ],
)
def test_chatgpt_detection_accepts_known_openai_model_families(model):
    assert model_info.is_chatgpt_model(model) is True


@pytest.mark.parametrize(
    "model",
    ["gpt-local:latest", "gpt-neo", "gpt-j", "gpt4all", "gpt-oss:120b-cloud"],
)
def test_auto_provider_does_not_steal_local_gpt_named_models(model):
    cfg = Config(model=model, model_provider="auto")

    assert model_info.is_chatgpt_model(model) is False
    assert model_routing.resolved_model_provider(cfg) == "ollama"
    assert model_routing.runtime_mode_label(cfg) == "local"


def test_provider_models_do_not_start_local_ollama(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(runtime_services, "start_local_ollama_host", lambda host: calls.append(host) or True)

    assert runtime_services.start_ollama_server(Config(model="grok-4-latest")) is True
    assert runtime_services.start_ollama_server(Config(model="gpt-5.1")) is True
    assert calls == []


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("http://localhost:11434", True),
        ("LOCALHOST:11434", True),
        ("http://127.0.0.2:11434", True),
        ("http://[::1]:11434", True),
        ("https://localhost.example", False),
        ("https://notlocalhost.test", False),
        ("https://ollama.com", False),
        ("", False),
    ],
)
def test_host_is_local_requires_an_exact_loopback_endpoint(host, expected):
    assert runtime_services.host_is_local(host) is expected


@pytest.mark.parametrize(
    ("url", "require_http", "expected"),
    [
        ("http://127.0.0.1:8765", True, "127.0.0.1:8765"),
        ("http://[::1]:8765/", True, "[::1]:8765"),
        ("https://localhost:11434", False, "localhost:11434"),
        ("https://localhost:11434", True, None),
        ("http://localhost", True, None),
        ("http://user:secret@localhost:8765", True, None),
        ("http://localhost:8765/path", True, None),
        ("http://localhost.example:8765", True, None),
        ("http://0.0.0.0:8765", True, None),
    ],
)
def test_local_service_address_is_explicit_loopback_and_credential_free(
    url,
    require_http,
    expected,
):
    assert runtime_services.local_service_address(url, require_http=require_http) == expected


def test_gateway_ready_never_contacts_remote_or_ambiguous_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_services, "urlopen", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert runtime_services.gateway_ready("https://example.com:8765") is False
    assert runtime_services.gateway_ready("http://user:secret@localhost:8765") is False
    assert runtime_services.gateway_ready("http://localhost") is False
    assert calls == []


def test_failed_server_probe_uses_short_negative_cache(monkeypatch):
    runtime_services.SERVER_READY_CACHE.clear()
    probe_times = iter((10.0, 10.1, 10.3))
    calls: list[object] = []

    def fail_probe(request, timeout):
        calls.append((request, timeout))
        raise URLError("offline")

    monkeypatch.setattr(runtime_services.time, "monotonic", lambda: next(probe_times))
    monkeypatch.setattr(runtime_services, "urlopen", fail_probe)

    assert runtime_services.ollama_server_ready("http://127.0.0.1:11434") is False
    assert runtime_services.ollama_server_ready("http://127.0.0.1:11434") is False
    assert runtime_services.ollama_server_ready("http://127.0.0.1:11434") is False
    assert len(calls) == 2


def test_agent_block_xai_falls_back_when_not_authenticated(monkeypatch):
    active = object()
    monkeypatch.setattr(runtime_services.xai_auth, "get_valid_token", lambda: None)
    messages: list[str] = []
    monkeypatch.setattr(runtime_services, "show_info", lambda msg: messages.append(msg))
    cfg = Config(model="qwen3:latest")

    assert runtime_services.client_for_model("grok-4-latest", cfg, active) is active
    assert any("XAI_API_KEY" in msg for msg in messages)


def test_agent_block_chatgpt_falls_back_when_not_authenticated(monkeypatch):
    active = object()
    from algo_cli import chatgpt_auth

    monkeypatch.setattr(chatgpt_auth, "get_valid_token", lambda: None)
    messages: list[str] = []
    monkeypatch.setattr(runtime_services, "show_info", lambda msg: messages.append(msg))
    cfg = Config(model="qwen3:latest")

    assert runtime_services.client_for_model("gpt-5.1", cfg, active) is active
    assert any("ChatGPT OAuth" in msg for msg in messages)
