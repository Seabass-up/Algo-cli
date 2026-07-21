from __future__ import annotations

import base64
from io import BytesIO
import json
import threading
import time
from typing import Iterable
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes
import pytest

import algo_cli.xenon_browser_entry as entry_module
from algo_cli.xenon_browser_broker import (
    XENON_BROKER_PROTOCOL_VERSION,
    XENON_BROKER_SCHEMA_VERSION,
    XenonBrokerPermit,
    issue_xenon_broker_permit,
)
from algo_cli.xenon_browser_entry import (
    XENON_ENTRY_MAX_FRAME_BYTES,
    XenonEntryRejected,
    XenonStartConfig,
    main,
    prepare_xenon_start,
    read_xenon_entry_frame,
    write_xenon_entry_frame,
)


KEY = b"x" * 32
NOW_MS = int(time.time() * 1000)
URL = "https://example.com/start?q=1"


def _resolver(*answers: str):
    def resolve(_host: str, _port: int) -> Iterable[str]:
        return answers or ("8.8.8.8",)

    return resolve


def _uuids():
    values = iter(
        (
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            uuid.UUID("22222222-2222-4222-8222-222222222222"),
        )
    )
    return lambda: next(values)


def _permit(**overrides) -> XenonBrokerPermit:
    values = {
        "authority_key": KEY,
        "raw_url": URL,
        "resolver": _resolver(),
        "issued_at_ms": NOW_MS,
        "expires_at_ms": NOW_MS + 60_000,
        "fencing_token": 7,
        "uuid_factory": _uuids(),
    }
    values.update(overrides)
    return issue_xenon_broker_permit(**values)


def _row(permit: XenonBrokerPermit | None = None) -> dict[str, object]:
    return {
        "schema_version": XENON_BROKER_SCHEMA_VERSION,
        "protocol_version": XENON_BROKER_PROTOCOL_VERSION,
        "type": "xenon.start",
        "authority_key_base64url": base64.urlsafe_b64encode(KEY)
        .decode("ascii")
        .rstrip("="),
        "permit": (permit or _permit()).to_dict(),
        "expected_fencing_token": 7,
    }


def test_start_config_is_exact_and_hides_key_and_permit() -> None:
    config = XenonStartConfig.from_dict(_row())
    assert config.authority_key == KEY
    assert config.permit.canonical_url == URL
    assert URL not in repr(config)
    assert config.authority_key_base64url not in repr(config)
    row = _row()
    row["unexpected"] = True
    with pytest.raises(XenonEntryRejected, match="start_schema"):
        XenonStartConfig.from_dict(row)


def test_prepare_reverifies_permit_and_returns_public_ca_only() -> None:
    config = XenonStartConfig.from_dict(_row())
    session, ready = prepare_xenon_start(
        config,
        resolver=_resolver(),
        clock_ms=lambda: NOW_MS + 1,
    )
    pem = base64.urlsafe_b64decode(
        str(ready["ca_pem_base64url"])
        + "=" * (-len(str(ready["ca_pem_base64url"])) % 4)
    )
    certificate = x509.load_pem_x509_certificate(pem)
    assert ready["ca_certificate_digest"] == (
        "sha256:" + certificate.fingerprint(hashes.SHA256()).hex()
    )
    assert ready["ca_pem_digest"] == (
        "sha256:" + __import__("hashlib").sha256(pem).hexdigest()
    )
    assert b"PRIVATE KEY" not in pem
    assert "authority_key" not in ready
    assert URL not in json.dumps(ready)
    assert session.permit == config.permit


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda row: row.__setitem__("expected_fencing_token", 8), "start_permit_fence_changed"),
        (lambda row: row.__setitem__("authority_key_base64url", "e" * 43), "authority_key"),
        (lambda row: row.__setitem__("protocol_version", 2), "protocol_version"),
    ],
)
def test_prepare_and_config_fail_closed_on_authority_changes(change, reason: str) -> None:
    row = _row()
    change(row)
    if reason == "start_permit_fence_changed":
        config = XenonStartConfig.from_dict(row)
        with pytest.raises(XenonEntryRejected, match=reason):
            prepare_xenon_start(
                config,
                resolver=_resolver(),
                clock_ms=lambda: NOW_MS + 1,
            )
    else:
        with pytest.raises(XenonEntryRejected, match=reason):
            XenonStartConfig.from_dict(row)


def test_prepare_rejects_expiry_and_dns_rebinding() -> None:
    config = XenonStartConfig.from_dict(_row())
    with pytest.raises(XenonEntryRejected, match="start_permit_expired"):
        prepare_xenon_start(
            config,
            resolver=_resolver(),
            clock_ms=lambda: config.permit.expires_at_ms,
        )
    with pytest.raises(XenonEntryRejected, match="start_permit_non_public_address"):
        prepare_xenon_start(
            config,
            resolver=_resolver("127.0.0.1"),
            clock_ms=lambda: NOW_MS + 1,
        )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b'{"a":1}', "entry_truncated"),
        (b'{"a":1}\x00{"b":2}\x00', "entry_frame_count"),
        (b'{"a":1,"a":2}\x00', "json_duplicate_key"),
        (b'{"a":1.5}\x00', "json_float"),
        (b"[]\x00", "entry_object"),
        (b"a" * (XENON_ENTRY_MAX_FRAME_BYTES + 1), "entry_frame_size"),
    ],
)
def test_entry_reader_rejects_open_ambiguous_and_oversized_frames(
    payload: bytes, reason: str
) -> None:
    with pytest.raises(XenonEntryRejected, match=reason):
        read_xenon_entry_frame(BytesIO(payload))


def test_entry_frames_round_trip_and_stream_errors_are_normalized() -> None:
    stream = BytesIO()
    write_xenon_entry_frame(stream, {"type": "test", "count": 1})
    assert read_xenon_entry_frame(BytesIO(stream.getvalue())) == {
        "type": "test",
        "count": 1,
    }

    class Broken:
        def read(self, _size: int) -> bytes:
            raise OSError("raw path and secret")

    with pytest.raises(XenonEntryRejected, match="entry_stream"):
        read_xenon_entry_frame(Broken())  # type: ignore[arg-type]


def test_main_uses_explicit_environment_and_normalizes_malformed_input() -> None:
    output = BytesIO()
    assert main(BytesIO(b"{}\x00"), output, environment={}) == 2
    assert read_xenon_entry_frame(BytesIO(output.getvalue())) == {
        "protocol_version": XENON_BROKER_PROTOCOL_VERSION,
        "reason_code": "start_schema",
        "schema_version": XENON_BROKER_SCHEMA_VERSION,
        "type": "xenon.error",
    }


def test_main_emits_ready_after_server_bind_and_normalizes_thread_failure(monkeypatch) -> None:
    class FakeServer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ready_event = threading.Event()
            self.ready_event.set()

        def serve(self) -> None:
            raise RuntimeError("raw URL and secret")

        def stop(self) -> None:
            pass

    monkeypatch.setattr(entry_module, "XenonBrokerServer", FakeServer)
    encoded = BytesIO()
    write_xenon_entry_frame(encoded, _row())
    output = BytesIO()
    assert main(
        BytesIO(encoded.getvalue()),
        output,
        environment={"XENON_LISTEN_ADDRESS": "172.30.0.3", "XENON_LISTEN_PORT": "3128"},
    ) == 2
    frames = output.getvalue().split(b"\x00")
    assert len(frames) == 3 and frames[-1] == b""
    ready = json.loads(frames[0])
    error = json.loads(frames[1])
    assert ready["type"] == "xenon.ready"
    assert error["reason_code"] == "server_unknown"
    assert URL not in output.getvalue().decode("ascii")


def test_main_emits_terminal_structural_evidence_after_clean_stop(monkeypatch) -> None:
    class FakeServer:
        def __init__(self, *_args, **_kwargs) -> None:
            self.ready_event = threading.Event()
            self.ready_event.set()

        def serve(self) -> None:
            return None

        def stop(self) -> None:
            pass

    monkeypatch.setattr(entry_module, "XenonBrokerServer", FakeServer)
    encoded = BytesIO()
    write_xenon_entry_frame(encoded, _row())
    output = BytesIO()
    assert main(
        BytesIO(encoded.getvalue()),
        output,
        environment={"XENON_LISTEN_ADDRESS": "172.30.0.3", "XENON_LISTEN_PORT": "3128"},
    ) == 0
    frames = output.getvalue().split(b"\x00")
    assert len(frames) == 3 and frames[-1] == b""
    ready = json.loads(frames[0])
    result = json.loads(frames[1])
    assert ready["type"] == "xenon.ready"
    assert result["type"] == "xenon.result"
    assert result["disposition"] == "verified"
    assert result["connection_count"] == 0
    assert result["ca_certificate_digest"] == ready["ca_certificate_digest"]
    assert URL not in output.getvalue().decode("ascii")
