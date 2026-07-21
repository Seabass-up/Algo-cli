from __future__ import annotations

from copy import deepcopy
import hashlib
from types import MappingProxyType

import pytest

from algo_cli import david_control_kernel as david


NOW_MS = 1_800_000_000_000


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _issue(
    *,
    operation: david.Operation = david.Operation.ACTIVATE,
    data_class: david.ControlDataClass = david.ControlDataClass.STRUCTURAL,
    route: david.ControlRoute = david.ControlRoute.AX,
    selector: str = "focused_element",
    arguments: dict[str, object] | None = None,
    expires_at_ms: int = NOW_MS + 5_000,
) -> tuple[david.ControlSigner, david.ControlPolicy, david.ControlPreparationEnvelope]:
    signer = david.ControlSigner.from_private_bytes(bytes(range(32)))
    policy = david.default_control_policy()
    envelope = david.issue_control_preparation(
        signer,
        policy,
        preparation_id=_uuid(601),
        request_id=_uuid(101),
        subject_id="runtime.operator",
        operation=operation,
        data_class=data_class,
        route=route,
        selector=selector,
        arguments={} if arguments is None else arguments,
        issued_at_ms=NOW_MS - 100,
        expires_at_ms=expires_at_ms,
    )
    return signer, policy, envelope


def test_target_free_preparation_round_trips_and_verifies_exact_authority() -> None:
    signer, policy, envelope = _issue()
    payload = david.canonical_json_bytes(envelope.to_dict())
    parsed = david.ControlPreparationEnvelope.from_payload(payload)
    verified = david.verify_control_preparation(
        parsed,
        signer.verifier,
        policy,
        now_ms=NOW_MS,
    )

    assert verified == envelope.preparation
    assert verified.request_id == _uuid(101)
    assert verified.digest.startswith("sha256:")
    encoded = envelope.to_dict()
    assert set(encoded) == {"message_type", "preparation", "protocol_version"}
    assert not {"target", "snapshot", "grant", "permit"} & set(encoded["preparation"])
    assert "element_id" not in encoded["preparation"]["arguments"]
    # Shared with AustinAuthorityTests.swift to pin Python/Swift canonical JSON
    # and Ed25519 domain separation to one exact cross-language vector.
    assert verified.signature == (
        "dRCGQbDBtg3yJDXaQb1swCb3IC_53yLYHHtXwZrKqfrkL8mHCxD9gDtfNJE_"
        "BRhASs-MNFPvIWSPDsxGPjfbAA"
    )
    assert verified.digest == (
        "sha256:2bb14fc1187d0d7681c32352b25972443f9de83a6c4ce2de19e860d884c8f5f3"
    )


@pytest.mark.parametrize(
    ("operation", "data_class", "route", "selector", "arguments"),
    [
        (
            david.Operation.ACTIVATE,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.AX,
            "focused_element",
            {},
        ),
        (
            david.Operation.SELECT_OPTION,
            david.ControlDataClass.PRIVATE,
            david.ControlRoute.AX,
            "focused_element",
            {"option_id": _opaque("option")},
        ),
        (
            david.Operation.SCROLL,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.AX,
            "focused_element",
            {"delta_x": 0, "delta_y": 100},
        ),
        (
            david.Operation.ACTIVATE,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.APPLE_EVENT,
            "activate_finder",
            {},
        ),
        (
            david.Operation.ACTIVATE,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.SHORTCUT,
            "review_current_task",
            {},
        ),
        (
            david.Operation.COORDINATE_ACTIVATE,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.COORDINATE,
            "frontmost_point",
            {"x": 100, "y": 200},
        ),
        (
            david.Operation.OBSERVE,
            david.ControlDataClass.PRIVATE,
            david.ControlRoute.SCREENSHOT,
            "persistent_programmatic",
            {},
        ),
    ],
)
def test_closed_preparation_matrix_accepts_only_reviewed_native_routes(
    operation: david.Operation,
    data_class: david.ControlDataClass,
    route: david.ControlRoute,
    selector: str,
    arguments: dict[str, object],
) -> None:
    signer, policy, envelope = _issue(
        operation=operation,
        data_class=data_class,
        route=route,
        selector=selector,
        arguments=arguments,
    )
    assert (
        david.verify_control_preparation(
            envelope,
            signer.verifier,
            policy,
            now_ms=NOW_MS,
        ).selector
        == selector
    )


@pytest.mark.parametrize(
    ("operation", "data_class", "route", "selector", "arguments"),
    [
        (
            david.Operation.ACTIVATE,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.COORDINATE,
            "frontmost_point",
            {},
        ),
        (
            david.Operation.INPUT_TEXT,
            david.ControlDataClass.PRIVATE,
            david.ControlRoute.AX,
            "focused_element",
            {"replace": True, "text": "must-not-cross-preparation"},
        ),
        (
            david.Operation.SCROLL,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.AX,
            "focused_element",
            {"delta_x": 0, "delta_y": 0},
        ),
        (
            david.Operation.OBSERVE,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.SCREENSHOT,
            "picker_scoped",
            {},
        ),
        (
            david.Operation.ACTIVATE,
            david.ControlDataClass.STRUCTURAL,
            david.ControlRoute.APPLE_EVENT,
            "caller_bundle_identifier",
            {},
        ),
    ],
)
def test_unreviewed_route_selector_and_argument_combinations_fail_before_signing(
    operation: david.Operation,
    data_class: david.ControlDataClass,
    route: david.ControlRoute,
    selector: str,
    arguments: dict[str, object],
) -> None:
    with pytest.raises((ValueError, david.SchemaRejected)):
        _issue(
            operation=operation,
            data_class=data_class,
            route=route,
            selector=selector,
            arguments=arguments,
        )


def test_tamper_wrong_key_replay_window_and_extra_fields_fail_closed() -> None:
    signer, policy, envelope = _issue(
        route=david.ControlRoute.APPLE_EVENT,
        selector="activate_finder",
    )
    changed = deepcopy(envelope.to_dict())
    changed["preparation"]["selector"] = "activate_system_settings"
    tampered = david.ControlPreparationEnvelope.from_dict(changed)
    with pytest.raises(david.AuthorityRejected, match="signature_invalid"):
        david.verify_control_preparation(
            tampered,
            signer.verifier,
            policy,
            now_ms=NOW_MS,
        )

    attacker = david.ControlSigner.generate()
    with pytest.raises(david.AuthorityRejected, match="preparation_key"):
        david.verify_control_preparation(
            envelope,
            attacker.verifier,
            policy,
            now_ms=NOW_MS,
        )
    with pytest.raises(david.PermitRejected, match="preparation_expired"):
        david.verify_control_preparation(
            envelope,
            signer.verifier,
            policy,
            now_ms=envelope.preparation.expires_at_ms,
        )

    extra = deepcopy(envelope.to_dict())
    extra["preparation"]["target_id"] = _opaque("attacker")
    with pytest.raises(david.SchemaRejected, match="preparation_schema"):
        david.ControlPreparationEnvelope.from_dict(extra)


def test_preparation_lifetime_is_bounded_and_arguments_require_plain_dict() -> None:
    with pytest.raises(david.SchemaRejected, match="preparation_window"):
        _issue(expires_at_ms=NOW_MS + david.MAX_PREPARATION_LIFETIME_MS + 1)
    signer = david.ControlSigner.from_private_bytes(bytes(range(32)))
    with pytest.raises(ValueError, match="preparation arguments"):
        david.issue_control_preparation(
            signer,
            david.default_control_policy(),
            preparation_id=_uuid(601),
            request_id=_uuid(101),
            subject_id="runtime.operator",
            operation=david.Operation.ACTIVATE,
            data_class=david.ControlDataClass.STRUCTURAL,
            route=david.ControlRoute.AX,
            selector="focused_element",
            arguments=MappingProxyType({}),  # type: ignore[arg-type]
            issued_at_ms=NOW_MS - 100,
            expires_at_ms=NOW_MS + 5_000,
        )
