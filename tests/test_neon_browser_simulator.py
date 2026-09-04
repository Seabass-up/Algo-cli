from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest

from algo_cli.ada_control_journal import ControlEffectState, ControlJournal
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
from algo_cli.neon_browser_simulator import (
    NeonBrowserSimulator,
    NeonDialogState,
    NeonElement,
    NeonElementKind,
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


def _simulator() -> NeonBrowserSimulator:
    target = TargetRef.from_dict(
        {
            "kind": TargetKind.BROWSER_DOCUMENT.value,
            "target_id": _opaque("neon-target"),
            "epoch": 1,
            "revision": "document-1",
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
    return NeonBrowserSimulator(target, snapshot, clock_ms=Clock(NOW_MS + 1))


def _envelope(
    simulator: NeonBrowserSimulator,
    operation: Operation,
    arguments: dict[str, Any],
    *,
    serial: int = 1,
    data_class: ControlDataClass = ControlDataClass.STRUCTURAL,
    routes: tuple[ControlRoute, ...] = (
        ControlRoute.CONNECTOR,
        ControlRoute.DOM,
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
    simulator: NeonBrowserSimulator,
    operation: Operation,
    arguments: dict[str, Any],
    *,
    serial: int = 1,
    data_class: ControlDataClass = ControlDataClass.STRUCTURAL,
    routes: tuple[ControlRoute, ...] = (
        ControlRoute.CONNECTOR,
        ControlRoute.DOM,
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
    journal = ControlJournal(tmp_path / "private" / "ada-neon.sqlite3")
    runtime = ControlRuntime(
        journal,
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    return runtime.execute(envelope, simulator), envelope, runtime


def test_neon_supports_only_canonical_finite_routes() -> None:
    simulator = _simulator()
    assert simulator.available_routes(simulator.target) == (
        ControlRoute.CONNECTOR,
        ControlRoute.DOM,
        ControlRoute.SCREENSHOT,
        ControlRoute.COORDINATE,
        ControlRoute.HANDOFF,
    )
    wrong = TargetRef.from_dict(
        {
            **simulator.target.to_dict(),
            "kind": TargetKind.DESKTOP_SURFACE.value,
        }
    )
    with pytest.raises(ValueError, match="browser_target"):
        NeonBrowserSimulator(wrong, simulator.snapshot)


def test_activation_is_verified_and_direct_duplicate_is_idempotent(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.BUTTON)
    receipt, envelope, _ = _execute(
        tmp_path,
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
    )
    assert receipt.state is ControlEffectState.VERIFIED
    assert simulator.mutation_count == 1

    prior = simulator.dispatch(receipt.effect_id, envelope.request, receipt.route)
    assert prior.disposition is DispatchDisposition.APPLIED
    assert simulator.mutation_count == 1


def test_navigation_and_bfcache_never_reuse_document_generation() -> None:
    simulator = _simulator()
    initial = simulator.target
    simulator.navigate("document-2")
    navigated = simulator.target
    simulator.restore_from_bfcache("document-1-restored")
    restored = simulator.target

    assert navigated.epoch == initial.epoch + 1
    assert restored.epoch == navigated.epoch + 1
    assert restored.fencing_token > navigated.fencing_token > initial.fencing_token
    assert restored.revision != initial.revision


def test_top_navigation_makes_old_request_fail_before_dispatch(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.BUTTON)
    signer, policy, envelope = _envelope(
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
    )
    simulator.navigate("document-2")
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-neon.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    with pytest.raises(ControlRuntimeError, match="adapter_snapshot_invalid"):
        runtime.execute(envelope, simulator)
    assert simulator.mutation_count == 0


def test_frame_navigation_invalidates_frame_tokens_and_snapshot(tmp_path) -> None:
    simulator = _simulator()
    frame = simulator.add_frame()
    element = simulator.add_element(NeonElementKind.BUTTON, frame_id=frame)
    signer, policy, envelope = _envelope(
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
    )
    simulator.navigate_frame(frame)
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-neon.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    with pytest.raises(PermitRejected, match="snapshot_changed"):
        runtime.execute(envelope, simulator)
    assert simulator.mutation_count == 0


def test_dialog_blocks_actions_but_handoff_remains_available(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.BUTTON)
    simulator.set_dialog(NeonDialogState.CONFIRM)
    receipt, _, _ = _execute(
        tmp_path / "blocked",
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
    )
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "dialog_blocked"
    assert simulator.mutation_count == 0

    other = _simulator()
    other.set_dialog(NeonDialogState.PROMPT)
    receipt, _, _ = _execute(
        tmp_path / "handoff",
        other,
        Operation.HANDOFF,
        {"reason_code": "dialog_handoff"},
        routes=(ControlRoute.HANDOFF,),
        data_class=ControlDataClass.SECRET,
    )
    assert receipt.state is ControlEffectState.VERIFIED


def test_secure_input_always_hands_off_and_never_stores_text(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.INPUT, secure=True)
    secret = "private-password-never-store"
    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.INPUT_TEXT,
        {"element_id": element.element_id, "replace": True, "text": secret},
        data_class=ControlDataClass.PRIVATE,
    )
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "secure_field_handoff"
    assert simulator.value_digest(element.element_id) is None
    assert secret not in repr(simulator.__dict__)


def test_normal_input_keeps_only_digest_not_plaintext(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.INPUT)
    secret = "private-text-value"
    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.INPUT_TEXT,
        {"element_id": element.element_id, "replace": True, "text": secret},
        data_class=ControlDataClass.PRIVATE,
    )
    assert receipt.state is ControlEffectState.VERIFIED
    digest = simulator.value_digest(element.element_id)
    assert digest is not None and digest.startswith("sha256:")
    assert secret not in repr(simulator.__dict__)


def test_upload_is_disabled_by_default_and_counts_only_when_enabled(tmp_path) -> None:
    disabled = _simulator()
    element = disabled.add_element(NeonElementKind.UPLOAD)
    arguments = {
        "element_id": element.element_id,
        "artifact_id": _uuid(900),
        "byte_count": 4096,
    }
    receipt, _, _ = _execute(
        tmp_path / "disabled",
        disabled,
        Operation.UPLOAD,
        arguments,
        data_class=ControlDataClass.FILE,
    )
    assert receipt.state is ControlEffectState.FAILED
    assert receipt.reason_code == "upload_disabled"
    assert disabled.upload_digest(element.element_id) is None

    enabled = _simulator()
    element = enabled.add_element(NeonElementKind.UPLOAD)
    enabled.set_uploads_enabled(True)
    receipt, _, _ = _execute(
        tmp_path / "enabled",
        enabled,
        Operation.UPLOAD,
        {**arguments, "element_id": element.element_id},
        serial=2,
        data_class=ControlDataClass.FILE,
    )
    assert receipt.state is ControlEffectState.VERIFIED
    digest = enabled.upload_digest(element.element_id)
    assert digest is not None and digest.startswith("sha256:")


def test_popup_is_quarantined_and_navigation_effect_reconciles(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(
        NeonElementKind.BUTTON,
        opens_popup=True,
        navigation_revision="document-2",
    )
    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
    )
    assert receipt.state is ControlEffectState.VERIFIED
    assert simulator.popup_count == 1
    assert simulator.quarantined_popup_count == 1
    assert simulator.target.revision == "document-2"


def test_denied_redirect_and_service_worker_restart_invalidate_snapshots() -> None:
    simulator = _simulator()
    before = simulator.snapshot
    simulator.redirect(allowed=False, revision="blocked-document")
    blocked = simulator.snapshot
    simulator.restart_service_worker()
    restarted = simulator.snapshot
    assert simulator.redirect_block_count == 1
    assert blocked.snapshot_id != before.snapshot_id
    assert restarted.snapshot_id != blocked.snapshot_id
    assert simulator.service_worker_generation == 2
    assert simulator.target.revision == "document-1"


def test_coordinate_activation_requires_exact_snapshot_mapping(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.BUTTON)
    simulator.map_coordinate(
        element.element_id,
        x=10,
        y=20,
        viewport_width=800,
        viewport_height=600,
    )
    receipt, _, _ = _execute(
        tmp_path,
        simulator,
        Operation.COORDINATE_ACTIVATE,
        {"x": 10, "y": 20, "viewport_width": 800, "viewport_height": 600},
        routes=(ControlRoute.COORDINATE,),
    )
    assert receipt.state is ControlEffectState.VERIFIED


def test_hung_target_is_unknown_until_explicit_reconciliation(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.BUTTON)
    simulator.set_hung(True)
    receipt, envelope, runtime = _execute(
        tmp_path,
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
    )
    assert receipt.state is ControlEffectState.UNKNOWN
    assert simulator.mutation_count == 0
    assert (
        simulator.reconcile(receipt.effect_id, envelope.request, receipt.route).disposition
        is ReconciliationDisposition.UNKNOWN
    )

    simulator.resolve_unknown(receipt.effect_id, applied=False)
    resolved = runtime.recover(receipt.effect_id, envelope, simulator)
    assert resolved.state is ControlEffectState.FAILED
    assert simulator.mutation_count == 0


def test_closed_target_exposes_only_handoff_and_no_blind_action(tmp_path) -> None:
    simulator = _simulator()
    element = simulator.add_element(NeonElementKind.BUTTON)
    signer, policy, envelope = _envelope(
        simulator,
        Operation.ACTIVATE,
        {"element_id": element.element_id},
    )
    simulator.close()
    runtime = ControlRuntime(
        ControlJournal(tmp_path / "private" / "ada-neon.sqlite3"),
        signer.verifier,
        policy,
        signer,
        clock_ms=Clock(envelope.permit.issued_at_ms + 1),
    )
    with pytest.raises(PermitRejected, match="snapshot_changed"):
        runtime.execute(envelope, simulator)
    assert simulator.mutation_count == 0


@pytest.mark.parametrize("unavailable", ("closed", "hung"))
def test_unavailable_target_can_only_record_explicit_handoff(tmp_path, unavailable) -> None:
    simulator = _simulator()
    if unavailable == "closed":
        simulator.close()
    else:
        simulator.set_hung(True)

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


def test_neon_source_has_no_browser_process_code_or_dynamic_execution() -> None:
    from algo_cli import neon_browser_simulator as module

    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert calls.isdisjoint(forbidden_calls)
    assert imports.isdisjoint({"subprocess", "selenium", "playwright"})
    assert "selector" not in NeonElement.__dataclass_fields__
    assert "url" not in NeonElement.__dataclass_fields__
