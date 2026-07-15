"""X account integration through the official xurl CLI.

This module is intentionally separate from xai_auth/xai_client. xAI API keys
are for Grok on api.x.ai; account actions use X API OAuth through xurl on
api.x.com.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


FORBIDDEN_XURL_FLAGS = {
    "--bearer-token",
    "--consumer-key",
    "--consumer-secret",
    "--access-token",
    "--token-secret",
    "--client-id",
    "--client-secret",
}
CONFIRMED_POST_ACTIONS = {
    "delete",
    "like",
    "unlike",
    "repost",
    "unrepost",
    "bookmark",
    "unbookmark",
}
POST_ID_RE = re.compile(r"(?:status|statuses)/(\d{1,20})(?:\D|$)|^(\d{1,20})$")


@dataclass(frozen=True)
class XAccountResult:
    ok: bool
    action: str
    message: str
    data: dict[str, Any] | None = None

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "message": self.message,
        }
        if self.data:
            payload["data"] = self.data
        return json.dumps(payload, indent=2)


def xurl_path() -> str | None:
    """Return the xurl executable path when it is available."""
    return shutil.which("xurl")


def normalize_post_id(value: str) -> str:
    """Extract an X post id from a numeric id or x.com status URL."""
    raw = (value or "").strip()
    match = POST_ID_RE.search(raw)
    if not match:
        raise ValueError("expected a post id or x.com status URL")
    return match.group(1) or match.group(2) or ""


def compose_post_url(text: str) -> str:
    return "https://x.com/compose/post?text=" + quote(text or "", safe="")


def reply_url(post: str) -> str:
    post_id = normalize_post_id(post)
    return f"https://x.com/intent/tweet?in_reply_to={post_id}"


def _validate_xurl_args(args: list[str]) -> None:
    for arg in args:
        if arg.split("=", 1)[0] in FORBIDDEN_XURL_FLAGS:
            raise ValueError(f"refusing inline credential flag: {arg.split('=', 1)[0]}")
        if arg in {"-v", "--verbose"}:
            raise ValueError("refusing verbose xurl mode because it can expose auth headers")


def _run_xurl(args: list[str], *, timeout: int = 30) -> XAccountResult:
    _validate_xurl_args(args)
    exe = xurl_path()
    if not exe:
        return XAccountResult(
            False,
            "xurl",
            "xurl is not installed or not on PATH. Install xdevplatform/xurl and run `xurl auth status`.",
        )
    try:
        proc = subprocess.run(
            [exe, *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return XAccountResult(False, "xurl", f"xurl timed out after {timeout}s.")
    except Exception as exc:
        return XAccountResult(False, "xurl", f"could not run xurl: {exc}")

    output = (proc.stdout or proc.stderr or "").strip()
    if len(output) > 8000:
        output = output[:8000] + "\n...[truncated]"
    return XAccountResult(
        proc.returncode == 0,
        "xurl",
        output or f"xurl exited with code {proc.returncode}.",
        {"exit_code": proc.returncode},
    )


def status() -> XAccountResult:
    """Check xurl auth state without reading token files."""
    return _run_xurl(["auth", "status"], timeout=15)


def draft_post(text: str) -> XAccountResult:
    text = (text or "").strip()
    if not text:
        return XAccountResult(False, "draft_post", "post text is empty")
    return XAccountResult(
        True,
        "draft_post",
        "Draft URL generated. Review in browser before posting.",
        {"url": compose_post_url(text), "text": text},
    )


def draft_reply(post: str, text: str) -> XAccountResult:
    text = (text or "").strip()
    if not text:
        return XAccountResult(False, "draft_reply", "reply text is empty")
    try:
        post_id = normalize_post_id(post)
    except ValueError as exc:
        return XAccountResult(False, "draft_reply", str(exc))
    return XAccountResult(
        True,
        "draft_reply",
        "Reply draft URL generated. Review in browser before posting.",
        {"url": reply_url(post_id), "text": text, "post_id": post_id},
    )


def post(text: str, *, confirm: bool = False) -> XAccountResult:
    text = (text or "").strip()
    if not text:
        return XAccountResult(False, "post", "post text is empty")
    if not confirm:
        draft = draft_post(text)
        return XAccountResult(
            False,
            "post",
            "Blocked write: pass confirm=True only after the user explicitly approves this exact post.",
            draft.data,
        )
    return _run_xurl(["post", text], timeout=45)


def reply(post_id_or_url: str, text: str, *, confirm: bool = False) -> XAccountResult:
    text = (text or "").strip()
    if not text:
        return XAccountResult(False, "reply", "reply text is empty")
    try:
        post_id = normalize_post_id(post_id_or_url)
    except ValueError as exc:
        return XAccountResult(False, "reply", str(exc))
    if not confirm:
        draft = draft_reply(post_id, text)
        return XAccountResult(
            False,
            "reply",
            "Blocked write: pass confirm=True only after the user explicitly approves this exact reply.",
            draft.data,
        )
    return _run_xurl(["reply", post_id, text], timeout=45)


def post_action(action: str, post_id_or_url: str, *, confirm: bool = False) -> XAccountResult:
    action = (action or "").strip().lower()
    if action not in CONFIRMED_POST_ACTIONS:
        return XAccountResult(
            False,
            "post_action",
            f"unsupported action: {action or '(empty)'}",
            {"supported_actions": sorted(CONFIRMED_POST_ACTIONS)},
        )
    try:
        post_id = normalize_post_id(post_id_or_url)
    except ValueError as exc:
        return XAccountResult(False, action, str(exc))
    if not confirm:
        return XAccountResult(
            False,
            action,
            f"Blocked write: pass confirm=True only after the user explicitly approves {action} for this post.",
            {"post_id": post_id},
        )
    return _run_xurl([action, post_id], timeout=45)
