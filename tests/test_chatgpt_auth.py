"""Offline tests for ChatGPT/OpenAI OAuth + PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
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
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
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

    rendered = chatgpt_auth.safe_error_message(f'provider failed: {{"access_token": "{secret}"}}')

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
    auth_file.chmod(0o600)

    tokens = chatgpt_auth.import_codex_auth_file(auth_file)

    assert tokens["access_token"] == "AT"
    assert tokens["refresh_token"] == "RT"
    assert tokens["provider"] == "chatgpt-codex"
    assert tokens["account_id"] == "acct_123"
    assert chatgpt_auth.load_tokens()["access_token"] == "AT"


def test_extracts_chatgpt_account_id_from_access_token(config_dir: Path):
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_jwt"}}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
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


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_prepare_codex_auth_home_rejects_junctioned_ancestor_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    alias = tmp_path / "alias"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(victim)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    monkeypatch.setattr(chatgpt_auth, "CODEX_AUTH_HOME", alias / "codex-chatgpt")

    with pytest.raises(OSError, match="ancestry is unsafe"):
        chatgpt_auth._prepare_codex_auth_home()
    assert list(victim.iterdir()) == []


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
        (codex_home / "auth.json").chmod(0o600)
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
    assert not chatgpt_auth.CODEX_AUTH_HOME.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited credential cleanup contract")
def test_failed_codex_login_removes_inherited_private_auth_source(
    config_dir: Path,
) -> None:
    from algo_cli import config as config_module

    def failed_run(cmd: list[str], *, env: dict[str, str], check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        auth_file = Path(env["CODEX_HOME"]) / "auth.json"
        auth_file.write_text('{"refresh_token":"TRANSIENT_SECRET"}', encoding="utf-8")
        assert (
            config_module._windows_dacl_is_safe(
                auth_file,
                require_current_owner=False,
                reject_untrusted_read=True,
                require_protected_dacl=False,
            )
            is True
        )
        return subprocess.CompletedProcess(cmd, 1)

    with pytest.raises(RuntimeError, match="exit code 1"):
        chatgpt_auth.run_codex_device_login(codex_bin="codex", runner=failed_run)

    assert not chatgpt_auth.CODEX_AUTH_HOME.exists()
    assert not chatgpt_auth.AUTH_FILE.exists()


def test_clear_tokens_removes_primary_and_historical_codex_source(config_dir: Path):
    canary = "REFRESH_TOKEN_CANARY_352ee8"
    chatgpt_auth.save_tokens({"access_token": "AT", "refresh_token": canary})
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.write_text(json.dumps({"access_token": "AT", "refresh_token": canary}), encoding="utf-8")
    source.chmod(0o600)
    if os.name == "nt":
        from algo_cli import config as config_module

        for inherited in (chatgpt_auth.CODEX_AUTH_HOME, source):
            assert (
                config_module._windows_dacl_is_safe(
                    inherited,
                    require_current_owner=False,
                    reject_untrusted_read=True,
                    require_protected_dacl=False,
                )
                is True
            )

    assert chatgpt_auth.clear_tokens() is True

    assert not chatgpt_auth.AUTH_FILE.exists()
    assert not source.exists()
    assert not chatgpt_auth.CODEX_AUTH_HOME.exists()
    assert chatgpt_auth.stored_auth_state_present() is False


def test_clear_tokens_reports_partial_failure_for_codex_source_symlink(config_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside-auth.json"
    outside.write_text('{"refresh_token":"RETAINED_CANARY"}', encoding="utf-8")
    outside.chmod(0o600)
    chatgpt_auth.save_tokens({"access_token": "AT", "refresh_token": "RT"})
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.symlink_to(outside)

    assert chatgpt_auth.clear_tokens() is False

    assert not chatgpt_auth.AUTH_FILE.exists()
    assert source.is_symlink()
    assert "RETAINED_CANARY" in outside.read_text(encoding="utf-8")
    assert chatgpt_auth.stored_auth_state_present() is True


def test_clear_tokens_refuses_special_codex_source(config_dir: Path):
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.mkdir()

    assert chatgpt_auth.clear_tokens() is False

    assert source.is_dir()
    assert chatgpt_auth.stored_auth_state_present() is True


def test_dedicated_import_refuses_symlink_without_creating_primary(config_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside-auth.json"
    outside.write_text(json.dumps({"access_token": "AT", "refresh_token": "RT"}), encoding="utf-8")
    outside.chmod(0o600)
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.symlink_to(outside)

    with pytest.raises(RuntimeError, match="owner-only regular file"):
        chatgpt_auth.import_codex_auth_file()

    assert source.is_symlink()
    assert not chatgpt_auth.AUTH_FILE.exists()


def test_import_rolls_back_primary_when_duplicate_cleanup_is_not_provable(config_dir: Path):
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.write_text(json.dumps({"access_token": "AT", "refresh_token": "RT"}), encoding="utf-8")
    source.chmod(0o600)
    (chatgpt_auth.CODEX_AUTH_HOME / "unexpected-state").write_text("retain", encoding="utf-8")

    with pytest.raises(RuntimeError, match="duplicate credential cleanup"):
        chatgpt_auth.import_codex_auth_file()

    assert not chatgpt_auth.AUTH_FILE.exists()
    assert source.exists()
    assert (chatgpt_auth.CODEX_AUTH_HOME / "unexpected-state").exists()


def test_failed_duplicate_cleanup_restores_exact_preexisting_primary(config_dir: Path):
    chatgpt_auth.save_tokens({"access_token": "OLD", "refresh_token": "OLD_RT", "marker": "preserve"})
    before = chatgpt_auth.AUTH_FILE.read_bytes()
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.write_text(json.dumps({"access_token": "NEW", "refresh_token": "NEW_RT"}), encoding="utf-8")
    source.chmod(0o600)
    (chatgpt_auth.CODEX_AUTH_HOME / "unexpected-state").write_text("retain", encoding="utf-8")

    with pytest.raises(RuntimeError, match="duplicate credential cleanup"):
        chatgpt_auth.import_codex_auth_file()

    assert chatgpt_auth.AUTH_FILE.read_bytes() == before
    assert chatgpt_auth.load_tokens()["access_token"] == "OLD"
    assert source.exists()


def test_external_auth_source_cannot_request_false_green_consumption(config_dir: Path, tmp_path: Path):
    canary = "EXTERNAL_REFRESH_CANARY_a0dd28"
    source = tmp_path / "external-auth.json"
    source.write_text(json.dumps({"access_token": "AT", "refresh_token": canary}), encoding="utf-8")
    source.chmod(0o600)

    with pytest.raises(RuntimeError, match="source-consumption policy"):
        chatgpt_auth.import_codex_auth_file(source, consume_source=True)

    assert canary in source.read_text(encoding="utf-8")
    assert not chatgpt_auth.AUTH_FILE.exists()


def test_dedicated_auth_source_cannot_disable_required_consumption(config_dir: Path):
    canary = "DEDICATED_REFRESH_CANARY_d61161"
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.write_text(json.dumps({"access_token": "AT", "refresh_token": canary}), encoding="utf-8")
    source.chmod(0o600)

    with pytest.raises(RuntimeError, match="source-consumption policy"):
        chatgpt_auth.import_codex_auth_file(source, consume_source=False)

    assert canary in source.read_text(encoding="utf-8")
    assert not chatgpt_auth.AUTH_FILE.exists()


def test_dedicated_auth_case_alias_is_still_mandatorily_consumed(config_dir: Path):
    canary = "CASE_ALIAS_REFRESH_CANARY_c353c9"
    chatgpt_auth.CODEX_AUTH_HOME.mkdir(mode=0o700)
    source = chatgpt_auth.CODEX_AUTH_HOME / "auth.json"
    source.write_text(json.dumps({"access_token": "AT", "refresh_token": canary}), encoding="utf-8")
    source.chmod(0o600)
    alias = source.with_name("AUTH.JSON")
    if not alias.exists():
        pytest.skip("filesystem is case-sensitive")

    tokens = chatgpt_auth.import_codex_auth_file(alias)

    assert tokens["refresh_token"] == canary
    assert not source.exists()
    assert not chatgpt_auth.CODEX_AUTH_HOME.exists()


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
    chatgpt_auth.save_tokens({"access_token": "OLD", "refresh_token": "RT", "expires_at": int(time.time()) - 10})
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
