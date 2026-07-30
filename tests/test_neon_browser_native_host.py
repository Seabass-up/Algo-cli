from __future__ import annotations

import io
import json
import struct
from typing import Any, Iterable
import uuid

import pytest

from algo_cli.neon_browser_native_host import (
    NEON_MAX_BUFFER_BYTES,
    NEON_MAX_FRAME_BYTES,
    NeonHostSession,
    NeonNativeFrameDecoder,
    NeonNativeRejected,
    decode_neon_message,
    encode_neon_message,
    run_neon_native_host,
)


ORIGIN = "chrome-extension://" + "a" * 32 + "/"
PUBLIC_IP = "8.8.8.8"


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


class UUIDs:
    def __init__(self, start: int = 100) -> None:
        self.value = start

    def __call__(self) -> uuid.UUID:
        value = uuid.UUID(_uuid(self.value))
        self.value += 1
        return value


def _resolver(_host: str, _port: int) -> Iterable[str]:
    return (PUBLIC_IP,)


def _session(*, resolver=_resolver) -> NeonHostSession:
    return NeonHostSession(
        actual_extension_origin=ORIGIN,
        allowed_extension_origin=ORIGIN,
        authority_key=bytes(range(32)),
        resolver=resolver,
        uuid_factory=UUIDs(),
    )


def _hello(**updates: Any) -> dict[str, Any]:
    row = {
        "schema_version": 1,
        "protocol_version": 1,
        "type": "neon.hello",
        "request_id": _uuid(1),
        "extension_version": "0.0.0",
        "worker_generation": _uuid(2),
        "user_gesture_id": _uuid(3),
        "window_id": 11,
        "tab_id": 22,
        "incognito": False,
    }
    row.update(updates)
    return row


def _observe(session_id_value: str, **updates: Any) -> dict[str, Any]:
    row = {
        "schema_version": 1,
        "protocol_version": 1,
        "type": "neon.observe",
        "request_id": _uuid(4),
        "session_id": session_id_value,
        "extension_version": "0.0.0",
        "worker_generation": _uuid(2),
        "user_gesture_id": _uuid(3),
        "window_id": 11,
        "tab_id": 22,
        "frame_id": 0,
        "document_id": _uuid(5),
        "origin": "https://example.com",
        "surface_kind": "dom",
        "content_type": "text/html",
        "secure_field_count": 0,
        "upload_control_count": 0,
        "canvas_count": 0,
        "frame_count": 0,
        "shadow_host_count": 0,
        "incognito": False,
    }
    row.update(updates)
    return row


def test_frame_round_trip_is_incremental_and_bounded() -> None:
    message = _hello()
    encoded = encode_neon_message(message)
    decoder = NeonNativeFrameDecoder()
    output: list[dict[str, Any]] = []
    for byte in encoded:
        output.extend(decoder.feed(bytes([byte])))
        assert decoder.buffered_bytes <= len(encoded)
    decoder.finish()
    assert output == [message]


def test_multiple_frames_decode_without_cross_frame_confusion() -> None:
    decoder = NeonNativeFrameDecoder()
    first = _hello()
    second = {**_hello(), "request_id": _uuid(9)}
    assert decoder.feed(encode_neon_message(first) + encode_neon_message(second)) == [
        first,
        second,
    ]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "frame_size"),
        (b"\xff", "frame_utf8"),
        (b"[]", "message_object"),
        (b'{"x":1,"x":2}', "json_duplicate_key"),
        (b'{"x":NaN}', "json_constant"),
        (b'{"x":1.5}', "json_float"),
        (b"{", "frame_json"),
    ],
)
def test_malformed_json_fails_with_content_free_reason(payload: bytes, reason: str) -> None:
    with pytest.raises(NeonNativeRejected, match=reason):
        decode_neon_message(payload)


def test_frame_size_buffer_and_truncation_limits_fail_closed() -> None:
    decoder = NeonNativeFrameDecoder()
    with pytest.raises(NeonNativeRejected, match="frame_size"):
        decoder.feed(struct.pack("@I", NEON_MAX_FRAME_BYTES + 1))
    with pytest.raises(NeonNativeRejected, match="feed_size"):
        NeonNativeFrameDecoder().feed(b"x" * (NEON_MAX_BUFFER_BYTES + 1))
    decoder = NeonNativeFrameDecoder()
    decoder.feed(struct.pack("@I", 10) + b"{}")
    with pytest.raises(NeonNativeRejected, match="frame_truncated"):
        decoder.finish()


def test_hello_then_public_top_document_observation_returns_only_opaque_state() -> None:
    session = _session()
    hello = session.handle(_hello())
    assert hello == {
        "schema_version": 1,
        "protocol_version": 1,
        "type": "neon.hello_ack",
        "request_id": _uuid(1),
        "session_id": _uuid(100),
        "native_version": "0.0.0",
    }
    observed = session.handle(_observe(hello["session_id"]))
    assert observed["type"] == "neon.observe_ack"
    assert observed["mode"] == "observe_only"
    assert observed["binding_id"] == _uuid(101)
    assert observed["snapshot_id"] == _uuid(102)
    assert observed["fencing_token"] == 1
    assert observed["profile_id"].startswith("hmac-sha256:")
    assert observed["origin_digest"].startswith("hmac-sha256:")
    assert observed["document_digest"].startswith("hmac-sha256:")
    rendered = repr(observed)
    assert "example.com" not in rendered
    assert _uuid(5) not in rendered
    assert "selector" not in rendered
    assert "cookie" not in rendered


def test_extension_origin_is_exact_and_wildcards_are_impossible() -> None:
    for actual, allowed in (
        ("chrome-extension://" + "b" * 32 + "/", ORIGIN),
        (ORIGIN, "chrome-extension://*/"),
        ("https://example.com/", "https://example.com/"),
    ):
        with pytest.raises(NeonNativeRejected, match="extension_origin"):
            NeonHostSession(
                actual_extension_origin=actual,
                allowed_extension_origin=allowed,
                authority_key=bytes(range(32)),
                resolver=_resolver,
            )


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"extension_version": "0.0.1"}, "extension_version_skew"),
        ({"protocol_version": 2}, "protocol_version"),
        ({"incognito": True}, "incognito_denied"),
        ({"tab_id": True}, "tab_id"),
        ({"request_id": "none"}, "request_id"),
    ],
)
def test_hello_version_identity_types_and_incognito_are_strict(
    updates: dict[str, Any],
    reason: str,
) -> None:
    with pytest.raises(NeonNativeRejected, match=reason):
        _session().handle(_hello(**updates))


def test_hello_and_observe_are_single_use_and_ordered() -> None:
    session = _session()
    with pytest.raises(NeonNativeRejected, match="hello_required"):
        session.handle(_observe(_uuid(100)))
    hello = session.handle(_hello())
    with pytest.raises(NeonNativeRejected, match="hello_replayed"):
        session.handle(_hello(request_id=_uuid(8)))
    session.handle(_observe(hello["session_id"]))
    with pytest.raises(NeonNativeRejected, match="observe_replayed"):
        session.handle(_observe(hello["session_id"], request_id=_uuid(9)))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("session_id", _uuid(90), "session_changed"),
        ("extension_version", "0.0.1", "extension_version_skew"),
        ("worker_generation", _uuid(91), "worker_generation_changed"),
        ("user_gesture_id", _uuid(92), "user_gesture_id_changed"),
        ("window_id", 44, "window_id_changed"),
        ("tab_id", 45, "tab_id_changed"),
        ("frame_id", 1, "frame_id"),
        ("incognito", True, "incognito_denied"),
        ("surface_kind", "screenshot", "surface_kind"),
        ("content_type", "text/html; charset=utf-8", "content_type"),
        ("secure_field_count", -1, "secure_field_count"),
    ],
)
def test_observation_binding_fields_and_classification_are_exact(
    field: str,
    value: Any,
    reason: str,
) -> None:
    session = _session()
    hello = session.handle(_hello())
    with pytest.raises(NeonNativeRejected, match=reason):
        session.handle(_observe(hello["session_id"], **{field: value}))


def test_request_id_cannot_be_reused_across_message_types() -> None:
    session = _session()
    hello = session.handle(_hello())
    with pytest.raises(NeonNativeRejected, match="request_replayed"):
        session.handle(_observe(hello["session_id"], request_id=_uuid(1)))


@pytest.mark.parametrize(
    ("origin", "answers", "reason"),
    [
        ("chrome://settings", (PUBLIC_IP,), "scheme_denied"),
        ("file:///etc/passwd", (PUBLIC_IP,), "scheme_denied"),
        ("https://localhost", (PUBLIC_IP,), "local_discovery_denied"),
        ("https://example.com", ("127.0.0.1",), "non_public_address"),
        ("https://example.com:8443", (PUBLIC_IP,), "port_denied"),
    ],
)
def test_internal_private_and_nonstandard_selected_origins_are_denied(
    origin: str,
    answers: tuple[str, ...],
    reason: str,
) -> None:
    def resolver(_host: str, _port: int) -> Iterable[str]:
        return answers

    session = _session(resolver=resolver)
    hello = session.handle(_hello())
    with pytest.raises(NeonNativeRejected, match=reason):
        session.handle(_observe(hello["session_id"], origin=origin))


def test_unknown_messages_and_extra_fields_fail_closed() -> None:
    with pytest.raises(NeonNativeRejected, match="message_type"):
        _session().handle({"type": "neon.execute"})
    row = _hello()
    row["selector"] = "#danger"
    with pytest.raises(NeonNativeRejected, match="hello_schema"):
        _session().handle(row)


def test_stdio_loop_processes_closed_frames_and_stops_cleanly_at_eof() -> None:
    hello = _hello()
    # The deterministic UUID factory makes the session ID known after hello.
    observe = _observe(_uuid(100))
    input_stream = io.BytesIO(encode_neon_message(hello) + encode_neon_message(observe))
    output_stream = io.BytesIO()
    assert (
        run_neon_native_host(
            _session(),
            input_stream=input_stream,
            output_stream=output_stream,
        )
        == 0
    )
    decoder = NeonNativeFrameDecoder()
    responses = decoder.feed(output_stream.getvalue())
    decoder.finish()
    assert [row["type"] for row in responses] == ["neon.hello_ack", "neon.observe_ack"]


def test_decoder_never_accepts_chrome_document_content_or_nested_programs() -> None:
    row = _hello()
    row["program"] = {"javascript": "fetch('https://example.com')"}
    encoded = json.dumps(row).encode()
    decoded = decode_neon_message(encoded)
    with pytest.raises(NeonNativeRejected, match="hello_schema"):
        _session().handle(decoded)
