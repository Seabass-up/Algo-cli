"""Google Workspace OAuth 2.0 + PKCE helpers.

Read/write access to Drive, Docs, Sheets, and Calendar plus Gmail read/draft
access over a local-loopback Authorization Code + PKCE flow. Gmail draft
creation is allowed; direct send is intentionally not exposed. Public client —
no client secret is required (matches the xai_auth / chatgpt_auth pattern).

Configuration via environment variables:

- GOOGLE_OAUTH_CLIENT_ID (required for /google-login)
- GOOGLE_OAUTH_CLIENT_SECRET (optional; needed only for confidential Web-app
  credentials; public Desktop-app credentials can omit it)
- GOOGLE_OAUTH_REDIRECT_PORT (optional, default 56251)

Tokens persist to CONFIG_DIR/google_workspace_auth.json with POSIX 0600
permissions. Refresh is automatic; revoked tokens are removed.

This module intentionally avoids the google-auth / google-api-python-client
libraries so Algo CLI stays zero new pip deps.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

from .config import CONFIG_DIR, _atomic_write_text

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

GOOGLE_REDIRECT_HOST = "127.0.0.1"
GOOGLE_REDIRECT_PORT = int(os.environ.get("GOOGLE_OAUTH_REDIRECT_PORT", "56251"))
GOOGLE_REDIRECT_PORT_RANGE = range(GOOGLE_REDIRECT_PORT, GOOGLE_REDIRECT_PORT + 20)
GOOGLE_REDIRECT_URI = f"http://{GOOGLE_REDIRECT_HOST}:{GOOGLE_REDIRECT_PORT}/callback"

# Full read/write scopes for Drive/Docs/Sheets/Calendar. Gmail includes read +
# compose so Algo CLI can read mail and create drafts for user review; direct
# send is intentionally not exposed.
GOOGLE_DEFAULT_SCOPE = (
    "openid email profile "
    "https://www.googleapis.com/auth/drive "
    "https://www.googleapis.com/auth/documents "
    "https://www.googleapis.com/auth/spreadsheets "
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/gmail.readonly "
    "https://www.googleapis.com/auth/gmail.compose"
)

AUTH_FILE = CONFIG_DIR / "google_workspace_auth.json"
PENDING_AUTH_FILE = CONFIG_DIR / "google_workspace_pending_login.json"
_REFRESH_WINDOW_SECONDS = 60
_CALLBACK_TIMEOUT_SECONDS = 300.0
_PENDING_LOGIN_TTL_SECONDS = 900


# ---------------------------------------------------------------------------
# Client id / secret lookup
# ---------------------------------------------------------------------------


def _client_id() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def redirect_uri_for_port(port: int) -> str:
    return f"http://{GOOGLE_REDIRECT_HOST}:{port}/callback"


def build_authorize_url(
    *,
    state: str,
    code_challenge: str,
    redirect_uri: str,
    scope: str = GOOGLE_DEFAULT_SCOPE,
) -> str:
    client_id = _client_id()
    if not client_id:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID is not set. Set it to the OAuth client id "
            "of a Google Cloud project with Drive, Docs, Sheets, Calendar, and "
            "Gmail APIs enabled, and redirect URI "
            f"{redirect_uri} registered."
        )
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": scope,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        # Always request a refresh token; force `consent` so Google returns
        # one for the first run even if the user has already granted scopes.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Google OAuth token endpoint returned HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Google OAuth token request failed: {exc.reason}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Google OAuth returned non-JSON body: {payload!r}") from exc


def exchange_code(code: str, code_verifier: str, *, redirect_uri: str) -> dict[str, Any]:
    data: dict[str, str] = {
        "client_id": _client_id(),
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    payload = _post_form(GOOGLE_TOKEN_URL, data)
    return _normalize_token_response(payload)


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    data: dict[str, str] = {
        "client_id": _client_id(),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    secret = _client_secret()
    if secret:
        data["client_secret"] = secret
    payload = _post_form(GOOGLE_TOKEN_URL, data)
    return _normalize_token_response(payload, fallback_refresh=refresh_token)


def _normalize_token_response(
    payload: dict[str, Any],
    *,
    fallback_refresh: str | None = None,
) -> dict[str, Any]:
    if "error" in payload:
        raise RuntimeError(
            f"Google OAuth error: {payload.get('error')}: "
            f"{payload.get('error_description', '(no description)')}"
        )
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"Google OAuth response missing access_token: {payload!r}")
    expires_in = int(payload.get("expires_in", 3600))
    tokens: dict[str, Any] = {
        "access_token": access_token,
        "expires_at": int(time.time()) + expires_in,
        "expires_in": expires_in,
        "scope": payload.get("scope", ""),
        "token_type": payload.get("token_type", "Bearer"),
    }
    refresh = payload.get("refresh_token") or fallback_refresh
    if refresh:
        tokens["refresh_token"] = refresh
    id_token = payload.get("id_token")
    if id_token:
        tokens["id_token"] = id_token
    return tokens


def revoke_token(token: str) -> bool:
    """Best-effort revoke at Google. Failures are non-fatal."""
    body = urllib.parse.urlencode({"token": token}).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_REVOKE_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError:
        return False
    except urllib.error.URLError:
        return False


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------


def save_tokens(tokens: dict[str, Any]) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(AUTH_FILE, json.dumps(tokens, indent=2))
    try:
        os.chmod(AUTH_FILE, 0o600)
    except (OSError, NotImplementedError):
        pass


def load_tokens() -> dict[str, Any] | None:
    if not AUTH_FILE.exists():
        return None
    try:
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_tokens(*, revoke: bool = True) -> bool:
    tokens = load_tokens() if revoke else None
    if tokens:
        for key in ("refresh_token", "access_token"):
            value = tokens.get(key)
            if value:
                revoke_token(value)
    if not AUTH_FILE.exists():
        return False
    try:
        AUTH_FILE.unlink()
        return True
    except OSError:
        return False


def save_pending_login(prep: dict[str, Any]) -> None:
    PENDING_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": prep.get("state", ""),
        "code_verifier": prep.get("code_verifier", ""),
        "redirect_uri": prep.get("redirect_uri", ""),
        "redirect_port": prep.get("redirect_port", ""),
        "created_at": int(time.time()),
    }
    _atomic_write_text(PENDING_AUTH_FILE, json.dumps(payload, indent=2))
    try:
        os.chmod(PENDING_AUTH_FILE, 0o600)
    except (OSError, NotImplementedError):
        pass


def load_pending_login(*, max_age_seconds: int = _PENDING_LOGIN_TTL_SECONDS) -> dict[str, str] | None:
    if not PENDING_AUTH_FILE.exists():
        return None
    try:
        payload = json.loads(PENDING_AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    created_at = int(payload.get("created_at", 0) or 0)
    if created_at and int(time.time()) - created_at > max_age_seconds:
        clear_pending_login()
        return None
    required = ("state", "code_verifier", "redirect_uri")
    if not all(str(payload.get(key) or "").strip() for key in required):
        return None
    return {key: str(value) for key, value in payload.items()}


def clear_pending_login() -> bool:
    if not PENDING_AUTH_FILE.exists():
        return False
    try:
        PENDING_AUTH_FILE.unlink()
        return True
    except OSError:
        return False


def is_token_expired(tokens: dict[str, Any], *, window: int = _REFRESH_WINDOW_SECONDS) -> bool:
    return int(tokens.get("expires_at", 0)) - window <= int(time.time())


def get_valid_token() -> str | None:
    tokens = load_tokens()
    if not tokens:
        return None
    if not is_token_expired(tokens):
        return tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    if not refresh:
        return None
    try:
        new_tokens = refresh_access_token(refresh)
    except Exception:
        return None
    save_tokens(new_tokens)
    return new_tokens.get("access_token")


def auth_status() -> dict[str, Any]:
    tokens = load_tokens()
    client_configured = bool(_client_id())
    if not tokens:
        return {
            "authenticated": False,
            "client_configured": client_configured,
            "client_secret_configured": bool(_client_secret()),
        }
    expires_at = int(tokens.get("expires_at", 0))
    return {
        "authenticated": True,
        "client_configured": client_configured,
        "client_secret_configured": bool(_client_secret()),
        "expires_at": expires_at,
        "expires_in": max(0, expires_at - int(time.time())),
        "scope": tokens.get("scope", ""),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "token_type": tokens.get("token_type", "Bearer"),
    }


# ---------------------------------------------------------------------------
# Loopback capture
# ---------------------------------------------------------------------------


def port_is_free(port: int = GOOGLE_REDIRECT_PORT) -> bool:
    sock = socket.socket()
    sock.settimeout(0.2)
    try:
        return sock.connect_ex((GOOGLE_REDIRECT_HOST, port)) != 0
    except OSError:
        return False
    finally:
        sock.close()


def select_redirect_port() -> int | None:
    for port in GOOGLE_REDIRECT_PORT_RANGE:
        if port_is_free(port):
            return port
    return None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-request handler that captures the OAuth callback query string."""

    query_params: dict[str, str] = {}
    done = False

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # quiet

    def do_GET(self) -> None:  # noqa: N802 - http.server signature
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        type(self).query_params = params
        type(self).done = True
        body = (
            b"<!doctype html><html><body><h2>Algo CLI: Google Workspace login complete.</h2>"
            b"<p>You can close this tab and return to Algo CLI.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_loopback_capture(*, redirect_port: int, timeout: float = _CALLBACK_TIMEOUT_SECONDS) -> dict[str, str]:
    """Block until a callback arrives, then return the query parameters."""
    class Handler(_CallbackHandler):
        query_params: dict[str, str] = {}
        done = False

    server = http.server.HTTPServer(
        (GOOGLE_REDIRECT_HOST, redirect_port), Handler
    )
    server.timeout = 1.0
    deadline = time.time() + max(1.0, float(timeout))
    try:
        while time.time() < deadline and not Handler.done:
            server.handle_request()
    finally:
        server.server_close()
    if not Handler.done:
        raise RuntimeError(
            f"Timed out after {int(timeout)}s waiting for Google OAuth callback."
        )
    return dict(Handler.query_params)


def wait_for_callback(*, redirect_port: int, timeout: float = _CALLBACK_TIMEOUT_SECONDS) -> dict[str, str]:
    """Compatibility wrapper for the Google login flow's loopback callback wait."""
    return run_loopback_capture(redirect_port=redirect_port, timeout=timeout)


# ---------------------------------------------------------------------------
# Login orchestration
# ---------------------------------------------------------------------------


def begin_login(*, no_browser: bool = False, redirect_port: int = GOOGLE_REDIRECT_PORT) -> dict[str, Any]:
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    redirect_uri = redirect_uri_for_port(redirect_port)
    url = build_authorize_url(state=state, code_challenge=challenge, redirect_uri=redirect_uri)
    if not no_browser:
        opened = webbrowser.open(url)
        if not opened:
            no_browser = True
    prep: dict[str, Any] = {
        "state": state,
        "code_verifier": verifier,
        "auth_url": url,
        "redirect_uri": redirect_uri,
        "redirect_port": str(redirect_port),
        "ssh_tunnel_cmd": (
            f"ssh -N -L {redirect_port}:{GOOGLE_REDIRECT_HOST}:{redirect_port} you@remote-host"
        ),
        "browser_opened": not no_browser,
    }
    save_pending_login(prep)
    return prep


def parse_callback_value(value: str) -> dict[str, str]:
    """Parse a full callback URL, malformed URL, or raw query string."""
    text = (value or "").strip()
    if not text:
        return {}
    parsed = urllib.parse.urlparse(text)
    query = parsed.query
    if not query:
        candidate = text[1:] if text.startswith("?") else text
        if "=" in candidate:
            query = candidate
    if not query:
        return {}
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    return {key: values[0] for key, values in params.items() if values}


def complete_login(
    code_verifier: str,
    state: str,
    callback: dict[str, str] | None,
) -> dict[str, Any]:
    if not callback:
        raise RuntimeError("Timed out waiting for OAuth callback.")
    if "error" in callback:
        raise RuntimeError(
            f"OAuth error: {callback.get('error_description', callback['error'])}"
        )
    if callback.get("state") != state:
        raise RuntimeError("OAuth state mismatch - possible CSRF attack.")
    code = callback.get("code")
    if not code:
        raise RuntimeError("No authorization code in callback.")
    tokens = exchange_code(code, code_verifier, redirect_uri=callback.get("redirect_uri", ""))
    save_tokens(tokens)
    clear_pending_login()
    return tokens
