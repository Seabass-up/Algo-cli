"""Hostile in-memory macOS/AX lifecycle simulator for control hardening."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import re
import secrets
import time
from typing import Callable
import uuid

from .david_control_kernel import (
    ROUTE_ORDER,
    ControlRequest,
    ControlRoute,
    Operation,
    SnapshotRef,
    TargetKind,
    TargetRef,
    content_digest,
)
from .david_control_runtime import (
    AdapterDispatchResult,
    AdapterReconciliationResult,
    DispatchDisposition,
    ReconciliationDisposition,
    fresh_postcondition_evidence,
    structural_evidence,
)


_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_OPAQUE_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_ROUTE_INDEX = {route: index for index, route in enumerate(ROUTE_ORDER)}


class AustinSimulatorError(RuntimeError):
    """A content-free simulator contract error."""


class AustinElementKind(str, Enum):
    BUTTON = "button"
    INPUT = "input"
    SELECT = "select"
    SCROLL_REGION = "scroll_region"
    FILE_PICKER = "file_picker"


class AustinModalState(str, Enum):
    NONE = "none"
    APP_MODAL = "app_modal"
    SYSTEM_MODAL = "system_modal"
    AUTHENTICATION = "authentication"
    PAYMENT = "payment"


@dataclass(frozen=True, slots=True)
class AustinElement:
    element_id: str
    window_id: str
    launch_epoch: int
    window_epoch: int
    kind: AustinElementKind
    enabled: bool
    secure: bool
    modal_only: bool


class AustinDesktopSimulator:
    """Finite desktop state with no AppKit, AX, CGEvent, or process execution."""

    def __init__(
        self,
        target: TargetRef,
        snapshot: SnapshotRef,
        *,
        pid: int,
        routes: tuple[ControlRoute, ...] = (
            ControlRoute.CONNECTOR,
            ControlRoute.SHORTCUT,
            ControlRoute.APPLE_EVENT,
            ControlRoute.AX,
            ControlRoute.SCREENSHOT,
            ControlRoute.COORDINATE,
            ControlRoute.HANDOFF,
        ),
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if type(target) is not TargetRef or target.kind is not TargetKind.DESKTOP_SURFACE:
            raise ValueError("desktop_target")
        if type(snapshot) is not SnapshotRef or not snapshot.matches_target(target):
            raise ValueError("desktop_snapshot")
        if type(pid) is not int or not 1 <= pid <= 2_147_483_647:
            raise ValueError("desktop_pid")
        if (
            type(routes) is not tuple
            or not routes
            or not all(type(route) is ControlRoute for route in routes)
            or len(set(routes)) != len(routes)
            or tuple(sorted(routes, key=lambda route: _ROUTE_INDEX[route])) != routes
        ):
            raise ValueError("desktop_routes")
        if clock_ms is not None and not callable(clock_ms):
            raise ValueError("desktop_clock")
        self.target = target
        self.snapshot = snapshot
        self.pid = pid
        self.launch_generation = target.epoch
        self._routes = routes
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._token_key = secrets.token_bytes(32)
        self._token_counter = 0
        self._window_epochs: dict[str, int] = {}
        self._elements: dict[str, AustinElement] = {}
        self._effect_results: dict[str, AdapterReconciliationResult] = {}
        self._coordinate_map: dict[tuple[int, int, int], str] = {}
        self._value_digests: dict[str, str] = {}
        self.process_running = True
        self.hung = False
        self.screen_locked = False
        self.user_session_active = True
        self.focused_window_id: str | None = None
        self.modal_state = AustinModalState.NONE
        self.modal_window_id: str | None = None
        self.display_generation = 1
        self.display_width = 1440
        self.display_height = 900
        self.scale_milli = 2_000
        self.keyboard_layout = "us"
        self.ime_active = False
        self.mutation_count = 0

    def _now(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= (1 << 53) - 1:
            raise AustinSimulatorError("desktop_clock")
        return max(value, self.snapshot.observed_at_ms)

    def _new_token(self, domain: str) -> str:
        if not _SAFE_ID_RE.fullmatch(domain):
            raise AustinSimulatorError("desktop_token_domain")
        self._token_counter += 1
        payload = (
            f"{domain}:{self._token_counter}:{self.target.target_id}:{self.target.epoch}:{self.target.fencing_token}"
        ).encode("ascii")
        return "hmac-sha256:" + hmac.new(self._token_key, payload, hashlib.sha256).hexdigest()

    def _refresh_snapshot(self) -> SnapshotRef:
        self.snapshot = SnapshotRef.from_dict(
            {
                "snapshot_id": str(uuid.uuid4()),
                "target_id": self.target.target_id,
                "epoch": self.target.epoch,
                "revision": self.target.revision,
                "fencing_token": self.target.fencing_token,
                "observed_at_ms": self._now(),
                "sequence": self.snapshot.sequence + 1,
            }
        )
        return self.snapshot

    def _replace_launch(self, pid: int) -> None:
        if type(pid) is not int or not 1 <= pid <= 2_147_483_647:
            raise ValueError("desktop_pid")
        self.pid = pid
        self.launch_generation += 1
        self.target = TargetRef.from_dict(
            {
                "kind": TargetKind.DESKTOP_SURFACE.value,
                "target_id": self.target.target_id,
                "epoch": self.target.epoch + 1,
                "revision": f"launch_{self.launch_generation}",
                "fencing_token": self.target.fencing_token + 1,
            }
        )
        self.snapshot = SnapshotRef.from_dict(
            {
                "snapshot_id": str(uuid.uuid4()),
                "target_id": self.target.target_id,
                "epoch": self.target.epoch,
                "revision": self.target.revision,
                "fencing_token": self.target.fencing_token,
                "observed_at_ms": self._now(),
                "sequence": self.snapshot.sequence + 1,
            }
        )
        self._window_epochs.clear()
        self._elements.clear()
        self._coordinate_map.clear()
        self._value_digests.clear()
        self.focused_window_id = None
        self.modal_state = AustinModalState.NONE
        self.modal_window_id = None
        self.process_running = True
        self.hung = False

    def add_window(self, *, focused: bool = False) -> str:
        if type(focused) is not bool:
            raise ValueError("desktop_focus")
        window_id = self._new_token("window")
        self._window_epochs[window_id] = 1
        if focused or self.focused_window_id is None:
            self.focused_window_id = window_id
        self._refresh_snapshot()
        return window_id

    def add_element(
        self,
        kind: AustinElementKind,
        *,
        window_id: str,
        enabled: bool = True,
        secure: bool = False,
        modal_only: bool = False,
    ) -> AustinElement:
        if type(kind) is not AustinElementKind:
            raise ValueError("desktop_element_kind")
        if window_id not in self._window_epochs:
            raise ValueError("desktop_window")
        if type(enabled) is not bool or type(secure) is not bool or type(modal_only) is not bool:
            raise ValueError("desktop_element_flags")
        element = AustinElement(
            element_id=self._new_token("element"),
            window_id=window_id,
            launch_epoch=self.target.epoch,
            window_epoch=self._window_epochs[window_id],
            kind=kind,
            enabled=enabled,
            secure=secure,
            modal_only=modal_only,
        )
        self._elements[element.element_id] = element
        self._refresh_snapshot()
        return element

    def relaunch(self, *, pid: int) -> None:
        self._replace_launch(pid)

    def terminate(self) -> None:
        self.process_running = False
        self._refresh_snapshot()

    def set_focus(self, window_id: str) -> None:
        if window_id not in self._window_epochs:
            raise ValueError("desktop_window")
        self.focused_window_id = window_id
        self._refresh_snapshot()

    def inject_focus_theft(self, window_id: str) -> None:
        """Race injection: change focus after observation without a new snapshot."""

        if window_id not in self._window_epochs:
            raise ValueError("desktop_window")
        self.focused_window_id = window_id

    def set_modal(
        self,
        state: AustinModalState,
        *,
        window_id: str | None = None,
    ) -> None:
        if type(state) is not AustinModalState:
            raise ValueError("desktop_modal")
        if state is AustinModalState.APP_MODAL:
            if window_id not in self._window_epochs:
                raise ValueError("desktop_modal_window")
            self.modal_window_id = window_id
        elif window_id is not None:
            raise ValueError("desktop_modal_window")
        else:
            self.modal_window_id = None
        self.modal_state = state
        self._refresh_snapshot()

    def inject_screen_lock(self) -> None:
        """Race injection: lock after observation without issuing a snapshot."""

        self.screen_locked = True

    def set_screen_locked(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("desktop_screen_lock")
        self.screen_locked = value
        self._refresh_snapshot()

    def set_user_session_active(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("desktop_user_session")
        self.user_session_active = value
        self._refresh_snapshot()

    def set_hung(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("desktop_hung")
        self.hung = value

    def set_ime_active(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("desktop_ime")
        self.ime_active = value
        self._refresh_snapshot()

    def set_keyboard_layout(self, layout: str) -> None:
        if type(layout) is not str or not _SAFE_ID_RE.fullmatch(layout):
            raise ValueError("desktop_keyboard_layout")
        self.keyboard_layout = layout
        self._refresh_snapshot()

    def change_display(
        self,
        *,
        width: int,
        height: int,
        scale_milli: int,
    ) -> None:
        if (
            type(width) is not int
            or type(height) is not int
            or type(scale_milli) is not int
            or not 1 <= width <= 16_384
            or not 1 <= height <= 16_384
            or not 500 <= scale_milli <= 4_000
        ):
            raise ValueError("desktop_display")
        self.display_width = width
        self.display_height = height
        self.scale_milli = scale_milli
        self.display_generation += 1
        self._coordinate_map.clear()
        self._refresh_snapshot()

    def interleave_user_action(self) -> None:
        self._refresh_snapshot()

    def map_coordinate(self, element_id: str, *, x: int, y: int) -> None:
        if element_id not in self._elements:
            raise ValueError("desktop_element")
        if (
            type(x) is not int
            or type(y) is not int
            or not 0 <= x < self.display_width
            or not 0 <= y < self.display_height
        ):
            raise ValueError("desktop_coordinate")
        self._coordinate_map[(self.display_generation, x, y)] = element_id
        self._refresh_snapshot()

    def available_routes(self, target: TargetRef) -> tuple[ControlRoute, ...]:
        if type(target) is not TargetRef or target.target_id != self.target.target_id:
            raise AustinSimulatorError("desktop_target")
        if not self.process_running or self.screen_locked or not self.user_session_active:
            return (ControlRoute.HANDOFF,)
        return self._routes

    def current_snapshot(self, target: TargetRef) -> SnapshotRef:
        if type(target) is not TargetRef or target.target_id != self.target.target_id:
            raise AustinSimulatorError("desktop_target")
        return self.snapshot

    def _record(
        self,
        effect_id: str,
        disposition: ReconciliationDisposition,
        reason_code: str,
        *,
        postcondition: SnapshotRef | None = None,
    ) -> AdapterReconciliationResult:
        evidence_digest = (
            fresh_postcondition_evidence(effect_id, reason_code, postcondition)
            if postcondition is not None
            else structural_evidence(effect_id, reason_code)
        )
        result = AdapterReconciliationResult(
            disposition,
            reason_code,
            evidence_digest,
            postcondition,
        )
        self._effect_results[effect_id] = result
        return result

    def _prior_dispatch(self, effect_id: str) -> AdapterDispatchResult | None:
        prior = self._effect_results.get(effect_id)
        if prior is None:
            return None
        if prior.disposition is ReconciliationDisposition.VERIFIED:
            return AdapterDispatchResult(
                DispatchDisposition.APPLIED,
                "none",
                prior.evidence_digest,
            )
        if prior.disposition is ReconciliationDisposition.FAILED:
            return AdapterDispatchResult(
                DispatchDisposition.REJECTED,
                prior.reason_code,
                prior.evidence_digest,
            )
        return AdapterDispatchResult(
            DispatchDisposition.UNKNOWN,
            prior.reason_code,
            prior.evidence_digest,
        )

    def _reject(self, effect_id: str, reason_code: str) -> AdapterDispatchResult:
        result = self._record(
            effect_id,
            ReconciliationDisposition.FAILED,
            reason_code,
        )
        return AdapterDispatchResult(
            DispatchDisposition.REJECTED,
            result.reason_code,
            result.evidence_digest,
        )

    def _unknown(self, effect_id: str, reason_code: str) -> AdapterDispatchResult:
        result = self._record(
            effect_id,
            ReconciliationDisposition.UNKNOWN,
            reason_code,
        )
        return AdapterDispatchResult(
            DispatchDisposition.UNKNOWN,
            result.reason_code,
            result.evidence_digest,
        )

    def _element_for(self, request: ControlRequest) -> AustinElement | None:
        element_id = request.arguments.get("element_id")
        if type(element_id) is not str or not _OPAQUE_ID_RE.fullmatch(element_id):
            return None
        element = self._elements.get(element_id)
        if element is None:
            return None
        if (
            element.launch_epoch != self.target.epoch
            or self._window_epochs.get(element.window_id) != element.window_epoch
        ):
            return None
        return element

    @staticmethod
    def _route_supports(route: ControlRoute, operation: Operation) -> bool:
        supported = {
            ControlRoute.CONNECTOR: frozenset(Operation),
            ControlRoute.SHORTCUT: frozenset({Operation.ACTIVATE, Operation.SELECT_OPTION, Operation.HANDOFF}),
            ControlRoute.APPLE_EVENT: frozenset({Operation.OBSERVE, Operation.ACTIVATE, Operation.SELECT_OPTION}),
            ControlRoute.AX: frozenset(
                {
                    Operation.OBSERVE,
                    Operation.ACTIVATE,
                    Operation.INPUT_TEXT,
                    Operation.SELECT_OPTION,
                    Operation.SCROLL,
                }
            ),
            ControlRoute.SCREENSHOT: frozenset({Operation.OBSERVE}),
            ControlRoute.COORDINATE: frozenset({Operation.COORDINATE_ACTIVATE}),
            ControlRoute.HANDOFF: frozenset({Operation.HANDOFF}),
        }
        return operation in supported.get(route, frozenset())

    def dispatch(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterDispatchResult:
        structural_evidence(effect_id, "dispatch_seen")
        prior = self._prior_dispatch(effect_id)
        if prior is not None:
            return prior
        if type(request) is not ControlRequest or type(route) is not ControlRoute:
            return self._unknown(effect_id, "adapter_protocol")
        if route not in self.available_routes(request.target):
            return self._reject(effect_id, "route_unavailable")
        if not self._route_supports(route, request.operation):
            return self._reject(effect_id, "route_operation_denied")
        if request.target != self.target or request.snapshot != self.snapshot:
            return self._reject(effect_id, "snapshot_changed")
        if request.operation is Operation.HANDOFF:
            result = self._record(
                effect_id,
                ReconciliationDisposition.VERIFIED,
                "handoff_recorded",
            )
            return AdapterDispatchResult(
                DispatchDisposition.APPLIED,
                "none",
                result.evidence_digest,
            )
        if not self.process_running:
            return self._reject(effect_id, "process_not_running")
        if self.screen_locked:
            return self._reject(effect_id, "screen_locked")
        if not self.user_session_active:
            return self._reject(effect_id, "user_session_inactive")
        if self.hung:
            return self._unknown(effect_id, "target_hung")
        if (
            self.modal_state
            in {
                AustinModalState.SYSTEM_MODAL,
                AustinModalState.AUTHENTICATION,
                AustinModalState.PAYMENT,
            }
            and request.operation is not Operation.HANDOFF
        ):
            return self._reject(effect_id, "secure_modal_handoff")

        element: AustinElement | None = None
        if request.operation in {
            Operation.ACTIVATE,
            Operation.INPUT_TEXT,
            Operation.SELECT_OPTION,
            Operation.SCROLL,
            Operation.UPLOAD,
        }:
            element = self._element_for(request)
            if element is None:
                return self._reject(effect_id, "element_stale")
            if not element.enabled:
                return self._reject(effect_id, "element_disabled")
            if element.secure:
                return self._reject(effect_id, "secure_field_handoff")
            if self.focused_window_id != element.window_id:
                return self._reject(effect_id, "focus_changed")
            if self.modal_state is AustinModalState.APP_MODAL:
                if element.window_id != self.modal_window_id or not element.modal_only:
                    return self._reject(effect_id, "modal_blocked")
            elif element.modal_only:
                return self._reject(effect_id, "modal_element_stale")

        mutates = False
        if request.operation is Operation.ACTIVATE:
            assert element is not None
            if element.kind is not AustinElementKind.BUTTON:
                return self._reject(effect_id, "element_kind")
            mutates = True
        elif request.operation is Operation.INPUT_TEXT:
            assert element is not None
            if element.kind is not AustinElementKind.INPUT:
                return self._reject(effect_id, "element_kind")
            if self.ime_active:
                return self._reject(effect_id, "ime_handoff")
            self._value_digests[element.element_id] = content_digest(
                {
                    "keyboard_layout": self.keyboard_layout,
                    "replace": request.arguments["replace"],
                    "text": request.arguments["text"],
                }
            )
            mutates = True
        elif request.operation is Operation.SELECT_OPTION:
            assert element is not None
            if element.kind is not AustinElementKind.SELECT:
                return self._reject(effect_id, "element_kind")
            self._value_digests[element.element_id] = content_digest({"option_id": request.arguments["option_id"]})
            mutates = True
        elif request.operation is Operation.SCROLL:
            assert element is not None
            if element.kind is not AustinElementKind.SCROLL_REGION:
                return self._reject(effect_id, "element_kind")
            mutates = True
        elif request.operation is Operation.UPLOAD:
            return self._reject(effect_id, "file_picker_handoff")
        elif request.operation is Operation.COORDINATE_ACTIVATE:
            if (
                request.arguments["viewport_width"] != self.display_width
                or request.arguments["viewport_height"] != self.display_height
            ):
                return self._reject(effect_id, "display_changed")
            coordinate = (
                self.display_generation,
                request.arguments["x"],
                request.arguments["y"],
            )
            if coordinate not in self._coordinate_map:
                return self._reject(effect_id, "coordinate_unmapped")
            target_element = self._elements.get(self._coordinate_map[coordinate])
            if target_element is None or target_element.secure or self.focused_window_id != target_element.window_id:
                return self._reject(effect_id, "coordinate_target_changed")
            mutates = True
        elif request.operation is not Operation.OBSERVE:
            return self._reject(effect_id, "operation_unsupported")

        postcondition: SnapshotRef | None = None
        if mutates:
            self.mutation_count += 1
            postcondition = self._refresh_snapshot()
        result = self._record(
            effect_id,
            ReconciliationDisposition.VERIFIED,
            "effect_applied",
            postcondition=postcondition,
        )
        return AdapterDispatchResult(
            DispatchDisposition.APPLIED,
            "none",
            result.evidence_digest,
        )

    def reconcile(
        self,
        effect_id: str,
        request: ControlRequest,
        route: ControlRoute,
    ) -> AdapterReconciliationResult:
        del request, route
        structural_evidence(effect_id, "reconcile_seen")
        return self._effect_results.get(
            effect_id,
            AdapterReconciliationResult(
                ReconciliationDisposition.FAILED,
                "effect_absent",
                structural_evidence(effect_id, "effect_absent"),
            ),
        )

    def resolve_unknown(self, effect_id: str, *, applied: bool) -> None:
        if type(applied) is not bool:
            raise ValueError("desktop_resolution")
        current = self._effect_results.get(effect_id)
        if current is None or current.disposition is not ReconciliationDisposition.UNKNOWN:
            raise ValueError("desktop_effect")
        postcondition = None
        if applied:
            postcondition = self._refresh_snapshot()
        self._record(
            effect_id,
            (ReconciliationDisposition.VERIFIED if applied else ReconciliationDisposition.FAILED),
            "effect_applied" if applied else "effect_absent",
            postcondition=postcondition,
        )

    def value_digest(self, element_id: str) -> str | None:
        return self._value_digests.get(element_id)


__all__ = [
    "AustinDesktopSimulator",
    "AustinElement",
    "AustinElementKind",
    "AustinModalState",
    "AustinSimulatorError",
]
