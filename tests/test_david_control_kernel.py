from __future__ import annotations

import ast
import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
from typing import Any

import pytest

from algo_cli import david_control_kernel as david


NOW_MS = 1_800_000_000_000


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _opaque(label: str) -> str:
    return "hmac-sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


VALID_ARGUMENTS: dict[david.Operation, dict[str, Any]] = {
    david.Operation.OBSERVE: {},
    david.Operation.ACTIVATE: {"element_id": _opaque("button")},
    david.Operation.INPUT_TEXT: {
        "element_id": _opaque("input"),
        "replace": True,
        "text": "bounded input",
    },
    david.Operation.SELECT_OPTION: {
        "element_id": _opaque("select"),
        "option_id": _opaque("option"),
    },
    david.Operation.SCROLL: {
        "delta_x": 0,
        "delta_y": 200,
        "element_id": _opaque("viewport"),
    },
    david.Operation.UPLOAD: {
        "artifact_id": _uuid(8),
        "byte_count": 4096,
        "element_id": _opaque("upload"),
    },
    david.Operation.COORDINATE_ACTIVATE: {
        "viewport_height": 900,
        "viewport_width": 1440,
        "x": 120,
        "y": 300,
    },
    david.Operation.HANDOFF: {"reason_code": "secure_field"},
}

VALID_DATA_CLASSES = {
    david.Operation.OBSERVE: david.ControlDataClass.STRUCTURAL,
    david.Operation.ACTIVATE: david.ControlDataClass.STRUCTURAL,
    david.Operation.INPUT_TEXT: david.ControlDataClass.PRIVATE,
    david.Operation.SELECT_OPTION: david.ControlDataClass.PRIVATE,
    david.Operation.SCROLL: david.ControlDataClass.STRUCTURAL,
    david.Operation.UPLOAD: david.ControlDataClass.FILE,
    david.Operation.COORDINATE_ACTIVATE: david.ControlDataClass.STRUCTURAL,
    david.Operation.HANDOFF: david.ControlDataClass.SECRET,
}

VALID_ROUTES = {
    david.Operation.OBSERVE: (david.ControlRoute.CONNECTOR, david.ControlRoute.DOM),
    david.Operation.ACTIVATE: (david.ControlRoute.CONNECTOR, david.ControlRoute.DOM),
    david.Operation.INPUT_TEXT: (david.ControlRoute.CONNECTOR, david.ControlRoute.DOM),
    david.Operation.SELECT_OPTION: (david.ControlRoute.CONNECTOR, david.ControlRoute.DOM),
    david.Operation.SCROLL: (david.ControlRoute.CONNECTOR, david.ControlRoute.DOM),
    david.Operation.UPLOAD: (david.ControlRoute.CONNECTOR, david.ControlRoute.DOM),
    david.Operation.COORDINATE_ACTIVATE: (david.ControlRoute.COORDINATE,),
    david.Operation.HANDOFF: (david.ControlRoute.HANDOFF,),
}


@dataclass(frozen=True)
class ControlBundle:
    signer: david.ControlSigner
    policy: david.ControlPolicy
    request: david.ControlRequest
    grant: david.ControlGrant
    permit: david.ControlPermit
    envelope: david.ControlEnvelope


def _request_row(
    operation: david.Operation = david.Operation.ACTIVATE,
    *,
    arguments: dict[str, Any] | None = None,
    data_class: david.ControlDataClass | None = None,
    routes: tuple[david.ControlRoute, ...] | None = None,
    sequence: int = 1,
    target_id: str | None = None,
    target_epoch: int = 7,
    target_revision: str = "document-4",
    fencing_token: int = 11,
    issued_at_ms: int = NOW_MS - 100,
    deadline_ms: int = NOW_MS + 5_000,
    observed_at_ms: int = NOW_MS - 50,
) -> dict[str, Any]:
    opaque_target = target_id or _opaque("browser-target")
    return {
        "schema_version": david.CONTROL_SCHEMA_VERSION,
        "request_id": _uuid(1),
        "session_id": _uuid(2),
        "subject_id": "runtime.operator",
        "sequence": sequence,
        "issued_at_ms": issued_at_ms,
        "deadline_ms": deadline_ms,
        "target": {
            "kind": david.TargetKind.BROWSER_DOCUMENT.value,
            "target_id": opaque_target,
            "epoch": target_epoch,
            "revision": target_revision,
            "fencing_token": fencing_token,
        },
        "snapshot": {
            "snapshot_id": _uuid(3),
            "target_id": opaque_target,
            "epoch": target_epoch,
            "revision": target_revision,
            "fencing_token": fencing_token,
            "observed_at_ms": observed_at_ms,
            "sequence": sequence,
        },
        "operation": operation.value,
        "data_class": (data_class or VALID_DATA_CLASSES[operation]).value,
        "arguments": deepcopy(arguments if arguments is not None else VALID_ARGUMENTS[operation]),
        "requested_routes": [route.value for route in (routes if routes is not None else VALID_ROUTES[operation])],
        "max_output_bytes": 4096,
    }


def _bundle(
    operation: david.Operation = david.Operation.ACTIVATE,
    *,
    arguments: dict[str, Any] | None = None,
    routes: tuple[david.ControlRoute, ...] | None = None,
) -> ControlBundle:
    signer = david.ControlSigner.from_private_bytes(bytes(range(32)))
    policy = david.default_control_policy()
    request = david.ControlRequest.from_dict(_request_row(operation, arguments=arguments, routes=routes))
    grant = david.issue_grant(
        signer,
        policy,
        grant_id=_uuid(4),
        subject_id=request.subject_id,
        target_ids=(request.target.target_id,),
        target_kinds=(request.target.kind,),
        operations=tuple(david.Operation),
        data_classes=tuple(david.ControlDataClass),
        routes=(route for route in david.ROUTE_ORDER),
        issued_at_ms=NOW_MS - 1_000,
        expires_at_ms=NOW_MS + 10_000,
        maximum_action_count=16,
        max_input_bytes=policy.max_input_bytes,
        max_output_bytes=policy.max_output_bytes,
        max_transmit_bytes=policy.max_transmit_bytes,
    )
    permit = david.issue_permit(
        signer,
        signer.verifier,
        policy,
        grant,
        request,
        permit_id=_uuid(5),
        issued_at_ms=NOW_MS,
        expires_at_ms=NOW_MS + 1_000,
    )
    envelope = david.ControlEnvelope(request=request, grant=grant, permit=permit)
    return ControlBundle(signer, policy, request, grant, permit, envelope)


def _resign_permit(bundle: ControlBundle, **changes: Any) -> david.ControlPermit:
    unsigned = {**bundle.permit.unsigned_dict(), **changes}
    return david.ControlPermit.from_dict(
        {
            **unsigned,
            "signature": bundle.signer.sign("control_permit", unsigned),
        }
    )


def _resign_grant(bundle: ControlBundle, **changes: Any) -> david.ControlGrant:
    unsigned = {**bundle.grant.unsigned_dict(), **changes}
    return david.ControlGrant.from_dict(
        {
            **unsigned,
            "signature": bundle.signer.sign("control_grant", unsigned),
        }
    )


def _verify(bundle: ControlBundle, **changes: Any) -> david.ControlRoute:
    return david.verify_envelope_authority(
        changes.pop("envelope", bundle.envelope),
        changes.pop("verifier", bundle.signer.verifier),
        changes.pop("policy", bundle.policy),
        now_ms=changes.pop("now_ms", NOW_MS + 1),
        live_routes=changes.pop("live_routes", david.ROUTE_ORDER),
        live_snapshot=changes.pop("live_snapshot", bundle.request.snapshot),
        **changes,
    )


@pytest.mark.parametrize("operation", tuple(david.Operation))
def test_every_finite_operation_round_trips_and_authorizes(operation) -> None:
    bundle = _bundle(operation)
    decoded = david.ControlEnvelope.from_payload(bundle.envelope.to_frame()[4:])

    assert decoded == bundle.envelope
    assert _verify(bundle) in VALID_ROUTES[operation]
    assert decoded.request.effects == david.OPERATION_SPECS[operation].effects


def test_frame_decoder_accepts_fragmented_and_multiple_frames() -> None:
    frame = _bundle().envelope.to_frame()
    decoder = david.FrameDecoder()
    decoded: list[dict[str, Any]] = []
    for byte in frame:
        decoded.extend(decoder.feed(bytes((byte,))))
    decoder.finish()

    assert decoded == [_bundle().envelope.to_dict()]
    assert david.FrameDecoder().feed(frame + frame) == [decoded[0], decoded[0]]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"\xef\xbb\xbf{}", "json_bom"),
        (b'{"x":"\xff"}', "json_utf8"),
        (b'{"x":1,"x":2}', "json_duplicate_key"),
        (b'{"x":1.0}', "json_number"),
        (b'{"x":NaN}', "json_number"),
        (b'{"x":9007199254740992}', "json_integer"),
        (b'{"x":"\\ud800"}', "json_unicode"),
        (b"[]", "json_root"),
        (b"null", "json_type"),
        (b'{"unsafe-key":1}', "json_key"),
        (b'{"x":}', "json_syntax"),
    ],
)
def test_strict_json_rejects_ambiguous_or_unsafe_payloads(payload, reason) -> None:
    with pytest.raises(david.FrameRejected, match=reason):
        david.decode_json_payload(payload)


def test_json_depth_item_string_and_payload_bounds_reject() -> None:
    nested: Any = True
    for _ in range(david.MAX_JSON_DEPTH + 2):
        nested = {"x": nested}
    with pytest.raises(david.FrameRejected, match="json_bounds"):
        david.canonical_json_bytes(nested)

    with pytest.raises(david.FrameRejected, match="json_bounds"):
        david.canonical_json_bytes({"x": [True] * david.MAX_JSON_ITEMS})
    with pytest.raises(david.FrameRejected, match="json_string"):
        david.canonical_json_bytes({"x": "a" * (david.MAX_STRING_BYTES + 1)})
    with pytest.raises(david.FrameRejected, match="frame_size"):
        david.decode_json_payload(b"{" + b" " * david.MAX_FRAME_BYTES + b"}")


def test_decoder_is_fail_stop_after_bad_length_bad_payload_or_truncation() -> None:
    for bad in (
        struct.pack(">I", 0),
        struct.pack(">I", david.MAX_FRAME_BYTES + 1),
        struct.pack(">I", 2) + b"[]",
    ):
        decoder = david.FrameDecoder()
        with pytest.raises(david.FrameRejected):
            decoder.feed(bad)
        assert decoder.poisoned is True
        assert decoder.buffered_bytes == 0
        with pytest.raises(david.FrameRejected, match="decoder_poisoned"):
            decoder.feed(b"")

    decoder = david.FrameDecoder()
    decoder.feed(struct.pack(">I", 12) + b"{}")
    with pytest.raises(david.FrameRejected, match="frame_truncated"):
        decoder.finish()
    assert decoder.poisoned is True


def test_decoder_rejects_excessive_feed_and_invalid_input_type() -> None:
    for data in (b"x" * (david.MAX_FEED_BYTES + 1), bytearray(b"x")):
        decoder = david.FrameDecoder()
        with pytest.raises(david.FrameRejected, match="feed_size"):
            decoder.feed(data)  # type: ignore[arg-type]
        assert decoder.poisoned is True


@pytest.mark.parametrize("operation", tuple(david.Operation))
def test_operation_arguments_are_exact_closed_objects(operation) -> None:
    valid = deepcopy(VALID_ARGUMENTS[operation])
    assert david.validate_operation_arguments(operation, valid) == valid

    with pytest.raises(david.SchemaRejected, match="arguments_schema"):
        david.validate_operation_arguments(operation, {**valid, "script": "return true"})
    if valid:
        missing = dict(valid)
        missing.pop(next(iter(missing)))
        with pytest.raises(david.SchemaRejected, match="arguments_schema"):
            david.validate_operation_arguments(operation, missing)


@pytest.mark.parametrize(
    ("operation", "arguments", "reason"),
    [
        (david.Operation.INPUT_TEXT, {**VALID_ARGUMENTS[david.Operation.INPUT_TEXT], "replace": 1}, "replace"),
        (david.Operation.INPUT_TEXT, {**VALID_ARGUMENTS[david.Operation.INPUT_TEXT], "text": "a\x00b"}, "input_text"),
        (david.Operation.INPUT_TEXT, {**VALID_ARGUMENTS[david.Operation.INPUT_TEXT], "text": "a" * 4097}, "input_text"),
        (david.Operation.SCROLL, {**VALID_ARGUMENTS[david.Operation.SCROLL], "delta_y": 0}, "scroll_zero"),
        (david.Operation.SCROLL, {**VALID_ARGUMENTS[david.Operation.SCROLL], "delta_y": 10_001}, "delta_y"),
        (david.Operation.UPLOAD, {**VALID_ARGUMENTS[david.Operation.UPLOAD], "byte_count": 0}, "upload_bytes"),
        (
            david.Operation.COORDINATE_ACTIVATE,
            {**VALID_ARGUMENTS[david.Operation.COORDINATE_ACTIVATE], "x": 1440},
            "coordinate_x",
        ),
        (david.Operation.HANDOFF, {"reason_code": "run; shell"}, "reason_code"),
    ],
)
def test_operation_semantic_bounds_reject(operation, arguments, reason) -> None:
    with pytest.raises(david.SchemaRejected, match=reason):
        david.validate_operation_arguments(operation, arguments)


def test_request_rejects_extra_fields_wrong_pairing_and_generation_mixups() -> None:
    row = _request_row()
    with pytest.raises(david.SchemaRejected, match="request_schema"):
        david.ControlRequest.from_dict({**row, "effects": ["read"]})

    wrong_class = {**row, "data_class": david.ControlDataClass.SECRET.value}
    with pytest.raises(david.SchemaRejected, match="operation_data_class"):
        david.ControlRequest.from_dict(wrong_class)

    wrong_snapshot = deepcopy(row)
    wrong_snapshot["snapshot"]["epoch"] += 1
    with pytest.raises(david.SchemaRejected, match="snapshot_target"):
        david.ControlRequest.from_dict(wrong_snapshot)


def test_request_rejects_noncanonical_identifiers_deadlines_and_routes() -> None:
    mutations = (
        ("request_id", "00000000-0000-0000-0000-000000000000"),
        ("subject_id", "Runtime Operator"),
        ("sequence", True),
        ("deadline_ms", NOW_MS + david.MAX_DEADLINE_HORIZON_MS + 1),
        ("requested_routes", ["dom", "connector"]),
        ("requested_routes", ["dom", "dom"]),
    )
    for field, value in mutations:
        row = _request_row()
        row[field] = value
        with pytest.raises(david.SchemaRejected):
            david.ControlRequest.from_dict(row)


def test_generator_routes_are_materialized_once_when_grant_is_issued() -> None:
    bundle = _bundle()
    assert bundle.grant.routes == david.ROUTE_ORDER
    assert bundle.grant.effects == tuple(sorted(david.Effect, key=lambda item: item.value))


def test_signatures_bind_exact_canonical_bytes_and_pinned_key() -> None:
    bundle = _bundle()
    bundle.signer.verifier.verify(
        "control_grant",
        bundle.grant.unsigned_dict(),
        bundle.grant.signature,
    )
    changed = bundle.grant.unsigned_dict()
    changed["maximum_action_count"] -= 1
    with pytest.raises(david.AuthorityRejected, match="signature_invalid"):
        bundle.signer.verifier.verify("control_grant", changed, bundle.grant.signature)

    attacker = david.ControlSigner.generate()
    with pytest.raises(david.AuthorityRejected):
        david.verify_grant(
            bundle.grant,
            attacker.verifier,
            bundle.policy,
            now_ms=NOW_MS,
            subject_id=bundle.request.subject_id,
        )


def test_noncanonical_base64url_signature_spelling_rejects() -> None:
    bundle = _bundle()
    signature = bundle.permit.signature
    decoded = base64.urlsafe_b64decode(signature + "==")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    equivalent = next(
        signature[:-1] + candidate
        for candidate in alphabet
        if candidate != signature[-1] and base64.urlsafe_b64decode(signature[:-1] + candidate + "==") == decoded
    )
    permit = david.ControlPermit.from_dict({**bundle.permit.unsigned_dict(), "signature": equivalent})
    envelope = david.ControlEnvelope(bundle.request, bundle.grant, permit)

    with pytest.raises(david.AuthorityRejected, match="signature_encoding"):
        _verify(bundle, envelope=envelope)


def test_model_or_untrusted_peer_cannot_mint_a_valid_permit() -> None:
    bundle = _bundle()
    attacker = david.ControlSigner.generate()
    unsigned = bundle.permit.unsigned_dict()
    forged = david.ControlPermit.from_dict({**unsigned, "signature": attacker.sign("control_permit", unsigned)})
    envelope = david.ControlEnvelope(bundle.request, bundle.grant, forged)

    with pytest.raises(david.AuthorityRejected, match="signature_invalid"):
        _verify(bundle, envelope=envelope)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("grant_id", _uuid(9), "permit_grant"),
        ("subject_id", "runtime.attacker", "permit_subject"),
        ("request_id", _uuid(9), "permit_request"),
        ("request_digest", "sha256:" + "f" * 64, "permit_request_digest"),
        ("target_kind", david.TargetKind.DESKTOP_SURFACE.value, "permit_target_kind"),
        ("target_id", _opaque("other-target"), "permit_target"),
        ("target_epoch", 8, "permit_epoch"),
        ("target_revision", "document-5", "permit_revision"),
        ("fencing_token", 12, "permit_fence"),
        ("snapshot_id", _uuid(9), "permit_snapshot"),
        ("sequence", 2, "permit_sequence"),
        ("operation", david.Operation.OBSERVE.value, "permit_operation"),
        ("effects", [david.Effect.READ.value], "permit_effects"),
        ("data_class", david.ControlDataClass.PRIVATE.value, "permit_data_class"),
        ("input_bytes", 1, "permit_input"),
        ("output_bytes", 1, "permit_output"),
        ("transmit_bytes", 1, "permit_transmit"),
    ],
)
def test_even_authority_signed_permit_scope_mismatch_rejects(field, value, reason) -> None:
    bundle = _bundle()
    permit = _resign_permit(bundle, **{field: value})
    envelope = david.ControlEnvelope(bundle.request, bundle.grant, permit)
    with pytest.raises(david.PermitRejected, match=reason):
        _verify(bundle, envelope=envelope)


def test_policy_revalidates_signed_grant_instead_of_trusting_issuer_claims() -> None:
    bundle = _bundle()
    bad_effects = _resign_grant(
        bundle,
        effects=sorted(item.value for item in david.Effect if item is not david.Effect.READ),
    )
    with pytest.raises(david.PermitRejected, match="grant_effects"):
        david.verify_grant(
            bad_effects,
            bundle.signer.verifier,
            bundle.policy,
            now_ms=NOW_MS,
            subject_id=bundle.request.subject_id,
        )


def test_request_and_grant_tampering_reject_before_dispatch() -> None:
    bundle = _bundle()
    request_row = bundle.request.to_dict()
    request_row["arguments"] = {"element_id": _opaque("different-button")}
    changed_request = david.ControlRequest.from_dict(request_row)
    with pytest.raises(david.PermitRejected, match="permit_request_digest"):
        _verify(
            bundle,
            envelope=david.ControlEnvelope(changed_request, bundle.grant, bundle.permit),
            live_snapshot=changed_request.snapshot,
        )

    grant_row = bundle.grant.to_dict()
    grant_row["maximum_action_count"] -= 1
    changed_grant = david.ControlGrant.from_dict(grant_row)
    with pytest.raises(david.AuthorityRejected, match="signature_invalid"):
        _verify(bundle, envelope=david.ControlEnvelope(bundle.request, changed_grant, bundle.permit))


def test_expiry_staleness_policy_drift_and_live_snapshot_drift_reject() -> None:
    bundle = _bundle()
    with pytest.raises(david.PermitRejected, match="permit_expired"):
        _verify(bundle, now_ms=bundle.permit.expires_at_ms)

    stale_row = _request_row(observed_at_ms=NOW_MS - bundle.policy.max_snapshot_age_ms - 1)
    stale_request = david.ControlRequest.from_dict(stale_row)
    with pytest.raises(david.PermitRejected, match="snapshot_stale"):
        bundle.policy.validate_request(stale_request, now_ms=NOW_MS)

    changed_policy = david.ControlPolicy(
        policy_id=bundle.policy.policy_id,
        revision=bundle.policy.revision + 1,
        operation_routes=bundle.policy.operation_routes,
        allowed_target_kinds=bundle.policy.allowed_target_kinds,
        allowed_data_classes=bundle.policy.allowed_data_classes,
    )
    with pytest.raises(david.PermitRejected, match="grant_policy"):
        _verify(bundle, policy=changed_policy)

    snapshot_row = bundle.request.snapshot.to_dict()
    snapshot_row["fencing_token"] += 1
    with pytest.raises(david.PermitRejected, match="snapshot_changed"):
        _verify(bundle, live_snapshot=david.SnapshotRef.from_dict(snapshot_row))


def test_route_selection_is_deterministic_and_requires_a_live_adapter() -> None:
    bundle = _bundle(routes=(david.ControlRoute.CONNECTOR, david.ControlRoute.DOM))
    assert (
        _verify(
            bundle,
            live_routes=(david.ControlRoute.DOM, david.ControlRoute.CONNECTOR),
        )
        is david.ControlRoute.CONNECTOR
    )
    assert _verify(bundle, live_routes=(david.ControlRoute.DOM,)) is david.ControlRoute.DOM
    with pytest.raises(david.PermitRejected, match="no_route"):
        _verify(bundle, live_routes=(david.ControlRoute.AX,))


def _assert_closed_objects(schema: Any) -> None:
    if type(schema) is dict:
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
            assert set(schema.get("required", ())) == set(schema.get("properties", ()))
        for child in schema.values():
            _assert_closed_objects(child)
    elif type(schema) is list:
        for child in schema:
            _assert_closed_objects(child)


def _schema_patterns(schema: Any) -> list[str]:
    if type(schema) is dict:
        patterns = [schema["pattern"]] if type(schema.get("pattern")) is str else []
        for child in schema.values():
            patterns.extend(_schema_patterns(child))
        return patterns
    if type(schema) is list:
        patterns = []
        for child in schema:
            patterns.extend(_schema_patterns(child))
        return patterns
    return []


def test_exported_json_schemas_are_recursive_closed_and_operation_bound() -> None:
    schemas = (
        david.CONTROL_PREPARATION_SCHEMA,
        david.CONTROL_PREPARATION_ENVELOPE_SCHEMA,
        david.CONTROL_REQUEST_SCHEMA,
        david.CONTROL_GRANT_SCHEMA,
        david.CONTROL_PERMIT_SCHEMA,
        david.CONTROL_ENVELOPE_SCHEMA,
    )
    for schema in schemas:
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        _assert_closed_objects(schema)

    branches = david.CONTROL_REQUEST_SCHEMA["oneOf"]
    assert {branch["properties"]["operation"]["const"] for branch in branches} == {
        operation.value for operation in david.Operation
    }
    for operation in david.Operation:
        branch = next(item for item in branches if item["properties"]["operation"]["const"] == operation.value)
        assert branch["properties"]["arguments"] is david.ARGUMENT_SCHEMAS[operation.value]

    preparation_branches = david.CONTROL_PREPARATION_SCHEMA["oneOf"]
    assert len(preparation_branches) == len(david.PREPARATION_SELECTORS)
    assert {
        (
            branch["properties"]["operation"]["const"],
            branch["properties"]["route"]["const"],
        )
        for branch in preparation_branches
    } == {(operation.value, route.value) for operation, route in david.PREPARATION_SELECTORS}


def test_exported_json_schema_patterns_use_absolute_end_guards() -> None:
    patterns = []
    for schema in (
        david.CONTROL_PREPARATION_SCHEMA,
        david.CONTROL_PREPARATION_ENVELOPE_SCHEMA,
        david.CONTROL_REQUEST_SCHEMA,
        david.CONTROL_GRANT_SCHEMA,
        david.CONTROL_PERMIT_SCHEMA,
        david.CONTROL_ENVELOPE_SCHEMA,
    ):
        patterns.extend(_schema_patterns(schema))
    assert patterns
    assert all(pattern.startswith("^") for pattern in patterns)
    assert all(pattern.endswith(r"(?![\s\S])") for pattern in patterns)


def test_control_module_contains_no_dynamic_code_execution_calls() -> None:
    source_path = Path(david.__file__ or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden = {"eval", "exec", "compile", "__import__"}
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert calls.isdisjoint(forbidden)
    assert "script" not in {field for spec in david.OPERATION_SPECS.values() for field in spec.argument_fields}


def test_canonical_json_is_stable_and_content_free_errors_do_not_echo_input() -> None:
    left = {"z": "private text", "a": [3, True]}
    right = {"a": [3, True], "z": "private text"}
    assert david.canonical_json_bytes(left) == david.canonical_json_bytes(right)

    secret = "never-echo-this-secret"
    with pytest.raises(david.SchemaRejected) as captured:
        david.ControlRequest.from_dict({"secret": secret})
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)


def test_schema_and_frames_are_json_serializable_without_runtime_objects() -> None:
    bundle = _bundle()
    for value in (
        bundle.envelope.to_dict(),
        david.CONTROL_PREPARATION_SCHEMA,
        david.CONTROL_PREPARATION_ENVELOPE_SCHEMA,
        david.CONTROL_REQUEST_SCHEMA,
        david.CONTROL_GRANT_SCHEMA,
        david.CONTROL_PERMIT_SCHEMA,
        david.CONTROL_ENVELOPE_SCHEMA,
    ):
        json.dumps(value, allow_nan=False)
