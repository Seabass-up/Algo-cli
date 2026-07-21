"""Hostile in-memory browser lifecycle simulator for control hardening."""

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
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPAQUE_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_ROUTE_INDEX = {route: index for index, route in enumerate(ROUTE_ORDER)}


class NeonSimulatorError(RuntimeError):
    """A content-free simulator contract error."""


class NeonElementKind(str, Enum):
    BUTTON = "button"
    INPUT = "input"
    SELECT = "select"
    UPLOAD = "upload"
    SCROLL_REGION = "scroll_region"


class NeonDialogState(str, Enum):
    NONE = "none"
    ALERT = "alert"
    CONFIRM = "confirm"
    PROMPT = "prompt"
    BEFORE_UNLOAD = "before_unload"


@dataclass(frozen=True, slots=True)
class NeonElement:
    element_id: str
    frame_id: str
    document_epoch: int
    frame_epoch: int
    kind: NeonElementKind
    enabled: bool
    secure: bool
    opens_popup: bool
    navigation_revision: str


class NeonBrowserSimulator:
    """Finite browser state with no browser process, URL, selector, or code path."""

    def __init__(
        self,
        target: TargetRef,
        snapshot: SnapshotRef,
        *,
        routes: tuple[ControlRoute, ...] = (
            ControlRoute.CONNECTOR,
            ControlRoute.DOM,
            ControlRoute.SCREENSHOT,
            ControlRoute.COORDINATE,
            ControlRoute.HANDOFF,
        ),
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if type(target) is not TargetRef or target.kind is not TargetKind.BROWSER_DOCUMENT:
            raise ValueError("browser_target")
        if type(snapshot) is not SnapshotRef or not snapshot.matches_target(target):
            raise ValueError("browser_snapshot")
        if (
            type(routes) is not tuple
            or not routes
            or not all(type(route) is ControlRoute for route in routes)
            or len(set(routes)) != len(routes)
            or tuple(sorted(routes, key=lambda route: _ROUTE_INDEX[route])) != routes
        ):
            raise ValueError("browser_routes")
        if clock_ms is not None and not callable(clock_ms):
            raise ValueError("browser_clock")
        self.target = target
        self.snapshot = snapshot
        self._routes = routes
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._token_key = secrets.token_bytes(32)
        self._token_counter = 0
        self._frame_epochs: dict[str, int] = {}
        self._elements: dict[str, NeonElement] = {}
        self._effect_results: dict[str, AdapterReconciliationResult] = {}
        self._coordinate_map: dict[tuple[int, int, int, int], str] = {}
        self._value_digests: dict[str, str] = {}
        self._upload_digests: dict[str, str] = {}
        self.top_frame_id = self._new_token("frame")
        self._frame_epochs[self.top_frame_id] = 1
        self.dialog_state = NeonDialogState.NONE
        self.uploads_enabled = False
        self.hung = False
        self.closed = False
        self.popup_count = 0
        self.quarantined_popup_count = 0
        self.redirect_block_count = 0
        self.service_worker_generation = 1
        self.mutation_count = 0

    def _now(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= (1 << 53) - 1:
            raise NeonSimulatorError("browser_clock")
        return max(value, self.snapshot.observed_at_ms)

    def _new_token(self, domain: str) -> str:
        if not _SAFE_ID_RE.fullmatch(domain):
            raise NeonSimulatorError("browser_token_domain")
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

    def _replace_target(self, revision: str) -> None:
        if type(revision) is not str or not _REVISION_RE.fullmatch(revision):
            raise ValueError("browser_revision")
        self.target = TargetRef.from_dict(
            {
                "kind": TargetKind.BROWSER_DOCUMENT.value,
                "target_id": self.target.target_id,
                "epoch": self.target.epoch + 1,
                "revision": revision,
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
        self._frame_epochs.clear()
        self._elements.clear()
        self._coordinate_map.clear()
        self.top_frame_id = self._new_token("frame")
        self._frame_epochs[self.top_frame_id] = 1
        self.dialog_state = NeonDialogState.NONE

    def add_frame(self) -> str:
        frame_id = self._new_token("frame")
        self._frame_epochs[frame_id] = 1
        self._refresh_snapshot()
        return frame_id

    def add_element(
        self,
        kind: NeonElementKind,
        *,
        frame_id: str | None = None,
        enabled: bool = True,
        secure: bool = False,
        opens_popup: bool = False,
        navigation_revision: str = "none",
    ) -> NeonElement:
        if type(kind) is not NeonElementKind:
            raise ValueError("browser_element_kind")
        selected_frame = frame_id or self.top_frame_id
        if selected_frame not in self._frame_epochs:
            raise ValueError("browser_frame")
        if type(enabled) is not bool or type(secure) is not bool or type(opens_popup) is not bool:
            raise ValueError("browser_element_flags")
        if type(navigation_revision) is not str or not _REVISION_RE.fullmatch(navigation_revision):
            raise ValueError("browser_navigation_revision")
        element = NeonElement(
            element_id=self._new_token("element"),
            frame_id=selected_frame,
            document_epoch=self.target.epoch,
            frame_epoch=self._frame_epochs[selected_frame],
            kind=kind,
            enabled=enabled,
            secure=secure,
            opens_popup=opens_popup,
            navigation_revision=navigation_revision,
        )
        self._elements[element.element_id] = element
        self._refresh_snapshot()
        return element

    def map_coordinate(
        self,
        element_id: str,
        *,
        x: int,
        y: int,
        viewport_width: int,
        viewport_height: int,
    ) -> None:
        if element_id not in self._elements:
            raise ValueError("browser_element")
        values = (x, y, viewport_width, viewport_height)
        if not all(type(value) is int for value in values):
            raise ValueError("browser_coordinate")
        if not (0 <= x < viewport_width and 0 <= y < viewport_height):
            raise ValueError("browser_coordinate")
        self._coordinate_map[(x, y, viewport_width, viewport_height)] = element_id
        self._refresh_snapshot()

    def navigate(self, revision: str) -> None:
        self._replace_target(revision)

    def restore_from_bfcache(self, revision: str) -> None:
        self._replace_target(revision)

    def navigate_frame(self, frame_id: str) -> None:
        if frame_id not in self._frame_epochs:
            raise ValueError("browser_frame")
        self._frame_epochs[frame_id] += 1
        self._elements = {key: value for key, value in self._elements.items() if value.frame_id != frame_id}
        self._coordinate_map = {key: value for key, value in self._coordinate_map.items() if value in self._elements}
        self._refresh_snapshot()

    def redirect(self, *, allowed: bool, revision: str) -> None:
        if type(allowed) is not bool:
            raise ValueError("browser_redirect")
        if allowed:
            self._replace_target(revision)
            return
        self.redirect_block_count += 1
        self._refresh_snapshot()

    def set_dialog(self, state: NeonDialogState) -> None:
        if type(state) is not NeonDialogState:
            raise ValueError("browser_dialog")
        self.dialog_state = state
        self._refresh_snapshot()

    def restart_service_worker(self) -> None:
        self.service_worker_generation += 1
        self._refresh_snapshot()

    def set_hung(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("browser_hung")
        self.hung = value

    def set_uploads_enabled(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("browser_uploads")
        self.uploads_enabled = value
        self._refresh_snapshot()

    def close(self) -> None:
        self.closed = True
        self._refresh_snapshot()

    def available_routes(self, target: TargetRef) -> tuple[ControlRoute, ...]:
        if type(target) is not TargetRef or target.target_id != self.target.target_id:
            raise NeonSimulatorError("browser_target")
        if self.closed:
            return (ControlRoute.HANDOFF,)
        return self._routes

    def current_snapshot(self, target: TargetRef) -> SnapshotRef:
        if type(target) is not TargetRef or target.target_id != self.target.target_id:
            raise NeonSimulatorError("browser_target")
        return self.snapshot

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

    def _element_for(self, request: ControlRequest) -> NeonElement | None:
        element_id = request.arguments.get("element_id")
        if type(element_id) is not str or not _OPAQUE_ID_RE.fullmatch(element_id):
            return None
        element = self._elements.get(element_id)
        if element is None:
            return None
        if (
            element.document_epoch != self.target.epoch
            or self._frame_epochs.get(element.frame_id) != element.frame_epoch
        ):
            return None
        return element

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
        if self.closed:
            return self._reject(effect_id, "target_closed")
        if self.hung:
            return self._unknown(effect_id, "target_hung")
        if self.dialog_state is not NeonDialogState.NONE and request.operation is not Operation.HANDOFF:
            return self._reject(effect_id, "dialog_blocked")

        element: NeonElement | None = None
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

        mutates = False
        if request.operation is Operation.ACTIVATE:
            assert element is not None
            if element.kind is not NeonElementKind.BUTTON:
                return self._reject(effect_id, "element_kind")
            mutates = True
        elif request.operation is Operation.INPUT_TEXT:
            assert element is not None
            if element.kind is not NeonElementKind.INPUT:
                return self._reject(effect_id, "element_kind")
            self._value_digests[element.element_id] = content_digest(
                {
                    "replace": request.arguments["replace"],
                    "text": request.arguments["text"],
                }
            )
            mutates = True
        elif request.operation is Operation.SELECT_OPTION:
            assert element is not None
            if element.kind is not NeonElementKind.SELECT:
                return self._reject(effect_id, "element_kind")
            self._value_digests[element.element_id] = content_digest({"option_id": request.arguments["option_id"]})
            mutates = True
        elif request.operation is Operation.SCROLL:
            assert element is not None
            if element.kind is not NeonElementKind.SCROLL_REGION:
                return self._reject(effect_id, "element_kind")
            mutates = True
        elif request.operation is Operation.UPLOAD:
            assert element is not None
            if element.kind is not NeonElementKind.UPLOAD:
                return self._reject(effect_id, "element_kind")
            if not self.uploads_enabled:
                return self._reject(effect_id, "upload_disabled")
            self._upload_digests[element.element_id] = content_digest(
                {
                    "artifact_id": request.arguments["artifact_id"],
                    "byte_count": request.arguments["byte_count"],
                }
            )
            mutates = True
        elif request.operation is Operation.COORDINATE_ACTIVATE:
            coordinate = (
                request.arguments["x"],
                request.arguments["y"],
                request.arguments["viewport_width"],
                request.arguments["viewport_height"],
            )
            if coordinate not in self._coordinate_map:
                return self._reject(effect_id, "coordinate_unmapped")
            mutates = True
        elif request.operation is not Operation.OBSERVE:
            return self._reject(effect_id, "operation_unsupported")

        postcondition: SnapshotRef | None = None
        if mutates:
            self.mutation_count += 1
            if element is not None and element.opens_popup:
                self.popup_count += 1
                self.quarantined_popup_count += 1
            if (
                element is not None
                and element.navigation_revision != "none"
                and request.operation is Operation.ACTIVATE
            ):
                self._replace_target(element.navigation_revision)
            else:
                self._refresh_snapshot()
            postcondition = self.snapshot
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
            raise ValueError("browser_resolution")
        current = self._effect_results.get(effect_id)
        if current is None or current.disposition is not ReconciliationDisposition.UNKNOWN:
            raise ValueError("browser_effect")
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

    def upload_digest(self, element_id: str) -> str | None:
        return self._upload_digests.get(element_id)


__all__ = [
    "NeonBrowserSimulator",
    "NeonDialogState",
    "NeonElement",
    "NeonElementKind",
    "NeonSimulatorError",
]
