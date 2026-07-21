"""Signed, finite browser target bindings for the disabled control foundation."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import hmac
import json
import re
from typing import Any, Mapping, NoReturn
import uuid


CARBON_SCHEMA_VERSION = 1
CARBON_PROTOCOL_VERSION = 1
CARBON_MAX_ACTIONS = 16
CARBON_MAX_LIFETIME_MS = 300_000

_OPAQUE_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_KEY_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")


class CarbonBindingRejected(ValueError):
    """A binding, observation, or requested action failed closed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class CarbonBrowserRoute(str, Enum):
    MANAGED_PUBLIC = "managed_public"
    SELECTED_TAB = "selected_tab"
    TRUSTED_FIXTURE = "trusted_fixture"


class CarbonBrowserOperation(str, Enum):
    OBSERVE = "observe"
    ACTIVATE = "activate"
    INPUT_TEXT = "input_text"
    SELECT_OPTION = "select_option"
    SCROLL = "scroll"
    UPLOAD = "upload"
    HANDOFF = "handoff"


class CarbonSurfaceKind(str, Enum):
    DOM = "dom"
    CANVAS = "canvas"
    PDF = "pdf"
    INTERNAL = "internal"
    AUTH = "auth"
    PASSKEY = "passkey"
    CAPTCHA = "captcha"
    SECURE_FIELD = "secure_field"
    UNKNOWN = "unknown"


class CarbonDocumentLifecycle(str, Enum):
    ACTIVE = "active"
    BFCACHE = "bfcache"
    PRERENDER = "prerender"
    DISCARDED = "discarded"


class CarbonShadowMode(str, Enum):
    NONE = "none"
    OPEN = "open"
    CLOSED = "closed"


_ELEMENT_OPERATIONS = frozenset(
    {
        CarbonBrowserOperation.ACTIVATE,
        CarbonBrowserOperation.INPUT_TEXT,
        CarbonBrowserOperation.SELECT_OPTION,
        CarbonBrowserOperation.UPLOAD,
    }
)
_SELECTED_TAB_OPERATIONS = frozenset(
    {
        CarbonBrowserOperation.OBSERVE,
        CarbonBrowserOperation.HANDOFF,
    }
)


def _reject(reason_code: str) -> NoReturn:
    raise CarbonBindingRejected(reason_code)


def _closed(row: Mapping[str, Any], fields: frozenset[str], label: str) -> None:
    if type(row) is not dict or frozenset(row) != fields:
        _reject(f"{label}_schema")


def _canonical_uuid(value: Any, label: str) -> str:
    if type(value) is not str:
        _reject(label)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        _reject(label)
    if str(parsed) != value or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        _reject(label)
    return value


def _opaque(value: Any, label: str) -> str:
    if type(value) is not str or not _OPAQUE_RE.fullmatch(value):
        _reject(label)
    return value


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value):
        _reject(label)
    return value


def _semver(value: Any, label: str) -> str:
    if type(value) is not str or not _SEMVER_RE.fullmatch(value):
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


def _enum(enum_type: type[Enum], value: Any, label: str) -> Any:
    if type(value) is not str:
        _reject(label)
    try:
        return enum_type(value)
    except ValueError:
        _reject(label)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError):
        _reject("canonical_json")
    return encoded


@dataclass(frozen=True, slots=True)
class CarbonBrowserBinding:
    schema_version: int
    binding_id: str
    route: CarbonBrowserRoute
    profile_id: str
    browser_instance_id: str
    window_id: int
    tab_id: int
    top_document_id: str
    frame_id: int
    frame_document_id: str
    origin_digest: str
    snapshot_id: str
    snapshot_revision: int
    element_token: str
    operations: tuple[CarbonBrowserOperation, ...]
    maximum_action_count: int
    actions_used: int
    issued_at_ms: int
    expires_at_ms: int
    fencing_token: int
    service_worker_generation: str
    extension_version: str
    extension_protocol: int
    native_version: str
    native_protocol: int
    user_gesture_id: str
    incognito: bool

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> CarbonBrowserBinding:
        fields = frozenset(cls.__dataclass_fields__)
        _closed(row, fields, "binding")
        route = _enum(CarbonBrowserRoute, row["route"], "route")
        raw_operations = row["operations"]
        if type(raw_operations) is not list or not raw_operations:
            _reject("operations")
        operations = tuple(
            _enum(CarbonBrowserOperation, value, "operation") for value in raw_operations
        )
        if len(set(operations)) != len(operations):
            _reject("operations")
        if route is CarbonBrowserRoute.SELECTED_TAB and not set(operations) <= _SELECTED_TAB_OPERATIONS:
            _reject("selected_tab_observe_only")

        maximum_action_count = _integer(
            row["maximum_action_count"],
            "maximum_action_count",
            1,
            CARBON_MAX_ACTIONS,
        )
        actions_used = _integer(row["actions_used"], "actions_used", 0, maximum_action_count)
        issued_at_ms = _integer(row["issued_at_ms"], "issued_at_ms", 1, (1 << 53) - 1)
        expires_at_ms = _integer(row["expires_at_ms"], "expires_at_ms", 1, (1 << 53) - 1)
        if expires_at_ms <= issued_at_ms or expires_at_ms - issued_at_ms > CARBON_MAX_LIFETIME_MS:
            _reject("binding_lifetime")

        element_token = row["element_token"]
        if element_token != "none":
            element_token = _opaque(element_token, "element_token")
        if any(operation in _ELEMENT_OPERATIONS for operation in operations) and element_token == "none":
            _reject("element_token_required")

        gesture = row["user_gesture_id"]
        if route is CarbonBrowserRoute.SELECTED_TAB:
            gesture = _canonical_uuid(gesture, "user_gesture_id")
        elif gesture != "none":
            _reject("user_gesture_unexpected")

        incognito = _boolean(row["incognito"], "incognito")
        if incognito:
            _reject("incognito_denied")

        return cls(
            schema_version=_integer(
                row["schema_version"],
                "schema_version",
                CARBON_SCHEMA_VERSION,
                CARBON_SCHEMA_VERSION,
            ),
            binding_id=_canonical_uuid(row["binding_id"], "binding_id"),
            route=route,
            profile_id=_opaque(row["profile_id"], "profile_id"),
            browser_instance_id=_canonical_uuid(row["browser_instance_id"], "browser_instance_id"),
            window_id=_integer(row["window_id"], "window_id", 0, (1 << 31) - 1),
            tab_id=_integer(row["tab_id"], "tab_id", 0, (1 << 31) - 1),
            top_document_id=_canonical_uuid(row["top_document_id"], "top_document_id"),
            frame_id=_integer(row["frame_id"], "frame_id", 0, (1 << 31) - 1),
            frame_document_id=_canonical_uuid(row["frame_document_id"], "frame_document_id"),
            origin_digest=_opaque(row["origin_digest"], "origin_digest"),
            snapshot_id=_canonical_uuid(row["snapshot_id"], "snapshot_id"),
            snapshot_revision=_integer(
                row["snapshot_revision"], "snapshot_revision", 1, (1 << 53) - 1
            ),
            element_token=element_token,
            operations=operations,
            maximum_action_count=maximum_action_count,
            actions_used=actions_used,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            fencing_token=_integer(
                row["fencing_token"], "fencing_token", 1, (1 << 53) - 1
            ),
            service_worker_generation=_canonical_uuid(
                row["service_worker_generation"], "service_worker_generation"
            ),
            extension_version=_semver(row["extension_version"], "extension_version"),
            extension_protocol=_integer(
                row["extension_protocol"], "extension_protocol", 1, 65_535
            ),
            native_version=_semver(row["native_version"], "native_version"),
            native_protocol=_integer(row["native_protocol"], "native_protocol", 1, 65_535),
            user_gesture_id=gesture,
            incognito=incognito,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "binding_id": self.binding_id,
            "route": self.route.value,
            "profile_id": self.profile_id,
            "browser_instance_id": self.browser_instance_id,
            "window_id": self.window_id,
            "tab_id": self.tab_id,
            "top_document_id": self.top_document_id,
            "frame_id": self.frame_id,
            "frame_document_id": self.frame_document_id,
            "origin_digest": self.origin_digest,
            "snapshot_id": self.snapshot_id,
            "snapshot_revision": self.snapshot_revision,
            "element_token": self.element_token,
            "operations": [operation.value for operation in self.operations],
            "maximum_action_count": self.maximum_action_count,
            "actions_used": self.actions_used,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "fencing_token": self.fencing_token,
            "service_worker_generation": self.service_worker_generation,
            "extension_version": self.extension_version,
            "extension_protocol": self.extension_protocol,
            "native_version": self.native_version,
            "native_protocol": self.native_protocol,
            "user_gesture_id": self.user_gesture_id,
            "incognito": self.incognito,
        }


@dataclass(frozen=True, slots=True)
class CarbonBrowserObservation:
    profile_id: str
    browser_instance_id: str
    window_id: int
    tab_id: int
    top_document_id: str
    frame_id: int
    frame_document_id: str
    origin_digest: str
    snapshot_id: str
    snapshot_revision: int
    element_token: str
    fencing_token: int
    service_worker_generation: str
    extension_version: str
    extension_protocol: int
    native_version: str
    native_protocol: int
    active_tab_granted: bool
    incognito: bool
    lifecycle: CarbonDocumentLifecycle
    surface_kind: CarbonSurfaceKind
    shadow_mode: CarbonShadowMode
    dialog_open: bool
    popup_count: int
    download_attempted: bool
    upload_picker_open: bool
    frame_attached: bool

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> CarbonBrowserObservation:
        _closed(row, frozenset(cls.__dataclass_fields__), "observation")
        element = row["element_token"]
        if element != "none":
            element = _opaque(element, "element_token")
        return cls(
            profile_id=_opaque(row["profile_id"], "profile_id"),
            browser_instance_id=_canonical_uuid(row["browser_instance_id"], "browser_instance_id"),
            window_id=_integer(row["window_id"], "window_id", 0, (1 << 31) - 1),
            tab_id=_integer(row["tab_id"], "tab_id", 0, (1 << 31) - 1),
            top_document_id=_canonical_uuid(row["top_document_id"], "top_document_id"),
            frame_id=_integer(row["frame_id"], "frame_id", 0, (1 << 31) - 1),
            frame_document_id=_canonical_uuid(row["frame_document_id"], "frame_document_id"),
            origin_digest=_opaque(row["origin_digest"], "origin_digest"),
            snapshot_id=_canonical_uuid(row["snapshot_id"], "snapshot_id"),
            snapshot_revision=_integer(
                row["snapshot_revision"], "snapshot_revision", 1, (1 << 53) - 1
            ),
            element_token=element,
            fencing_token=_integer(
                row["fencing_token"], "fencing_token", 1, (1 << 53) - 1
            ),
            service_worker_generation=_canonical_uuid(
                row["service_worker_generation"], "service_worker_generation"
            ),
            extension_version=_semver(row["extension_version"], "extension_version"),
            extension_protocol=_integer(
                row["extension_protocol"], "extension_protocol", 1, 65_535
            ),
            native_version=_semver(row["native_version"], "native_version"),
            native_protocol=_integer(row["native_protocol"], "native_protocol", 1, 65_535),
            active_tab_granted=_boolean(row["active_tab_granted"], "active_tab_granted"),
            incognito=_boolean(row["incognito"], "incognito"),
            lifecycle=_enum(CarbonDocumentLifecycle, row["lifecycle"], "lifecycle"),
            surface_kind=_enum(CarbonSurfaceKind, row["surface_kind"], "surface_kind"),
            shadow_mode=_enum(CarbonShadowMode, row["shadow_mode"], "shadow_mode"),
            dialog_open=_boolean(row["dialog_open"], "dialog_open"),
            popup_count=_integer(row["popup_count"], "popup_count", 0, 32),
            download_attempted=_boolean(
                row["download_attempted"], "download_attempted"
            ),
            upload_picker_open=_boolean(
                row["upload_picker_open"], "upload_picker_open"
            ),
            frame_attached=_boolean(row["frame_attached"], "frame_attached"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "browser_instance_id": self.browser_instance_id,
            "window_id": self.window_id,
            "tab_id": self.tab_id,
            "top_document_id": self.top_document_id,
            "frame_id": self.frame_id,
            "frame_document_id": self.frame_document_id,
            "origin_digest": self.origin_digest,
            "snapshot_id": self.snapshot_id,
            "snapshot_revision": self.snapshot_revision,
            "element_token": self.element_token,
            "fencing_token": self.fencing_token,
            "service_worker_generation": self.service_worker_generation,
            "extension_version": self.extension_version,
            "extension_protocol": self.extension_protocol,
            "native_version": self.native_version,
            "native_protocol": self.native_protocol,
            "active_tab_granted": self.active_tab_granted,
            "incognito": self.incognito,
            "lifecycle": self.lifecycle.value,
            "surface_kind": self.surface_kind.value,
            "shadow_mode": self.shadow_mode.value,
            "dialog_open": self.dialog_open,
            "popup_count": self.popup_count,
            "download_attempted": self.download_attempted,
            "upload_picker_open": self.upload_picker_open,
            "frame_attached": self.frame_attached,
        }


@dataclass(frozen=True, slots=True)
class CarbonSignedBinding:
    binding: CarbonBrowserBinding
    key_id: str
    signature: str

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> CarbonSignedBinding:
        _closed(row, frozenset({"binding", "key_id", "signature"}), "signed_binding")
        key_id = row["key_id"]
        signature = row["signature"]
        if type(key_id) is not str or not _KEY_ID_RE.fullmatch(key_id):
            _reject("key_id")
        if type(signature) is not str or not _SIGNATURE_RE.fullmatch(signature):
            _reject("signature")
        return cls(CarbonBrowserBinding.from_dict(row["binding"]), key_id, signature)

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "key_id": self.key_id,
            "signature": self.signature,
        }


class CarbonBindingAuthority:
    """HMAC authority intended for a future Keychain-backed native broker."""

    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) < 32:
            _reject("authority_key")
        self._key = key
        self.key_id = "hmac-sha256:" + hashlib.sha256(key).hexdigest()

    def sign(self, binding: CarbonBrowserBinding) -> CarbonSignedBinding:
        if type(binding) is not CarbonBrowserBinding:
            _reject("binding_type")
        binding = CarbonBrowserBinding.from_dict(binding.to_dict())
        digest = hmac.new(self._key, _canonical_bytes(binding.to_dict()), hashlib.sha256).digest()
        signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return CarbonSignedBinding(binding, self.key_id, signature)

    def verify(self, signed: CarbonSignedBinding) -> CarbonBrowserBinding:
        if type(signed) is not CarbonSignedBinding or signed.key_id != self.key_id:
            _reject("binding_authority")
        binding = CarbonBrowserBinding.from_dict(signed.binding.to_dict())
        try:
            decoded = base64.b64decode(signed.signature + "=", altchars=b"-_", validate=True)
        except (ValueError, TypeError):
            _reject("signature")
        canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
        if canonical != signed.signature:
            _reject("signature")
        expected = hmac.new(
            self._key,
            _canonical_bytes(binding.to_dict()),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(decoded, expected):
            _reject("binding_signature")
        return binding


def validate_browser_action(
    binding: CarbonBrowserBinding,
    observation: CarbonBrowserObservation,
    operation: CarbonBrowserOperation,
    *,
    now_ms: int,
    expected_fencing_token: int,
    element_token: str = "none",
) -> CarbonBrowserBinding:
    """Validate a fresh observation and atomically consume one logical action."""

    if type(binding) is not CarbonBrowserBinding or type(observation) is not CarbonBrowserObservation:
        _reject("binding_type")
    binding = CarbonBrowserBinding.from_dict(binding.to_dict())
    observation = CarbonBrowserObservation.from_dict(observation.to_dict())
    if type(operation) is not CarbonBrowserOperation:
        _reject("operation")
    now = _integer(now_ms, "now_ms", 1, (1 << 53) - 1)
    fence = _integer(expected_fencing_token, "expected_fencing_token", 1, (1 << 53) - 1)
    if now < binding.issued_at_ms:
        _reject("clock_regression")
    if now >= binding.expires_at_ms:
        _reject("binding_expired")
    if binding.actions_used >= binding.maximum_action_count:
        _reject("action_count_exhausted")
    if operation not in binding.operations:
        _reject("operation_not_granted")
    if fence != binding.fencing_token or observation.fencing_token != binding.fencing_token:
        _reject("fencing_token_changed")

    exact_fields = (
        "profile_id",
        "browser_instance_id",
        "window_id",
        "tab_id",
        "top_document_id",
        "frame_id",
        "frame_document_id",
        "origin_digest",
        "snapshot_id",
        "snapshot_revision",
        "service_worker_generation",
        "extension_version",
        "extension_protocol",
        "native_version",
        "native_protocol",
        "incognito",
    )
    for field_name in exact_fields:
        if getattr(binding, field_name) != getattr(observation, field_name):
            _reject(f"{field_name}_changed")

    if binding.incognito or observation.incognito:
        _reject("incognito_denied")
    if not observation.frame_attached:
        _reject("frame_detached")
    if observation.lifecycle is not CarbonDocumentLifecycle.ACTIVE:
        _reject("document_not_active")
    if binding.route is CarbonBrowserRoute.SELECTED_TAB and not observation.active_tab_granted:
        _reject("active_tab_revoked")

    if operation is CarbonBrowserOperation.HANDOFF:
        return replace(binding, actions_used=binding.actions_used + 1)

    if observation.dialog_open:
        _reject("dialog_handoff")
    if observation.popup_count:
        _reject("popup_handoff")
    if observation.download_attempted:
        _reject("download_denied")
    if observation.upload_picker_open:
        _reject("upload_selection_unconfirmed")
    if observation.surface_kind is not CarbonSurfaceKind.DOM:
        _reject("surface_handoff")
    if observation.shadow_mode is CarbonShadowMode.CLOSED:
        _reject("closed_shadow_handoff")

    if operation in _ELEMENT_OPERATIONS:
        requested_token = _opaque(element_token, "element_token")
        if (
            requested_token != binding.element_token
            or observation.element_token != binding.element_token
        ):
            _reject("element_token_changed")
    elif element_token != "none":
        _reject("element_token_unexpected")

    if binding.route is CarbonBrowserRoute.SELECTED_TAB and operation not in _SELECTED_TAB_OPERATIONS:
        _reject("selected_tab_observe_only")
    return replace(binding, actions_used=binding.actions_used + 1)


__all__ = [
    "CARBON_MAX_ACTIONS",
    "CARBON_MAX_LIFETIME_MS",
    "CARBON_PROTOCOL_VERSION",
    "CARBON_SCHEMA_VERSION",
    "CarbonBindingAuthority",
    "CarbonBindingRejected",
    "CarbonBrowserBinding",
    "CarbonBrowserObservation",
    "CarbonBrowserOperation",
    "CarbonBrowserRoute",
    "CarbonDocumentLifecycle",
    "CarbonShadowMode",
    "CarbonSignedBinding",
    "CarbonSurfaceKind",
    "validate_browser_action",
]
