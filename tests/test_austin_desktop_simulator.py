from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest

from algo_cli.ada_control_journal import ControlEffectState, ControlJournal
from algo_cli.austin_desktop_simulator import (
    AustinDesktopSimulator,
    AustinElement,
    AustinElementKind,
    AustinModalState,
)
from algo_cli.david_control_kernel import (
    ControlDataClass,
    ControlEnvelope,
    ControlRequest,
    ControlRoute,
    ControlSigner,
    Operation,
    PermitRejected,
    SnapshotRef,
    TargetKind,
    TargetRef,
    default_control_policy,
    issue_grant,
    issue_permit,
)
from algo_cli.david_control_runtime import (
    ControlRuntime,
    ControlRuntimeError,
    DispatchDisposition,
    ReconciliationDisposition,
)


NOW_MS = 1_800_000_000_000


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


class Clock:
    def __init__(self, start: int = NOW_MS) -> None:
        self.value = start

    def __call__(self) -> int:
        value = self.value
        self.value += 1
        return value


def _simulator() -> AustinDesktopSimulator:
    target = TargetRef.from_dict(
        {
            "kind": TargetKind.DESKTOP_SURFACE.value,
            "target_id": _opaque("austin-target"),
            "epoch": 1,
            "revision": "launch_1",
            "fencing_token": 1,
        }
    )
    snapshot = SnapshotRef.from_dict(
        {
            "snapshot_id": _uuid(1),
            "target_id": target.target_id,
            "epoch": target.epoch,
            "revision": target.revision,
            "fencing_token": target.fencing_token,
            "observed_at_ms": NOW_MS,
            "sequence": 1,
        }
    )
    return AustinDesktopSimulator(
        target,
        snapshot,
        pid=4242,
        clock_ms=Clock(NOW_MS + 1),
    )


def _envelope(
    simulator: AustinDesktopSimulator,
    operation: Operation,
    arguments: dict[str, Any],
    *,
    serial: int = 1,
    data_class: ControlDataClass = ControlDataClass.STRUCTURAL,
    routes: tuple[ControlRoute, ...] = (
        ControlRoute.CONNECTOR,
        ControlRoute.AX,
    ),
) -> tuple[ControlSigner, Any, ControlEnvelope]:
    signer = ControlSigner.from_private_bytes(bytes(range(32)))
    policy = default_control_policy()
    issued_at = simulator.snapshot.observed_at_ms + 10
    request = ControlRequest.from_dict(
        {
            "schema_version": 1,
            "request_id": _uuid(100 + serial),
            "session_id": _uuid(200 + serial),
            "subject_id": "runtime.operator",
            "sequence": 1,
            "issued_at_ms": issued_at - 5,
            "deadline_ms": issued_at + 5_000,
            "target": simulator.target.to_dict(),
            "snapshot": simulator.snapshot.to_dict(),
            "operation": operation.value,
            "data_class": data_class.value,
            "arguments": arguments,
            "requested_routes": [route.value for route in routes],
            "max_output_bytes": 4096,
        }
    )
    grant = issue_grant(
        signer,
        policy,
        grant_id=_uuid(300 + serial),
        subject_id=request.subject_id,
        target_ids=(request.target.target_id,),
        target_kinds=(request.target.kind,),
        operations=(operation,),
        data_classes=(data_class,),
        routes=routes,
        issued_at_ms=issued_at - 100,
        expires_at_ms=issued_at + 10_000,
        maximum_action_count=1,
        max_input_bytes=policy.max_input_bytes,
        max_output_bytes=policy.max_output_bytes,
        max_transmit_bytes=policy.max_transmit_bytes,
    )
    permit = issue_permit(
        signer,
        signer.verifier,
        policy,
        grant,
        request,
        permit_id=_uuid(400 + serial),
        issued_at_ms=issued_at,
        expires_at_ms=issued_at + 2_000,
    )
    return signer, policy, ControlEnvelope(request, grant, permit)


def _execute(
    tmp_path: Path,
    simulator: AustinDesktopSimulator,
    operation: Operation,
    arguments: dict[str, Any],
    *,
    serial: int = 1,
    data_class: ControlDataClass = ControlDataClass.STRUCTURAL,
    routes: tuple[ControlRoute, ...] = (
        ControlRoute.CONNECTOR,
        ControlRoute.AX,
    ),
):
    tmp_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    signer, policy, envelope = _envelope(
        simulator,
        operation,
        arguments,
        serial=serial,
        data_class=data_class,
        routes=routes,
    )
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-austin.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    return runtime.execute(envelope, simulator), envelope, runtime


def test_austin_exposes_canonical_finite_routes_only() -> None:
    simulator = _simulator()
    assert simulator.available_routes(simulator.target) == (
        ControlRoute.CONNECTOR,
        ControlRoute.SHORTCUT,
        ControlRoute.APPLE_EVENT,
        ControlRoute.AX,
        ControlRoute.SCREENSHOT,
        ControlRoute.COORDINATE,
        ControlRoute.HANDOFF,
    )
    wrong = TargetRef.from_dict({**simulator.target.to_dict(), "kind": TargetKind.BROWSER_DOCUMENT.value})
    with pytest.raises(ValueError, match="desktop_target"):
        AustinDesktopSimulator(wrong, simulator.snapshot, pid=1)


def test_ax_button_activation_is_verified_and_idempotent(tmp_path) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    receipt, envelope, _ = _execute(
        tmp_path,
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    assert receipt.state is ControlEffectState.VERIFIED
    assert simulator.mutation_count == 1
    duplicate = simulator.dispatch(receipt.effect_id, envelope.request, receipt.route)
    assert duplicate.disposition is DispatchDisposition.APPLIED
    assert simulator.mutation_count == 1


def test_relaunch_and_pid_reuse_always_advance_generation_and_clear_tokens() -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    before = simulator.target
    simulator.relaunch(pid=simulator.pid)

    assert simulator.pid == 4242
    assert simulator.target.epoch == before.epoch + 1
    assert simulator.target.fencing_token == before.fencing_token + 1
    assert simulator.target.revision != before.revision
    effect = simulator.dispatch(
        _uuid(900),
        ControlRequest.from_dict(
            {
                "schema_version": 1,
                "request_id": _uuid(901),
                "session_id": _uuid(902),
                "subject_id": "runtime.operator",
                "sequence": 1,
                "issued_at_ms": NOW_MS,
                "deadline_ms": NOW_MS + 1_000,
                "target": simulator.target.to_dict(),
                "snapshot": simulator.snapshot.to_dict(),
                "operation": Operation.ACTIVATE.value,
                "data_class": ControlDataClass.STRUCTURAL.value,
                "arguments": {"element_id": element.element_id},
                "requested_routes": [ControlRoute.AX.value],
                "max_output_bytes": 100,
            }
        ),
        ControlRoute.AX,
    )
    assert effect.disposition is DispatchDisposition.REJECTED
    assert effect.reason_code == "element_stale"


def test_old_request_after_relaunch_fails_before_dispatch(tmp_path) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    signer, policy, envelope = _envelope(
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    simulator.relaunch(pid=9876)
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-austin.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    with pytest.raises(ControlRuntimeError, match="adapter_snapshot_invalid"):
        runtime.execute(envelope, simulator)
    assert simulator.mutation_count == 0


def test_focus_theft_race_is_caught_inside_adapter(tmp_path) -> None:
    simulator = _simulator()
    first = simulator.add_window(focused=True)
    second = simulator.add_window()
    simulator.set_focus(first)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=first)
    signer, policy, envelope = _envelope(
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    simulator.inject_focus_theft(second)
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-austin.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    receipt = runtime.execute(envelope, simulator)
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "focus_changed"
    assert simulator.mutation_count == 0


def test_app_modal_allows_only_modal_scoped_element(tmp_path) -> None:
    blocked = _simulator()
    main = blocked.add_window(focused=True)
    modal = blocked.add_window()
    element = blocked.add_element(AustinElementKind.BUTTON, window_id=main)
    blocked.set_modal(AustinModalState.APP_MODAL, window_id=modal)
    receipt, _, _ = _execute(
        tmp_path / "blocked",
        blocked,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "modal_blocked"

    allowed = _simulator()
    allowed.add_window(focused=True)
    modal = allowed.add_window()
    element = allowed.add_element(
        AustinElementKind.BUTTON,
        window_id=modal,
        modal_only=True,
    )
    allowed.set_modal(AustinModalState.APP_MODAL, window_id=modal)
    allowed.set_focus(modal)
    receipt, _, _ = _execute(
        tmp_path / "allowed",
        allowed,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    assert receipt.state is ControlEffectState.VERIFIED


@pytest.mark.parametrize(
    "state",
    (AustinModalState.SYSTEM_MODAL, AustinModalState.AUTHENTICATION, AustinModalState.PAYMENT),
)
def test_system_auth_and_payment_modals_always_handoff(tmp_path, state) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    simulator.set_modal(state)
    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "secure_modal_handoff"


def test_secure_field_and_ime_never_receive_automatic_text(tmp_path) -> None:
    for index, configure in enumerate(("secure", "ime")):
        simulator = _simulator()
        window = simulator.add_window(focused=True)
        element = simulator.add_element(
            AustinElementKind.INPUT,
            window_id=window,
            secure=configure == "secure",
        )
        if configure == "ime":
            simulator.set_ime_active(True)
        receipt, _, _ = _execute(
            tmp_path / str(index),
            simulator,
            Operation.INPUT_TEXT,
            {
                "element_id": element.element_id,
                "replace": True,
                "text": "private-text-never-store",
            },
            data_class=ControlDataClass.PRIVATE,
            routes=(ControlRoute.AX,),
        )
        assert receipt.state is ControlEffectState.FAILED
        assert receipt.reason_code in {"secure_field_handoff", "ime_handoff"}
        assert simulator.value_digest(element.element_id) is None
        assert "private-text-never-store" not in repr(simulator.__dict__)


def test_normal_text_uses_layout_bound_digest_only(tmp_path) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.INPUT, window_id=window)
    simulator.set_keyboard_layout("us_international")
    secret = "private-desktop-text"
    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.INPUT_TEXT,
        {"element_id": element.element_id, "replace": True, "text": secret},
        data_class=ControlDataClass.PRIVATE,
        routes=(ControlRoute.AX,),
    )
    digest = simulator.value_digest(element.element_id)
    assert receipt.state is ControlEffectState.VERIFIED
    assert digest is not None and digest.startswith("sha256:")
    assert secret not in repr(simulator.__dict__)


def test_screen_lock_and_inactive_session_remove_action_routes(tmp_path) -> None:
    for index, state in enumerate(("lock", "inactive")):
        simulator = _simulator()
        window = simulator.add_window(focused=True)
        element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
        signer, policy, envelope = _envelope(
            simulator,
            Operation.ACTIVATE,
            {"element_id": element.element_id},
            routes=(ControlRoute.AX,),
        )
        if state == "lock":
            simulator.inject_screen_lock()
        else:
            simulator.set_user_session_active(False)
        child = tmp_path / str(index)
        child.mkdir(mode=0o700)
        runtime = ControlRuntime(
            ControlJournal(child / "private" / "ada-austin.sqlite3"),
            signer.verifier,
            policy,
            signer,
            clock_ms=Clock(envelope.permit.issued_at_ms + 1),
        )
        with pytest.raises(PermitRejected):
            runtime.execute(envelope, simulator)
        assert simulator.mutation_count == 0


@pytest.mark.parametrize("unavailable", ("terminated", "locked"))
def test_unavailable_desktop_can_only_record_explicit_handoff(tmp_path, unavailable) -> None:
    simulator = _simulator()
    if unavailable == "terminated":
        simulator.terminate()
    else:
        simulator.set_screen_locked(True)

    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.HANDOFF,
        {"reason_code": "target_unavailable"},
        routes=(ControlRoute.HANDOFF,),
        data_class=ControlDataClass.SECRET,
    )

    assert receipt.state is ControlEffectState.VERIFIED
    assert receipt.reason_code == "handoff_recorded"
    assert simulator.mutation_count == 0


def test_hung_target_is_unknown_until_explicit_resolution(tmp_path) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    simulator.set_hung(True)
    receipt, envelope, runtime = _execute(
        tmp_path,
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    assert receipt.state is ControlEffectState.UNKNOWN
    assert (
        simulator.reconcile(receipt.effect_id, envelope.request, receipt.route).disposition
        is ReconciliationDisposition.UNKNOWN
    )
    simulator.resolve_unknown(receipt.effect_id, applied=False)
    resolved = runtime.recover(receipt.effect_id, envelope, simulator)
    assert resolved.state is ControlEffectState.FAILED
    assert simulator.mutation_count == 0


def test_display_change_invalidates_coordinate_mapping_and_old_snapshot(tmp_path) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    simulator.map_coordinate(element.element_id, x=100, y=200)
    signer, policy, envelope = _envelope(
        simulator,
        Operation.COORDINATE_ACTIVATE,
        {
            "x": 100,
            "y": 200,
            "viewport_width": 1440,
            "viewport_height": 900,
        },
        routes=(ControlRoute.COORDINATE,),
    )
    simulator.change_display(width=1920, height=1080, scale_milli=1_000)
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-austin.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    with pytest.raises(PermitRejected, match="snapshot_changed"):
        runtime.execute(envelope, simulator)
    assert simulator.mutation_count == 0


def test_exact_coordinate_mapping_can_activate_current_display(tmp_path) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    simulator.map_coordinate(element.element_id, x=100, y=200)
    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.COORDINATE_ACTIVATE,
        {
            "x": 100,
            "y": 200,
            "viewport_width": 1440,
            "viewport_height": 900,
        },
        routes=(ControlRoute.COORDINATE,),
    )
    assert receipt.state is ControlEffectState.VERIFIED
    assert simulator.mutation_count == 1


def test_unapproved_route_operation_and_file_picker_fail_closed(tmp_path) -> None:
    screenshot = _simulator()
    window = screenshot.add_window(focused=True)
    element = screenshot.add_element(AustinElementKind.BUTTON, window_id=window)
    receipt, _, _ = _execute(
        tmp_path / "route",
        screenshot,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.SCREENSHOT,),
    )
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "route_operation_denied"

    upload = _simulator()
    window = upload.add_window(focused=True)
    element = upload.add_element(AustinElementKind.FILE_PICKER, window_id=window)
    receipt, _, _ = _execute(
        tmp_path / "upload",
        upload,
        Operation.UPLOAD,
        {
            "element_id": element.element_id,
            "artifact_id": _uuid(700),
            "byte_count": 1024,
        },
        data_class=ControlDataClass.FILE,
        routes=(ControlRoute.CONNECTOR,),
    )
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "file_picker_handoff"


def test_user_interleaving_invalidates_prior_snapshot(tmp_path) -> None:
    simulator = _simulator()
    window = simulator.add_window(focused=True)
    element = simulator.add_element(AustinElementKind.BUTTON, window_id=window)
    signer, policy, envelope = _envelope(
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
        routes=(ControlRoute.AX,),
    )
    simulator.interleave_user_action()
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-austin.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    with pytest.raises(PermitRejected, match="snapshot_changed"):
        runtime.execute(envelope, simulator)
    assert simulator.mutation_count == 0


def test_austin_source_has_no_native_framework_or_dynamic_execution() -> None:
    from algo_cli import austin_desktop_simulator as module

    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert calls.isdisjoint(forbidden_calls)
    assert imports.isdisjoint({"subprocess", "AppKit", "Quartz", "ApplicationServices"})
    assert "selector" not in AustinElement.__dataclass_fields__
    assert "path" not in AustinElement.__dataclass_fields__
