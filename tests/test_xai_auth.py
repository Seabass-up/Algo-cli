"""Offline tests for the xAI OAuth + PKCE flow.

No network calls — the token endpoint is monkeypatched at the
_post_token_endpoint level. The loopback HTTP listener is not started
in any test.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from algo_cli import xai_auth

TEST_XAI_CLIENT_ID = "algo-cli-test-xai-client"


@pytest.fixture(autouse=True)
def configured_xai_client(monkeypatch):
    """Most OAuth tests exercise the explicitly configured path."""
    monkeypatch.setenv(xai_auth.XAI_CLIENT_ID_ENV, TEST_XAI_CLIENT_ID)


def _decode_b64url(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class TestPkce:
    def test_generate_pair_returns_two_strings(self):
        verifier, challenge = xai_auth.generate_pkce_pair()
        assert isinstance(verifier, str) and isinstance(challenge, str)
        assert verifier and challenge

    def test_verifier_is_url_safe(self):
        verifier, _ = xai_auth.generate_pkce_pair()
        # RFC 7636: verifier characters in [A-Za-z0-9-._~]
        assert all(c.isalnum() or c in "-._~" for c in verifier)

    def test_verifier_length_in_rfc_range(self):
        # Spec allows 43-128 chars; 32 random bytes b64url-encoded = 43 chars.
        verifier, _ = xai_auth.generate_pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_challenge_matches_s256_of_verifier(self):
        verifier, challenge = xai_auth.generate_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_pairs_are_distinct(self):
        # Cryptographic randomness — two calls must not match.
        a = xai_auth.generate_pkce_pair()
        b = xai_auth.generate_pkce_pair()
        assert a != b


class TestAuthorizeUrl:
    def test_missing_client_id_fails_before_url_is_built(self, monkeypatch):
        monkeypatch.delenv(xai_auth.XAI_CLIENT_ID_ENV, raising=False)

        with pytest.raises(RuntimeError, match="Set XAI_CLIENT_ID"):
            xai_auth.build_authorize_url(state="abc", code_challenge="xyz")

    def test_contains_all_required_oauth_params(self):
        url = xai_auth.build_authorize_url(state="abc", code_challenge="xyz")
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.netloc == "auth.x.ai"
        assert parsed.path == "/oauth2/authorize"
        assert qs["response_type"] == ["code"]
        assert qs["client_id"] == [TEST_XAI_CLIENT_ID]
        assert qs["redirect_uri"] == [xai_auth.XAI_REDIRECT_URI]
        assert qs["state"] == ["abc"]
        assert qs["code_challenge"] == ["xyz"]
        assert qs["code_challenge_method"] == ["S256"]

    def test_default_scope_openid_offline_access(self):
        url = xai_auth.build_authorize_url(state="s", code_challenge="c")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert qs["scope"] == ["openid offline_access api:access"]

    def test_custom_scope_passed_through(self):
        url = xai_auth.build_authorize_url(
            state="s", code_challenge="c", scope="openid profile offline_access"
        )
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert qs["scope"] == ["openid profile offline_access"]


class TestNormalizeTokenResponse:
    def test_minimal_payload_filled_with_defaults(self):
        out = xai_auth._normalize_token_response(
            {"access_token": "a", "expires_in": 3600}
        )
        assert out["access_token"] == "a"
        assert out["token_type"] == "Bearer"
        assert out["refresh_token"] is None
        assert out["expires_at"] > int(time.time())
        assert out["scope"] == ""

    def test_missing_access_token_raises(self):
        with pytest.raises(RuntimeError):
            xai_auth._normalize_token_response({"expires_in": 3600})

    def test_refresh_token_falls_back_when_absent(self):
        out = xai_auth._normalize_token_response(
            {"access_token": "a", "expires_in": 60},
            fallback_refresh="OLD_REFRESH",
        )
        assert out["refresh_token"] == "OLD_REFRESH"

    def test_refresh_token_in_payload_wins_over_fallback(self):
        out = xai_auth._normalize_token_response(
            {"access_token": "a", "refresh_token": "NEW", "expires_in": 60},
            fallback_refresh="OLD",
        )
        assert out["refresh_token"] == "NEW"

    def test_bad_expires_in_falls_back_to_default(self):
        out = xai_auth._normalize_token_response(
            {"access_token": "a", "expires_in": "garbage"}
        )
        assert out["expires_at"] >= int(time.time())


class TestTokenStorage:
    def test_save_then_load_roundtrip(self, config_dir: Path):
        tokens = {
            "access_token": "AT",
            "refresh_token": "RT",
            "token_type": "Bearer",
            "expires_at": int(time.time()) + 3600,
            "scope": "openid offline_access",
            "obtained_at": int(time.time()),
        }
        xai_auth.save_tokens(tokens)
        loaded = xai_auth.load_tokens()
        assert loaded == tokens

    def test_load_returns_none_when_no_file(self, config_dir: Path):
        # clean_state fixture wipes config_dir before each test.
        assert xai_auth.load_tokens() is None

    def test_load_returns_none_on_corrupt_json(self, config_dir: Path):
        xai_auth.AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        xai_auth.AUTH_FILE.write_text("{not json", encoding="utf-8")
        assert xai_auth.load_tokens() is None

    def test_clear_removes_file(self, config_dir: Path):
        xai_auth.save_tokens({"access_token": "x", "expires_at": 0})
        assert xai_auth.AUTH_FILE.exists()
        assert xai_auth.clear_tokens() is True
        assert not xai_auth.AUTH_FILE.exists()

    def test_clear_returns_false_when_no_file(self, config_dir: Path):
        assert xai_auth.clear_tokens() is False


class TestExpiryAndStatus:
    def test_is_token_expired_true_when_past(self):
        assert xai_auth.is_token_expired({"expires_at": int(time.time()) - 1000}) is True

    def test_is_token_expired_false_when_far_future(self):
        assert (
            xai_auth.is_token_expired({"expires_at": int(time.time()) + 10_000}) is False
        )

    def test_is_token_expired_true_inside_refresh_window(self):
        # Within the 60s refresh window the token is treated as expired.
        assert xai_auth.is_token_expired({"expires_at": int(time.time()) + 10}) is True

    def test_auth_status_when_unauthenticated(self, config_dir: Path):
        assert xai_auth.auth_status() == {
            "authenticated": False,
            "client_configured": True,
            "token_present": False,
        }

    def test_auth_status_when_authenticated(self, config_dir: Path):
        xai_auth.save_tokens(
            {
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_at": int(time.time()) + 3600,
                "scope": "openid offline_access",
                "token_type": "Bearer",
            }
        )
        status = xai_auth.auth_status()
        assert status["authenticated"] is True
        assert status["client_configured"] is True
        assert status["token_present"] is True
        assert status["has_refresh_token"] is True
        assert status["scope"] == "openid offline_access"
        assert 3500 <= status["expires_in"] <= 3600


class TestGetValidToken:
    def test_returns_none_when_no_tokens(self, config_dir: Path):
        assert xai_auth.get_valid_token() is None

    def test_returns_cached_token_when_not_expired(self, config_dir: Path):
        xai_auth.save_tokens(
            {"access_token": "FRESH", "expires_at": int(time.time()) + 3600}
        )
        assert xai_auth.get_valid_token() == "FRESH"

    def test_returns_none_without_client_id_even_when_token_is_fresh(
        self, config_dir: Path, monkeypatch
    ):
        xai_auth.save_tokens(
            {"access_token": "FRESH", "expires_at": int(time.time()) + 3600}
        )
        monkeypatch.delenv(xai_auth.XAI_CLIENT_ID_ENV, raising=False)

        assert xai_auth.get_valid_token() is None
        status = xai_auth.auth_status()
        assert status["authenticated"] is False
        assert status["token_present"] is True

    def test_refreshes_when_expired(self, config_dir: Path, monkeypatch):
        xai_auth.save_tokens(
            {
                "access_token": "OLD",
                "refresh_token": "RT",
                "expires_at": int(time.time()) - 100,
            }
        )

        def fake_post(form: dict[str, str]) -> dict[str, Any]:
            assert form["grant_type"] == "refresh_token"
            assert form["client_id"] == TEST_XAI_CLIENT_ID
            assert form["refresh_token"] == "RT"
            return {"access_token": "NEW", "expires_in": 3600}

        monkeypatch.setattr(xai_auth, "_post_token_endpoint", fake_post)
        assert xai_auth.get_valid_token() == "NEW"
        stored = xai_auth.load_tokens()
        assert stored["access_token"] == "NEW"
        # Refresh token preserved when server didn't rotate it.
        assert stored["refresh_token"] == "RT"

    def test_returns_none_when_refresh_fails(self, config_dir: Path, monkeypatch):
        xai_auth.save_tokens(
            {
                "access_token": "OLD",
                "refresh_token": "RT",
                "expires_at": int(time.time()) - 100,
            }
        )

        def fake_post(form: dict[str, str]) -> dict[str, Any]:
            raise RuntimeError("network down")

        monkeypatch.setattr(xai_auth, "_post_token_endpoint", fake_post)
        assert xai_auth.get_valid_token() is None


def test_complete_login_uses_callback_redirect_uri(config_dir: Path, monkeypatch):
    captured: dict[str, str] = {}

    def fake_post(form: dict[str, str]) -> dict[str, Any]:
        captured.update(form)
        return {"access_token": "AT", "expires_in": 3600}

    monkeypatch.setattr(xai_auth, "_post_token_endpoint", fake_post)

    tokens = xai_auth.complete_login(
        "verifier",
        "expected-state",
        {
            "code": "CODE",
            "state": "expected-state",
            "redirect_uri": "http://127.0.0.1:56125/callback",
        },
    )

    assert tokens["access_token"] == "AT"
    assert captured["redirect_uri"] == "http://127.0.0.1:56125/callback"

    def test_returns_none_when_expired_and_no_refresh_token(self, config_dir: Path):
        xai_auth.save_tokens(
            {"access_token": "OLD", "expires_at": int(time.time()) - 100}
        )
        assert xai_auth.get_valid_token() is None


class TestCompleteLogin:
    def test_rejects_empty_callback(self):
        with pytest.raises(RuntimeError, match="Timed out"):
            xai_auth.complete_login("v", "s", {})

    def test_rejects_none_callback(self):
        with pytest.raises(RuntimeError, match="Timed out"):
            xai_auth.complete_login("v", "s", None)

    def test_rejects_error_response_prefers_description(self):
        with pytest.raises(RuntimeError, match="user said no"):
            xai_auth.complete_login(
                "v", "s", {"error": "access_denied", "error_description": "user said no"}
            )

    def test_rejects_error_response_falls_back_to_code(self):
        with pytest.raises(RuntimeError, match="access_denied"):
            xai_auth.complete_login("v", "s", {"error": "access_denied"})

    def test_rejects_missing_code(self):
        with pytest.raises(RuntimeError, match="No authorization code"):
            xai_auth.complete_login("v", "s", {"state": "s"})

    def test_rejects_state_mismatch(self):
        with pytest.raises(RuntimeError, match="state mismatch"):
            xai_auth.complete_login("v", "expected", {"code": "c", "state": "wrong"})

    def test_happy_path_exchanges_and_saves(self, config_dir: Path, monkeypatch):
        def fake_post(form: dict[str, str]) -> dict[str, Any]:
            assert form["grant_type"] == "authorization_code"
            assert form["code"] == "CODE123"
            assert form["code_verifier"] == "VERIFIER"
            assert form["client_id"] == TEST_XAI_CLIENT_ID
            return {
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "scope": "openid offline_access",
            }

        monkeypatch.setattr(xai_auth, "_post_token_endpoint", fake_post)
        tokens = xai_auth.complete_login(
            "VERIFIER", "STATE", {"code": "CODE123", "state": "STATE"}
        )
        assert tokens["access_token"] == "AT"
        assert tokens["refresh_token"] == "RT"
        # And it persisted.
        on_disk = json.loads(xai_auth.AUTH_FILE.read_text(encoding="utf-8"))
        assert on_disk["access_token"] == "AT"


class TestBeginLogin:
    def test_missing_client_id_fails_before_browser_is_opened(self, monkeypatch):
        monkeypatch.delenv(xai_auth.XAI_CLIENT_ID_ENV, raising=False)
        browser_calls: list[str] = []
        monkeypatch.setattr(xai_auth.webbrowser, "open", lambda url: browser_calls.append(url))

        with pytest.raises(RuntimeError, match="does not bundle a client id"):
            xai_auth.begin_login(no_browser=False)

        assert browser_calls == []

    def test_skips_browser_when_no_browser_true(self, monkeypatch):
        calls: list[str] = []

        def fake_open(url: str, new: int = 0) -> bool:
            calls.append(url)
            return True

        monkeypatch.setattr(xai_auth.webbrowser, "open", fake_open)
        prep = xai_auth.begin_login(no_browser=True)
        assert calls == []
        assert prep["auth_url"].startswith(xai_auth.XAI_AUTHORIZE_URL + "?")
        assert prep["code_verifier"]
        assert prep["state"]
        assert "ssh -N -L" in prep["ssh_tunnel_cmd"]
        assert prep["redirect_uri"] == xai_auth.XAI_REDIRECT_URI

    def test_attempts_browser_when_no_browser_false(self, monkeypatch):
        calls: list[str] = []

        def fake_open(url: str, new: int = 0) -> bool:
            calls.append(url)
            return True

        monkeypatch.setattr(xai_auth.webbrowser, "open", fake_open)
        prep = xai_auth.begin_login(no_browser=False)
        assert calls and calls[0] == prep["auth_url"]

    def test_state_and_verifier_are_distinct_per_call(self, monkeypatch):
        monkeypatch.setattr(xai_auth.webbrowser, "open", lambda *a, **k: True)
        a = xai_auth.begin_login(no_browser=True)
        b = xai_auth.begin_login(no_browser=True)
        assert a["state"] != b["state"]
        assert a["code_verifier"] != b["code_verifier"]

    def test_begin_login_can_target_selected_redirect_port(self, monkeypatch):
        monkeypatch.setattr(xai_auth.webbrowser, "open", lambda *a, **k: True)

        prep = xai_auth.begin_login(no_browser=True, redirect_port=56125)
        parsed = urllib.parse.urlparse(prep["auth_url"])
        params = urllib.parse.parse_qs(parsed.query)

        assert prep["redirect_uri"] == "http://127.0.0.1:56125/callback"
        assert params["redirect_uri"] == [prep["redirect_uri"]]
        assert "56125:127.0.0.1:56125" in prep["ssh_tunnel_cmd"]


class TestPortCheck:
    def test_port_is_free_returns_bool(self):
        # We can't guarantee port state, but the call must not crash and must
        # return a bool.
        result = xai_auth.port_is_free(xai_auth.XAI_REDIRECT_PORT)
        assert isinstance(result, bool)


class TestB64UrlEncoder:
    def test_no_padding(self):
        # Padding equals signs are stripped per RFC 7636.
        assert "=" not in xai_auth._b64url(b"\x00" * 32)

    def test_roundtrip_preserves_bytes(self):
        data = b"hello, world!" * 3
        encoded = xai_auth._b64url(data)
        assert _decode_b64url(encoded) == data


def test_exchange_and_refresh_require_configured_client_id(monkeypatch):
    monkeypatch.delenv(xai_auth.XAI_CLIENT_ID_ENV, raising=False)
    called = False

    def fake_post(_form: dict[str, str]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"access_token": "AT"}

    monkeypatch.setattr(xai_auth, "_post_token_endpoint", fake_post)

    with pytest.raises(RuntimeError, match="XAI_CLIENT_ID"):
        xai_auth.exchange_code("code", "verifier")
    with pytest.raises(RuntimeError, match="XAI_CLIENT_ID"):
        xai_auth.refresh_access_token("refresh")
    assert called is False


def test_reported_oauth_errors_redact_configured_client_id(monkeypatch):
    client_id = "client-id-that-must-not-appear-in-output"
    monkeypatch.setenv(xai_auth.XAI_CLIENT_ID_ENV, client_id)

    message = xai_auth.safe_error_message(f"provider rejected client_id={client_id}")

    assert client_id not in message
    assert "[redacted-client-id]" in message
