"""Fail-closed readiness states for disabled browser and computer control.

This module reports evidence; it does not install, pair, grant, or activate a
control surface. A component can be called ready only when the exact mandatory
checks for its surface are present and independently pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping, NoReturn


READINESS_SCHEMA_VERSION = 1
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")


class ReadinessEvidenceRejected(ValueError):
    """A readiness report was incomplete, contradictory, or malformed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ControlComponent(str, Enum):
    MACOS_NATIVE = "macos_native"
    CHROME_SELECTED_TAB = "chrome_selected_tab"
    MANAGED_BROWSER = "managed_browser"


class ControlReadinessState(str, Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLED_UNPAIRED = "installed_unpaired"
    PAIRED_MISSING_PERMISSIONS = "paired_missing_permissions"
    READY_IDLE = "ready_idle"
    CONNECTED_NO_GRANT = "connected_no_grant"
    ACTIVE = "active"
    DEGRADED = "degraded"
    VERSION_MISMATCH = "version_mismatch"


class CheckCategory(str, Enum):
    IDENTITY = "identity"
    PERMISSION = "permission"
    AVAILABILITY = "availability"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class PairingState(str, Enum):
    UNPAIRED = "unpaired"
    PAIRED = "paired"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"


class GrantState(str, Enum):
    NONE = "none"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ProtocolState(str, Enum):
    UNKNOWN = "unknown"
    COMPATIBLE = "compatible"
    VERSION_MISMATCH = "version_mismatch"


@dataclass(frozen=True, slots=True)
class CheckSpec:
    category: CheckCategory
    required: bool = True


CHECK_SPECS: Mapping[ControlComponent, Mapping[str, CheckSpec]] = {
    ControlComponent.MACOS_NATIVE: {
        "install_path": CheckSpec(CheckCategory.IDENTITY),
        "bundle_signature": CheckSpec(CheckCategory.IDENTITY),
        "team_identity": CheckSpec(CheckCategory.IDENTITY),
        "designated_requirement": CheckSpec(CheckCategory.IDENTITY),
        "hardened_runtime": CheckSpec(CheckCategory.IDENTITY),
        "gatekeeper": CheckSpec(CheckCategory.IDENTITY),
        "notarization": CheckSpec(CheckCategory.IDENTITY),
        "entitlement_allowlist": CheckSpec(CheckCategory.IDENTITY),
        "launch_agent": CheckSpec(CheckCategory.IDENTITY),
        "xpc_peer_identity": CheckSpec(CheckCategory.IDENTITY),
        "accessibility_permission": CheckSpec(CheckCategory.PERMISSION),
        "screen_recording_permission": CheckSpec(CheckCategory.PERMISSION),
        "post_event_permission": CheckSpec(CheckCategory.PERMISSION),
        "apple_events_permission": CheckSpec(CheckCategory.PERMISSION),
        "xpc_connection": CheckSpec(CheckCategory.AVAILABILITY),
        "screen_capture_picker": CheckSpec(CheckCategory.AVAILABILITY),
        "dispatcher_enabled": CheckSpec(CheckCategory.AVAILABILITY),
    },
    ControlComponent.CHROME_SELECTED_TAB: {
        "extension_manifest": CheckSpec(CheckCategory.IDENTITY),
        "extension_installed": CheckSpec(CheckCategory.IDENTITY),
        "native_host_manifest": CheckSpec(CheckCategory.IDENTITY),
        "native_host_signature": CheckSpec(CheckCategory.IDENTITY),
        "native_host_path": CheckSpec(CheckCategory.IDENTITY),
        "allowed_origin": CheckSpec(CheckCategory.IDENTITY),
        "active_tab_permission": CheckSpec(CheckCategory.PERMISSION),
        "extension_live": CheckSpec(CheckCategory.AVAILABILITY),
        "native_host_connection": CheckSpec(CheckCategory.AVAILABILITY),
    },
    ControlComponent.MANAGED_BROWSER: {
        "browser_image_digest": CheckSpec(CheckCategory.IDENTITY),
        "browser_security_freshness": CheckSpec(CheckCategory.IDENTITY),
        "container_isolation": CheckSpec(CheckCategory.IDENTITY),
        "egress_broker": CheckSpec(CheckCategory.IDENTITY),
        "debug_transport": CheckSpec(CheckCategory.IDENTITY),
        "browser_process": CheckSpec(CheckCategory.AVAILABILITY),
        "broker_process": CheckSpec(CheckCategory.AVAILABILITY),
    },
}


def _reject(reason_code: str) -> NoReturn:
    raise ReadinessEvidenceRejected(reason_code)


def _enum(enum_type: type[Enum], value: Any, label: str) -> Any:
    if type(value) is not str:
        _reject(label)
    try:
        return enum_type(value)
    except ValueError:
        _reject(label)


def _safe_code(value: Any, label: str) -> str:
    if type(value) is not str or not _SAFE_CODE_RE.fullmatch(value):
        _reject(label)
    return value


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    name: str
    category: CheckCategory
    status: CheckStatus
    reason_code: str

    def __post_init__(self) -> None:
        _safe_code(self.name, "check_name")
        if type(self.category) is not CheckCategory:
            _reject("check_category")
        if type(self.status) is not CheckStatus:
            _reject("check_status")
        _safe_code(self.reason_code, "check_reason")
        if self.status is CheckStatus.PASS and self.reason_code != "verified":
            _reject("check_reason")
        if self.status is not CheckStatus.PASS and self.reason_code == "verified":
            _reject("check_reason")

    @classmethod
    def from_dict(cls, value: Any) -> ReadinessCheck:
        if type(value) is not dict or set(value) != {
            "category",
            "name",
            "reason_code",
            "status",
        }:
            _reject("check_schema")
        return cls(
            name=_safe_code(value["name"], "check_name"),
            category=_enum(CheckCategory, value["category"], "check_category"),
            status=_enum(CheckStatus, value["status"], "check_status"),
            reason_code=_safe_code(value["reason_code"], "check_reason"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "category": self.category.value,
            "status": self.status.value,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ControlReadinessReport:
    component: ControlComponent
    installed: bool
    pairing: PairingState
    connection: ConnectionState
    grant: GrantState
    protocol: ProtocolState
    checks: tuple[ReadinessCheck, ...]
    schema_version: int = READINESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.component) is not ControlComponent:
            _reject("component")
        if type(self.installed) is not bool:
            _reject("installed")
        if type(self.pairing) is not PairingState:
            _reject("pairing")
        if type(self.connection) is not ConnectionState:
            _reject("connection")
        if type(self.grant) is not GrantState:
            _reject("grant")
        if type(self.protocol) is not ProtocolState:
            _reject("protocol")
        if type(self.checks) is not tuple or not all(
            type(check) is ReadinessCheck for check in self.checks
        ):
            _reject("checks")
        if type(self.schema_version) is not int or self.schema_version != READINESS_SCHEMA_VERSION:
            _reject("schema_version")

        specs = CHECK_SPECS[self.component]
        names = tuple(check.name for check in self.checks)
        if names != tuple(sorted(specs)) or len(set(names)) != len(names):
            _reject("checks_complete")
        for check in self.checks:
            spec = specs[check.name]
            if check.category is not spec.category:
                _reject("check_category_binding")
            if spec.required and check.status is CheckStatus.NOT_APPLICABLE:
                _reject("check_required")

        if not self.installed and (
            self.pairing is not PairingState.UNPAIRED
            or self.connection is not ConnectionState.DISCONNECTED
            or self.grant is not GrantState.NONE
            or self.protocol is not ProtocolState.UNKNOWN
        ):
            _reject("not_installed_state")
        if self.pairing is PairingState.UNPAIRED and (
            self.connection is ConnectionState.CONNECTED or self.grant is not GrantState.NONE
        ):
            _reject("unpaired_state")
        if self.connection is ConnectionState.DISCONNECTED and self.grant is GrantState.ACTIVE:
            _reject("active_without_connection")

    @classmethod
    def from_dict(cls, value: Any) -> ControlReadinessReport:
        fields = {
            "checks",
            "component",
            "connection",
            "grant",
            "installed",
            "pairing",
            "protocol",
            "schema_version",
            "state",
        }
        if type(value) is not dict or set(value) != fields:
            _reject("report_schema")
        raw_checks = value["checks"]
        if type(raw_checks) is not list:
            _reject("checks")
        report = cls(
            component=_enum(ControlComponent, value["component"], "component"),
            installed=value["installed"],
            pairing=_enum(PairingState, value["pairing"], "pairing"),
            connection=_enum(ConnectionState, value["connection"], "connection"),
            grant=_enum(GrantState, value["grant"], "grant"),
            protocol=_enum(ProtocolState, value["protocol"], "protocol"),
            checks=tuple(ReadinessCheck.from_dict(check) for check in raw_checks),
            schema_version=value["schema_version"],
        )
        if value["state"] != report.state.value:
            _reject("derived_state")
        return report

    @property
    def state(self) -> ControlReadinessState:
        if not self.installed:
            return ControlReadinessState.NOT_INSTALLED
        if self.protocol is ProtocolState.VERSION_MISMATCH:
            return ControlReadinessState.VERSION_MISMATCH

        by_category = {
            category: tuple(check for check in self.checks if check.category is category)
            for category in CheckCategory
        }
        if any(check.status is not CheckStatus.PASS for check in by_category[CheckCategory.IDENTITY]):
            return ControlReadinessState.DEGRADED
        if self.pairing is PairingState.UNPAIRED:
            return ControlReadinessState.INSTALLED_UNPAIRED
        if any(check.status is not CheckStatus.PASS for check in by_category[CheckCategory.PERMISSION]):
            return ControlReadinessState.PAIRED_MISSING_PERMISSIONS
        if self.protocol is not ProtocolState.COMPATIBLE:
            return ControlReadinessState.DEGRADED
        if self.connection is ConnectionState.DISCONNECTED:
            return ControlReadinessState.READY_IDLE
        if any(
            check.status is not CheckStatus.PASS
            for check in by_category[CheckCategory.AVAILABILITY]
        ):
            return ControlReadinessState.DEGRADED
        if self.grant is GrantState.ACTIVE:
            return ControlReadinessState.ACTIVE
        return ControlReadinessState.CONNECTED_NO_GRANT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "component": self.component.value,
            "installed": self.installed,
            "pairing": self.pairing.value,
            "connection": self.connection.value,
            "grant": self.grant.value,
            "protocol": self.protocol.value,
            "state": self.state.value,
            "checks": [check.to_dict() for check in self.checks],
        }


def make_checks(
    component: ControlComponent,
    statuses: Mapping[str, tuple[CheckStatus, str]],
) -> tuple[ReadinessCheck, ...]:
    """Build an exact check set; omitted or extra checks fail closed."""

    if type(component) is not ControlComponent or type(statuses) is not dict:
        _reject("checks")
    specs = CHECK_SPECS[component]
    if set(statuses) != set(specs):
        _reject("checks_complete")
    checks = []
    for name in sorted(specs):
        value = statuses[name]
        if type(value) is not tuple or len(value) != 2:
            _reject("check_value")
        status, reason_code = value
        checks.append(
            ReadinessCheck(
                name=name,
                category=specs[name].category,
                status=status,
                reason_code=reason_code,
            )
        )
    return tuple(checks)


def uninstalled_report(component: ControlComponent) -> ControlReadinessReport:
    statuses = {
        name: (CheckStatus.UNKNOWN, "component_not_installed")
        for name in CHECK_SPECS[component]
    }
    return ControlReadinessReport(
        component=component,
        installed=False,
        pairing=PairingState.UNPAIRED,
        connection=ConnectionState.DISCONNECTED,
        grant=GrantState.NONE,
        protocol=ProtocolState.UNKNOWN,
        checks=make_checks(component, statuses),
    )


__all__ = [
    "CHECK_SPECS",
    "READINESS_SCHEMA_VERSION",
    "CheckCategory",
    "CheckStatus",
    "ConnectionState",
    "ControlComponent",
    "ControlReadinessReport",
    "ControlReadinessState",
    "GrantState",
    "PairingState",
    "ProtocolState",
    "ReadinessCheck",
    "ReadinessEvidenceRejected",
    "make_checks",
    "uninstalled_report",
]
