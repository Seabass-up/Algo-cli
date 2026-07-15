"""Offline tests for xAI's documented API-key configuration path."""

from __future__ import annotations

import os
import stat

import pytest

from algo_cli import config, xai_auth


def test_api_key_status_is_safe_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv(xai_auth.XAI_API_KEY_ENV, raising=False)
    monkeypatch.delenv(xai_auth.LEGACY_XAI_CLIENT_ID_ENV, raising=False)
    xai_auth.LEGACY_AUTH_FILE.unlink(missing_ok=True)

    assert xai_auth.auth_status() == {
        "authenticated": False,
        "api_key_configured": False,
        "legacy_oauth_detected": False,
    }


def test_get_valid_token_returns_documented_api_key(monkeypatch) -> None:
    monkeypatch.setenv(xai_auth.XAI_API_KEY_ENV, "xai-test-key")

    assert xai_auth.resolve_api_key() == "xai-test-key"
    assert xai_auth.get_valid_token() == "xai-test-key"
    assert xai_auth.auth_status()["authenticated"] is True


def test_require_api_key_gives_config_guidance(monkeypatch) -> None:
    monkeypatch.delenv(xai_auth.XAI_API_KEY_ENV, raising=False)

    with pytest.raises(RuntimeError, match="algo-cli config setup xai"):
        xai_auth.require_api_key()


def test_safe_error_message_redacts_api_and_legacy_values(monkeypatch) -> None:
    api_key = "xai-secret-not-for-output"
    legacy_id = "legacy-client-not-for-output"
    monkeypatch.setenv(xai_auth.XAI_API_KEY_ENV, api_key)
    monkeypatch.setenv(xai_auth.LEGACY_XAI_CLIENT_ID_ENV, legacy_id)

    rendered = xai_auth.safe_error_message(
        f"Authorization: Bearer {api_key}; client_id={legacy_id}; api_key={api_key}"
    )

    assert api_key not in rendered
    assert legacy_id not in rendered
    assert "Bearer [redacted]" in rendered
    assert "api_key=[redacted]" in rendered


def test_legacy_oauth_is_detected_but_never_used(monkeypatch) -> None:
    monkeypatch.delenv(xai_auth.XAI_API_KEY_ENV, raising=False)
    xai_auth.LEGACY_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    xai_auth.LEGACY_AUTH_FILE.write_text('{"access_token": "old"}', encoding="utf-8")

    assert xai_auth.legacy_oauth_detected() is True
    assert xai_auth.get_valid_token() is None
    assert xai_auth.clear_legacy_oauth_state() is True
    assert xai_auth.legacy_oauth_detected() is False


def test_runtime_env_updates_load_without_exposing_key(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / "env"
    monkeypatch.setattr(config, "DEFAULT_RUNTIME_ENV_FILE", env_path)
    monkeypatch.setattr(config, "DOTENV_RUNTIME_ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv(xai_auth.XAI_API_KEY_ENV, raising=False)

    config.update_runtime_env({xai_auth.XAI_API_KEY_ENV: "xai-from-file"})
    os.environ.pop(xai_auth.XAI_API_KEY_ENV, None)

    assert xai_auth.get_valid_token() == "xai-from-file"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_unsupported_oauth_helpers_are_not_part_of_runtime_surface() -> None:
    assert not hasattr(xai_auth, "begin_login")
    assert not hasattr(xai_auth, "refresh_access_token")
