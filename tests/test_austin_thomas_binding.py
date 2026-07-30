from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any

import pytest

from algo_cli.austin_thomas_binding import (
    AustinBindingRejected,
    AustinPreparedBinding,
)
from algo_cli import david_control_kernel as david


NOW_MS = 1_800_000_000_000


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _preparation(
    *,
    operation: david.Operation = david.Operation.ACTIVATE,
    route: david.ControlRoute = david.ControlRoute.AX,
    selector: str = "focused_element",
    arguments: dict[str, Any] | None = None,
    request_id: str | None = None,
    subject_id: str = "runtime.operator",
) -> david.ControlPreparation:
    signer = david.ControlSigner.from_private_bytes(bytes(range(32)))
    data_class = (
        david.ControlDataClass.PRIVATE
        if operation is david.Operation.SELECT_OPTION
        else david.ControlDataClass.STRUCTURAL
    )
    return david.issue_control_preparation(
        signer,
        david.default_control_policy(),
        preparation_id=_uuid(601),
        request_id=request_id or _uuid(101),
        subject_id=subject_id,
        operation=operation,
        data_class=data_class,
        route=route,
        selector=selector,
        arguments={} if arguments is None else arguments,
        issued_at_ms=NOW_MS - 100,
        expires_at_ms=NOW_MS + 30_000,
    ).preparation


def _reply(
    preparation: david.ControlPreparation,
    *,
    target_id: str | None = None,
    observed_at_ms: int = NOW_MS,
    expires_at_ms: int = NOW_MS + 5_000,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    opaque_target = target_id or _opaque("native-target")
    if arguments is None:
        arguments = {"element_id": _opaque("element")}
    return {
        "arguments": arguments,
        "binding_expires_at_ms": expires_at_ms,
        "data_class": preparation.data_class.value,
        "operation": preparation.operation.value,
        "preparation_id": preparation.preparation_id,
        "protocol_version": 1,
        "reason_code": "binding_prepared",
        "request_id": preparation.request_id,
        "route": preparation.route.value,
        "snapshot": {
            "epoch": 1,
            "fencing_token": 1,
            "observed_at_ms": observed_at_ms,
            "revision": "prepared_1",
            "sequence": 1,
            "snapshot_id": _uuid(301),
            "target_id": opaque_target,
        },
        "status": "succeeded",
        "target": {
            "epoch": 1,
            "fencing_token": 1,
            "kind": "desktop_surface",
            "revision": "prepared_1",
            "target_id": opaque_target,
        },
    }


def _payload(row: dict[str, Any]) -> bytes:
    return david.canonical_json_bytes(row)


def test_binding_reply_constructs_the_only_target_bound_request() -> None:
    preparation = _preparation()
    binding = AustinPreparedBinding.from_payload(
        preparation,
        _payload(_reply(preparation)),
        now_ms=NOW_MS + 1,
    )
    request = binding.control_request(
        preparation,
        session_id=_uuid(201),
        sequence=1,
        issued_at_ms=NOW_MS + 2,
        deadline_ms=NOW_MS + 4_000,
        max_output_bytes=4_096,
    )

    assert request.request_id == preparation.request_id
    assert request.subject_id == preparation.subject_id
    assert request.target == binding.target
    assert request.snapshot == binding.snapshot
    assert request.arguments == {"element_id": _opaque("element")}
    assert request.requested_routes == (david.ControlRoute.AX,)
    assert request.deadline_ms <= binding.expires_at_ms
    with pytest.raises(TypeError):
        binding.arguments["element_id"] = _opaque("substituted")  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("preparation_id", _uuid(602), "binding_preparation"),
        ("request_id", _uuid(102), "binding_request"),
        ("operation", "select_option", "binding_operation"),
        ("data_class", "private", "binding_data_class"),
        ("route", "shortcut", "binding_route"),
        ("status", "denied", "binding_status"),
        ("reason_code", "other", "binding_status"),
    ],
)
def test_binding_reply_cross_binding_rejects_swaps(
    field: str,
    value: Any,
    reason: str,
) -> None:
    preparation = _preparation()
    row = _reply(preparation)
    row[field] = value
    with pytest.raises(AustinBindingRejected, match=reason):
        AustinPreparedBinding.from_payload(
            preparation,
            _payload(row),
            now_ms=NOW_MS + 1,
        )


def test_binding_reply_rejects_target_snapshot_and_lifetime_races() -> None:
    preparation = _preparation()
    changed_snapshot = _reply(preparation)
    changed_snapshot["snapshot"]["target_id"] = _opaque("other-target")
    with pytest.raises((AustinBindingRejected, david.SchemaRejected), match="snapshot"):
        AustinPreparedBinding.from_payload(
            preparation,
            _payload(changed_snapshot),
            now_ms=NOW_MS + 1,
        )

    for row in (
        _reply(preparation, observed_at_ms=NOW_MS + 2_002),
        _reply(preparation, expires_at_ms=NOW_MS + 5_001),
        _reply(preparation, expires_at_ms=NOW_MS + 1),
    ):
        with pytest.raises(AustinBindingRejected, match="binding_window"):
            AustinPreparedBinding.from_payload(
                preparation,
                _payload(row),
                now_ms=NOW_MS + 1,
            )


def test_binding_reply_requires_exact_final_operation_arguments() -> None:
    preparation = _preparation()
    for arguments in (
        {},
        {"element_id": _opaque("element"), "selector": "attacker"},
        {"element_id": "not-opaque"},
    ):
        with pytest.raises(david.SchemaRejected):
            AustinPreparedBinding.from_payload(
                preparation,
                _payload(_reply(preparation, arguments=arguments)),
                now_ms=NOW_MS + 1,
            )


def test_coordinate_binding_derives_viewport_and_preserves_exact_point() -> None:
    preparation = _preparation(
        operation=david.Operation.COORDINATE_ACTIVATE,
        route=david.ControlRoute.COORDINATE,
        selector="frontmost_point",
        arguments={"x": 100, "y": 200},
    )
    reply = _reply(
        preparation,
        expires_at_ms=NOW_MS + 2_000,
        arguments={
            "viewport_height": 900,
            "viewport_width": 1440,
            "x": 100,
            "y": 200,
        },
    )
    binding = AustinPreparedBinding.from_payload(
        preparation,
        _payload(reply),
        now_ms=NOW_MS + 1,
    )
    assert binding.arguments["viewport_width"] == 1440

    changed = deepcopy(reply)
    changed["arguments"]["x"] = 101
    with pytest.raises(AustinBindingRejected, match="binding_arguments"):
        AustinPreparedBinding.from_payload(
            preparation,
            _payload(changed),
            now_ms=NOW_MS + 1,
        )


def test_request_window_and_preparation_identity_cannot_be_rebound() -> None:
    preparation = _preparation()
    binding = AustinPreparedBinding.from_payload(
        preparation,
        _payload(_reply(preparation)),
        now_ms=NOW_MS + 1,
    )
    for issued, deadline in (
        (NOW_MS - 1, NOW_MS + 1_000),
        (NOW_MS + 5_000, NOW_MS + 5_001),
        (NOW_MS + 1, NOW_MS + 5_001),
    ):
        with pytest.raises(AustinBindingRejected, match="binding_request_window"):
            binding.control_request(
                preparation,
                session_id=_uuid(201),
                sequence=1,
                issued_at_ms=issued,
                deadline_ms=deadline,
                max_output_bytes=4_096,
            )

    other = _preparation(subject_id="runtime.attacker")
    with pytest.raises(AustinBindingRejected, match="binding_preparation_changed"):
        binding.control_request(
            other,
            session_id=_uuid(201),
            sequence=1,
            issued_at_ms=NOW_MS + 1,
            deadline_ms=NOW_MS + 1_000,
            max_output_bytes=4_096,
        )


def test_binding_errors_are_content_free_and_extra_fields_reject() -> None:
    preparation = _preparation()
    secret = "never-echo-binding-content"
    row = _reply(preparation)
    row["secret"] = secret
    with pytest.raises(AustinBindingRejected) as captured:
        AustinPreparedBinding.from_payload(
            preparation,
            _payload(row),
            now_ms=NOW_MS + 1,
        )
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)

    noncanonical = david.canonical_json_bytes(_reply(preparation)) + b"\n"
    with pytest.raises(AustinBindingRejected, match="binding_canonical"):
        AustinPreparedBinding.from_payload(
            preparation,
            noncanonical,
            now_ms=NOW_MS + 1,
        )
