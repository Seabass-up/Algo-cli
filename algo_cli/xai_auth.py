"""Optional xAI OAuth 2.0 + PKCE authentication.

Public client, Authorization Code + PKCE (S256), loopback redirect. Tokens
persist to CONFIG_DIR/xai_auth.json with POSIX 0600 permissions. This is the
subscription OAuth path.

API-key fallback is intentionally disabled elsewhere.

Algo CLI deliberately does not bundle an OAuth client identity. Users who
enable this optional provider must supply a client id they are authorized to
use through ``XAI_CLIENT_ID``.
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

XAI_CLIENT_ID_ENV = "XAI_CLIENT_ID"
XAI_AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_REDIRECT_HOST = "127.0.0.1"
XAI_REDIRECT_PORT = 56121
# We'll try a range of ports if the default is in use
XAI_REDIRECT_PORT_RANGE = range(56121, 56151)
XAI_REDIRECT_URI = f"http://{XAI_REDIRECT_HOST}:{XAI_REDIRECT_PORT}/callback"
XAI_DEFAULT_SCOPE = "openid offline_access api:access"
XAI_API_BASE = "https://api.x.ai/v1"

AUTH_FILE = CONFIG_DIR / "xai_auth.json"
_REFRESH_WINDOW_SECONDS = 60
_CALLBACK_TIMEOUT_SECONDS = 300.0


def resolve_client_id() -> str:
    """Return the user-supplied xAI OAuth client id, or an empty string."""
    return os.environ.get(XAI_CLIENT_ID_ENV, "").strip()


def client_id_configured() -> bool:
    """Whether optional xAI OAuth has a user-provided client identity."""
    return bool(resolve_client_id())


def require_client_id() -> str:
    """Resolve the xAI client id or fail before starting an OAuth flow."""
    client_id = resolve_client_id()
    if not client_id:
        raise RuntimeError(
            "xAI subscription OAuth is optional and is not configured. "
            "Set XAI_CLIENT_ID to an OAuth client id you are authorized to use, "
            "then retry /xai-login. Algo CLI does not bundle a client id."
        )
    return client_id


def safe_error_message(error: BaseException | str) -> str:
    """Format an OAuth error without echoing the configured client identity."""
    message = str(error)
    client_id = resolve_client_id()
    if client_id:
        message = message.replace(client_id, "[redacted-client-id]")
        encoded = urllib.parse.quote_plus(client_id)
        if encoded != client_id:
            message = message.replace(encoded, "[redacted-client-id]")
    return message


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def redirect_uri_for_port(port: int = XAI_REDIRECT_PORT) -> str:
    return f"http://{XAI_REDIRECT_HOST}:{port}/callback"


def build_authorize_url(
    *,
    state: str,
    code_challenge: str,
    scope: str = XAI_DEFAULT_SCOPE,
    redirect_uri: str | None = None,
) -> str:
    client_id = require_client_id()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri or XAI_REDIRECT_URI,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{XAI_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        type(self).received = {k: v[0] for k, v in qs.items() if v}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in type(self).received and "error" not in type(self).received
        body = (
            "<html><body style='font-family:sans-serif;padding:2em'>"
            "<h2>xAI login complete.</h2>"
            "<p>You can close this tab and return to algo-cli.</p></body></html>"
            if ok
            else "<html><body style='font-family:sans-serif;padding:2em'>"
            "<h2>xAI login failed.</h2>"
            "<p>Return to algo-cli for details.</p></body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        return


def run_loopback_capture(
    *,
    timeout: float = _CALLBACK_TIMEOUT_SECONDS,
    redirect_port: int = XAI_REDIRECT_PORT,
) -> dict[str, str]:
    """Run a single-shot HTTP listener on the selected loopback redirect port.

    Returns the parsed query string from the first /callback request, or an
    empty dict on timeout/bind failure. Port selection happens before the auth
    URL is built so the browser redirect URI and token exchange stay aligned.
    """
    class Handler(_CallbackHandler):
        received: dict[str, str] = {}

    try:
        server = http.server.HTTPServer((XAI_REDIRECT_HOST, redirect_port), Handler)
    except OSError:
        return {}
    server.timeout = 1.0
    deadline = time.time() + timeout
    try:
        while not Handler.received and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return dict(Handler.received)


def _post_token_endpoint(form: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(
        XAI_TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def exchange_code(code: str, code_verifier: str, *, redirect_uri: str | None = None) -> dict[str, Any]:
    client_id = require_client_id()
    payload = _post_token_endpoint(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri or XAI_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
    )
    return _normalize_token_response(payload)


def refresh_access_token(refresh_token: str, *, scope: str = XAI_DEFAULT_SCOPE) -> dict[str, Any]:
    client_id = require_client_id()
    payload = _post_token_endpoint(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": scope,
        }
    )
    return _normalize_token_response(payload, fallback_refresh=refresh_token)


def _normalize_token_response(
    payload: dict[str, Any], *, fallback_refresh: str | None = None
) -> dict[str, Any]:
    if "access_token" not in payload:
        # Token responses may include provider diagnostics or credentials. Do
        # not reflect the payload into terminal output or logs.
        raise RuntimeError("xAI OAuth token response did not include an access token.")
    now = int(time.time())
    try:
        expires_in = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    return {
        "access_token": str(payload["access_token"]),
        "refresh_token": payload.get("refresh_token") or fallback_refresh,
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": now + expires_in,
        "scope": payload.get("scope", ""),
        "obtained_at": now,
    }


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


def clear_tokens() -> bool:
    if not AUTH_FILE.exists():
        return False
    try:
        AUTH_FILE.unlink()
        return True
    except OSError:
        return False


def is_token_expired(tokens: dict[str, Any], *, window: int = _REFRESH_WINDOW_SECONDS) -> bool:
    return int(tokens.get("expires_at", 0)) - window <= int(time.time())


def get_valid_token() -> str | None:
    """Return a fresh access token, refreshing silently if expiry is near.

    Returns None when the optional client identity is not configured, when no
    tokens are stored, when refresh is required but no refresh_token exists,
    or when the refresh call fails.
    """
    if not client_id_configured():
        return None
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
    client_configured = client_id_configured()
    if not tokens:
        return {
            "authenticated": False,
            "client_configured": client_configured,
            "token_present": False,
        }
    expires_at = int(tokens.get("expires_at", 0))
    return {
        "authenticated": client_configured,
        "client_configured": client_configured,
        "token_present": True,
        "expires_at": expires_at,
        "expires_in": max(0, expires_at - int(time.time())),
        "scope": tokens.get("scope", ""),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "token_type": tokens.get("token_type", "Bearer"),
    }


def port_is_free(port: int = XAI_REDIRECT_PORT) -> bool:
    sock = socket.socket()
    sock.settimeout(0.2)
    try:
        result = sock.connect_ex((XAI_REDIRECT_HOST, port))
        return result != 0
    except OSError:
        return False
    finally:
        sock.close()


def select_redirect_port() -> int | None:
    for port in XAI_REDIRECT_PORT_RANGE:
        if port_is_free(port):
            return port
    return None


def begin_login(*, no_browser: bool = False, redirect_port: int = XAI_REDIRECT_PORT) -> dict[str, Any]:
    # Resolve before generating state, opening a browser, or starting a
    # callback listener so an unconfigured install fails closed and cleanly.
    require_client_id()
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    redirect_uri = redirect_uri_for_port(redirect_port)

    url = build_authorize_url(state=state, code_challenge=challenge, redirect_uri=redirect_uri)
    if not no_browser:
        opened = webbrowser.open(url)
        if not opened:
            no_browser = True
    ssh_tunnel_cmd = f"ssh -N -L {redirect_port}:{XAI_REDIRECT_HOST}:{redirect_port} you@remote-host"
    return {
        "state": state,
        "code_verifier": verifier,
        "auth_url": url,
        "redirect_uri": redirect_uri,
        "redirect_port": str(redirect_port),
        "ssh_tunnel_cmd": ssh_tunnel_cmd,
        "browser_opened": not no_browser,
    }


def complete_login(code_verifier: str, state: str, callback: dict[str, str] | None) -> dict[str, Any]:
    if not callback:
        raise RuntimeError("Timed out waiting for OAuth callback.")
    if "error" in callback:
        detail = callback.get("error_description", callback["error"])
        raise RuntimeError(f"OAuth error: {safe_error_message(detail)}")
    if callback.get("state") != state:
        raise RuntimeError("OAuth state mismatch - possible CSRF attack.")
    code = callback.get("code")
    if not code:
        raise RuntimeError("No authorization code in callback.")
    tokens = exchange_code(code, code_verifier, redirect_uri=callback.get("redirect_uri"))
    save_tokens(tokens)
    return tokens
