"""Provider-neutral finite control protocol and authority kernel.

This module is a disabled hardening foundation.  It is intentionally absent
from the normal action registry and accepts no generic program or executable
language.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import unicodedata
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, NoReturn, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


CONTROL_SCHEMA_VERSION = 1
CONTROL_PROTOCOL_VERSION = 1
CONTROL_MESSAGE_TYPE = "control.execute"
CONTROL_PREPARATION_MESSAGE_TYPE = "control.prepare"
MAX_FRAME_BYTES = 65_536
MAX_FEED_BYTES = 4 * MAX_FRAME_BYTES
MAX_JSON_DEPTH = 12
MAX_JSON_ITEMS = 512
MAX_STRING_BYTES = 8_192
MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_DEADLINE_HORIZON_MS = 300_000
MAX_GRANT_LIFETIME_MS = 86_400_000
MAX_ACTION_COUNT = 64
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 1_048_576
MAX_VIEWPORT_DIMENSION = 16_384
MAX_SCROLL_DELTA = 10_000
MAX_TEXT_BYTES = 4_096
MAX_PREPARATION_LIFETIME_MS = 60_000

_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPAQUE_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")


class ControlKernelError(RuntimeError):
    """Base class for content-free control-kernel failures."""


class FrameRejected(ControlKernelError):
    """A framed payload is malformed or outside protocol bounds."""


class SchemaRejected(ControlKernelError):
    """A decoded object violates the closed protocol schema."""


class AuthorityRejected(ControlKernelError):
    """A grant or permit is not authenticated by the configured authority."""


class PermitRejected(ControlKernelError):
    """A content-free policy/permit rejection with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        if not _SAFE_ID_RE.fullmatch(str(reason_code or "")):
            reason_code = "invalid_rejection"
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)


class TargetKind(str, Enum):
    BROWSER_DOCUMENT = "browser_document"
    DESKTOP_SURFACE = "desktop_surface"
    EXTERNAL_RESOURCE = "external_resource"


class Operation(str, Enum):
    OBSERVE = "observe"
    ACTIVATE = "activate"
    INPUT_TEXT = "input_text"
    SELECT_OPTION = "select_option"
    SCROLL = "scroll"
    UPLOAD = "upload"
    COORDINATE_ACTIVATE = "coordinate_activate"
    HANDOFF = "handoff"


class Effect(str, Enum):
    READ = "read"
    UI_MUTATION = "ui_mutation"
    INPUT = "input"
    TRANSMIT = "transmit"
    HANDOFF = "handoff"


class ControlDataClass(str, Enum):
    STRUCTURAL = "structural"
    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"
    FILE = "file"


class ControlRoute(str, Enum):
    CONNECTOR = "connector"
    SHORTCUT = "shortcut"
    APPLE_EVENT = "apple_event"
    DOM = "dom"
    AX = "ax"
    SCREENSHOT = "screenshot"
    COORDINATE = "coordinate"
    HANDOFF = "handoff"


ROUTE_ORDER: tuple[ControlRoute, ...] = (
    ControlRoute.CONNECTOR,
    ControlRoute.SHORTCUT,
    ControlRoute.APPLE_EVENT,
    ControlRoute.DOM,
    ControlRoute.AX,
    ControlRoute.SCREENSHOT,
    ControlRoute.COORDINATE,
    ControlRoute.HANDOFF,
)
_ROUTE_INDEX = {route: index for index, route in enumerate(ROUTE_ORDER)}

_TARGET_ROUTES: dict[TargetKind, frozenset[ControlRoute]] = {
    TargetKind.BROWSER_DOCUMENT: frozenset(
        {
            ControlRoute.CONNECTOR,
            ControlRoute.DOM,
            ControlRoute.SCREENSHOT,
            ControlRoute.COORDINATE,
            ControlRoute.HANDOFF,
        }
    ),
    TargetKind.DESKTOP_SURFACE: frozenset(
        {
            ControlRoute.CONNECTOR,
            ControlRoute.SHORTCUT,
            ControlRoute.APPLE_EVENT,
            ControlRoute.AX,
            ControlRoute.SCREENSHOT,
            ControlRoute.COORDINATE,
            ControlRoute.HANDOFF,
        }
    ),
    TargetKind.EXTERNAL_RESOURCE: frozenset({ControlRoute.CONNECTOR, ControlRoute.HANDOFF}),
}


@dataclass(frozen=True, slots=True)
class OperationSpec:
    effects: tuple[Effect, ...]
    data_classes: frozenset[ControlDataClass]
    argument_fields: tuple[str, ...]


OPERATION_SPECS: dict[Operation, OperationSpec] = {
    Operation.OBSERVE: OperationSpec(
        (Effect.READ,),
        frozenset(
            {
                ControlDataClass.STRUCTURAL,
                ControlDataClass.PUBLIC,
                ControlDataClass.PRIVATE,
            }
        ),
        (),
    ),
    Operation.ACTIVATE: OperationSpec(
        (Effect.UI_MUTATION,),
        frozenset({ControlDataClass.STRUCTURAL}),
        ("element_id",),
    ),
    Operation.INPUT_TEXT: OperationSpec(
        (Effect.INPUT, Effect.UI_MUTATION),
        frozenset({ControlDataClass.PRIVATE}),
        ("element_id", "replace", "text"),
    ),
    Operation.SELECT_OPTION: OperationSpec(
        (Effect.UI_MUTATION,),
        frozenset({ControlDataClass.STRUCTURAL, ControlDataClass.PRIVATE}),
        ("element_id", "option_id"),
    ),
    Operation.SCROLL: OperationSpec(
        (Effect.UI_MUTATION,),
        frozenset({ControlDataClass.STRUCTURAL}),
        ("delta_x", "delta_y", "element_id"),
    ),
    Operation.UPLOAD: OperationSpec(
        (Effect.TRANSMIT, Effect.UI_MUTATION),
        frozenset({ControlDataClass.FILE, ControlDataClass.PRIVATE}),
        ("artifact_id", "byte_count", "element_id"),
    ),
    Operation.COORDINATE_ACTIVATE: OperationSpec(
        (Effect.UI_MUTATION,),
        frozenset({ControlDataClass.STRUCTURAL}),
        ("viewport_height", "viewport_width", "x", "y"),
    ),
    Operation.HANDOFF: OperationSpec(
        (Effect.HANDOFF,),
        frozenset(ControlDataClass),
        ("reason_code",),
    ),
}


PREPARATION_SELECTORS: dict[tuple[Operation, ControlRoute], frozenset[str]] = {
    (Operation.ACTIVATE, ControlRoute.AX): frozenset({"focused_element"}),
    (Operation.SELECT_OPTION, ControlRoute.AX): frozenset({"focused_element"}),
    (Operation.SCROLL, ControlRoute.AX): frozenset({"focused_element"}),
    (Operation.ACTIVATE, ControlRoute.APPLE_EVENT): frozenset(
        {"activate_finder", "activate_system_settings"}
    ),
    (Operation.ACTIVATE, ControlRoute.SHORTCUT): frozenset({"review_current_task"}),
    (Operation.COORDINATE_ACTIVATE, ControlRoute.COORDINATE): frozenset(
        {"frontmost_point"}
    ),
    # Picker-scoped capture must originate from a direct native-app gesture.
    # The CLI preparation protocol can authorize only the independently
    # confirmed, persistent one-frame path.
    (Operation.OBSERVE, ControlRoute.SCREENSHOT): frozenset(
        {"persistent_programmatic"}
    ),
}


def _reject(reason: str) -> NoReturn:
    raise SchemaRejected(reason)


def _exact_dict(value: Any, fields: Iterable[str], label: str) -> dict[str, Any]:
    expected = frozenset(fields)
    if type(value) is not dict or frozenset(value) != expected:
        _reject(f"{label}_schema")
    if not all(type(key) is str for key in value):
        _reject(f"{label}_schema")
    return value


def _exact_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(label)
    return value


def _safe_text(value: Any, label: str, *, maximum_bytes: int = MAX_STRING_BYTES) -> str:
    if type(value) is not str:
        _reject(label)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _reject(label)
    if not encoded or len(encoded) > maximum_bytes:
        _reject(label)
    return value


def _safe_identifier(value: Any, label: str) -> str:
    text = _safe_text(value, label, maximum_bytes=128)
    if not _SAFE_ID_RE.fullmatch(text):
        _reject(label)
    return text


def _canonical_uuid(value: Any, label: str) -> str:
    text = _safe_text(value, label, maximum_bytes=36)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError):
        _reject(label)
    if str(parsed) != text or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        _reject(label)
    return text


def _opaque_id(value: Any, label: str) -> str:
    text = _safe_text(value, label, maximum_bytes=76)
    if not _OPAQUE_ID_RE.fullmatch(text):
        _reject(label)
    return text


def _revision(value: Any, label: str) -> str:
    text = _safe_text(value, label, maximum_bytes=128)
    if not _REVISION_RE.fullmatch(text):
        _reject(label)
    return text


def _digest(value: Any, label: str) -> str:
    text = _safe_text(value, label, maximum_bytes=71)
    if not _DIGEST_RE.fullmatch(text):
        _reject(label)
    return text


def _key_id(value: Any, label: str) -> str:
    text = _safe_text(value, label, maximum_bytes=72)
    if not _KEY_ID_RE.fullmatch(text):
        _reject(label)
    return text


def _signature(value: Any, label: str) -> str:
    text = _safe_text(value, label, maximum_bytes=86)
    if not _SIGNATURE_RE.fullmatch(text):
        _reject(label)
    return text


def _enum_value(enum_type: type[Enum], value: Any, label: str) -> Any:
    if type(value) is not str:
        _reject(label)
    try:
        return enum_type(value)
    except ValueError:
        _reject(label)


def _ordered_enum_tuple(
    enum_type: type[Enum],
    values: Any,
    label: str,
    *,
    order: Mapping[Any, int] | None = None,
    maximum: int = 16,
) -> tuple[Any, ...]:
    if type(values) is not list or not 1 <= len(values) <= maximum:
        _reject(label)
    parsed = tuple(_enum_value(enum_type, value, label) for value in values)
    if len(set(parsed)) != len(parsed):
        _reject(label)
    key = (lambda item: order[item]) if order is not None else (lambda item: item.value)
    if tuple(sorted(parsed, key=key)) != parsed:
        _reject(label)
    return parsed


def _sorted_string_tuple(
    values: Any,
    label: str,
    validator: Any,
    *,
    maximum: int = 16,
) -> tuple[str, ...]:
    if type(values) is not list or not 1 <= len(values) <= maximum:
        _reject(label)
    parsed = tuple(validator(value, label) for value in values)
    if len(set(parsed)) != len(parsed) or tuple(sorted(parsed)) != parsed:
        _reject(label)
    return parsed


def _validate_json_tree(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_ITEMS or depth > MAX_JSON_DEPTH:
        raise FrameRejected("json_bounds")
    if value is None or type(value) is float:
        raise FrameRejected("json_type")
    if type(value) is bool:
        return
    if type(value) is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise FrameRejected("json_integer")
        return
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise FrameRejected("json_unicode") from exc
        if len(encoded) > MAX_STRING_BYTES:
            raise FrameRejected("json_string")
        return
    if type(value) is list:
        for child in value:
            _validate_json_tree(child, depth=depth + 1, counter=counter)
        return
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str or not _FIELD_NAME_RE.fullmatch(key):
                raise FrameRejected("json_key")
            _validate_json_tree(key, depth=depth + 1, counter=counter)
            _validate_json_tree(child, depth=depth + 1, counter=counter)
        return
    raise FrameRejected("json_type")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the finite integer-only JCS subset used for signatures."""

    _validate_json_tree(value)
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise FrameRejected("json_encoding") from exc
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        raise FrameRejected("json_size")
    return encoded


def content_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise FrameRejected("json_duplicate_key")
        result[key] = value
    return result


def _parse_integer(raw: str) -> int:
    if len(raw) > 17:
        raise FrameRejected("json_integer")
    value = int(raw)
    if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
        raise FrameRejected("json_integer")
    return value


def _reject_number(_raw: str) -> Any:
    raise FrameRejected("json_number")


def decode_json_payload(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > MAX_FRAME_BYTES:
        raise FrameRejected("frame_size")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise FrameRejected("json_bom")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FrameRejected("json_utf8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except FrameRejected:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise FrameRejected("json_syntax") from exc
    _validate_json_tree(value)
    if type(value) is not dict:
        raise FrameRejected("json_root")
    return value


def encode_frame(value: Any) -> bytes:
    payload = canonical_json_bytes(value)
    return struct.pack(">I", len(payload)) + payload


class FrameDecoder:
    """Bounded fail-stop decoder for four-byte length-prefixed JSON frames."""

    def __init__(self, *, maximum_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        if type(maximum_frame_bytes) is not int or not 1 <= maximum_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("maximum_frame_bytes is invalid")
        self.maximum_frame_bytes = maximum_frame_bytes
        self._buffer = bytearray()
        self._poisoned = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    def _fail(self, reason: str) -> None:
        self._buffer.clear()
        self._poisoned = True
        raise FrameRejected(reason)

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        if self._poisoned:
            raise FrameRejected("decoder_poisoned")
        if type(data) is not bytes or len(data) > MAX_FEED_BYTES:
            self._fail("feed_size")
        self._buffer.extend(data)
        decoded: list[dict[str, Any]] = []
        try:
            while len(self._buffer) >= 4:
                declared = struct.unpack(">I", self._buffer[:4])[0]
                if declared == 0 or declared > self.maximum_frame_bytes:
                    self._fail("frame_length")
                frame_end = 4 + declared
                if len(self._buffer) < frame_end:
                    if len(self._buffer) > self.maximum_frame_bytes + 4:
                        self._fail("frame_buffer")
                    break
                payload = bytes(self._buffer[4:frame_end])
                del self._buffer[:frame_end]
                decoded.append(decode_json_payload(payload))
        except FrameRejected:
            if not self._poisoned:
                self._buffer.clear()
                self._poisoned = True
            raise
        return decoded

    def finish(self) -> None:
        if self._poisoned:
            raise FrameRejected("decoder_poisoned")
        if self._buffer:
            self._fail("frame_truncated")


@dataclass(frozen=True, slots=True)
class TargetRef:
    kind: TargetKind
    target_id: str
    epoch: int
    revision: str
    fencing_token: int

    @classmethod
    def from_dict(cls, value: Any) -> "TargetRef":
        row = _exact_dict(
            value,
            ("epoch", "fencing_token", "kind", "revision", "target_id"),
            "target",
        )
        return cls(
            kind=_enum_value(TargetKind, row["kind"], "target_kind"),
            target_id=_opaque_id(row["target_id"], "target_id"),
            epoch=_exact_int(row["epoch"], "target_epoch", minimum=1, maximum=MAX_SAFE_INTEGER),
            revision=_revision(row["revision"], "target_revision"),
            fencing_token=_exact_int(
                row["fencing_token"],
                "target_fence",
                minimum=1,
                maximum=MAX_SAFE_INTEGER,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "target_id": self.target_id,
            "epoch": self.epoch,
            "revision": self.revision,
            "fencing_token": self.fencing_token,
        }


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    snapshot_id: str
    target_id: str
    epoch: int
    revision: str
    fencing_token: int
    observed_at_ms: int
    sequence: int

    @classmethod
    def from_dict(cls, value: Any) -> "SnapshotRef":
        row = _exact_dict(
            value,
            (
                "epoch",
                "fencing_token",
                "observed_at_ms",
                "revision",
                "sequence",
                "snapshot_id",
                "target_id",
            ),
            "snapshot",
        )
        return cls(
            snapshot_id=_canonical_uuid(row["snapshot_id"], "snapshot_id"),
            target_id=_opaque_id(row["target_id"], "snapshot_target"),
            epoch=_exact_int(row["epoch"], "snapshot_epoch", minimum=1, maximum=MAX_SAFE_INTEGER),
            revision=_revision(row["revision"], "snapshot_revision"),
            fencing_token=_exact_int(
                row["fencing_token"],
                "snapshot_fence",
                minimum=1,
                maximum=MAX_SAFE_INTEGER,
            ),
            observed_at_ms=_exact_int(
                row["observed_at_ms"],
                "snapshot_time",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            ),
            sequence=_exact_int(
                row["sequence"],
                "snapshot_sequence",
                minimum=1,
                maximum=MAX_SAFE_INTEGER,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "target_id": self.target_id,
            "epoch": self.epoch,
            "revision": self.revision,
            "fencing_token": self.fencing_token,
            "observed_at_ms": self.observed_at_ms,
            "sequence": self.sequence,
        }

    def matches_target(self, target: TargetRef) -> bool:
        return (
            self.target_id == target.target_id
            and self.epoch == target.epoch
            and self.revision == target.revision
            and self.fencing_token == target.fencing_token
        )


def _validate_text_input(value: Any) -> str:
    text = _safe_text(value, "input_text", maximum_bytes=MAX_TEXT_BYTES)
    if any(
        ord(character) == 0 or (unicodedata.category(character) == "Cc" and character not in "\t\n\r")
        for character in text
    ):
        _reject("input_text")
    return text


def validate_operation_arguments(operation: Operation, value: Any) -> dict[str, Any]:
    spec = OPERATION_SPECS[operation]
    row = _exact_dict(value, spec.argument_fields, "arguments")
    if operation is Operation.OBSERVE:
        return {}
    if operation is Operation.ACTIVATE:
        return {"element_id": _opaque_id(row["element_id"], "element_id")}
    if operation is Operation.INPUT_TEXT:
        if type(row["replace"]) is not bool:
            _reject("replace")
        return {
            "element_id": _opaque_id(row["element_id"], "element_id"),
            "replace": row["replace"],
            "text": _validate_text_input(row["text"]),
        }
    if operation is Operation.SELECT_OPTION:
        return {
            "element_id": _opaque_id(row["element_id"], "element_id"),
            "option_id": _opaque_id(row["option_id"], "option_id"),
        }
    if operation is Operation.SCROLL:
        delta_x = _exact_int(
            row["delta_x"],
            "delta_x",
            minimum=-MAX_SCROLL_DELTA,
            maximum=MAX_SCROLL_DELTA,
        )
        delta_y = _exact_int(
            row["delta_y"],
            "delta_y",
            minimum=-MAX_SCROLL_DELTA,
            maximum=MAX_SCROLL_DELTA,
        )
        if delta_x == 0 and delta_y == 0:
            _reject("scroll_zero")
        return {
            "delta_x": delta_x,
            "delta_y": delta_y,
            "element_id": _opaque_id(row["element_id"], "element_id"),
        }
    if operation is Operation.UPLOAD:
        return {
            "artifact_id": _canonical_uuid(row["artifact_id"], "artifact_id"),
            "byte_count": _exact_int(
                row["byte_count"],
                "upload_bytes",
                minimum=1,
                maximum=MAX_UPLOAD_BYTES,
            ),
            "element_id": _opaque_id(row["element_id"], "element_id"),
        }
    if operation is Operation.COORDINATE_ACTIVATE:
        width = _exact_int(
            row["viewport_width"],
            "viewport_width",
            minimum=1,
            maximum=MAX_VIEWPORT_DIMENSION,
        )
        height = _exact_int(
            row["viewport_height"],
            "viewport_height",
            minimum=1,
            maximum=MAX_VIEWPORT_DIMENSION,
        )
        x = _exact_int(row["x"], "coordinate_x", minimum=0, maximum=width - 1)
        y = _exact_int(row["y"], "coordinate_y", minimum=0, maximum=height - 1)
        return {
            "viewport_height": height,
            "viewport_width": width,
            "x": x,
            "y": y,
        }
    if operation is Operation.HANDOFF:
        return {"reason_code": _safe_identifier(row["reason_code"], "reason_code")}
    _reject("operation")


def validate_preparation_arguments(
    operation: Operation,
    route: ControlRoute,
    selector: str,
    value: Any,
) -> dict[str, Any]:
    selectors = PREPARATION_SELECTORS.get((operation, route))
    if selectors is None:
        _reject("preparation_route_operation")
    selected = _safe_identifier(selector, "preparation_selector")
    if selected not in selectors:
        _reject("preparation_selector")
    if operation is Operation.SELECT_OPTION and route is ControlRoute.AX:
        row = _exact_dict(value, ("option_id",), "preparation_arguments")
        return {"option_id": _opaque_id(row["option_id"], "option_id")}
    if operation is Operation.SCROLL and route is ControlRoute.AX:
        row = _exact_dict(value, ("delta_x", "delta_y"), "preparation_arguments")
        delta_x = _exact_int(
            row["delta_x"],
            "delta_x",
            minimum=-MAX_SCROLL_DELTA,
            maximum=MAX_SCROLL_DELTA,
        )
        delta_y = _exact_int(
            row["delta_y"],
            "delta_y",
            minimum=-MAX_SCROLL_DELTA,
            maximum=MAX_SCROLL_DELTA,
        )
        if delta_x == 0 and delta_y == 0:
            _reject("scroll_zero")
        return {"delta_x": delta_x, "delta_y": delta_y}
    if operation is Operation.COORDINATE_ACTIVATE and route is ControlRoute.COORDINATE:
        row = _exact_dict(value, ("x", "y"), "preparation_arguments")
        return {
            "x": _exact_int(
                row["x"],
                "coordinate_x",
                minimum=0,
                maximum=MAX_VIEWPORT_DIMENSION - 1,
            ),
            "y": _exact_int(
                row["y"],
                "coordinate_y",
                minimum=0,
                maximum=MAX_VIEWPORT_DIMENSION - 1,
            ),
        }
    return dict(_exact_dict(value, (), "preparation_arguments"))


@dataclass(frozen=True, slots=True)
class ControlRequest:
    request_id: str
    session_id: str
    subject_id: str
    sequence: int
    issued_at_ms: int
    deadline_ms: int
    target: TargetRef
    snapshot: SnapshotRef
    operation: Operation
    data_class: ControlDataClass
    arguments: dict[str, Any]
    requested_routes: tuple[ControlRoute, ...]
    max_output_bytes: int
    schema_version: int = CONTROL_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> "ControlRequest":
        row = _exact_dict(
            value,
            (
                "arguments",
                "data_class",
                "deadline_ms",
                "issued_at_ms",
                "max_output_bytes",
                "operation",
                "request_id",
                "requested_routes",
                "schema_version",
                "sequence",
                "session_id",
                "snapshot",
                "subject_id",
                "target",
            ),
            "request",
        )
        if row["schema_version"] != CONTROL_SCHEMA_VERSION or type(row["schema_version"]) is not int:
            _reject("request_version")
        operation = _enum_value(Operation, row["operation"], "operation")
        data_class = _enum_value(ControlDataClass, row["data_class"], "data_class")
        if data_class not in OPERATION_SPECS[operation].data_classes:
            _reject("operation_data_class")
        target = TargetRef.from_dict(row["target"])
        snapshot = SnapshotRef.from_dict(row["snapshot"])
        if not snapshot.matches_target(target):
            _reject("snapshot_target")
        issued_at_ms = _exact_int(
            row["issued_at_ms"],
            "issued_at",
            minimum=0,
            maximum=MAX_SAFE_INTEGER,
        )
        deadline_ms = _exact_int(
            row["deadline_ms"],
            "deadline",
            minimum=1,
            maximum=MAX_SAFE_INTEGER,
        )
        if not issued_at_ms < deadline_ms <= issued_at_ms + MAX_DEADLINE_HORIZON_MS:
            _reject("deadline_window")
        return cls(
            request_id=_canonical_uuid(row["request_id"], "request_id"),
            session_id=_canonical_uuid(row["session_id"], "session_id"),
            subject_id=_safe_identifier(row["subject_id"], "subject_id"),
            sequence=_exact_int(
                row["sequence"],
                "sequence",
                minimum=1,
                maximum=MAX_SAFE_INTEGER,
            ),
            issued_at_ms=issued_at_ms,
            deadline_ms=deadline_ms,
            target=target,
            snapshot=snapshot,
            operation=operation,
            data_class=data_class,
            arguments=validate_operation_arguments(operation, row["arguments"]),
            requested_routes=_ordered_enum_tuple(
                ControlRoute,
                row["requested_routes"],
                "requested_routes",
                order=_ROUTE_INDEX,
                maximum=len(ROUTE_ORDER),
            ),
            max_output_bytes=_exact_int(
                row["max_output_bytes"],
                "max_output_bytes",
                minimum=1,
                maximum=MAX_OUTPUT_BYTES,
            ),
        )

    @property
    def effects(self) -> tuple[Effect, ...]:
        return OPERATION_SPECS[self.operation].effects

    @property
    def argument_bytes(self) -> int:
        return len(canonical_json_bytes(self.arguments))

    @property
    def transmit_bytes(self) -> int:
        if self.operation is Operation.UPLOAD:
            return int(self.arguments["byte_count"])
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "subject_id": self.subject_id,
            "sequence": self.sequence,
            "issued_at_ms": self.issued_at_ms,
            "deadline_ms": self.deadline_ms,
            "target": self.target.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "operation": self.operation.value,
            "data_class": self.data_class.value,
            "arguments": dict(self.arguments),
            "requested_routes": [route.value for route in self.requested_routes],
            "max_output_bytes": self.max_output_bytes,
        }

    @property
    def digest(self) -> str:
        return content_digest(self.to_dict())


def _encode_signature(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_signature(value: str) -> bytes:
    _signature(value, "signature")
    try:
        decoded = base64.b64decode(value + "==", altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthorityRejected("signature_encoding") from exc
    if len(decoded) != 64:
        raise AuthorityRejected("signature_length")
    if _encode_signature(decoded) != value:
        raise AuthorityRejected("signature_encoding")
    return decoded


def _signature_payload(kind: str, value: dict[str, Any]) -> bytes:
    safe_kind = _safe_identifier(kind, "signature_kind")
    return b"algo-control-v1\0" + safe_kind.encode("ascii") + b"\0" + canonical_json_bytes(value)


class ControlVerifier:
    """One pinned Ed25519 authority public key."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._public_key = public_key
        raw = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.key_id = "ed25519:" + hashlib.sha256(raw).hexdigest()

    @classmethod
    def from_public_bytes(cls, raw: bytes) -> "ControlVerifier":
        if type(raw) is not bytes or len(raw) != 32:
            raise ValueError("Ed25519 public key must contain 32 bytes")
        return cls(Ed25519PublicKey.from_public_bytes(raw))

    @property
    def public_bytes(self) -> bytes:
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def verify(self, kind: str, value: dict[str, Any], signature: str) -> None:
        try:
            self._public_key.verify(
                _decode_signature(signature),
                _signature_payload(kind, value),
            )
        except InvalidSignature as exc:
            raise AuthorityRejected("signature_invalid") from exc


class ControlSigner:
    """Injected Ed25519 signer; production loading is OS-keyring-backed."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key
        self.verifier = ControlVerifier(private_key.public_key())
        self.key_id = self.verifier.key_id

    @classmethod
    def generate(cls) -> "ControlSigner":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> "ControlSigner":
        if type(raw) is not bytes or len(raw) != 32:
            raise ValueError("Ed25519 private key must contain 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    @property
    def private_bytes(self) -> bytes:
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def sign(self, kind: str, value: dict[str, Any]) -> str:
        return _encode_signature(self._private_key.sign(_signature_payload(kind, value)))


@dataclass(frozen=True, slots=True)
class ControlPreparation:
    """One target-free native binding authorization.

    This object can authorize fixed native confirmation and discovery only.
    It is never an execution permit and contains no target supplied by the
    model or relay.
    """

    preparation_id: str
    request_id: str
    subject_id: str
    operation: Operation
    data_class: ControlDataClass
    route: ControlRoute
    selector: str
    arguments: dict[str, Any]
    issued_at_ms: int
    expires_at_ms: int
    policy_digest: str
    authority_key_id: str
    signature: str
    schema_version: int = CONTROL_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> "ControlPreparation":
        row = _exact_dict(
            value,
            (
                "arguments",
                "authority_key_id",
                "data_class",
                "expires_at_ms",
                "issued_at_ms",
                "operation",
                "policy_digest",
                "preparation_id",
                "request_id",
                "route",
                "schema_version",
                "selector",
                "signature",
                "subject_id",
            ),
            "preparation",
        )
        if type(row["schema_version"]) is not int or row["schema_version"] != CONTROL_SCHEMA_VERSION:
            _reject("preparation_version")
        issued = _exact_int(
            row["issued_at_ms"],
            "preparation_issued",
            minimum=0,
            maximum=MAX_SAFE_INTEGER,
        )
        expires = _exact_int(
            row["expires_at_ms"],
            "preparation_expires",
            minimum=1,
            maximum=MAX_SAFE_INTEGER,
        )
        if not issued < expires <= issued + MAX_PREPARATION_LIFETIME_MS:
            _reject("preparation_window")
        operation = _enum_value(Operation, row["operation"], "preparation_operation")
        data_class = _enum_value(
            ControlDataClass,
            row["data_class"],
            "preparation_data_class",
        )
        if operation is Operation.UPLOAD or data_class not in OPERATION_SPECS[operation].data_classes:
            _reject("preparation_data_class")
        route = _enum_value(ControlRoute, row["route"], "preparation_route")
        selector = _safe_identifier(row["selector"], "preparation_selector")
        arguments = validate_preparation_arguments(
            operation,
            route,
            selector,
            row["arguments"],
        )
        return cls(
            preparation_id=_canonical_uuid(row["preparation_id"], "preparation_id"),
            request_id=_canonical_uuid(row["request_id"], "preparation_request_id"),
            subject_id=_safe_identifier(row["subject_id"], "preparation_subject_id"),
            operation=operation,
            data_class=data_class,
            route=route,
            selector=selector,
            arguments=arguments,
            issued_at_ms=issued,
            expires_at_ms=expires,
            policy_digest=_digest(row["policy_digest"], "preparation_policy"),
            authority_key_id=_key_id(row["authority_key_id"], "preparation_key"),
            signature=_signature(row["signature"], "preparation_signature"),
        )

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preparation_id": self.preparation_id,
            "request_id": self.request_id,
            "subject_id": self.subject_id,
            "operation": self.operation.value,
            "data_class": self.data_class.value,
            "route": self.route.value,
            "selector": self.selector,
            "arguments": dict(self.arguments),
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "policy_digest": self.policy_digest,
            "authority_key_id": self.authority_key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}

    @property
    def digest(self) -> str:
        return content_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ControlPreparationEnvelope:
    preparation: ControlPreparation
    protocol_version: int = CONTROL_PROTOCOL_VERSION
    message_type: str = CONTROL_PREPARATION_MESSAGE_TYPE

    @classmethod
    def from_dict(cls, value: Any) -> "ControlPreparationEnvelope":
        row = _exact_dict(
            value,
            ("message_type", "preparation", "protocol_version"),
            "preparation_envelope",
        )
        if type(row["protocol_version"]) is not int or row["protocol_version"] != CONTROL_PROTOCOL_VERSION:
            _reject("protocol_version")
        if row["message_type"] != CONTROL_PREPARATION_MESSAGE_TYPE:
            _reject("message_type")
        return cls(preparation=ControlPreparation.from_dict(row["preparation"]))

    @classmethod
    def from_payload(cls, payload: bytes) -> "ControlPreparationEnvelope":
        return cls.from_dict(decode_json_payload(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "preparation": self.preparation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ControlGrant:
    grant_id: str
    subject_id: str
    target_ids: tuple[str, ...]
    target_kinds: tuple[TargetKind, ...]
    operations: tuple[Operation, ...]
    effects: tuple[Effect, ...]
    data_classes: tuple[ControlDataClass, ...]
    routes: tuple[ControlRoute, ...]
    issued_at_ms: int
    expires_at_ms: int
    maximum_action_count: int
    max_input_bytes: int
    max_output_bytes: int
    max_transmit_bytes: int
    policy_digest: str
    authority_key_id: str
    signature: str
    schema_version: int = CONTROL_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> "ControlGrant":
        row = _exact_dict(
            value,
            (
                "authority_key_id",
                "data_classes",
                "effects",
                "expires_at_ms",
                "grant_id",
                "issued_at_ms",
                "max_input_bytes",
                "max_output_bytes",
                "max_transmit_bytes",
                "maximum_action_count",
                "operations",
                "policy_digest",
                "routes",
                "schema_version",
                "signature",
                "subject_id",
                "target_ids",
                "target_kinds",
            ),
            "grant",
        )
        if type(row["schema_version"]) is not int or row["schema_version"] != CONTROL_SCHEMA_VERSION:
            _reject("grant_version")
        issued = _exact_int(row["issued_at_ms"], "grant_issued", minimum=0, maximum=MAX_SAFE_INTEGER)
        expires = _exact_int(row["expires_at_ms"], "grant_expires", minimum=1, maximum=MAX_SAFE_INTEGER)
        if not issued < expires <= issued + MAX_GRANT_LIFETIME_MS:
            _reject("grant_window")
        return cls(
            grant_id=_canonical_uuid(row["grant_id"], "grant_id"),
            subject_id=_safe_identifier(row["subject_id"], "subject_id"),
            target_ids=_sorted_string_tuple(row["target_ids"], "target_ids", _opaque_id),
            target_kinds=_ordered_enum_tuple(TargetKind, row["target_kinds"], "target_kinds"),
            operations=_ordered_enum_tuple(Operation, row["operations"], "operations"),
            effects=_ordered_enum_tuple(Effect, row["effects"], "effects"),
            data_classes=_ordered_enum_tuple(
                ControlDataClass,
                row["data_classes"],
                "data_classes",
            ),
            routes=_ordered_enum_tuple(
                ControlRoute,
                row["routes"],
                "routes",
                order=_ROUTE_INDEX,
                maximum=len(ROUTE_ORDER),
            ),
            issued_at_ms=issued,
            expires_at_ms=expires,
            maximum_action_count=_exact_int(
                row["maximum_action_count"],
                "grant_action_count",
                minimum=1,
                maximum=MAX_ACTION_COUNT,
            ),
            max_input_bytes=_exact_int(
                row["max_input_bytes"],
                "grant_input_bytes",
                minimum=1,
                maximum=MAX_STRING_BYTES,
            ),
            max_output_bytes=_exact_int(
                row["max_output_bytes"],
                "grant_output_bytes",
                minimum=1,
                maximum=MAX_OUTPUT_BYTES,
            ),
            max_transmit_bytes=_exact_int(
                row["max_transmit_bytes"],
                "grant_transmit_bytes",
                minimum=0,
                maximum=MAX_UPLOAD_BYTES,
            ),
            policy_digest=_digest(row["policy_digest"], "policy_digest"),
            authority_key_id=_key_id(row["authority_key_id"], "authority_key_id"),
            signature=_signature(row["signature"], "signature"),
        )

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "grant_id": self.grant_id,
            "subject_id": self.subject_id,
            "target_ids": list(self.target_ids),
            "target_kinds": [item.value for item in self.target_kinds],
            "operations": [item.value for item in self.operations],
            "effects": [item.value for item in self.effects],
            "data_classes": [item.value for item in self.data_classes],
            "routes": [item.value for item in self.routes],
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "maximum_action_count": self.maximum_action_count,
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_transmit_bytes": self.max_transmit_bytes,
            "policy_digest": self.policy_digest,
            "authority_key_id": self.authority_key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}


@dataclass(frozen=True, slots=True)
class ControlPermit:
    permit_id: str
    grant_id: str
    subject_id: str
    request_id: str
    request_digest: str
    target_kind: TargetKind
    target_id: str
    target_epoch: int
    target_revision: str
    fencing_token: int
    snapshot_id: str
    sequence: int
    operation: Operation
    effects: tuple[Effect, ...]
    data_class: ControlDataClass
    routes: tuple[ControlRoute, ...]
    input_bytes: int
    output_bytes: int
    transmit_bytes: int
    issued_at_ms: int
    expires_at_ms: int
    maximum_action_count: int
    policy_digest: str
    authority_key_id: str
    signature: str
    schema_version: int = CONTROL_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> "ControlPermit":
        row = _exact_dict(
            value,
            (
                "authority_key_id",
                "data_class",
                "effects",
                "expires_at_ms",
                "fencing_token",
                "grant_id",
                "input_bytes",
                "issued_at_ms",
                "maximum_action_count",
                "operation",
                "output_bytes",
                "permit_id",
                "policy_digest",
                "request_digest",
                "request_id",
                "routes",
                "schema_version",
                "sequence",
                "signature",
                "snapshot_id",
                "subject_id",
                "target_epoch",
                "target_id",
                "target_kind",
                "target_revision",
                "transmit_bytes",
            ),
            "permit",
        )
        if type(row["schema_version"]) is not int or row["schema_version"] != CONTROL_SCHEMA_VERSION:
            _reject("permit_version")
        issued = _exact_int(row["issued_at_ms"], "permit_issued", minimum=0, maximum=MAX_SAFE_INTEGER)
        expires = _exact_int(row["expires_at_ms"], "permit_expires", minimum=1, maximum=MAX_SAFE_INTEGER)
        if not issued < expires <= issued + MAX_DEADLINE_HORIZON_MS:
            _reject("permit_window")
        return cls(
            permit_id=_canonical_uuid(row["permit_id"], "permit_id"),
            grant_id=_canonical_uuid(row["grant_id"], "grant_id"),
            subject_id=_safe_identifier(row["subject_id"], "subject_id"),
            request_id=_canonical_uuid(row["request_id"], "request_id"),
            request_digest=_digest(row["request_digest"], "request_digest"),
            target_kind=_enum_value(TargetKind, row["target_kind"], "target_kind"),
            target_id=_opaque_id(row["target_id"], "target_id"),
            target_epoch=_exact_int(
                row["target_epoch"],
                "target_epoch",
                minimum=1,
                maximum=MAX_SAFE_INTEGER,
            ),
            target_revision=_revision(row["target_revision"], "target_revision"),
            fencing_token=_exact_int(
                row["fencing_token"],
                "fencing_token",
                minimum=1,
                maximum=MAX_SAFE_INTEGER,
            ),
            snapshot_id=_canonical_uuid(row["snapshot_id"], "snapshot_id"),
            sequence=_exact_int(row["sequence"], "sequence", minimum=1, maximum=MAX_SAFE_INTEGER),
            operation=_enum_value(Operation, row["operation"], "operation"),
            effects=_ordered_enum_tuple(Effect, row["effects"], "effects"),
            data_class=_enum_value(ControlDataClass, row["data_class"], "data_class"),
            routes=_ordered_enum_tuple(
                ControlRoute,
                row["routes"],
                "routes",
                order=_ROUTE_INDEX,
                maximum=len(ROUTE_ORDER),
            ),
            input_bytes=_exact_int(
                row["input_bytes"],
                "input_bytes",
                minimum=1,
                maximum=MAX_STRING_BYTES,
            ),
            output_bytes=_exact_int(
                row["output_bytes"],
                "output_bytes",
                minimum=1,
                maximum=MAX_OUTPUT_BYTES,
            ),
            transmit_bytes=_exact_int(
                row["transmit_bytes"],
                "transmit_bytes",
                minimum=0,
                maximum=MAX_UPLOAD_BYTES,
            ),
            issued_at_ms=issued,
            expires_at_ms=expires,
            maximum_action_count=_exact_int(
                row["maximum_action_count"],
                "permit_action_count",
                minimum=1,
                maximum=1,
            ),
            policy_digest=_digest(row["policy_digest"], "policy_digest"),
            authority_key_id=_key_id(row["authority_key_id"], "authority_key_id"),
            signature=_signature(row["signature"], "signature"),
        )

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "permit_id": self.permit_id,
            "grant_id": self.grant_id,
            "subject_id": self.subject_id,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "target_epoch": self.target_epoch,
            "target_revision": self.target_revision,
            "fencing_token": self.fencing_token,
            "snapshot_id": self.snapshot_id,
            "sequence": self.sequence,
            "operation": self.operation.value,
            "effects": [item.value for item in self.effects],
            "data_class": self.data_class.value,
            "routes": [item.value for item in self.routes],
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
            "transmit_bytes": self.transmit_bytes,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "maximum_action_count": self.maximum_action_count,
            "policy_digest": self.policy_digest,
            "authority_key_id": self.authority_key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}


@dataclass(frozen=True, slots=True)
class ControlEnvelope:
    request: ControlRequest
    grant: ControlGrant
    permit: ControlPermit
    protocol_version: int = CONTROL_PROTOCOL_VERSION
    message_type: str = CONTROL_MESSAGE_TYPE

    @classmethod
    def from_dict(cls, value: Any) -> "ControlEnvelope":
        row = _exact_dict(
            value,
            ("grant", "message_type", "permit", "protocol_version", "request"),
            "envelope",
        )
        if type(row["protocol_version"]) is not int or row["protocol_version"] != CONTROL_PROTOCOL_VERSION:
            _reject("protocol_version")
        if type(row["message_type"]) is not str or row["message_type"] != CONTROL_MESSAGE_TYPE:
            _reject("message_type")
        return cls(
            request=ControlRequest.from_dict(row["request"]),
            grant=ControlGrant.from_dict(row["grant"]),
            permit=ControlPermit.from_dict(row["permit"]),
        )

    @classmethod
    def from_payload(cls, payload: bytes) -> "ControlEnvelope":
        return cls.from_dict(decode_json_payload(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "request": self.request.to_dict(),
            "grant": self.grant.to_dict(),
            "permit": self.permit.to_dict(),
        }

    def to_frame(self) -> bytes:
        return encode_frame(self.to_dict())


@dataclass(frozen=True, slots=True)
class ControlPolicy:
    policy_id: str
    revision: int
    operation_routes: tuple[tuple[Operation, tuple[ControlRoute, ...]], ...]
    allowed_target_kinds: frozenset[TargetKind]
    allowed_data_classes: frozenset[ControlDataClass]
    max_frame_bytes: int = MAX_FRAME_BYTES
    max_input_bytes: int = MAX_STRING_BYTES
    max_output_bytes: int = 65_536
    max_transmit_bytes: int = 16 * 1024 * 1024
    max_deadline_ms: int = 120_000
    max_snapshot_age_ms: int = 30_000
    clock_skew_ms: int = 2_000

    def __post_init__(self) -> None:
        _safe_identifier(self.policy_id, "policy_id")
        _exact_int(self.revision, "policy_revision", minimum=1, maximum=MAX_SAFE_INTEGER)
        if not self.allowed_target_kinds or not self.allowed_data_classes:
            raise ValueError("policy allowlists must not be empty")
        operations = tuple(item[0] for item in self.operation_routes)
        if (
            len(set(operations)) != len(operations)
            or tuple(sorted(operations, key=lambda item: item.value)) != operations
        ):
            raise ValueError("policy operations must be unique and sorted")
        for operation, routes in self.operation_routes:
            if not routes or len(set(routes)) != len(routes):
                raise ValueError("policy routes must be non-empty and unique")
            if tuple(sorted(routes, key=lambda item: _ROUTE_INDEX[item])) != routes:
                raise ValueError("policy routes must follow deterministic order")
        bounds = (
            (self.max_frame_bytes, 1, MAX_FRAME_BYTES),
            (self.max_input_bytes, 1, MAX_STRING_BYTES),
            (self.max_output_bytes, 1, MAX_OUTPUT_BYTES),
            (self.max_transmit_bytes, 0, MAX_UPLOAD_BYTES),
            (self.max_deadline_ms, 1, MAX_DEADLINE_HORIZON_MS),
            (self.max_snapshot_age_ms, 0, MAX_DEADLINE_HORIZON_MS),
            (self.clock_skew_ms, 0, 60_000),
        )
        if any(type(value) is not int or not minimum <= value <= maximum for value, minimum, maximum in bounds):
            raise ValueError("policy bounds are invalid")

    @property
    def route_map(self) -> dict[Operation, tuple[ControlRoute, ...]]:
        return dict(self.operation_routes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "policy_id": self.policy_id,
            "revision": self.revision,
            "operation_routes": {
                operation.value: [route.value for route in routes] for operation, routes in self.operation_routes
            },
            "allowed_target_kinds": sorted(item.value for item in self.allowed_target_kinds),
            "allowed_data_classes": sorted(item.value for item in self.allowed_data_classes),
            "max_frame_bytes": self.max_frame_bytes,
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_transmit_bytes": self.max_transmit_bytes,
            "max_deadline_ms": self.max_deadline_ms,
            "max_snapshot_age_ms": self.max_snapshot_age_ms,
            "clock_skew_ms": self.clock_skew_ms,
        }

    @property
    def digest(self) -> str:
        return content_digest(self.to_dict())

    def validate_request(
        self,
        request: ControlRequest,
        *,
        now_ms: int,
        live_snapshot: SnapshotRef | None = None,
    ) -> None:
        _exact_int(now_ms, "now", minimum=0, maximum=MAX_SAFE_INTEGER)
        if request.target.kind not in self.allowed_target_kinds:
            raise PermitRejected("target_kind_denied")
        if request.data_class not in self.allowed_data_classes:
            raise PermitRejected("data_class_denied")
        if request.operation not in self.route_map:
            raise PermitRejected("operation_denied")
        if request.issued_at_ms > now_ms + self.clock_skew_ms:
            raise PermitRejected("request_not_yet_valid")
        if request.deadline_ms <= now_ms:
            raise PermitRejected("request_expired")
        if request.deadline_ms - request.issued_at_ms > self.max_deadline_ms:
            raise PermitRejected("request_deadline_limit")
        if request.snapshot.observed_at_ms > now_ms + self.clock_skew_ms:
            raise PermitRejected("snapshot_from_future")
        if now_ms - request.snapshot.observed_at_ms > self.max_snapshot_age_ms:
            raise PermitRejected("snapshot_stale")
        if request.argument_bytes > self.max_input_bytes:
            raise PermitRejected("input_limit")
        if request.max_output_bytes > self.max_output_bytes:
            raise PermitRejected("output_limit")
        if request.transmit_bytes > self.max_transmit_bytes:
            raise PermitRejected("transmit_limit")
        policy_routes = frozenset(self.route_map[request.operation])
        target_routes = _TARGET_ROUTES[request.target.kind]
        if any(route not in policy_routes or route not in target_routes for route in request.requested_routes):
            raise PermitRejected("route_denied")
        if request.operation is Operation.COORDINATE_ACTIVATE and request.requested_routes != (
            ControlRoute.COORDINATE,
        ):
            raise PermitRejected("coordinate_route_required")
        if request.operation is Operation.HANDOFF and request.requested_routes != (ControlRoute.HANDOFF,):
            raise PermitRejected("handoff_route_required")
        if live_snapshot is not None and live_snapshot != request.snapshot:
            raise PermitRejected("snapshot_changed")

    def select_route(
        self,
        request: ControlRequest,
        *,
        grant_routes: Sequence[ControlRoute],
        permit_routes: Sequence[ControlRoute],
        live_routes: Iterable[ControlRoute],
    ) -> ControlRoute:
        allowed = (
            frozenset(request.requested_routes)
            & frozenset(grant_routes)
            & frozenset(permit_routes)
            & frozenset(self.route_map.get(request.operation, ()))
            & frozenset(_TARGET_ROUTES[request.target.kind])
            & frozenset(live_routes)
        )
        for route in ROUTE_ORDER:
            if route in allowed:
                return route
        raise PermitRejected("no_route")


def default_control_policy() -> ControlPolicy:
    route_map = {
        Operation.OBSERVE: (
            ControlRoute.CONNECTOR,
            ControlRoute.DOM,
            ControlRoute.AX,
            ControlRoute.SCREENSHOT,
        ),
        Operation.ACTIVATE: (
            ControlRoute.CONNECTOR,
            ControlRoute.SHORTCUT,
            ControlRoute.APPLE_EVENT,
            ControlRoute.DOM,
            ControlRoute.AX,
            ControlRoute.SCREENSHOT,
            ControlRoute.COORDINATE,
        ),
        Operation.INPUT_TEXT: (
            ControlRoute.CONNECTOR,
            ControlRoute.SHORTCUT,
            ControlRoute.APPLE_EVENT,
            ControlRoute.DOM,
            ControlRoute.AX,
        ),
        Operation.SELECT_OPTION: (
            ControlRoute.CONNECTOR,
            ControlRoute.SHORTCUT,
            ControlRoute.APPLE_EVENT,
            ControlRoute.DOM,
            ControlRoute.AX,
        ),
        Operation.SCROLL: (
            ControlRoute.CONNECTOR,
            ControlRoute.DOM,
            ControlRoute.AX,
            ControlRoute.COORDINATE,
        ),
        Operation.UPLOAD: (
            ControlRoute.CONNECTOR,
            ControlRoute.DOM,
            ControlRoute.AX,
        ),
        Operation.COORDINATE_ACTIVATE: (ControlRoute.COORDINATE,),
        Operation.HANDOFF: (ControlRoute.HANDOFF,),
    }
    return ControlPolicy(
        policy_id="algo.control.default",
        revision=1,
        operation_routes=tuple(sorted(route_map.items(), key=lambda item: item[0].value)),
        allowed_target_kinds=frozenset(TargetKind),
        allowed_data_classes=frozenset(ControlDataClass),
    )


def issue_control_preparation(
    signer: ControlSigner,
    policy: ControlPolicy,
    *,
    preparation_id: str,
    request_id: str,
    subject_id: str,
    operation: Operation,
    data_class: ControlDataClass,
    route: ControlRoute,
    selector: str,
    arguments: Mapping[str, Any],
    issued_at_ms: int,
    expires_at_ms: int,
) -> ControlPreparationEnvelope:
    """Sign one exact native binding-discovery attempt.

    The authorization deliberately omits a target. Only the native adapter can
    mint that target, after which the ordinary grant/permit path must authorize
    the separately constructed target-bound execution request.
    """

    if type(signer) is not ControlSigner or type(policy) is not ControlPolicy:
        raise ValueError("preparation authority")
    if type(operation) is not Operation or type(data_class) is not ControlDataClass:
        raise ValueError("preparation operation")
    if type(route) is not ControlRoute or route not in policy.route_map.get(operation, ()):
        raise ValueError("preparation route")
    if data_class not in OPERATION_SPECS[operation].data_classes:
        raise ValueError("preparation data class")
    if type(arguments) is not dict:
        raise ValueError("preparation arguments")
    candidate_arguments = dict(arguments)
    row: dict[str, Any] = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "preparation_id": preparation_id,
        "request_id": request_id,
        "subject_id": subject_id,
        "operation": operation.value,
        "data_class": data_class.value,
        "route": route.value,
        "selector": selector,
        "arguments": candidate_arguments,
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": expires_at_ms,
        "policy_digest": policy.digest,
        "authority_key_id": signer.key_id,
    }
    candidate = ControlPreparation.from_dict({**row, "signature": "A" * 86})
    signed = ControlPreparation.from_dict(
        {
            **candidate.unsigned_dict(),
            "signature": signer.sign("control_prepare", candidate.unsigned_dict()),
        }
    )
    return ControlPreparationEnvelope(preparation=signed)


def verify_control_preparation(
    envelope: ControlPreparationEnvelope,
    verifier: ControlVerifier,
    policy: ControlPolicy,
    *,
    now_ms: int,
) -> ControlPreparation:
    if type(envelope) is not ControlPreparationEnvelope:
        raise AuthorityRejected("preparation_envelope")
    preparation = ControlPreparation.from_dict(envelope.preparation.to_dict())
    if preparation.authority_key_id != verifier.key_id:
        raise AuthorityRejected("preparation_key")
    verifier.verify("control_prepare", preparation.unsigned_dict(), preparation.signature)
    if preparation.policy_digest != policy.digest:
        raise PermitRejected("preparation_policy")
    if preparation.route not in policy.route_map.get(preparation.operation, ()):
        raise PermitRejected("preparation_route")
    if preparation.data_class not in policy.allowed_data_classes:
        raise PermitRejected("preparation_data_class")
    if now_ms < preparation.issued_at_ms:
        raise PermitRejected("preparation_not_yet_valid")
    if now_ms >= preparation.expires_at_ms:
        raise PermitRejected("preparation_expired")
    return preparation


def issue_grant(
    signer: ControlSigner,
    policy: ControlPolicy,
    *,
    grant_id: str,
    subject_id: str,
    target_ids: Iterable[str],
    target_kinds: Iterable[TargetKind],
    operations: Iterable[Operation],
    data_classes: Iterable[ControlDataClass],
    routes: Iterable[ControlRoute],
    issued_at_ms: int,
    expires_at_ms: int,
    maximum_action_count: int,
    max_input_bytes: int,
    max_output_bytes: int,
    max_transmit_bytes: int,
) -> ControlGrant:
    operation_set = set(operations)
    target_kind_set = set(target_kinds)
    data_class_set = set(data_classes)
    route_set = set(routes)
    if not all(type(item) is Operation for item in operation_set):
        raise ValueError("grant operations must be Operation values")
    if not all(type(item) is TargetKind for item in target_kind_set):
        raise ValueError("grant target kinds must be TargetKind values")
    if not all(type(item) is ControlDataClass for item in data_class_set):
        raise ValueError("grant data classes must be ControlDataClass values")
    if not all(type(item) is ControlRoute for item in route_set):
        raise ValueError("grant routes must be ControlRoute values")
    operation_tuple = tuple(sorted(operation_set, key=lambda item: item.value))
    effect_tuple = tuple(
        sorted(
            {effect for operation in operation_tuple for effect in OPERATION_SPECS[operation].effects},
            key=lambda item: item.value,
        )
    )
    row: dict[str, Any] = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "grant_id": _canonical_uuid(grant_id, "grant_id"),
        "subject_id": _safe_identifier(subject_id, "subject_id"),
        "target_ids": sorted({_opaque_id(value, "target_id") for value in target_ids}),
        "target_kinds": sorted(item.value for item in target_kind_set),
        "operations": [item.value for item in operation_tuple],
        "effects": [item.value for item in effect_tuple],
        "data_classes": sorted(item.value for item in data_class_set),
        "routes": [item.value for item in ROUTE_ORDER if item in route_set],
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": expires_at_ms,
        "maximum_action_count": maximum_action_count,
        "max_input_bytes": max_input_bytes,
        "max_output_bytes": max_output_bytes,
        "max_transmit_bytes": max_transmit_bytes,
        "policy_digest": policy.digest,
        "authority_key_id": signer.key_id,
    }
    if (
        not row["target_ids"]
        or not row["target_kinds"]
        or not row["operations"]
        or not row["data_classes"]
        or not row["routes"]
    ):
        raise ValueError("grant allowlists must not be empty")
    if not set(TargetKind(value) for value in row["target_kinds"]) <= policy.allowed_target_kinds:
        raise ValueError("grant target kinds exceed policy")
    if not set(ControlDataClass(value) for value in row["data_classes"]) <= policy.allowed_data_classes:
        raise ValueError("grant data classes exceed policy")
    for operation in operation_tuple:
        if operation not in policy.route_map:
            raise ValueError("grant operation exceeds policy")
    allowed_routes = {route for operation in operation_tuple for route in policy.route_map[operation]}
    if not route_set <= allowed_routes:
        raise ValueError("grant routes exceed policy")
    candidate = ControlGrant.from_dict({**row, "signature": "A" * 86})
    signed = {**candidate.unsigned_dict(), "signature": signer.sign("control_grant", candidate.unsigned_dict())}
    return ControlGrant.from_dict(signed)


def verify_grant(
    grant: ControlGrant,
    verifier: ControlVerifier,
    policy: ControlPolicy,
    *,
    now_ms: int,
    subject_id: str,
) -> None:
    if grant.authority_key_id != verifier.key_id:
        raise AuthorityRejected("grant_key")
    verifier.verify("control_grant", grant.unsigned_dict(), grant.signature)
    if grant.policy_digest != policy.digest:
        raise PermitRejected("grant_policy")
    if grant.subject_id != subject_id:
        raise PermitRejected("grant_subject")
    if not frozenset(grant.target_kinds) <= policy.allowed_target_kinds:
        raise PermitRejected("grant_target_kind")
    if not frozenset(grant.data_classes) <= policy.allowed_data_classes:
        raise PermitRejected("grant_data_class")
    if any(operation not in policy.route_map for operation in grant.operations):
        raise PermitRejected("grant_operation")
    expected_effects = tuple(
        sorted(
            {effect for operation in grant.operations for effect in OPERATION_SPECS[operation].effects},
            key=lambda item: item.value,
        )
    )
    if grant.effects != expected_effects:
        raise PermitRejected("grant_effects")
    allowed_routes = {route for operation in grant.operations for route in policy.route_map[operation]}
    if not set(grant.routes) <= allowed_routes:
        raise PermitRejected("grant_routes")
    if grant.max_input_bytes > policy.max_input_bytes:
        raise PermitRejected("grant_input_limit")
    if grant.max_output_bytes > policy.max_output_bytes:
        raise PermitRejected("grant_output_limit")
    if grant.max_transmit_bytes > policy.max_transmit_bytes:
        raise PermitRejected("grant_transmit_limit")
    if now_ms < grant.issued_at_ms:
        raise PermitRejected("grant_not_yet_valid")
    if now_ms >= grant.expires_at_ms:
        raise PermitRejected("grant_expired")


def issue_permit(
    signer: ControlSigner,
    verifier: ControlVerifier,
    policy: ControlPolicy,
    grant: ControlGrant,
    request: ControlRequest,
    *,
    permit_id: str,
    issued_at_ms: int,
    expires_at_ms: int,
) -> ControlPermit:
    verify_grant(
        grant,
        verifier,
        policy,
        now_ms=issued_at_ms,
        subject_id=request.subject_id,
    )
    policy.validate_request(request, now_ms=issued_at_ms)
    if signer.key_id != verifier.key_id:
        raise AuthorityRejected("permit_signer")
    if request.target.target_id not in grant.target_ids or request.target.kind not in grant.target_kinds:
        raise PermitRejected("grant_target")
    if request.operation not in grant.operations:
        raise PermitRejected("grant_operation")
    if any(effect not in grant.effects for effect in request.effects):
        raise PermitRejected("grant_effect")
    if request.data_class not in grant.data_classes:
        raise PermitRejected("grant_data_class")
    routes = tuple(route for route in request.requested_routes if route in grant.routes)
    if not routes:
        raise PermitRejected("grant_route")
    if request.argument_bytes > grant.max_input_bytes:
        raise PermitRejected("grant_input_limit")
    if request.max_output_bytes > grant.max_output_bytes:
        raise PermitRejected("grant_output_limit")
    if request.transmit_bytes > grant.max_transmit_bytes:
        raise PermitRejected("grant_transmit_limit")
    if not issued_at_ms < expires_at_ms:
        raise PermitRejected("permit_window")
    if expires_at_ms > min(grant.expires_at_ms, request.deadline_ms):
        raise PermitRejected("permit_expiry_scope")
    row = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "permit_id": _canonical_uuid(permit_id, "permit_id"),
        "grant_id": grant.grant_id,
        "subject_id": request.subject_id,
        "request_id": request.request_id,
        "request_digest": request.digest,
        "target_kind": request.target.kind.value,
        "target_id": request.target.target_id,
        "target_epoch": request.target.epoch,
        "target_revision": request.target.revision,
        "fencing_token": request.target.fencing_token,
        "snapshot_id": request.snapshot.snapshot_id,
        "sequence": request.sequence,
        "operation": request.operation.value,
        "effects": [effect.value for effect in request.effects],
        "data_class": request.data_class.value,
        "routes": [route.value for route in routes],
        "input_bytes": request.argument_bytes,
        "output_bytes": request.max_output_bytes,
        "transmit_bytes": request.transmit_bytes,
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": expires_at_ms,
        "maximum_action_count": 1,
        "policy_digest": policy.digest,
        "authority_key_id": signer.key_id,
    }
    candidate = ControlPermit.from_dict({**row, "signature": "A" * 86})
    signed = {**candidate.unsigned_dict(), "signature": signer.sign("control_permit", candidate.unsigned_dict())}
    return ControlPermit.from_dict(signed)


def verify_envelope_authority(
    envelope: ControlEnvelope,
    verifier: ControlVerifier,
    policy: ControlPolicy,
    *,
    now_ms: int,
    live_routes: Iterable[ControlRoute],
    live_snapshot: SnapshotRef | None = None,
) -> ControlRoute:
    request = envelope.request
    grant = envelope.grant
    permit = envelope.permit
    verify_grant(
        grant,
        verifier,
        policy,
        now_ms=now_ms,
        subject_id=request.subject_id,
    )
    if permit.authority_key_id != verifier.key_id:
        raise AuthorityRejected("permit_key")
    verifier.verify("control_permit", permit.unsigned_dict(), permit.signature)
    policy.validate_request(request, now_ms=now_ms, live_snapshot=live_snapshot)
    checks = (
        (permit.grant_id == grant.grant_id, "permit_grant"),
        (permit.subject_id == request.subject_id, "permit_subject"),
        (permit.request_id == request.request_id, "permit_request"),
        (permit.request_digest == request.digest, "permit_request_digest"),
        (permit.target_kind == request.target.kind, "permit_target_kind"),
        (permit.target_id == request.target.target_id, "permit_target"),
        (permit.target_epoch == request.target.epoch, "permit_epoch"),
        (permit.target_revision == request.target.revision, "permit_revision"),
        (permit.fencing_token == request.target.fencing_token, "permit_fence"),
        (permit.snapshot_id == request.snapshot.snapshot_id, "permit_snapshot"),
        (permit.sequence == request.sequence, "permit_sequence"),
        (permit.operation == request.operation, "permit_operation"),
        (permit.effects == request.effects, "permit_effects"),
        (permit.data_class == request.data_class, "permit_data_class"),
        (permit.input_bytes == request.argument_bytes, "permit_input"),
        (permit.output_bytes == request.max_output_bytes, "permit_output"),
        (permit.transmit_bytes == request.transmit_bytes, "permit_transmit"),
        (permit.policy_digest == policy.digest, "permit_policy"),
        (permit.maximum_action_count == 1, "permit_action_count"),
        (permit.issued_at_ms >= grant.issued_at_ms, "permit_issued_scope"),
        (permit.expires_at_ms <= min(grant.expires_at_ms, request.deadline_ms), "permit_expiry_scope"),
        (all(route in request.requested_routes and route in grant.routes for route in permit.routes), "permit_routes"),
        (request.target.target_id in grant.target_ids, "grant_target"),
        (request.target.kind in grant.target_kinds, "grant_target_kind"),
        (request.operation in grant.operations, "grant_operation"),
        (all(effect in grant.effects for effect in request.effects), "grant_effects"),
        (request.data_class in grant.data_classes, "grant_data_class"),
        (request.argument_bytes <= grant.max_input_bytes, "grant_input_limit"),
        (request.max_output_bytes <= grant.max_output_bytes, "grant_output_limit"),
        (request.transmit_bytes <= grant.max_transmit_bytes, "grant_transmit_limit"),
    )
    for accepted, reason in checks:
        if not accepted:
            raise PermitRejected(reason)
    if now_ms < permit.issued_at_ms:
        raise PermitRejected("permit_not_yet_valid")
    if now_ms >= permit.expires_at_ms:
        raise PermitRejected("permit_expired")
    return policy.select_route(
        request,
        grant_routes=grant.routes,
        permit_routes=permit.routes,
        live_routes=live_routes,
    )


def _closed_object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }


def _json_schema_pattern(pattern: str) -> str:
    """Convert Python full-line patterns to an absolute ECMA-262 end guard."""

    if not pattern.startswith("^") or not pattern.endswith("$"):
        raise RuntimeError("schema_pattern")
    return pattern[:-1] + r"(?![\s\S])"


TARGET_REF_SCHEMA = _closed_object_schema(
    {
        "kind": {"enum": [item.value for item in TargetKind]},
        "target_id": {"type": "string", "pattern": _json_schema_pattern(_OPAQUE_ID_RE.pattern)},
        "epoch": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
        "revision": {"type": "string", "pattern": _json_schema_pattern(_REVISION_RE.pattern)},
        "fencing_token": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
    }
)
_UUID_SCHEMA = {
    "type": "string",
    "pattern": _json_schema_pattern(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
}
SNAPSHOT_REF_SCHEMA = _closed_object_schema(
    {
        "snapshot_id": _UUID_SCHEMA,
        "target_id": {"type": "string", "pattern": _json_schema_pattern(_OPAQUE_ID_RE.pattern)},
        "epoch": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
        "revision": {"type": "string", "pattern": _json_schema_pattern(_REVISION_RE.pattern)},
        "fencing_token": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
        "observed_at_ms": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER},
        "sequence": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
    }
)
_OPAQUE_SCHEMA = {"type": "string", "pattern": _json_schema_pattern(_OPAQUE_ID_RE.pattern)}
_SAFE_ID_SCHEMA = {"type": "string", "pattern": _json_schema_pattern(_SAFE_ID_RE.pattern)}
_REVISION_SCHEMA = {"type": "string", "pattern": _json_schema_pattern(_REVISION_RE.pattern)}
_DIGEST_SCHEMA = {"type": "string", "pattern": _json_schema_pattern(_DIGEST_RE.pattern)}
_KEY_ID_SCHEMA = {"type": "string", "pattern": _json_schema_pattern(_KEY_ID_RE.pattern)}
_SIGNATURE_SCHEMA = {"type": "string", "pattern": _json_schema_pattern(_SIGNATURE_RE.pattern)}
_ROUTE_ARRAY_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": len(ROUTE_ORDER),
    "uniqueItems": True,
    "items": {"enum": [item.value for item in ROUTE_ORDER]},
}

ARGUMENT_SCHEMAS: dict[str, dict[str, Any]] = {
    Operation.OBSERVE.value: _closed_object_schema({}),
    Operation.ACTIVATE.value: _closed_object_schema({"element_id": _OPAQUE_SCHEMA}),
    Operation.INPUT_TEXT.value: _closed_object_schema(
        {
            "element_id": _OPAQUE_SCHEMA,
            "replace": {"type": "boolean"},
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_BYTES},
        }
    ),
    Operation.SELECT_OPTION.value: _closed_object_schema({"element_id": _OPAQUE_SCHEMA, "option_id": _OPAQUE_SCHEMA}),
    Operation.SCROLL.value: {
        **_closed_object_schema(
            {
                "delta_x": {
                    "type": "integer",
                    "minimum": -MAX_SCROLL_DELTA,
                    "maximum": MAX_SCROLL_DELTA,
                },
                "delta_y": {
                    "type": "integer",
                    "minimum": -MAX_SCROLL_DELTA,
                    "maximum": MAX_SCROLL_DELTA,
                },
                "element_id": _OPAQUE_SCHEMA,
            }
        ),
        "not": {
            "required": ["delta_x", "delta_y"],
            "properties": {"delta_x": {"const": 0}, "delta_y": {"const": 0}},
        },
    },
    Operation.UPLOAD.value: _closed_object_schema(
        {
            "artifact_id": _UUID_SCHEMA,
            "byte_count": {"type": "integer", "minimum": 1, "maximum": MAX_UPLOAD_BYTES},
            "element_id": _OPAQUE_SCHEMA,
        }
    ),
    Operation.COORDINATE_ACTIVATE.value: _closed_object_schema(
        {
            "viewport_height": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_VIEWPORT_DIMENSION,
            },
            "viewport_width": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_VIEWPORT_DIMENSION,
            },
            "x": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_VIEWPORT_DIMENSION - 1,
            },
            "y": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_VIEWPORT_DIMENSION - 1,
            },
        }
    ),
    Operation.HANDOFF.value: _closed_object_schema({"reason_code": _SAFE_ID_SCHEMA}),
}

_CONTROL_REQUEST_PROPERTIES: dict[str, Any] = {
    "schema_version": {"const": CONTROL_SCHEMA_VERSION},
    "request_id": _UUID_SCHEMA,
    "session_id": _UUID_SCHEMA,
    "subject_id": _SAFE_ID_SCHEMA,
    "sequence": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
    "issued_at_ms": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER},
    "deadline_ms": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
    "target": TARGET_REF_SCHEMA,
    "snapshot": SNAPSHOT_REF_SCHEMA,
    "operation": {"enum": [item.value for item in Operation]},
    "data_class": {"enum": [item.value for item in ControlDataClass]},
    "arguments": {"oneOf": list(ARGUMENT_SCHEMAS.values())},
    "requested_routes": _ROUTE_ARRAY_SCHEMA,
    "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_BYTES},
}
CONTROL_REQUEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **_closed_object_schema(_CONTROL_REQUEST_PROPERTIES),
    "oneOf": [
        {
            "required": ["operation", "data_class", "arguments"],
            "properties": {
                "operation": {"const": operation.value},
                "data_class": {"enum": sorted(item.value for item in spec.data_classes)},
                "arguments": ARGUMENT_SCHEMAS[operation.value],
            },
        }
        for operation, spec in OPERATION_SPECS.items()
    ],
}

_PREPARATION_ARGUMENT_SCHEMAS: dict[tuple[Operation, ControlRoute], dict[str, Any]] = {
    (Operation.ACTIVATE, ControlRoute.AX): _closed_object_schema({}),
    (Operation.SELECT_OPTION, ControlRoute.AX): _closed_object_schema(
        {"option_id": _OPAQUE_SCHEMA}
    ),
    (Operation.SCROLL, ControlRoute.AX): {
        **_closed_object_schema(
            {
                "delta_x": {
                    "type": "integer",
                    "minimum": -MAX_SCROLL_DELTA,
                    "maximum": MAX_SCROLL_DELTA,
                },
                "delta_y": {
                    "type": "integer",
                    "minimum": -MAX_SCROLL_DELTA,
                    "maximum": MAX_SCROLL_DELTA,
                },
            }
        ),
        "not": {
            "required": ["delta_x", "delta_y"],
            "properties": {"delta_x": {"const": 0}, "delta_y": {"const": 0}},
        },
    },
    (Operation.ACTIVATE, ControlRoute.APPLE_EVENT): _closed_object_schema({}),
    (Operation.ACTIVATE, ControlRoute.SHORTCUT): _closed_object_schema({}),
    (Operation.COORDINATE_ACTIVATE, ControlRoute.COORDINATE): _closed_object_schema(
        {
            "x": {"type": "integer", "minimum": 0, "maximum": MAX_VIEWPORT_DIMENSION - 1},
            "y": {"type": "integer", "minimum": 0, "maximum": MAX_VIEWPORT_DIMENSION - 1},
        }
    ),
    (Operation.OBSERVE, ControlRoute.SCREENSHOT): _closed_object_schema({}),
}

_CONTROL_PREPARATION_PROPERTIES: dict[str, Any] = {
    "schema_version": {"const": CONTROL_SCHEMA_VERSION},
    "preparation_id": _UUID_SCHEMA,
    "request_id": _UUID_SCHEMA,
    "subject_id": _SAFE_ID_SCHEMA,
    "operation": {"enum": [item.value for item in Operation]},
    "data_class": {"enum": [item.value for item in ControlDataClass]},
    "route": {"enum": [item.value for item in ControlRoute]},
    "selector": _SAFE_ID_SCHEMA,
    # The route/operation branch below supplies the exact closed argument
    # object. A top-level oneOf would be ambiguous because several reviewed
    # routes intentionally use the same empty argument shape.
    "arguments": {},
    "issued_at_ms": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER},
    "expires_at_ms": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
    "policy_digest": _DIGEST_SCHEMA,
    "authority_key_id": _KEY_ID_SCHEMA,
    "signature": _SIGNATURE_SCHEMA,
}
CONTROL_PREPARATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **_closed_object_schema(_CONTROL_PREPARATION_PROPERTIES),
    "oneOf": [
        {
            "required": ["operation", "data_class", "route", "selector", "arguments"],
            "properties": {
                "operation": {"const": operation.value},
                "data_class": {
                    "enum": sorted(item.value for item in OPERATION_SPECS[operation].data_classes)
                },
                "route": {"const": route.value},
                "selector": {"enum": sorted(PREPARATION_SELECTORS[(operation, route)])},
                "arguments": _PREPARATION_ARGUMENT_SCHEMAS[(operation, route)],
            },
        }
        for operation, route in PREPARATION_SELECTORS
    ],
}
CONTROL_PREPARATION_ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **_closed_object_schema(
        {
            "protocol_version": {"const": CONTROL_PROTOCOL_VERSION},
            "message_type": {"const": CONTROL_PREPARATION_MESSAGE_TYPE},
            "preparation": CONTROL_PREPARATION_SCHEMA,
        }
    ),
}

CONTROL_GRANT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **_closed_object_schema(
        {
            "schema_version": {"const": CONTROL_SCHEMA_VERSION},
            "grant_id": _UUID_SCHEMA,
            "subject_id": _SAFE_ID_SCHEMA,
            "target_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 16,
                "uniqueItems": True,
                "items": _OPAQUE_SCHEMA,
            },
            "target_kinds": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(TargetKind),
                "uniqueItems": True,
                "items": {"enum": [item.value for item in TargetKind]},
            },
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(Operation),
                "uniqueItems": True,
                "items": {"enum": [item.value for item in Operation]},
            },
            "effects": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(Effect),
                "uniqueItems": True,
                "items": {"enum": [item.value for item in Effect]},
            },
            "data_classes": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(ControlDataClass),
                "uniqueItems": True,
                "items": {"enum": [item.value for item in ControlDataClass]},
            },
            "routes": _ROUTE_ARRAY_SCHEMA,
            "issued_at_ms": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER},
            "expires_at_ms": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "maximum_action_count": {"type": "integer", "minimum": 1, "maximum": MAX_ACTION_COUNT},
            "max_input_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_STRING_BYTES},
            "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_BYTES},
            "max_transmit_bytes": {"type": "integer", "minimum": 0, "maximum": MAX_UPLOAD_BYTES},
            "policy_digest": _DIGEST_SCHEMA,
            "authority_key_id": _KEY_ID_SCHEMA,
            "signature": _SIGNATURE_SCHEMA,
        }
    ),
}

CONTROL_PERMIT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **_closed_object_schema(
        {
            "schema_version": {"const": CONTROL_SCHEMA_VERSION},
            "permit_id": _UUID_SCHEMA,
            "grant_id": _UUID_SCHEMA,
            "subject_id": _SAFE_ID_SCHEMA,
            "request_id": _UUID_SCHEMA,
            "request_digest": _DIGEST_SCHEMA,
            "target_kind": {"enum": [item.value for item in TargetKind]},
            "target_id": _OPAQUE_SCHEMA,
            "target_epoch": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "target_revision": _REVISION_SCHEMA,
            "fencing_token": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "snapshot_id": _UUID_SCHEMA,
            "sequence": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "operation": {"enum": [item.value for item in Operation]},
            "effects": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(Effect),
                "uniqueItems": True,
                "items": {"enum": [item.value for item in Effect]},
            },
            "data_class": {"enum": [item.value for item in ControlDataClass]},
            "routes": _ROUTE_ARRAY_SCHEMA,
            "input_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_STRING_BYTES},
            "output_bytes": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_BYTES},
            "transmit_bytes": {"type": "integer", "minimum": 0, "maximum": MAX_UPLOAD_BYTES},
            "issued_at_ms": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER},
            "expires_at_ms": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "maximum_action_count": {"const": 1},
            "policy_digest": _DIGEST_SCHEMA,
            "authority_key_id": _KEY_ID_SCHEMA,
            "signature": _SIGNATURE_SCHEMA,
        }
    ),
}

CONTROL_ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **_closed_object_schema(
        {
            "protocol_version": {"const": CONTROL_PROTOCOL_VERSION},
            "message_type": {"const": CONTROL_MESSAGE_TYPE},
            "request": CONTROL_REQUEST_SCHEMA,
            "grant": CONTROL_GRANT_SCHEMA,
            "permit": CONTROL_PERMIT_SCHEMA,
        }
    ),
}


__all__ = [
    "ARGUMENT_SCHEMAS",
    "CONTROL_ENVELOPE_SCHEMA",
    "CONTROL_GRANT_SCHEMA",
    "CONTROL_MESSAGE_TYPE",
    "CONTROL_PREPARATION_ENVELOPE_SCHEMA",
    "CONTROL_PREPARATION_MESSAGE_TYPE",
    "CONTROL_PREPARATION_SCHEMA",
    "CONTROL_PERMIT_SCHEMA",
    "CONTROL_PROTOCOL_VERSION",
    "CONTROL_REQUEST_SCHEMA",
    "CONTROL_SCHEMA_VERSION",
    "ControlDataClass",
    "ControlEnvelope",
    "ControlGrant",
    "ControlKernelError",
    "ControlPermit",
    "ControlPolicy",
    "ControlPreparation",
    "ControlPreparationEnvelope",
    "ControlRequest",
    "ControlRoute",
    "ControlSigner",
    "ControlVerifier",
    "Effect",
    "FrameDecoder",
    "FrameRejected",
    "MAX_FRAME_BYTES",
    "OPERATION_SPECS",
    "PREPARATION_SELECTORS",
    "Operation",
    "PermitRejected",
    "ROUTE_ORDER",
    "SNAPSHOT_REF_SCHEMA",
    "SchemaRejected",
    "SnapshotRef",
    "TARGET_REF_SCHEMA",
    "TargetKind",
    "TargetRef",
    "canonical_json_bytes",
    "content_digest",
    "decode_json_payload",
    "default_control_policy",
    "encode_frame",
    "issue_grant",
    "issue_control_preparation",
    "issue_permit",
    "validate_operation_arguments",
    "validate_preparation_arguments",
    "verify_control_preparation",
    "verify_envelope_authority",
    "verify_grant",
]
