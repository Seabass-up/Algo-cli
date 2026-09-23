"""Regression tests for provider routing, Ollama host probing, and xAI key precedence."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from algo_cli import config, model_routing, xai_auth
from algo_cli import theodore_runtime_services as runtime_services
from algo_cli.config import Config


class _VersionHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"version":"0.0.0"}')

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def version_server():
    server = HTTPServer(("127.0.0.1", 0), _VersionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _clear_ready_cache():
    runtime_services.SERVER_READY_CACHE.clear()
    yield
    runtime_services.SERVER_READY_CACHE.clear()


@pytest.mark.parametrize("host_template", ["127.0.0.1:{port}", "localhost:{port}", "http://127.0.0.1:{port}"])
def test_ollama_server_ready_accepts_host_without_scheme(version_server, host_template):
    host = host_template.format(port=version_server)

    assert runtime_services.ollama_server_ready(host) is True


def test_start_local_ollama_host_does_not_spawn_for_schemeless_running_host(version_server, monkeypatch):
    spawned: list[object] = []
    monkeypatch.setattr(runtime_services.subprocess, "Popen", lambda *a, **k: spawned.append(a))

    assert runtime_services.start_local_ollama_host(f"127.0.0.1:{version_server}") is True
    assert spawned == []


def _block_client_host(client) -> str:
    return str(client._client.base_url).rstrip("/")


def _no_env_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", tmp_path / "env")
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("ALGO_CLI_ENV_FILE", raising=False)
    monkeypatch.delenv("OLLAMA_CLI_ENV_FILE", raising=False)


def test_cloud_block_routes_to_ollama_cloud_while_xai_model_is_active(tmp_path, monkeypatch):
    _no_env_file(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test-key")
    cfg = Config(model="grok-4", cloud=True, host="http://localhost:11434")

    client = runtime_services.client_for_model("gpt-oss:120b-cloud", cfg, object())

    assert _block_client_host(client) == "https://ollama.com"


def test_unsuffixed_block_routes_to_ollama_cloud_in_direct_cloud_mode(tmp_path, monkeypatch):
    _no_env_file(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test-key")
    cfg = Config(model="gpt-oss:120b", cloud=True, host="http://localhost:11434")

    assert model_routing.uses_ollama_cloud(cfg, "qwen3-coder:480b") is model_routing.uses_ollama_cloud(cfg)
    client = runtime_services.client_for_model("qwen3-coder:480b", cfg, object())

    assert _block_client_host(client) == "https://ollama.com"


def test_cloud_suffixed_block_uses_local_daemon_when_direct_cloud_is_off(tmp_path, monkeypatch):
    _no_env_file(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test-key")
    cfg = Config(model="qwen3:8b", cloud=False, host="http://localhost:11434")

    client = runtime_services.client_for_model("gpt-oss:120b-cloud", cfg, object())

    assert _block_client_host(client) == "http://localhost:11434"


def test_uses_ollama_cloud_for_active_model_is_unchanged(tmp_path, monkeypatch):
    _no_env_file(tmp_path, monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-test-key")
    cfg = Config(model="gpt-oss:120b-cloud", cloud=True)

    assert model_routing.uses_ollama_cloud(cfg) is True
    assert model_routing.uses_ollama_cloud(cfg, cfg.model) is True
    assert model_routing.uses_ollama_cloud(cfg, "grok-4") is False


def test_xai_status_reports_env_file_key_precedence(tmp_path, monkeypatch):
    env_path = tmp_path / "env"
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_path)
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
    env_path.write_text("XAI_API_KEY=file-old-value\n", encoding="utf-8")
    monkeypatch.setenv(xai_auth.XAI_API_KEY_ENV, "shell-new-value")

    status = xai_auth.auth_status()

    assert status["api_key_source"] == "runtime_env_file"
    assert "file-old-value" not in repr(status)


def test_xai_status_reports_environment_key_source(tmp_path, monkeypatch):
    _no_env_file(tmp_path, monkeypatch)
    monkeypatch.setenv(xai_auth.XAI_API_KEY_ENV, "shell-value")

    assert xai_auth.auth_status()["api_key_source"] == "environment"


def test_require_api_key_explains_env_file_precedence(monkeypatch):
    monkeypatch.delenv(xai_auth.XAI_API_KEY_ENV, raising=False)

    with pytest.raises(RuntimeError, match="env file takes precedence"):
        xai_auth.require_api_key()


class _ExitedProcess:
    def poll(self):
        return 2


def test_supplemental_gateway_gets_normalized_host_and_fails_fast_on_exit(monkeypatch):
    launched: list[list[str]] = []
    errors: list[str] = []

    def fake_popen(args, **_kwargs):
        launched.append(list(args))
        return _ExitedProcess()

    monkeypatch.setattr(runtime_services, "uses_ollama_cloud", lambda _cfg: True)
    monkeypatch.setattr(runtime_services, "start_local_ollama_host", lambda _host: True)
    monkeypatch.setattr(runtime_services, "gateway_url", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(runtime_services, "gateway_ready", lambda _url=None: False)
    monkeypatch.setattr(runtime_services, "gateway_command", lambda: (["harness-gateway"], runtime_services.Path(".")))
    monkeypatch.setattr(runtime_services.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_services, "show_error", errors.append)
    monkeypatch.setattr(runtime_services.time, "sleep", lambda _s: pytest.fail("waited on an exited gateway"))
    cfg = Config(model="gpt-oss:120b", cloud=True, host="127.0.0.1:11434")

    assert runtime_services.start_supplemental_gateway(cfg) is False

    args = launched[0]
    assert args[args.index("-ollama") + 1] == "http://127.0.0.1:11434"
    assert runtime_services.GATEWAY_PROCESS is None
    assert "exited with code 2" in errors[-1]
