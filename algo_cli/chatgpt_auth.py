"""ChatGPT/OpenAI OAuth 2.0 + PKCE helpers.

OpenAI's public OAuth configuration can vary by application. Algo CLI defaults
to the Codex browser OAuth client used by the subscription runtime, while still
allowing provider configuration through environment variables:

- OPENAI_OAUTH_CLIENT_ID (optional override for ``algo-cli config setup chatgpt``)
- OPENAI_CODEX_CLIENT_ID (optional override for the bundled Codex client)
- OPENAI_OAUTH_AUTHORIZE_URL (optional)
- OPENAI_OAUTH_TOKEN_URL (optional)
- OPENAI_API_BASE (optional, defaults to https://api.openai.com/v1)

The runtime shape intentionally mirrors xai_auth.py so tests and user-facing
commands behave consistently across cloud providers.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, _atomic_write_text, _exclusive_state_lock, _state_descriptor_payload

CHATGPT_CLIENT_ID = os.environ.get("OPENAI_OAUTH_CLIENT_ID", "").strip()
CHATGPT_AUTHORIZE_URL = os.environ.get("OPENAI_OAUTH_AUTHORIZE_URL", "https://auth.openai.com/oauth/authorize")
CHATGPT_TOKEN_URL = os.environ.get("OPENAI_OAUTH_TOKEN_URL", "https://auth.openai.com/oauth/token")
CHATGPT_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
CHATGPT_CODEX_CLIENT_ID = os.environ.get("OPENAI_CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann").strip()
CHATGPT_REDIRECT_HOST = "localhost"
CHATGPT_REDIRECT_PORT = 1455
CHATGPT_REDIRECT_PORT_RANGE = range(1455, 1456)
CHATGPT_REDIRECT_PATH = "/auth/callback"
CHATGPT_REDIRECT_URI = f"http://{CHATGPT_REDIRECT_HOST}:{CHATGPT_REDIRECT_PORT}{CHATGPT_REDIRECT_PATH}"
CHATGPT_DEFAULT_SCOPE = os.environ.get("OPENAI_OAUTH_SCOPE", "openid offline_access profile email")
CODEX_DEVICE_VERIFY_URL = "https://auth.openai.com/codex/device"
CODEX_AUTH_HOME = CONFIG_DIR / "codex-chatgpt"
AUTH_FILE = CONFIG_DIR / "chatgpt_auth.json"
_REFRESH_WINDOW_SECONDS = 60
_CALLBACK_TIMEOUT_SECONDS = 300.0
_MAX_AUTH_FILE_BYTES = 2 * 1024 * 1024


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def safe_error_message(error: BaseException | str) -> str:
    """Redact OAuth credential fields before an error reaches the terminal."""

    message = str(error)
    message = re.sub(
        r"(?i)(['\"]?(?:access_token|refresh_token|id_token|client_secret)['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}&]+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(r"(?i)(\bcode\s*=\s*['\"]?)[^'\"&\s,}]+", r"\1[redacted]", message)
    message = re.sub(
        r"(?i)((?:['\"]code['\"]|(?:[{,]\s*)code)\s*:\s*['\"]?)[^'\"\s,}&]+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+", r"\1[redacted]", message)
    return message


def _client_id() -> str:
    return (CHATGPT_CLIENT_ID or os.environ.get("OPENAI_OAUTH_CLIENT_ID", "") or CHATGPT_CODEX_CLIENT_ID).strip()


def validate_credential_endpoint(url: str, label: str) -> str:
    """Reject endpoint overrides that could disclose OAuth credentials.

    HTTPS is mandatory for remote hosts. Plain HTTP remains available only for
    explicit loopback development endpoints, where traffic cannot leave the
    machine. Embedded URL credentials are never accepted.
    """

    value = str(url or "").strip().rstrip("/")
    try:
        parsed = urllib.parse.urlparse(value)
        hostname = (parsed.hostname or "").lower()
    except ValueError as exc:
        raise RuntimeError(f"{label} is not a valid URL.") from exc
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
    ):
        raise RuntimeError(f"{label} must use HTTPS (HTTP is allowed only for loopback hosts).")
    return value


def resolve_codex_bin() -> str | None:
    """Find Codex even when a long-running shell has stale PATH."""
    override = os.environ.get("CODEX_BIN", "").strip()
    if override:
        return override
    for name in ("codex", "codex.cmd", "codex.exe", "codex.ps1"):
        found = shutil.which(name)
        if found:
            return found
    candidate_dirs: list[str] = []
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidate_dirs.append(os.path.join(appdata, "npm"))
    userprofile = os.environ.get("USERPROFILE", "").strip()
    if userprofile:
        candidate_dirs.append(os.path.join(userprofile, "AppData", "Roaming", "npm"))
    home = os.path.expanduser("~")
    if home and home != "~":
        candidate_dirs.append(os.path.join(home, "AppData", "Roaming", "npm"))
        candidate_dirs.append(os.path.join(home, ".npm-global", "bin"))
    for directory in candidate_dirs:
        for filename in ("codex.cmd", "codex.exe", "codex", "codex.ps1"):
            candidate = os.path.join(directory, filename)
            if os.path.exists(candidate):
                return candidate
    return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def redirect_uri_for_port(port: int = CHATGPT_REDIRECT_PORT) -> str:
    return f"http://{CHATGPT_REDIRECT_HOST}:{port}{CHATGPT_REDIRECT_PATH}"


def build_authorize_url(
    *,
    state: str,
    code_challenge: str,
    scope: str = CHATGPT_DEFAULT_SCOPE,
    redirect_uri: str | None = None,
) -> str:
    client_id = _client_id()
    if not client_id:
        raise RuntimeError("ChatGPT OAuth client id is empty. Set OPENAI_OAUTH_CLIENT_ID or OPENAI_CODEX_CLIENT_ID.")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri or CHATGPT_REDIRECT_URI,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": os.environ.get("OPENAI_CODEX_ORIGINATOR", "pi").strip() or "pi",
    }
    authorize_url = validate_credential_endpoint(CHATGPT_AUTHORIZE_URL, "OpenAI OAuth authorization endpoint")
    return f"{authorize_url}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/callback", CHATGPT_REDIRECT_PATH}:
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        type(self).received = {k: v[0] for k, v in qs.items() if v}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in type(self).received and "error" not in type(self).received
        title = "ChatGPT login complete." if ok else "ChatGPT login failed."
        self.wfile.write(
            f"<html><body style='font-family:sans-serif;padding:2em'><h2>{title}</h2>"
            "<p>You can close this tab and return to algo-cli.</p></body></html>".encode("utf-8")
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def run_loopback_capture(
    *, timeout: float = _CALLBACK_TIMEOUT_SECONDS, redirect_port: int = CHATGPT_REDIRECT_PORT
) -> dict[str, str]:
    class Handler(_CallbackHandler):
        received: dict[str, str] = {}

    try:
        server = http.server.HTTPServer((CHATGPT_REDIRECT_HOST, redirect_port), Handler)
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
    token_url = validate_credential_endpoint(CHATGPT_TOKEN_URL, "OpenAI OAuth token endpoint")
    req = urllib.request.Request(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_token_response(payload: dict[str, Any], *, fallback_refresh: str | None = None) -> dict[str, Any]:
    if not payload.get("access_token"):
        raise RuntimeError("Token endpoint returned no access_token.")
    now = int(time.time())
    try:
        expires_in = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    expires_at = payload.get("expires_at")
    try:
        expires_at_int = int(expires_at) if expires_at is not None else now + expires_in
    except (TypeError, ValueError):
        expires_at_int = now + expires_in
    normalized = {
        "access_token": str(payload["access_token"]),
        "refresh_token": payload.get("refresh_token") or fallback_refresh,
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": expires_at_int,
        "scope": payload.get("scope", ""),
        "obtained_at": _safe_int(payload.get("obtained_at", now) or now, now),
    }
    account_id = (
        payload.get("account_id")
        or payload.get("accountId")
        or payload.get("chatgpt_account_id")
        or _extract_chatgpt_account_id(str(payload["access_token"]))
    )
    if account_id:
        normalized["account_id"] = account_id
    return normalized


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_chatgpt_account_id(token: str) -> str | None:
    claims = _decode_jwt_payload(token)
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        value = auth_claim.get("chatgpt_account_id") or auth_claim.get("account_id")
        if value:
            return str(value)
    for key in ("chatgpt_account_id", "account_id"):
        value = claims.get(key)
        if value:
            return str(value)
    return None


def _extract_codex_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for key in ("tokens", "chatgptAuthTokens", "chatgpt_auth_tokens"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    candidates.append(payload)

    for candidate in candidates:
        access = candidate.get("access_token") or candidate.get("accessToken")
        if not access:
            continue
        token_payload = {
            "access_token": access,
            "refresh_token": candidate.get("refresh_token") or candidate.get("refreshToken"),
            "token_type": candidate.get("token_type") or candidate.get("tokenType") or "Bearer",
            "scope": candidate.get("scope") or payload.get("scope") or "",
            "obtained_at": candidate.get("obtained_at") or candidate.get("obtainedAt") or payload.get("obtained_at"),
        }
        if candidate.get("expires_at") is not None:
            token_payload["expires_at"] = candidate.get("expires_at")
        elif candidate.get("expiresAt") is not None:
            token_payload["expires_at"] = candidate.get("expiresAt")
        elif candidate.get("expires_in") is not None:
            token_payload["expires_in"] = candidate.get("expires_in")
        elif candidate.get("expiresIn") is not None:
            token_payload["expires_in"] = candidate.get("expiresIn")
        normalized = _normalize_token_response(token_payload)
        normalized["provider"] = "chatgpt-codex"
        account_id = (
            payload.get("account_id")
            or payload.get("accountId")
            or payload.get("chatgpt_account_id")
            or candidate.get("account_id")
            or candidate.get("accountId")
            or candidate.get("chatgpt_account_id")
            or _extract_chatgpt_account_id(str(access))
        )
        if account_id:
            normalized["account_id"] = account_id
        return normalized
    raise RuntimeError("Codex auth.json did not contain ChatGPT access tokens.")


def _dedicated_codex_auth_path() -> Path:
    return Path(CODEX_AUTH_HOME) / "auth.json"


def _same_path(left: Path, right: Path) -> bool:
    try:
        left_info = left.lstat()
        right_info = right.lstat()
    except OSError:
        pass
    else:
        if (left_info.st_dev, left_info.st_ino) == (right_info.st_dev, right_info.st_ino):
            return True
    try:
        return os.path.abspath(os.fspath(left)) == os.path.abspath(os.fspath(right))
    except (OSError, TypeError, ValueError):
        return False


def _read_private_auth_json(path: Path) -> dict[str, Any]:
    """Read one bounded owner-only auth file through no-follow descriptors."""

    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Codex auth file was not created at {path}.") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not inspect Codex auth file at {path}.") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > _MAX_AUTH_FILE_BYTES
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
        or (os.name == "posix" and before.st_mode & 0o077)
    ):
        raise RuntimeError(f"Codex auth file at {path} is not a private owner-only regular file.")
    try:
        raw = _state_descriptor_payload(path, max_bytes=_MAX_AUTH_FILE_BYTES)
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) != (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
        ):
            raise OSError("auth source changed during read")
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not safely read Codex auth file at {path}.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Codex auth file at {path} did not contain a JSON object.")
    return payload


def _prepare_codex_auth_home() -> None:
    """Create the dedicated transient Codex home as an owner-only real directory."""

    home = Path(CODEX_AUTH_HOME)
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = home.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or info.st_mode & 0o022
    ):
        raise RuntimeError("The dedicated Codex auth directory is unsafe.")
    if os.name == "posix":
        os.chmod(home, 0o700)


def _remove_primary_auth_unlocked() -> bool:
    """Remove the authoritative token file, refusing aliases and special files."""

    try:
        info = AUTH_FILE.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise OSError("primary ChatGPT auth state identity is unsafe")
    AUTH_FILE.unlink()
    if AUTH_FILE.exists() or AUTH_FILE.is_symlink():
        raise OSError("primary ChatGPT auth state remained after cleanup")
    return True


def _snapshot_primary_auth_unlocked() -> bytes | None:
    """Capture exact safe primary bytes so a failed import can restore them."""

    try:
        info = AUTH_FILE.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > _MAX_AUTH_FILE_BYTES
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or (os.name == "posix" and info.st_mode & 0o077)
    ):
        raise OSError("primary ChatGPT auth state identity is unsafe")
    return _state_descriptor_payload(AUTH_FILE, max_bytes=_MAX_AUTH_FILE_BYTES)


def _restore_primary_auth_unlocked(snapshot: bytes | None) -> None:
    if snapshot is None:
        _remove_primary_auth_unlocked()
        return
    try:
        text = snapshot.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OSError("prior ChatGPT auth state was not UTF-8") from exc
    _write_tokens_text_unlocked(text)


def _remove_dedicated_codex_auth_source() -> bool:
    """Remove the exact transient Codex source and its empty private home.

    The directory is intentionally flat. Any alias, hardlink, special file, or
    unexpected sibling makes cleanup fail closed because deleting it would not
    prove that all credential copies were removed.
    """

    home = Path(CODEX_AUTH_HOME)
    directory_flags = (
        os.O_RDONLY
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    try:
        parent_fd = os.open(home.parent, directory_flags)
    except FileNotFoundError:
        return False
    try:
        try:
            home_info = os.stat(home.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (
            stat.S_ISLNK(home_info.st_mode)
            or not stat.S_ISDIR(home_info.st_mode)
            or (hasattr(os, "getuid") and home_info.st_uid != os.getuid())
        ):
            raise OSError("dedicated Codex auth directory identity is unsafe")
        home_fd = os.open(home.name, directory_flags, dir_fd=parent_fd)
        try:
            opened_home = os.fstat(home_fd)
            if (opened_home.st_dev, opened_home.st_ino) != (home_info.st_dev, home_info.st_ino):
                raise OSError("dedicated Codex auth directory changed during cleanup")
            if os.name == "posix":
                os.fchmod(home_fd, 0o700)
            names = sorted(os.listdir(home_fd))
            if names not in ([], ["auth.json"]):
                raise OSError("dedicated Codex auth directory contains unexpected state")
            if names:
                auth_info = os.stat("auth.json", dir_fd=home_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(auth_info.st_mode)
                    or auth_info.st_nlink != 1
                    or (hasattr(os, "getuid") and auth_info.st_uid != os.getuid())
                ):
                    raise OSError("dedicated Codex auth source identity is unsafe")
                os.unlink("auth.json", dir_fd=home_fd)
            if os.listdir(home_fd):
                raise OSError("dedicated Codex auth state remained after cleanup")
        finally:
            os.close(home_fd)
        current_home = os.stat(home.name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_home.st_dev, current_home.st_ino) != (home_info.st_dev, home_info.st_ino):
            raise OSError("dedicated Codex auth directory changed during cleanup")
        os.rmdir(home.name, dir_fd=parent_fd)
        try:
            os.stat(home.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        raise OSError("dedicated Codex auth directory remained after cleanup")
    finally:
        os.close(parent_fd)


def stored_auth_state_present() -> bool:
    """Return whether either ChatGPT credential location has any path state."""

    for path in (AUTH_FILE, Path(CODEX_AUTH_HOME)):
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _import_codex_auth_file_unlocked(auth_path: Path, *, consume_source: bool) -> dict[str, Any]:
    payload = _read_private_auth_json(auth_path)
    tokens = _extract_codex_tokens(payload)
    previous_primary = _snapshot_primary_auth_unlocked() if consume_source else None
    _write_tokens_unlocked(tokens)
    if consume_source:
        try:
            _remove_dedicated_codex_auth_source()
        except OSError as exc:
            rollback_error: OSError | None = None
            try:
                _restore_primary_auth_unlocked(previous_primary)
            except OSError as rollback_exc:
                rollback_error = rollback_exc
            if rollback_error is not None:
                raise RuntimeError(
                    "Codex authentication could not prove duplicate cleanup or roll back the imported token."
                ) from exc
            raise RuntimeError("Codex authentication could not prove duplicate credential cleanup.") from exc
    return tokens


def import_codex_auth_file(
    path: str | os.PathLike[str] | None = None,
    *,
    consume_source: bool | None = None,
) -> dict[str, Any]:
    auth_path = Path(path or _dedicated_codex_auth_path())
    should_consume = _same_path(auth_path, _dedicated_codex_auth_path())
    if consume_source is not None and bool(consume_source) is not should_consume:
        raise RuntimeError("Codex auth source-consumption policy is fixed by the selected path.")
    with _exclusive_state_lock(AUTH_FILE, timeout_seconds=60.0):
        return _import_codex_auth_file_unlocked(auth_path, consume_source=should_consume)


def run_codex_device_login(*, codex_bin: str | None = None, runner: Any = subprocess.run) -> dict[str, Any]:
    resolved_codex_bin = codex_bin or resolve_codex_bin()
    if not resolved_codex_bin:
        raise RuntimeError(
            "Codex CLI is not installed or not discoverable. Install it with: "
            "npm install -g @openai/codex. If it is already installed, restart "
            "your terminal or set CODEX_BIN to the full path to codex.cmd."
        )
    with _exclusive_state_lock(AUTH_FILE, timeout_seconds=60.0):
        _prepare_codex_auth_home()
        env = os.environ.copy()
        env["CODEX_HOME"] = str(CODEX_AUTH_HOME)
        cmd = [resolved_codex_bin, "login", "--device-auth", "-c", 'cli_auth_credentials_store="file"']
        try:
            result = runner(cmd, env=env, check=False)
            returncode = getattr(result, "returncode", 0)
            if returncode != 0:
                raise RuntimeError(f"Codex device-code login failed with exit code {returncode}.")
            return _import_codex_auth_file_unlocked(
                _dedicated_codex_auth_path(),
                consume_source=True,
            )
        except FileNotFoundError as exc:
            try:
                _remove_dedicated_codex_auth_source()
            except OSError as cleanup_exc:
                raise RuntimeError(
                    "Codex CLI was unavailable and transient credential cleanup could not be proven."
                ) from cleanup_exc
            raise RuntimeError(
                "Codex CLI is not installed or not discoverable. Install it with: "
                "npm install -g @openai/codex. If it is already installed, restart "
                "your terminal or set CODEX_BIN to the full path to codex.cmd."
            ) from exc
        except Exception:
            try:
                _remove_dedicated_codex_auth_source()
            except OSError as cleanup_exc:
                raise RuntimeError(
                    "Codex device-code login failed and transient credential cleanup could not be proven."
                ) from cleanup_exc
            raise


def exchange_code(code: str, code_verifier: str, *, redirect_uri: str | None = None) -> dict[str, Any]:
    payload = _post_token_endpoint(
        {
            "grant_type": "authorization_code",
            "client_id": _client_id(),
            "code": code,
            "redirect_uri": redirect_uri or CHATGPT_REDIRECT_URI,
            "code_verifier": code_verifier,
        }
    )
    return _normalize_token_response(payload)


def refresh_access_token(
    refresh_token: str,
    *,
    scope: str | None = CHATGPT_DEFAULT_SCOPE,
    client_id: str | None = None,
) -> dict[str, Any]:
    form = {
        "grant_type": "refresh_token",
        "client_id": client_id or _client_id(),
        "refresh_token": refresh_token,
    }
    if scope is not None:
        form["scope"] = scope
    payload = _post_token_endpoint(form)
    return _normalize_token_response(payload, fallback_refresh=refresh_token)


def refresh_codex_access_token(refresh_token: str) -> dict[str, Any]:
    tokens = refresh_access_token(refresh_token, scope=None, client_id=CHATGPT_CODEX_CLIENT_ID)
    tokens["provider"] = "chatgpt-codex"
    return tokens


def _write_tokens_text_unlocked(text: str) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(AUTH_FILE, text)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except (OSError, NotImplementedError):
        pass


def _write_tokens_unlocked(tokens: dict[str, Any]) -> None:
    _write_tokens_text_unlocked(json.dumps(tokens, indent=2))


def save_tokens(tokens: dict[str, Any]) -> None:
    with _exclusive_state_lock(AUTH_FILE, timeout_seconds=60.0):
        _write_tokens_unlocked(tokens)


def load_tokens() -> dict[str, Any] | None:
    try:
        payload = _read_private_auth_json(AUTH_FILE)
    except RuntimeError:
        return None
    return payload if isinstance(payload, dict) else None


def clear_tokens() -> bool:
    removed = False
    complete = True
    try:
        with _exclusive_state_lock(AUTH_FILE, timeout_seconds=60.0):
            try:
                removed = _remove_primary_auth_unlocked() or removed
            except OSError:
                complete = False
            try:
                removed = _remove_dedicated_codex_auth_source() or removed
            except OSError:
                complete = False
            if stored_auth_state_present():
                complete = False
    except (OSError, TimeoutError):
        return False
    return bool(removed and complete)


def is_token_expired(tokens: dict[str, Any], *, window: int = _REFRESH_WINDOW_SECONDS) -> bool:
    return _safe_int(tokens.get("expires_at"), 0) - window <= int(time.time())


def get_valid_token() -> str | None:
    try:
        with _exclusive_state_lock(AUTH_FILE, timeout_seconds=60.0):
            tokens = load_tokens()
            if not tokens:
                return None
            if not is_token_expired(tokens):
                return tokens.get("access_token")
            refresh = tokens.get("refresh_token")
            if not refresh:
                return None
            try:
                if tokens.get("provider") == "chatgpt-codex":
                    new_tokens = refresh_codex_access_token(refresh)
                else:
                    new_tokens = refresh_access_token(refresh)
            except Exception:
                return None
            _write_tokens_unlocked(new_tokens)
            return new_tokens.get("access_token")
    except (OSError, TimeoutError):
        return None


def force_refresh_token() -> str | None:
    try:
        with _exclusive_state_lock(AUTH_FILE, timeout_seconds=60.0):
            tokens = load_tokens()
            if not tokens or not tokens.get("refresh_token"):
                return None
            try:
                if tokens.get("provider") == "chatgpt-codex":
                    new_tokens = refresh_codex_access_token(str(tokens["refresh_token"]))
                else:
                    new_tokens = refresh_access_token(str(tokens["refresh_token"]))
            except Exception:
                return None
            _write_tokens_unlocked(new_tokens)
            return new_tokens.get("access_token")
    except (OSError, TimeoutError):
        return None


def get_chatgpt_account_id() -> str | None:
    try:
        with _exclusive_state_lock(AUTH_FILE, timeout_seconds=60.0):
            tokens = load_tokens()
            if not tokens:
                return None
            account_id = tokens.get("account_id") or tokens.get("accountId") or tokens.get("chatgpt_account_id")
            if account_id:
                return str(account_id)
            access_token = tokens.get("access_token")
            if not access_token:
                return None
            account_id = _extract_chatgpt_account_id(str(access_token))
            if account_id:
                tokens["account_id"] = account_id
                _write_tokens_unlocked(tokens)
            return account_id
    except (OSError, TimeoutError):
        return None


def auth_status() -> dict[str, Any]:
    tokens = load_tokens()
    if not tokens:
        return {"authenticated": False, "client_configured": bool(_client_id())}
    expires_at = _safe_int(tokens.get("expires_at"), 0)
    has_access_token = bool(tokens.get("access_token"))
    token_valid = has_access_token and not is_token_expired(tokens)
    refreshable = bool(tokens.get("refresh_token")) and bool(_client_id())
    return {
        "authenticated": token_valid or refreshable,
        "client_configured": bool(_client_id()),
        "token_present": has_access_token or bool(tokens.get("refresh_token")),
        "expires_at": expires_at,
        "expires_in": max(0, expires_at - int(time.time())),
        "token_valid": token_valid,
        "scope": tokens.get("scope", ""),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "token_type": tokens.get("token_type", "Bearer"),
        "has_account_id": bool(tokens.get("account_id") or tokens.get("accountId") or tokens.get("chatgpt_account_id")),
    }


def port_is_free(port: int = CHATGPT_REDIRECT_PORT) -> bool:
    sock = socket.socket()
    sock.settimeout(0.2)
    try:
        return sock.connect_ex((CHATGPT_REDIRECT_HOST, port)) != 0
    except OSError:
        return False
    finally:
        sock.close()


def select_redirect_port() -> int | None:
    for port in CHATGPT_REDIRECT_PORT_RANGE:
        if port_is_free(port):
            return port
    return None


def begin_login(*, no_browser: bool = False, redirect_port: int = CHATGPT_REDIRECT_PORT) -> dict[str, Any]:
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    redirect_uri = redirect_uri_for_port(redirect_port)
    url = build_authorize_url(state=state, code_challenge=challenge, redirect_uri=redirect_uri)
    if not no_browser:
        opened = webbrowser.open(url)
        if not opened:
            no_browser = True
    return {
        "state": state,
        "code_verifier": verifier,
        "auth_url": url,
        "redirect_uri": redirect_uri,
        "redirect_port": str(redirect_port),
        "ssh_tunnel_cmd": f"ssh -N -L {redirect_port}:{CHATGPT_REDIRECT_HOST}:{redirect_port} you@remote-host",
        "browser_opened": not no_browser,
    }


def complete_login(code_verifier: str, state: str, callback: dict[str, str] | None) -> dict[str, Any]:
    if not callback:
        raise RuntimeError("Timed out waiting for OAuth callback.")
    if "error" in callback:
        raise RuntimeError(f"OAuth error: {callback.get('error_description', callback['error'])}")
    if callback.get("state") != state:
        raise RuntimeError("OAuth state mismatch - possible CSRF attack.")
    code = callback.get("code")
    if not code:
        raise RuntimeError("No authorization code in callback.")
    tokens = exchange_code(code, code_verifier, redirect_uri=callback.get("redirect_uri"))
    save_tokens(tokens)
    return tokens
