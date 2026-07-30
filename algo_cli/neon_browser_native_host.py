"""Bounded native-messaging protocol for the observe-only Neon extension.

The host is not installed or registered by M5 and is absent from the normal
action registry.  It accepts only a hello followed by a structural top-frame
observation; it exposes no mutation, JavaScript, selector, URL-navigation, CDP,
filesystem, shell, or credential operation.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import struct
from typing import Any, BinaryIO, Callable, Iterable, Mapping, NoReturn
import uuid

from .xenon_browser_egress import (
    Resolver,
    XenonEgressPolicy,
    XenonEgressRejected,
    resolve_public_url,
)


NEON_NATIVE_SCHEMA_VERSION = 1
NEON_NATIVE_PROTOCOL_VERSION = 1
NEON_NATIVE_VERSION = "0.0.0"
NEON_MAX_FRAME_BYTES = 65_536
NEON_MAX_BUFFER_BYTES = 4 * NEON_MAX_FRAME_BYTES
NEON_MAX_JSON_DEPTH = 6
NEON_MAX_JSON_ITEMS = 96
NEON_MAX_STRING_BYTES = 4096

_HEADER = struct.Struct("@I")
_EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SEMVER_RE = re.compile(r"^(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})$")
_CONTENT_TYPE_RE = re.compile(r"^(?:unknown|[a-z0-9][a-z0-9.+-]{0,62}/[a-z0-9][a-z0-9.+-]{0,62})$")
_SURFACE_KINDS = frozenset(
    {"dom", "canvas", "pdf", "internal", "auth", "passkey", "captcha", "secure_field", "unknown"}
)


class NeonNativeRejected(ValueError):
    """A native-message frame or session transition failed closed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject(reason_code: str) -> NoReturn:
    raise NeonNativeRejected(reason_code)


def _closed(row: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if type(row) is not dict or frozenset(row) != fields:
        _reject(f"{label}_schema")


def _uuid(value: Any, label: str) -> str:
    if type(value) is not str or not _UUID_RE.fullmatch(value):
        _reject(label)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        _reject(label)
    if str(parsed) != value or parsed.int == 0:
        _reject(label)
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(label)
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _reject(label)
    return value


def _semver(value: Any, label: str) -> str:
    if type(value) is not str or not _SEMVER_RE.fullmatch(value):
        _reject(label)
    return value


def _pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _reject("json_duplicate_key")
        result[key] = value
    return result


def _constant(_value: str) -> NoReturn:
    _reject("json_constant")


def _bound_tree(value: Any, *, depth: int = 0, items: list[int] | None = None) -> None:
    if items is None:
        items = [0]
    if depth > NEON_MAX_JSON_DEPTH:
        _reject("json_depth")
    items[0] += 1
    if items[0] > NEON_MAX_JSON_ITEMS:
        _reject("json_items")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        _reject("json_float")
    if type(value) is str:
        if len(value.encode("utf-8")) > NEON_MAX_STRING_BYTES:
            _reject("json_string")
        return
    if type(value) is list:
        for item in value:
            _bound_tree(item, depth=depth + 1, items=items)
        return
    if type(value) is dict:
        for key, item in value.items():
            _bound_tree(key, depth=depth + 1, items=items)
            _bound_tree(item, depth=depth + 1, items=items)
        return
    _reject("json_type")


def decode_neon_message(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > NEON_MAX_FRAME_BYTES:
        _reject("frame_size")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except UnicodeDecodeError:
        _reject("frame_utf8")
    except json.JSONDecodeError:
        _reject("frame_json")
    if type(value) is not dict:
        _reject("message_object")
    _bound_tree(value)
    return value


def encode_neon_message(message: Mapping[str, Any]) -> bytes:
    if type(message) is not dict:
        _reject("message_object")
    _bound_tree(message)
    try:
        payload = json.dumps(
            message,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        _reject("frame_json")
    if not payload or len(payload) > NEON_MAX_FRAME_BYTES:
        _reject("frame_size")
    return _HEADER.pack(len(payload)) + payload


class NeonNativeFrameDecoder:
    """Incremental decoder bounded below Chrome's documented 1 MiB input."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if type(chunk) is not bytes:
            _reject("feed_type")
        if len(self._buffer) + len(chunk) > NEON_MAX_BUFFER_BYTES:
            _reject("feed_size")
        self._buffer.extend(chunk)
        messages: list[dict[str, Any]] = []
        while len(self._buffer) >= _HEADER.size:
            size = _HEADER.unpack(self._buffer[: _HEADER.size])[0]
            if size == 0 or size > NEON_MAX_FRAME_BYTES:
                _reject("frame_size")
            frame_end = _HEADER.size + size
            if len(self._buffer) < frame_end:
                break
            payload = bytes(self._buffer[_HEADER.size : frame_end])
            del self._buffer[:frame_end]
            messages.append(decode_neon_message(payload))
        return messages

    def finish(self) -> None:
        if self._buffer:
            _reject("frame_truncated")


class NeonHostSession:
    """One native host process session bound to one exact extension origin."""

    def __init__(
        self,
        *,
        actual_extension_origin: str,
        allowed_extension_origin: str,
        authority_key: bytes,
        resolver: Resolver,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        if (
            type(actual_extension_origin) is not str
            or type(allowed_extension_origin) is not str
            or not _EXTENSION_ORIGIN_RE.fullmatch(actual_extension_origin)
            or actual_extension_origin != allowed_extension_origin
        ):
            _reject("extension_origin")
        if type(authority_key) is not bytes or len(authority_key) < 32:
            _reject("authority_key")
        if not callable(resolver) or not callable(uuid_factory):
            _reject("session_dependency")
        self._key = authority_key
        self._resolver = resolver
        self._uuid_factory = uuid_factory
        self._hello: dict[str, Any] | None = None
        self._session_id: str | None = None
        self._observed = False

    def _new_uuid(self) -> str:
        value = self._uuid_factory()
        if type(value) is not uuid.UUID or value.int == 0:
            _reject("uuid_factory")
        return str(value)

    def _opaque(self, label: str) -> str:
        digest = hmac.new(self._key, label.encode("utf-8"), hashlib.sha256).hexdigest()
        return "hmac-sha256:" + digest

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any]:
        if type(message) is not dict:
            _reject("message_object")
        message_type = message.get("type")
        if message_type == "neon.hello":
            return self._hello_message(message)
        if message_type == "neon.observe":
            return self._observe_message(message)
        _reject("message_type")

    def _hello_message(self, row: Mapping[str, Any]) -> dict[str, Any]:
        _closed(
            row,
            frozenset(
                {
                    "schema_version",
                    "protocol_version",
                    "type",
                    "request_id",
                    "extension_version",
                    "worker_generation",
                    "user_gesture_id",
                    "window_id",
                    "tab_id",
                    "incognito",
                }
            ),
            "hello",
        )
        if self._hello is not None:
            _reject("hello_replayed")
        if row["type"] != "neon.hello":
            _reject("message_type")
        if _integer(row["schema_version"], "schema_version", 1, 1) != NEON_NATIVE_SCHEMA_VERSION:
            _reject("schema_version")
        if _integer(row["protocol_version"], "protocol_version", 1, 1) != NEON_NATIVE_PROTOCOL_VERSION:
            _reject("protocol_version")
        if _semver(row["extension_version"], "extension_version") != NEON_NATIVE_VERSION:
            _reject("extension_version_skew")
        if _boolean(row["incognito"], "incognito"):
            _reject("incognito_denied")
        hello = {
            "request_id": _uuid(row["request_id"], "request_id"),
            "extension_version": row["extension_version"],
            "worker_generation": _uuid(row["worker_generation"], "worker_generation"),
            "user_gesture_id": _uuid(row["user_gesture_id"], "user_gesture_id"),
            "window_id": _integer(row["window_id"], "window_id", 0, (1 << 31) - 1),
            "tab_id": _integer(row["tab_id"], "tab_id", 0, (1 << 31) - 1),
        }
        self._hello = hello
        self._session_id = self._new_uuid()
        return {
            "schema_version": NEON_NATIVE_SCHEMA_VERSION,
            "protocol_version": NEON_NATIVE_PROTOCOL_VERSION,
            "type": "neon.hello_ack",
            "request_id": hello["request_id"],
            "session_id": self._session_id,
            "native_version": NEON_NATIVE_VERSION,
        }

    def _observe_message(self, row: Mapping[str, Any]) -> dict[str, Any]:
        _closed(
            row,
            frozenset(
                {
                    "schema_version",
                    "protocol_version",
                    "type",
                    "request_id",
                    "session_id",
                    "extension_version",
                    "worker_generation",
                    "user_gesture_id",
                    "window_id",
                    "tab_id",
                    "frame_id",
                    "document_id",
                    "origin",
                    "surface_kind",
                    "content_type",
                    "secure_field_count",
                    "upload_control_count",
                    "canvas_count",
                    "frame_count",
                    "shadow_host_count",
                    "incognito",
                }
            ),
            "observe",
        )
        if self._hello is None or self._session_id is None:
            _reject("hello_required")
        if self._observed:
            _reject("observe_replayed")
        if row["type"] != "neon.observe":
            _reject("message_type")
        _integer(row["schema_version"], "schema_version", 1, 1)
        _integer(row["protocol_version"], "protocol_version", 1, 1)
        request_id = _uuid(row["request_id"], "request_id")
        if request_id == self._hello["request_id"]:
            _reject("request_replayed")
        if _uuid(row["session_id"], "session_id") != self._session_id:
            _reject("session_changed")
        if _semver(row["extension_version"], "extension_version") != self._hello["extension_version"]:
            _reject("extension_version_skew")
        for label in ("worker_generation", "user_gesture_id"):
            if _uuid(row[label], label) != self._hello[label]:
                _reject(f"{label}_changed")
        for label in ("window_id", "tab_id"):
            if _integer(row[label], label, 0, (1 << 31) - 1) != self._hello[label]:
                _reject(f"{label}_changed")
        if _integer(row["frame_id"], "frame_id", 0, 0) != 0:
            _reject("top_frame_only")
        document_id = _uuid(row["document_id"], "document_id")
        if _boolean(row["incognito"], "incognito"):
            _reject("incognito_denied")
        surface_kind = row["surface_kind"]
        if type(surface_kind) is not str or surface_kind not in _SURFACE_KINDS:
            _reject("surface_kind")
        content_type = row["content_type"]
        if type(content_type) is not str or not _CONTENT_TYPE_RE.fullmatch(content_type):
            _reject("content_type")
        for label in (
            "secure_field_count",
            "upload_control_count",
            "canvas_count",
            "frame_count",
            "shadow_host_count",
        ):
            _integer(row[label], label, 0, 10_000)

        origin = row["origin"]
        if type(origin) is not str:
            _reject("origin")
        try:
            target = resolve_public_url(
                origin,
                XenonEgressPolicy(
                    allowed_schemes=("http", "https"),
                    allowed_ports=(80, 443),
                ),
                resolver=self._resolver,
            )
        except XenonEgressRejected as exc:
            _reject(exc.reason_code)
        origin_digest = self._opaque("origin\0" + target.origin)
        binding_id = self._new_uuid()
        snapshot_id = self._new_uuid()
        profile_id = self._opaque("selected-profile\0" + self._session_id)
        self._observed = True
        # Raw origin and page structure deliberately leave scope here.  The
        # response contains only opaque identities and bounded classifications.
        return {
            "schema_version": NEON_NATIVE_SCHEMA_VERSION,
            "protocol_version": NEON_NATIVE_PROTOCOL_VERSION,
            "type": "neon.observe_ack",
            "request_id": request_id,
            "mode": "observe_only",
            "binding_id": binding_id,
            "profile_id": profile_id,
            "origin_digest": origin_digest,
            "snapshot_id": snapshot_id,
            "fencing_token": 1,
            "document_digest": self._opaque("document\0" + document_id),
            "surface_kind": surface_kind,
        }


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            if not chunks:
                return b""
            _reject("frame_truncated")
        chunks.extend(chunk)
    return bytes(chunks)


def run_neon_native_host(
    session: NeonHostSession,
    *,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
) -> int:
    """Run a native-messaging stdio loop; protocol failure terminates closed."""

    if type(session) is not NeonHostSession or not isinstance(input_stream, io.BufferedIOBase):
        _reject("host_stream")
    if not isinstance(output_stream, io.BufferedIOBase):
        _reject("host_stream")
    while True:
        header = _read_exact(input_stream, _HEADER.size)
        if not header:
            return 0
        size = _HEADER.unpack(header)[0]
        if size == 0 or size > NEON_MAX_FRAME_BYTES:
            _reject("frame_size")
        payload = _read_exact(input_stream, size)
        if len(payload) != size:
            _reject("frame_truncated")
        response = session.handle(decode_neon_message(payload))
        output_stream.write(encode_neon_message(response))
        output_stream.flush()


__all__ = [
    "NEON_MAX_BUFFER_BYTES",
    "NEON_MAX_FRAME_BYTES",
    "NEON_NATIVE_PROTOCOL_VERSION",
    "NEON_NATIVE_SCHEMA_VERSION",
    "NEON_NATIVE_VERSION",
    "NeonHostSession",
    "NeonNativeFrameDecoder",
    "NeonNativeRejected",
    "decode_neon_message",
    "encode_neon_message",
    "run_neon_native_host",
]
