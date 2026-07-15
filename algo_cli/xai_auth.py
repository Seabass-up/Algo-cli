"""xAI API-key configuration helpers.

xAI's public API documents Bearer API-key authentication.  Earlier Algo CLI
versions attempted a consumer subscription OAuth flow against inferred
endpoints, which cannot reliably authorize ``api.x.ai`` requests.  This module
is deliberately small: it reads the documented ``XAI_API_KEY`` setting,
redacts it from errors, and detects old OAuth artifacts so users can migrate
without silently reusing an unsupported credential path.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .config import CONFIG_DIR, load_runtime_env


XAI_API_KEY_ENV = "XAI_API_KEY"
XAI_API_BASE = "https://api.x.ai/v1"

# Read only for a clear migration notice; these values are never used to call
# the xAI API again.
LEGACY_XAI_CLIENT_ID_ENV = "XAI_CLIENT_ID"
LEGACY_AUTH_FILE = CONFIG_DIR / "xai_auth.json"

_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;]+)")
_QUERY_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|code)=)([^&\s,]+)"
)


def resolve_api_key() -> str:
    """Return the configured xAI API key without printing it."""

    return os.environ.get(XAI_API_KEY_ENV, "").strip()


def api_key_configured() -> bool:
    return bool(resolve_api_key())


def require_api_key() -> str:
    key = resolve_api_key()
    if not key:
        raise RuntimeError(
            "xAI API authentication is not configured. Run `algo-cli config setup xai` "
            "to save XAI_API_KEY locally, or set XAI_API_KEY in the environment."
        )
    return key


def legacy_oauth_detected() -> bool:
    """Return whether pre-v0.17 unsupported xAI OAuth state is present."""

    return bool(os.environ.get(LEGACY_XAI_CLIENT_ID_ENV, "").strip()) or LEGACY_AUTH_FILE.exists()


def clear_legacy_oauth_state() -> bool:
    """Remove an obsolete local OAuth token file without affecting API keys."""

    try:
        LEGACY_AUTH_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def safe_error_message(error: BaseException | str) -> str:
    """Redact configured xAI credentials and common bearer/query forms."""

    message = str(error)
    for value, label in (
        (resolve_api_key(), "[redacted-api-key]"),
        (os.environ.get(LEGACY_XAI_CLIENT_ID_ENV, "").strip(), "[redacted-legacy-client-id]"),
    ):
        if value:
            message = message.replace(value, label)
    message = _BEARER_RE.sub(r"\1[redacted]", message)
    return _QUERY_SECRET_RE.sub(r"\1[redacted]", message)


def get_valid_token() -> str | None:
    """Compatibility name for the xAI client; returns the configured API key.

    Calling ``load_runtime_env`` here keeps agent blocks and direct client use
    aligned even when a subprocess did not inherit the user's shell variables.
    No OAuth refresh or implicit network request is attempted.
    """

    load_runtime_env(override=True)
    return resolve_api_key() or None


def auth_status() -> dict[str, Any]:
    """Return safe xAI readiness metadata with no credential material."""

    load_runtime_env(override=True)
    configured = api_key_configured()
    return {
        "authenticated": configured,
        "api_key_configured": configured,
        "legacy_oauth_detected": legacy_oauth_detected(),
    }
