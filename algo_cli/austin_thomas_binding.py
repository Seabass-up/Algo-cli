"""Strict consumer for Austin's opaque native preparation reply.

This disabled foundation never discovers or invents a desktop target in
Python. It accepts only the exact canonical structural reply cross-bound to a
previously verified Samuel preparation, then constructs the one allowed future
ControlRequest from that binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from .david_control_kernel import (
    MAX_SAFE_INTEGER,
    ControlDataClass,
    ControlPreparation,
    ControlRequest,
    ControlRoute,
    Operation,
    SnapshotRef,
    TargetKind,
    TargetRef,
    canonical_json_bytes,
    decode_json_payload,
    validate_operation_arguments,
)


class AustinBindingRejected(RuntimeError):
    """A content-free native-binding response rejection."""


_MAXIMUM_BINDING_LIFETIME_MS: dict[ControlRoute, int] = {
    ControlRoute.AX: 5_000,
    ControlRoute.APPLE_EVENT: 5_000,
    ControlRoute.SHORTCUT: 5_000,
    ControlRoute.SCREENSHOT: 3_000,
    ControlRoute.COORDINATE: 2_000,
}


def _reject(reason: str) -> NoReturn:
    raise AustinBindingRejected(reason)


def _exact_dict(value: Any, fields: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(fields):
        _reject(f"{label}_schema")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(label)
    return value


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8", errors="strict")) > 128:
        _reject(label)
    return value


def _validate_prepared_arguments(
    preparation: ControlPreparation,
    arguments: dict[str, Any],
) -> None:
    """Cross-bind every signed partial argument to Thomas's final arguments."""

    if any(arguments.get(key) != value for key, value in preparation.arguments.items()):
        _reject("binding_arguments")


@dataclass(frozen=True, slots=True)
class AustinPreparedBinding:
    preparation_id: str
    preparation_digest: str
    request_id: str
    subject_id: str
    operation: Operation
    data_class: ControlDataClass
    route: ControlRoute
    target: TargetRef
    snapshot: SnapshotRef
    arguments: Mapping[str, Any]
    observed_at_ms: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        validated = validate_operation_arguments(self.operation, dict(self.arguments))
        object.__setattr__(self, "arguments", MappingProxyType(validated))

    @classmethod
    def from_payload(
        cls,
        preparation: ControlPreparation,
        payload: bytes,
        *,
        now_ms: int,
    ) -> "AustinPreparedBinding":
        if type(preparation) is not ControlPreparation:
            raise TypeError("preparation must be ControlPreparation")
        now = _integer(now_ms, "binding_clock")
        decoded = decode_json_payload(payload)
        if canonical_json_bytes(decoded) != payload:
            _reject("binding_canonical")
        root = _exact_dict(
            decoded,
            (
                "arguments",
                "binding_expires_at_ms",
                "data_class",
                "operation",
                "preparation_id",
                "protocol_version",
                "reason_code",
                "request_id",
                "route",
                "snapshot",
                "status",
                "target",
            ),
            "binding_reply",
        )
        if root["protocol_version"] != 1 or type(root["protocol_version"]) is not int:
            _reject("protocol_version")
        if root["status"] != "succeeded" or root["reason_code"] != "binding_prepared":
            _reject("binding_status")
        checks = (
            (root["preparation_id"] == preparation.preparation_id, "binding_preparation"),
            (root["request_id"] == preparation.request_id, "binding_request"),
            (root["operation"] == preparation.operation.value, "binding_operation"),
            (root["data_class"] == preparation.data_class.value, "binding_data_class"),
            (root["route"] == preparation.route.value, "binding_route"),
        )
        for accepted, reason in checks:
            if not accepted:
                _reject(reason)

        target = TargetRef.from_dict(root["target"])
        if target.kind is not TargetKind.DESKTOP_SURFACE:
            _reject("binding_target_kind")
        snapshot = SnapshotRef.from_dict(root["snapshot"])
        if not snapshot.matches_target(target):
            _reject("binding_snapshot_target")
        expires = _integer(
            root["binding_expires_at_ms"],
            "binding_expires",
            minimum=1,
        )
        maximum_lifetime = _MAXIMUM_BINDING_LIFETIME_MS.get(preparation.route)
        if maximum_lifetime is None:
            _reject("binding_route")
        if (
            now < preparation.issued_at_ms
            or now >= preparation.expires_at_ms
            or snapshot.observed_at_ms < preparation.issued_at_ms
            or snapshot.observed_at_ms > now + 2_000
            or now >= expires
            or expires > preparation.expires_at_ms
            or not snapshot.observed_at_ms < expires
            or expires - snapshot.observed_at_ms > maximum_lifetime
        ):
            _reject("binding_window")
        arguments = validate_operation_arguments(preparation.operation, root["arguments"])
        _validate_prepared_arguments(preparation, arguments)
        return cls(
            preparation_id=preparation.preparation_id,
            preparation_digest=preparation.digest,
            request_id=preparation.request_id,
            subject_id=preparation.subject_id,
            operation=preparation.operation,
            data_class=preparation.data_class,
            route=preparation.route,
            target=target,
            snapshot=snapshot,
            arguments=arguments,
            observed_at_ms=snapshot.observed_at_ms,
            expires_at_ms=expires,
        )

    def control_request(
        self,
        preparation: ControlPreparation,
        *,
        session_id: str,
        sequence: int,
        issued_at_ms: int,
        deadline_ms: int,
        max_output_bytes: int,
    ) -> ControlRequest:
        if type(preparation) is not ControlPreparation:
            raise TypeError("preparation must be ControlPreparation")
        if (
            preparation.preparation_id != self.preparation_id
            or preparation.digest != self.preparation_digest
            or preparation.request_id != self.request_id
            or preparation.subject_id != self.subject_id
            or preparation.operation is not self.operation
            or preparation.data_class is not self.data_class
            or preparation.route is not self.route
        ):
            _reject("binding_preparation_changed")
        issued = _integer(issued_at_ms, "binding_request_issued")
        deadline = _integer(deadline_ms, "binding_request_deadline", minimum=1)
        if (
            issued < self.observed_at_ms
            or issued >= self.expires_at_ms
            or deadline <= issued
            or deadline > self.expires_at_ms
        ):
            _reject("binding_request_window")
        return ControlRequest.from_dict(
            {
                "schema_version": 1,
                "request_id": self.request_id,
                "session_id": _text(session_id, "binding_session_id"),
                "subject_id": self.subject_id,
                "sequence": sequence,
                "issued_at_ms": issued,
                "deadline_ms": deadline,
                "target": self.target.to_dict(),
                "snapshot": self.snapshot.to_dict(),
                "operation": self.operation.value,
                "data_class": self.data_class.value,
                "arguments": dict(self.arguments),
                "requested_routes": [self.route.value],
                "max_output_bytes": max_output_bytes,
            }
        )


__all__ = ["AustinBindingRejected", "AustinPreparedBinding"]
