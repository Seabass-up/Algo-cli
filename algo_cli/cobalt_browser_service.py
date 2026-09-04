"""HTTP client for the local Camoufox browser service (default http://localhost:9377).

The service manages browser tabs backed by the Camoufox engine.  This module
is a thin, fail-closed wrapper: every call validates the service is reachable
and surfaces structured errors rather than raising, so tool callers can
return error strings directly to the model.

The base URL and timeout can be overridden via the ``ALGO_BROWSER_URL`` and
``ALGO_BROWSER_TIMEOUT`` environment variables.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import URLError
from urllib.request import Request, build_opener

DEFAULT_BROWSER_URL = "http://localhost:9377"
DEFAULT_TIMEOUT = 30.0


def _base_url() -> str:
    return os.environ.get("ALGO_BROWSER_URL", DEFAULT_BROWSER_URL).rstrip("/")


def _timeout() -> float:
    try:
        return max(1.0, min(float(os.environ.get("ALGO_BROWSER_TIMEOUT", "30")), 120.0))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Issue a single HTTP request and return the parsed JSON body."""
    url = f"{_base_url()}{path}"
    if query:
        params = "&".join(f"{k}={v}" for k, v in query.items())
        url = f"{url}?{params}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = Request(url, data=data, headers=headers, method=method)
    opener = build_opener()
    try:
        resp = opener.open(req, timeout=timeout or _timeout())
        raw = resp.read()
    except URLError as exc:
        return {"error": f"browser service unreachable: {exc.reason}", "code": "unreachable"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"browser request failed: {exc}", "code": "request_error"}
    if not raw:
        return {"error": "browser service returned empty response", "code": "empty"}
    # Screenshot responses are binary PNG; only JSON is expected here.
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": "browser service returned non-JSON response", "code": "non_json"}


def health() -> dict[str, Any]:
    """Check browser service health.  Returns the parsed JSON body."""
    return _request("GET", "/health")


def is_available() -> bool:
    """Return True if the browser service is healthy and running."""
    result = health()
    return bool(result.get("ok") and result.get("running"))


# ---------------------------------------------------------------------------
# Tab lifecycle
# ---------------------------------------------------------------------------

def open_tab(url: str, user_id: str = "algo-cli", session_key: str = "algo-cli") -> dict[str, Any]:
    """Create a new browser tab at *url*.  Returns ``{"tabId": ..., "url": ...}``."""
    if not url or not url.strip():
        return {"error": "url is required", "code": "invalid_url"}
    return _request(
        "POST",
        "/tabs",
        body={"url": url.strip(), "userId": user_id, "sessionKey": session_key},
    )


def list_tabs(user_id: str = "algo-cli") -> dict[str, Any]:
    """List active browser tabs."""
    return _request("GET", "/tabs", query={"userId": user_id})


def close_tab(tab_id: str, user_id: str = "algo-cli") -> dict[str, Any]:
    """Close a browser tab by its ID."""
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    return _request("DELETE", f"/tabs/{tab_id}", query={"userId": user_id})


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

def snapshot(tab_id: str, user_id: str = "algo-cli") -> dict[str, Any]:
    """Get an accessibility-tree snapshot of a tab."""
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    return _request("GET", f"/tabs/{tab_id}/snapshot", query={"userId": user_id})


def screenshot_url(tab_id: str, user_id: str = "algo-cli") -> str:
    """Return the screenshot URL for a tab (for vision_describe or external fetch)."""
    return f"{_base_url()}/tabs/{tab_id}/screenshot?userId={user_id}"


def screenshot(tab_id: str, user_id: str = "algo-cli") -> dict[str, Any]:
    """Fetch a screenshot as binary PNG data.

    Returns ``{"ok": True, "data": bytes, "size": int}`` or
    ``{"error": ..., "code": ...}``.
    """
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    url = screenshot_url(tab_id, user_id)
    req = Request(url, method="GET")
    opener = build_opener()
    try:
        resp = opener.open(req, timeout=_timeout())
        data = resp.read()
    except URLError as exc:
        return {"error": f"browser service unreachable: {exc.reason}", "code": "unreachable"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"browser request failed: {exc}", "code": "request_error"}
    if not data:
        return {"error": "browser service returned empty screenshot", "code": "empty"}
    return {"ok": True, "data": data, "size": len(data)}


# ---------------------------------------------------------------------------
# Navigation and interaction
# ---------------------------------------------------------------------------

def navigate(tab_id: str, url: str, user_id: str = "algo-cli") -> dict[str, Any]:
    """Navigate an existing tab to a new URL."""
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    if not url or not url.strip():
        return {"error": "url is required", "code": "invalid_url"}
    return _request(
        "POST",
        f"/tabs/{tab_id}/navigate",
        body={"userId": user_id, "url": url.strip()},
    )


def click(tab_id: str, ref: str | None = None, selector: str | None = None, user_id: str = "algo-cli") -> dict[str, Any]:
    """Click an element by accessibility ref (e.g. ``e1``) or CSS selector."""
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    if not ref and not selector:
        return {"error": "ref or selector is required for click", "code": "invalid_target"}
    body: dict[str, Any] = {"userId": user_id}
    if ref:
        body["ref"] = ref
    if selector:
        body["selector"] = selector
    return _request("POST", f"/tabs/{tab_id}/click", body=body)


def type_text(tab_id: str, text: str, ref: str | None = None, selector: str | None = None, user_id: str = "algo-cli") -> dict[str, Any]:
    """Type text into a fillable element identified by ref or CSS selector."""
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    if not ref and not selector:
        return {"error": "ref or selector is required for type", "code": "invalid_target"}
    body: dict[str, Any] = {"userId": user_id, "text": text}
    if ref:
        body["ref"] = ref
    if selector:
        body["selector"] = selector
    return _request("POST", f"/tabs/{tab_id}/type", body=body)


def scroll(tab_id: str, direction: str = "down", amount: int = 3, user_id: str = "algo-cli") -> dict[str, Any]:
    """Scroll a tab.  Direction is ``up``, ``down``, ``left``, or ``right``."""
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    if direction not in ("up", "down", "left", "right"):
        return {"error": "direction must be up, down, left, or right", "code": "invalid_direction"}
    return _request(
        "POST",
        f"/tabs/{tab_id}/scroll",
        body={"userId": user_id, "direction": direction, "amount": max(1, min(int(amount), 20))},
    )


def evaluate(tab_id: str, expression: str, user_id: str = "algo-cli") -> dict[str, Any]:
    """Evaluate a JavaScript expression in a tab and return the result."""
    if not tab_id:
        return {"error": "tab_id is required", "code": "invalid_tab_id"}
    if not expression or not expression.strip():
        return {"error": "expression is required", "code": "invalid_expression"}
    return _request(
        "POST",
        f"/tabs/{tab_id}/evaluate",
        body={"userId": user_id, "expression": expression},
    )