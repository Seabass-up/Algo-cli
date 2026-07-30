from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
from typing import Any

import pytest

from algo_cli.carbon_browser_binding import (
    CARBON_MAX_LIFETIME_MS,
    CarbonBindingAuthority,
    CarbonBindingRejected,
    CarbonBrowserBinding,
    CarbonBrowserObservation,
    CarbonBrowserOperation,
    CarbonBrowserRoute,
    CarbonDocumentLifecycle,
    CarbonShadowMode,
    CarbonSignedBinding,
    CarbonSurfaceKind,
    validate_browser_action,
)


NOW_MS = 1_800_000_000_000


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _binding_dict(
    *,
    route: CarbonBrowserRoute = CarbonBrowserRoute.MANAGED_PUBLIC,
    operations: list[str] | None = None,
    element_token: str | None = None,
) -> dict[str, Any]:
    selected = route is CarbonBrowserRoute.SELECTED_TAB
    selected_operations = ["observe", "handoff"] if selected else ["activate", "handoff"]
    return {
        "schema_version": 1,
        "binding_id": _uuid(1),
        "route": route.value,
        "profile_id": _opaque("profile"),
        "browser_instance_id": _uuid(2),
        "window_id": 11,
        "tab_id": 22,
        "top_document_id": _uuid(3),
        "frame_id": 0,
        "frame_document_id": _uuid(3),
        "origin_digest": _opaque("origin"),
        "snapshot_id": _uuid(4),
        "snapshot_revision": 7,
        "element_token": element_token
        if element_token is not None
        else ("none" if selected else _opaque("element")),
        "operations": operations or selected_operations,
        "maximum_action_count": 2,
        "actions_used": 0,
        "issued_at_ms": NOW_MS,
        "expires_at_ms": NOW_MS + 10_000,
        "fencing_token": 9,
        "service_worker_generation": _uuid(5),
        "extension_version": "0.0.0",
        "extension_protocol": 1,
        "native_version": "0.0.0",
        "native_protocol": 1,
        "user_gesture_id": _uuid(6) if selected else "none",
        "incognito": False,
    }


def _binding(**kwargs: Any) -> CarbonBrowserBinding:
    return CarbonBrowserBinding.from_dict(_binding_dict(**kwargs))


def _observation_dict(binding: CarbonBrowserBinding) -> dict[str, Any]:
    return {
        "profile_id": binding.profile_id,
        "browser_instance_id": binding.browser_instance_id,
        "window_id": binding.window_id,
        "tab_id": binding.tab_id,
        "top_document_id": binding.top_document_id,
        "frame_id": binding.frame_id,
        "frame_document_id": binding.frame_document_id,
        "origin_digest": binding.origin_digest,
        "snapshot_id": binding.snapshot_id,
        "snapshot_revision": binding.snapshot_revision,
        "element_token": binding.element_token,
        "fencing_token": binding.fencing_token,
        "service_worker_generation": binding.service_worker_generation,
        "extension_version": binding.extension_version,
        "extension_protocol": binding.extension_protocol,
        "native_version": binding.native_version,
        "native_protocol": binding.native_protocol,
        "active_tab_granted": True,
        "incognito": False,
        "lifecycle": "active",
        "surface_kind": "dom",
        "shadow_mode": "none",
        "dialog_open": False,
        "popup_count": 0,
        "download_attempted": False,
        "upload_picker_open": False,
        "frame_attached": True,
    }


def _observation(binding: CarbonBrowserBinding) -> CarbonBrowserObservation:
    return CarbonBrowserObservation.from_dict(_observation_dict(binding))


def test_managed_element_action_consumes_exactly_one_count() -> None:
    binding = _binding()
    observation = _observation(binding)
    consumed = validate_browser_action(
        binding,
        observation,
        CarbonBrowserOperation.ACTIVATE,
        now_ms=NOW_MS + 1,
        expected_fencing_token=binding.fencing_token,
        element_token=binding.element_token,
    )
    assert consumed.actions_used == 1
    assert binding.actions_used == 0


def test_signed_binding_round_trip_and_canonical_signature() -> None:
    authority = CarbonBindingAuthority(bytes(range(32)))
    binding = _binding()
    signed = authority.sign(binding)
    assert signed.key_id == authority.key_id
    assert len(signed.signature) == 43
    assert authority.verify(CarbonSignedBinding.from_dict(signed.to_dict())) == binding

    decoded = base64.urlsafe_b64decode(signed.signature + "=")
    assert base64.urlsafe_b64encode(decoded).decode().rstrip("=") == signed.signature


def test_tampering_and_wrong_authority_are_rejected() -> None:
    binding = _binding()
    authority = CarbonBindingAuthority(bytes(range(32)))
    signed = authority.sign(binding)
    tampered = CarbonSignedBinding(replace(binding, tab_id=99), signed.key_id, signed.signature)
    with pytest.raises(CarbonBindingRejected, match="binding_signature"):
        authority.verify(tampered)
    with pytest.raises(CarbonBindingRejected, match="binding_authority"):
        CarbonBindingAuthority(bytes(range(1, 33))).verify(signed)


def test_forged_exact_dataclass_instances_are_revalidated() -> None:
    binding = replace(_binding(), maximum_action_count=True)  # type: ignore[arg-type]
    observation = _observation(_binding())
    with pytest.raises(CarbonBindingRejected, match="maximum_action_count"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.ACTIVATE,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
            element_token=_opaque("element"),
        )

    authority = CarbonBindingAuthority(bytes(range(32)))
    with pytest.raises(CarbonBindingRejected, match="maximum_action_count"):
        authority.sign(binding)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("profile_id", _opaque("other-profile"), "profile_id_changed"),
        ("browser_instance_id", _uuid(80), "browser_instance_id_changed"),
        ("window_id", 90, "window_id_changed"),
        ("tab_id", 91, "tab_id_changed"),
        ("top_document_id", _uuid(81), "top_document_id_changed"),
        ("frame_id", 2, "frame_id_changed"),
        ("frame_document_id", _uuid(82), "frame_document_id_changed"),
        ("origin_digest", _opaque("other-origin"), "origin_digest_changed"),
        ("snapshot_id", _uuid(83), "snapshot_id_changed"),
        ("snapshot_revision", 8, "snapshot_revision_changed"),
        ("service_worker_generation", _uuid(84), "service_worker_generation_changed"),
        ("extension_version", "0.0.1", "extension_version_changed"),
        ("extension_protocol", 2, "extension_protocol_changed"),
        ("native_version", "0.0.1", "native_version_changed"),
        ("native_protocol", 2, "native_protocol_changed"),
    ],
)
def test_every_target_generation_and_protocol_field_is_exactly_bound(
    field: str,
    value: Any,
    reason: str,
) -> None:
    binding = _binding()
    observation = replace(_observation(binding), **{field: value})
    with pytest.raises(CarbonBindingRejected, match=reason):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.ACTIVATE,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
            element_token=binding.element_token,
        )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("lifecycle", CarbonDocumentLifecycle.BFCACHE, "document_not_active"),
        ("lifecycle", CarbonDocumentLifecycle.PRERENDER, "document_not_active"),
        ("lifecycle", CarbonDocumentLifecycle.DISCARDED, "document_not_active"),
        ("frame_attached", False, "frame_detached"),
        ("dialog_open", True, "dialog_handoff"),
        ("popup_count", 1, "popup_handoff"),
        ("download_attempted", True, "download_denied"),
        ("upload_picker_open", True, "upload_selection_unconfirmed"),
        ("shadow_mode", CarbonShadowMode.CLOSED, "closed_shadow_handoff"),
    ],
)
def test_lifecycle_and_ambient_effects_fail_before_mutation(
    field: str,
    value: Any,
    reason: str,
) -> None:
    binding = _binding()
    observation = replace(_observation(binding), **{field: value})
    with pytest.raises(CarbonBindingRejected, match=reason):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.ACTIVATE,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
            element_token=binding.element_token,
        )


@pytest.mark.parametrize(
    "surface",
    [
        CarbonSurfaceKind.CANVAS,
        CarbonSurfaceKind.PDF,
        CarbonSurfaceKind.INTERNAL,
        CarbonSurfaceKind.AUTH,
        CarbonSurfaceKind.PASSKEY,
        CarbonSurfaceKind.CAPTCHA,
        CarbonSurfaceKind.SECURE_FIELD,
        CarbonSurfaceKind.UNKNOWN,
    ],
)
def test_non_dom_surfaces_require_handoff(surface: CarbonSurfaceKind) -> None:
    binding = _binding()
    observation = replace(_observation(binding), surface_kind=surface)
    with pytest.raises(CarbonBindingRejected, match="surface_handoff"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.ACTIVATE,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
            element_token=binding.element_token,
        )


def test_handoff_remains_available_for_unsupported_and_ambient_states() -> None:
    binding = _binding()
    observation = replace(
        _observation(binding),
        lifecycle=CarbonDocumentLifecycle.BFCACHE,
        surface_kind=CarbonSurfaceKind.PASSKEY,
        dialog_open=True,
        popup_count=3,
        download_attempted=True,
        upload_picker_open=True,
        shadow_mode=CarbonShadowMode.CLOSED,
    )
    # Lifecycle and exact target binding are checked before handoff; a cached
    # document must first obtain a fresh binding even though no mutation occurs.
    with pytest.raises(CarbonBindingRejected, match="document_not_active"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.HANDOFF,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
        )
    observation = replace(observation, lifecycle=CarbonDocumentLifecycle.ACTIVE)
    consumed = validate_browser_action(
        binding,
        observation,
        CarbonBrowserOperation.HANDOFF,
        now_ms=NOW_MS + 1,
        expected_fencing_token=9,
    )
    assert consumed.actions_used == 1


def test_selected_tab_is_observe_and_handoff_only_and_requires_active_tab() -> None:
    selected = _binding(route=CarbonBrowserRoute.SELECTED_TAB)
    observation = _observation(selected)
    consumed = validate_browser_action(
        selected,
        observation,
        CarbonBrowserOperation.OBSERVE,
        now_ms=NOW_MS + 1,
        expected_fencing_token=9,
    )
    assert consumed.actions_used == 1

    with pytest.raises(CarbonBindingRejected, match="active_tab_revoked"):
        validate_browser_action(
            selected,
            replace(observation, active_tab_granted=False),
            CarbonBrowserOperation.OBSERVE,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
        )

    row = _binding_dict(route=CarbonBrowserRoute.SELECTED_TAB, operations=["activate"])
    row["element_token"] = _opaque("element")
    with pytest.raises(CarbonBindingRejected, match="selected_tab_observe_only"):
        CarbonBrowserBinding.from_dict(row)


def test_element_token_operation_and_fence_are_all_exact() -> None:
    binding = _binding()
    observation = _observation(binding)
    with pytest.raises(CarbonBindingRejected, match="element_token_changed"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.ACTIVATE,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
            element_token=_opaque("other"),
        )
    with pytest.raises(CarbonBindingRejected, match="fencing_token_changed"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.ACTIVATE,
            now_ms=NOW_MS + 1,
            expected_fencing_token=10,
            element_token=binding.element_token,
        )
    with pytest.raises(CarbonBindingRejected, match="operation_not_granted"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.INPUT_TEXT,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
            element_token=binding.element_token,
        )


def test_expiry_clock_and_action_count_are_fail_closed() -> None:
    binding = _binding()
    observation = _observation(binding)
    with pytest.raises(CarbonBindingRejected, match="clock_regression"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.HANDOFF,
            now_ms=NOW_MS - 1,
            expected_fencing_token=9,
        )
    with pytest.raises(CarbonBindingRejected, match="binding_expired"):
        validate_browser_action(
            binding,
            observation,
            CarbonBrowserOperation.HANDOFF,
            now_ms=binding.expires_at_ms,
            expected_fencing_token=9,
        )
    exhausted = replace(binding, actions_used=binding.maximum_action_count)
    with pytest.raises(CarbonBindingRejected, match="action_count_exhausted"):
        validate_browser_action(
            exhausted,
            observation,
            CarbonBrowserOperation.HANDOFF,
            now_ms=NOW_MS + 1,
            expected_fencing_token=9,
        )


def test_closed_shapes_types_lifetime_gesture_and_incognito_are_rejected() -> None:
    row = _binding_dict()
    row["extra"] = "field"
    with pytest.raises(CarbonBindingRejected, match="binding_schema"):
        CarbonBrowserBinding.from_dict(row)

    for field, value, reason in (
        ("incognito", True, "incognito_denied"),
        ("maximum_action_count", True, "maximum_action_count"),
        ("operations", ["activate", "activate"], "operations"),
        ("user_gesture_id", _uuid(99), "user_gesture_unexpected"),
    ):
        row = _binding_dict()
        row[field] = value
        with pytest.raises(CarbonBindingRejected, match=reason):
            CarbonBrowserBinding.from_dict(row)

    row = _binding_dict()
    row["expires_at_ms"] = NOW_MS + CARBON_MAX_LIFETIME_MS + 1
    with pytest.raises(CarbonBindingRejected, match="binding_lifetime"):
        CarbonBrowserBinding.from_dict(row)

    selected = _binding_dict(route=CarbonBrowserRoute.SELECTED_TAB)
    selected["user_gesture_id"] = "none"
    with pytest.raises(CarbonBindingRejected, match="user_gesture_id"):
        CarbonBrowserBinding.from_dict(selected)


def test_binding_and_observation_serialization_contains_no_raw_origin_or_selector() -> None:
    binding = _binding()
    observation = _observation(binding)
    combined = repr(binding.to_dict()) + repr(observation.to_dict())
    assert "https://" not in combined
    assert "selector" not in combined
    assert "cookie" not in combined
    assert "password" not in combined
