"""Schema-driven recursive privacy projections for action arguments."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import json
from pathlib import Path
import re
import threading
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .grace_key_store import get_key_material


MAX_PRIVACY_DEPTH = 16
MAX_PRIVACY_ITEMS = 512
PRIVACY_KEY_LABEL = "irene-privacy-hmac-v1"
_SAFE_ACTION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|credential|"
    r"authorization|cookie|session[_-]?key|private[_-]?key|client[_-]?secret|otp|pin)(?:$|[_-])",
    re.IGNORECASE,
)
_URL_KEYS = frozenset({"url", "uri", "origin", "href", "endpoint", "redirect_uri"})
_SELECTOR_KEYS = frozenset({"selector", "xpath", "element", "element_id", "dom", "ax_node"})
_PATH_KEYS = frozenset({"path", "cwd", "file", "filename", "directory", "upload", "download"})
_CONTENT_KEYS = frozenset(
    {
        "content",
        "text",
        "value",
        "query",
        "command",
        "prompt",
        "payload",
        "body",
        "html",
        "screenshot",
        "data",
    }
)
_COMPACT_SECRET_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "refreshtoken",
        "password",
        "passwd",
        "secret",
        "credential",
        "authorization",
        "cookie",
        "sessionkey",
        "privatekey",
        "clientsecret",
        "otp",
        "pin",
    }
)


class PrivacyProjectionError(RuntimeError):
    """Raised when an argument graph cannot be projected safely."""


class PrivacyView(str, Enum):
    CONFIRMATION = "confirmation"
    MODEL = "model"
    AUDIT = "audit"
    TELEMETRY = "telemetry"


class DataClass(str, Enum):
    STRUCTURAL = "structural"
    IDENTIFIER = "identifier"
    TARGET = "target"
    PATH = "path"
    URL = "url"
    SELECTOR = "selector"
    CONTENT = "content"
    SECRET = "secret"
    BINARY = "binary"
    UNKNOWN = "unknown"


class ConfirmationDisclosure(str, Enum):
    EXACT = "exact"
    SAFE_TARGET = "safe_target"
    SUMMARY = "summary"
    REDACTED = "redacted"


@dataclass(frozen=True)
class FieldRule:
    path: tuple[str, ...]
    data_class: DataClass
    confirmation: ConfirmationDisclosure


_ACTION_RULES: dict[str, tuple[FieldRule, ...]] = {
    "credential_helpers_store": (
        FieldRule(("value",), DataClass.SECRET, ConfirmationDisclosure.REDACTED),
    ),
    "run_shell": (
        FieldRule(("command",), DataClass.CONTENT, ConfirmationDisclosure.EXACT),
    ),
    "session_command": (
        FieldRule(("command",), DataClass.CONTENT, ConfirmationDisclosure.EXACT),
    ),
    "session_slash": (
        FieldRule(("command",), DataClass.CONTENT, ConfirmationDisclosure.EXACT),
    ),
    "web_fetch": (
        FieldRule(("url",), DataClass.URL, ConfirmationDisclosure.SAFE_TARGET),
    ),
    "web_search": (
        FieldRule(("query",), DataClass.CONTENT, ConfirmationDisclosure.EXACT),
    ),
    "write_file": (
        FieldRule(("path",), DataClass.PATH, ConfirmationDisclosure.SAFE_TARGET),
        FieldRule(("content",), DataClass.CONTENT, ConfirmationDisclosure.SUMMARY),
    ),
    "edit_file": (
        FieldRule(("path",), DataClass.PATH, ConfirmationDisclosure.SAFE_TARGET),
        FieldRule(("old_string",), DataClass.CONTENT, ConfirmationDisclosure.SUMMARY),
        FieldRule(("new_string",), DataClass.CONTENT, ConfirmationDisclosure.SUMMARY),
    ),
    "batch_edit": (
        FieldRule(("path",), DataClass.PATH, ConfirmationDisclosure.SAFE_TARGET),
        FieldRule(("edits", "*", "old_string"), DataClass.CONTENT, ConfirmationDisclosure.SUMMARY),
        FieldRule(("edits", "*", "new_string"), DataClass.CONTENT, ConfirmationDisclosure.SUMMARY),
    ),
    "x_account_post": (
        FieldRule(("text",), DataClass.CONTENT, ConfirmationDisclosure.EXACT),
    ),
    "x_account_reply": (
        FieldRule(("post",), DataClass.URL, ConfirmationDisclosure.SAFE_TARGET),
        FieldRule(("text",), DataClass.CONTENT, ConfirmationDisclosure.EXACT),
    ),
    "x_account_post_action": (
        FieldRule(("post",), DataClass.URL, ConfirmationDisclosure.SAFE_TARGET),
    ),
}

_PRIVACY_KEY_LOCK = threading.Lock()
_PRIVACY_KEY: bytes | None = None


def _privacy_hmac_key() -> bytes:
    global _PRIVACY_KEY
    with _PRIVACY_KEY_LOCK:
        if _PRIVACY_KEY is None:
            _PRIVACY_KEY = get_key_material(
                PRIVACY_KEY_LABEL,
                length=32,
                require_persistent=False,
            ).key
        return _PRIVACY_KEY


def _matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    return len(pattern) == len(path) and all(
        expected == "*" or expected == actual
        for expected, actual in zip(pattern, path)
    )


def _rule_for(action: str, path: tuple[str, ...], value: Any) -> FieldRule:
    for rule in _ACTION_RULES.get(action, ()):
        if _matches(rule.path, path):
            return rule
    key = path[-1].casefold() if path else ""
    compact_key = re.sub(r"[^a-z0-9]", "", key)
    if _SECRET_KEY_RE.search(key) or compact_key in _COMPACT_SECRET_KEYS:
        return FieldRule(path, DataClass.SECRET, ConfirmationDisclosure.REDACTED)
    if key in _URL_KEYS or key.endswith("_url") or key.endswith("_uri"):
        return FieldRule(path, DataClass.URL, ConfirmationDisclosure.SAFE_TARGET)
    if key in _SELECTOR_KEYS or key.endswith("_selector"):
        return FieldRule(path, DataClass.SELECTOR, ConfirmationDisclosure.SUMMARY)
    if key in _PATH_KEYS or key.endswith("_path") or key.endswith("_file"):
        return FieldRule(path, DataClass.PATH, ConfirmationDisclosure.SAFE_TARGET)
    if key in _CONTENT_KEYS or key.endswith("_content") or key.endswith("_text"):
        return FieldRule(path, DataClass.CONTENT, ConfirmationDisclosure.SUMMARY)
    if value is None or isinstance(value, (bool, int, float)):
        return FieldRule(path, DataClass.STRUCTURAL, ConfirmationDisclosure.EXACT)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return FieldRule(path, DataClass.BINARY, ConfirmationDisclosure.SUMMARY)
    if isinstance(value, str) and len(value) <= 128:
        return FieldRule(path, DataClass.IDENTIFIER, ConfirmationDisclosure.EXACT)
    return FieldRule(path, DataClass.UNKNOWN, ConfirmationDisclosure.SUMMARY)


def _safe_url(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if not parsed.scheme or not hostname:
        return "<invalid-url>"
    host = f"{hostname}:{port}" if port is not None else hostname
    query = "<redacted>" if parsed.query else ""
    fragment = "<redacted>" if parsed.fragment else ""
    return urlunsplit((parsed.scheme.casefold(), host, parsed.path or "/", query, fragment))


def _safe_path(value: Any) -> str:
    text = str(value or "")
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~/" + text[len(home) + 1 :]
    return text


def _canonical_value(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> Any:
    if depth > MAX_PRIVACY_DEPTH:
        raise PrivacyProjectionError("argument graph exceeds the privacy depth limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"binary_sha256": hashlib.sha256(bytes(value)).hexdigest(), "bytes": len(value)}
    active = seen if seen is not None else set()
    identity = id(value)
    if identity in active:
        raise PrivacyProjectionError("argument graph contains a cycle")
    if isinstance(value, Mapping):
        if len(value) > MAX_PRIVACY_ITEMS:
            raise PrivacyProjectionError("argument mapping exceeds the privacy item limit")
        active.add(identity)
        try:
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise PrivacyProjectionError("argument mapping keys must be strings")
                normalized[key] = _canonical_value(item, depth=depth + 1, seen=active)
            return dict(sorted(normalized.items()))
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_PRIVACY_ITEMS:
            raise PrivacyProjectionError("argument collection exceeds the privacy item limit")
        active.add(identity)
        try:
            return [_canonical_value(item, depth=depth + 1, seen=active) for item in value]
        finally:
            active.remove(identity)
    return {"unsupported_type": type(value).__name__}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _summary(value: Any, data_class: DataClass) -> dict[str, Any]:
    if isinstance(value, str):
        payload = value.encode("utf-8", errors="replace")
        units = {"chars": len(value), "bytes": len(payload)}
    elif isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        units = {"bytes": len(payload)}
    else:
        payload = _canonical_bytes(value)
        units = {"items": len(value) if isinstance(value, (list, tuple, dict)) else 1}
    return {
        "class": data_class.value,
        **units,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "redacted": True,
    }


def _keyed_descriptor(value: Any, data_class: DataClass, *, key: bytes) -> dict[str, Any]:
    payload = _canonical_bytes(value)
    return {
        "class": data_class.value,
        "hmac_sha256": hmac.new(key, payload, hashlib.sha256).hexdigest(),
        "bytes": len(payload),
        "redacted": True,
    }


@dataclass
class _ProjectionState:
    action: str
    view: PrivacyView
    hmac_key: bytes
    seen: set[int]
    item_count: int = 0

    def audit_key(self, path: tuple[str, ...], key: str) -> str:
        payload = ".".join((*path, key)).encode("utf-8")
        digest = hmac.new(self.hmac_key, payload, hashlib.sha256).hexdigest()
        return f"field_{digest[:24]}"

    def visit(self, value: Any, path: tuple[str, ...], depth: int) -> Any:
        if depth > MAX_PRIVACY_DEPTH:
            raise PrivacyProjectionError("argument graph exceeds the privacy depth limit")
        self.item_count += 1
        if self.item_count > MAX_PRIVACY_ITEMS:
            raise PrivacyProjectionError("argument graph exceeds the privacy item limit")
        rule = _rule_for(self.action, path, value)
        if rule.data_class is DataClass.SECRET:
            if self.view is PrivacyView.AUDIT:
                return _keyed_descriptor(value, rule.data_class, key=self.hmac_key)
            return "<redacted>"
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in self.seen:
                raise PrivacyProjectionError("argument graph contains a cycle")
            self.seen.add(identity)
            try:
                projected: dict[str, Any] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise PrivacyProjectionError("argument mapping keys must be strings")
                    projected_key = (
                        self.audit_key(path, key)
                        if self.view is PrivacyView.AUDIT
                        else key
                    )
                    if projected_key in projected:
                        raise PrivacyProjectionError("audit field identity collision")
                    projected[projected_key] = self.visit(item, (*path, key), depth + 1)
                return projected
            finally:
                self.seen.remove(identity)
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in self.seen:
                raise PrivacyProjectionError("argument graph contains a cycle")
            self.seen.add(identity)
            try:
                return [self.visit(item, (*path, "*"), depth + 1) for item in value]
            finally:
                self.seen.remove(identity)
        if self.view is PrivacyView.AUDIT and rule.data_class is not DataClass.STRUCTURAL:
            return _keyed_descriptor(value, rule.data_class, key=self.hmac_key)
        if self.view is PrivacyView.CONFIRMATION:
            if rule.confirmation is ConfirmationDisclosure.REDACTED:
                return "<redacted>"
            if rule.confirmation is ConfirmationDisclosure.SUMMARY:
                return _summary(value, rule.data_class)
            if rule.confirmation is ConfirmationDisclosure.SAFE_TARGET:
                if rule.data_class is DataClass.URL:
                    return _safe_url(value)
                if rule.data_class is DataClass.PATH:
                    return _safe_path(value)
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return _summary(value, DataClass.BINARY)
        return {"class": DataClass.UNKNOWN.value, "type": type(value).__name__, "redacted": True}


def _telemetry_projection(action: str, args: Mapping[str, Any]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    seen: set[int] = set()
    items = 0

    def walk(value: Any, path: tuple[str, ...], depth: int) -> None:
        nonlocal items
        if depth > MAX_PRIVACY_DEPTH:
            raise PrivacyProjectionError("argument graph exceeds the privacy depth limit")
        items += 1
        if items > MAX_PRIVACY_ITEMS:
            raise PrivacyProjectionError("argument graph exceeds the privacy item limit")
        counts[_rule_for(action, path, value).data_class.value] += 1
        if isinstance(value, Mapping) or isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen:
                raise PrivacyProjectionError("argument graph contains a cycle")
            seen.add(identity)
            try:
                iterable = value.items() if isinstance(value, Mapping) else enumerate(value)
                for key, child in iterable:
                    if isinstance(value, Mapping) and not isinstance(key, str):
                        raise PrivacyProjectionError("argument mapping keys must be strings")
                    child_key = key if isinstance(value, Mapping) else "*"
                    walk(child, (*path, str(child_key)), depth + 1)
            finally:
                seen.remove(identity)

    walk(args, (), 0)
    return {
        "field_count": max(0, items - 1),
        "max_depth": MAX_PRIVACY_DEPTH,
        "classes": dict(sorted(counts.items())),
    }


def project_action_args(
    action: str,
    args: Mapping[str, Any],
    view: PrivacyView,
    *,
    hmac_key: bytes | None = None,
) -> dict[str, Any]:
    """Project one argument mapping for an explicitly named privacy consumer."""

    safe_action = str(action or "").strip()
    if not _SAFE_ACTION_RE.fullmatch(safe_action):
        raise ValueError("action must be a bounded identifier")
    if not isinstance(args, Mapping):
        raise TypeError("action arguments must be a mapping")
    if view is PrivacyView.TELEMETRY:
        return _telemetry_projection(safe_action, args)
    key = bytes(hmac_key) if hmac_key is not None else _privacy_hmac_key()
    if len(key) < 32:
        raise ValueError("privacy HMAC key must contain at least 32 bytes")
    projected = _ProjectionState(safe_action, view, key, set()).visit(args, (), 0)
    if not isinstance(projected, dict):  # pragma: no cover - mapping root invariant
        raise PrivacyProjectionError("argument projection did not produce a mapping")
    return projected


def keyed_action_fingerprint(
    action: str,
    args: Mapping[str, Any],
    *,
    hmac_key: bytes | None = None,
) -> str:
    """Return a stable keyed identity without persisting argument plaintext."""

    safe_action = str(action or "").strip()
    if not _SAFE_ACTION_RE.fullmatch(safe_action):
        raise ValueError("action must be a bounded identifier")
    key = bytes(hmac_key) if hmac_key is not None else _privacy_hmac_key()
    if len(key) < 32:
        raise ValueError("privacy HMAC key must contain at least 32 bytes")
    payload = safe_action.encode("utf-8") + b"\0" + _canonical_bytes(args)
    return f"hmac-sha256:{hmac.new(key, payload, hashlib.sha256).hexdigest()}"


__all__ = [
    "ConfirmationDisclosure",
    "DataClass",
    "FieldRule",
    "PrivacyProjectionError",
    "PrivacyView",
    "PRIVACY_KEY_LABEL",
    "keyed_action_fingerprint",
    "project_action_args",
]
