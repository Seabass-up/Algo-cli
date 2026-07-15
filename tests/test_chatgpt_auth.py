"""Offline tests for ChatGPT/OpenAI OAuth + PKCE helpers."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from algo_cli import chatgpt_auth


def test_generate_pkce_pair_is_rfc7636_s256():
    verifier, challenge = chatgpt_auth.generate_pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert 43 <= len(verifier) <= 128
    assert challenge == expected


def test_begin_login_uses_bundled_codex_client_id(monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "CHATGPT_CLIENT_ID", "")
    monkeypatch.delenv("OPENAI_OAUTH_CLIENT_ID", raising=False)

    prep = chatgpt_auth.begin_login(no_browser=True)
    parsed = urllib.parse.urlparse(prep["auth_url"])
    qs = urllib.parse.parse_qs(parsed.query)

    assert qs["client_id"] == [chatgpt_auth.CHATGPT_CODEX_CLIENT_ID]
    assert qs["redirect_uri"] == [chatgpt_auth.CHATGPT_REDIRECT_URI]


def test_build_authorize_url_uses_pkce_and_openai_scope(monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "CHATGPT_CLIENT_ID", "client-123")
    url = chatgpt_auth.build_authorize_url(state="s", code_challenge="c")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["client-123"]
    assert qs["redirect_uri"] == [chatgpt_auth.CHATGPT_REDIRECT_URI]
    assert qs["state"] == ["s"]
    assert qs["code_challenge"] == ["c"]
    assert qs["code_challenge_method"] == ["S256"]
    assert "offline_access" in qs["scope"][0]
    assert qs["id_token_add_organizations"] == ["true"]
    assert qs["codex_cli_simplified_flow"] == ["true"]
    assert qs["originator"] == ["pi"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/token",
        "file:///tmp/token",
        "https://user:password@example.com/token",
        "not a url",
    ],
)
def test_validate_credential_endpoint_rejects_unsafe_urls(url):
    with pytest.raises(RuntimeError, match="endpoint"):
        chatgpt_auth.validate_credential_endpoint(url, "test endpoint")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/token/",
        "http://localhost:8080/token",
        "http://127.0.0.1:8080/token",
        "http://[::1]:8080/token",
    ],
)
def test_validate_credential_endpoint_accepts_https_and_loopback(url):
    assert chatgpt_auth.validate_credential_endpoint(url, "test endpoint") == url.rstrip("/")


def test_save_load_clear_tokens(config_dir: Path):
    tokens = {
        "access_token": "AT",
        "refresh_token": "RT",
        "expires_at": int(time.time()) + 3600,
        "scope": "openid offline_access",
    }
    chatgpt_auth.save_tokens(tokens)
    assert chatgpt_auth.load_tokens() == tokens
    assert chatgpt_auth.clear_tokens() is True
    assert chatgpt_auth.load_tokens() is None


def test_auth_status_fails_closed_for_expired_or_malformed_token_state(config_dir: Path):
    chatgpt_auth.save_tokens({"access_token": "AT", "expires_at": "not-a-timestamp"})

    status = chatgpt_auth.auth_status()

    assert status["authenticated"] is False
    assert status["token_present"] is True
    assert status["token_valid"] is False
    assert status["expires_at"] == 0


def test_token_normalization_error_does_not_echo_secrets():
    secret = "refresh-secret-not-for-terminal"

    with pytest.raises(RuntimeError) as exc:
        chatgpt_auth._normalize_token_response({"refresh_token": secret})

    assert secret not in str(exc.value)


def test_safe_error_message_redacts_json_token_fields():
    secret = "access-secret-not-for-terminal"

    rendered = chatgpt_auth.safe_error_message(
        f'provider failed: {{"access_token": "{secret}"}}'
    )

    assert secret not in rendered
    assert "[redacted]" in rendered


def test_safe_error_message_redacts_oauth_code_but_preserves_http_status_code():
    rendered = chatgpt_auth.safe_error_message(
        'request failed with status code: 404; callback={"code": "oauth-secret"}'
    )

    assert "status code: 404" in rendered
    assert "oauth-secret" not in rendered


def test_complete_login_rejects_state_mismatch():
    with pytest.raises(RuntimeError, match="state mismatch"):
        chatgpt_auth.complete_login("verifier", "expected", {"code": "c", "state": "wrong"})


def test_complete_login_exchanges_and_saves(config_dir: Path, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "CHATGPT_CLIENT_ID", "client-123")
    captured: dict[str, str] = {}

    def fake_post(form: dict[str, str]) -> dict[str, Any]:
        captured.update(form)
        return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    monkeypatch.setattr(chatgpt_auth, "_post_token_endpoint", fake_post)
    tokens = chatgpt_auth.complete_login(
        "VERIFIER",
        "STATE",
        {"code": "CODE", "state": "STATE", "redirect_uri": "http://127.0.0.1:56225/callback"},
    )

    assert tokens["access_token"] == "AT"
    assert captured["grant_type"] == "authorization_code"
    assert captured["client_id"] == "client-123"
    assert captured["code_verifier"] == "VERIFIER"
    assert captured["redirect_uri"] == "http://127.0.0.1:56225/callback"
    assert json.loads(chatgpt_auth.AUTH_FILE.read_text(encoding="utf-8"))["access_token"] == "AT"


def test_import_codex_auth_file_saves_chatgpt_tokens(config_dir: Path, tmp_path: Path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth_file = codex_home / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "AT",
                    "refresh_token": "RT",
                    "expires_at": int(time.time()) + 3600,
                    "scope": "openid offline_access",
                    "token_type": "Bearer",
                },
                "account_id": "acct_123",
            }
        ),
        encoding="utf-8",
    )

    tokens = chatgpt_auth.import_codex_auth_file(auth_file)

    assert tokens["access_token"] == "AT"
    assert tokens["refresh_token"] == "RT"
    assert tokens["provider"] == "chatgpt-codex"
    assert tokens["account_id"] == "acct_123"
    assert chatgpt_auth.load_tokens()["access_token"] == "AT"


def test_extracts_chatgpt_account_id_from_access_token(config_dir: Path):
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_jwt"}}).encode()
    ).rstrip(b"=").decode()
    token = f"{header}.{payload}."

    chatgpt_auth.save_tokens({"access_token": token, "refresh_token": "RT", "expires_at": int(time.time()) + 3600})

    assert chatgpt_auth.get_chatgpt_account_id() == "acct_jwt"
    assert chatgpt_auth.load_tokens()["account_id"] == "acct_jwt"


def test_resolve_codex_bin_finds_windows_npm_shim_when_path_is_stale(tmp_path: Path, monkeypatch):
    appdata = tmp_path / "AppData" / "Roaming"
    npm_dir = appdata / "npm"
    npm_dir.mkdir(parents=True)
    shim = npm_dir / "codex.cmd"
    shim.write_text("@echo off\r\n", encoding="utf-8")

    monkeypatch.setattr(chatgpt_auth.shutil, "which", lambda _name: None)
    monkeypatch.setenv("APPDATA", str(appdata))

    assert chatgpt_auth.resolve_codex_bin() == str(shim)


def test_run_codex_device_login_uses_algo_owned_codex_home(config_dir: Path, monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], *, env: dict[str, str], check: bool) -> subprocess.CompletedProcess[str]:
        calls.append({"cmd": cmd, "env": env, "check": check})
        codex_home = Path(env["CODEX_HOME"])
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "auth.json").write_text(
            json.dumps({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0)

    tokens = chatgpt_auth.run_codex_device_login(codex_bin="codex", runner=fake_run)

    assert tokens["access_token"] == "AT"
    assert calls[0]["cmd"] == [
        "codex",
        "login",
        "--device-auth",
        "-c",
        'cli_auth_credentials_store="file"',
    ]
    assert Path(calls[0]["env"]["CODEX_HOME"]) == config_dir / "codex-chatgpt"
    assert chatgpt_auth.AUTH_FILE.exists()


def test_run_codex_device_login_reports_missing_codex(config_dir: Path):
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("codex")

    with pytest.raises(RuntimeError, match="Codex CLI is not installed"):
        chatgpt_auth.run_codex_device_login(runner=fake_run)


def test_get_valid_token_refreshes_expired_token(config_dir: Path, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "CHATGPT_CLIENT_ID", "client-123")
    chatgpt_auth.save_tokens({"access_token": "OLD", "refresh_token": "RT", "expires_at": int(time.time()) - 10})

    def fake_post(form: dict[str, str]) -> dict[str, Any]:
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "RT"
        return {"access_token": "NEW", "expires_in": 3600}

    monkeypatch.setattr(chatgpt_auth, "_post_token_endpoint", fake_post)
    assert chatgpt_auth.get_valid_token() == "NEW"
    assert chatgpt_auth.load_tokens()["refresh_token"] == "RT"


def test_concurrent_token_refresh_is_serialized(config_dir: Path, monkeypatch):
    monkeypatch.setattr(chatgpt_auth, "CHATGPT_CLIENT_ID", "client-123")
    chatgpt_auth.save_tokens(
        {"access_token": "OLD", "refresh_token": "RT", "expires_at": int(time.time()) - 10}
    )
    refreshes: list[str] = []

    def fake_refresh(refresh_token: str, **_kwargs: Any) -> dict[str, Any]:
        refreshes.append(refresh_token)
        time.sleep(0.05)
        return {
            "access_token": "NEW",
            "refresh_token": "NRT",
            "expires_at": int(time.time()) + 3600,
        }

    monkeypatch.setattr(chatgpt_auth, "refresh_access_token", fake_refresh)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: chatgpt_auth.get_valid_token(), range(2)))

    assert results == ["NEW", "NEW"]
    assert refreshes == ["RT"]


def test_codex_token_refresh_uses_codex_client_without_scope(config_dir: Path, monkeypatch):
    captured: dict[str, str] = {}

    def fake_post(form: dict[str, str]) -> dict[str, Any]:
        captured.update(form)
        return {"access_token": "NEW", "refresh_token": "NRT", "expires_in": 3600}

    monkeypatch.setattr(chatgpt_auth, "_post_token_endpoint", fake_post)

    tokens = chatgpt_auth.refresh_codex_access_token("RT")

    assert tokens["access_token"] == "NEW"
    assert tokens["provider"] == "chatgpt-codex"
    assert captured["client_id"] == chatgpt_auth.CHATGPT_CODEX_CLIENT_ID
    assert "scope" not in captured
