"""Single-permit stdin entry protocol for the Xenon broker container."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from typing import Any, BinaryIO, Callable, Iterable, Mapping, NoReturn

from .xenon_browser_broker import (
    XENON_BROKER_PROTOCOL_VERSION,
    XENON_BROKER_SCHEMA_VERSION,
    XenonBrokerPermit,
    XenonBrokerRejected,
    XenonBrokerServer,
    XenonBrokerSession,
    XenonEphemeralCertificateAuthority,
    verify_xenon_broker_permit,
)


XENON_ENTRY_MAX_FRAME_BYTES = 131_072
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class XenonEntryRejected(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject(reason_code: str) -> NoReturn:
    raise XenonEntryRejected(reason_code)


def _pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in row:
            _reject("json_duplicate_key")
        row[key] = value
    return row


def _constant(_value: str) -> NoReturn:
    _reject("json_constant")


def _bound(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if count is None:
        count = [0]
    if depth > 8:
        _reject("json_depth")
    count[0] += 1
    if count[0] > 256:
        _reject("json_items")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        _reject("json_float")
    if type(value) is str:
        if len(value.encode("utf-8")) > 65_536:
            _reject("json_string")
        return
    if type(value) is list:
        for item in value:
            _bound(item, depth=depth + 1, count=count)
        return
    if type(value) is dict:
        for key, item in value.items():
            _bound(key, depth=depth + 1, count=count)
            _bound(item, depth=depth + 1, count=count)
        return
    _reject("json_type")


def _decode_key(value: Any) -> bytes:
    if type(value) is not str or not value or "=" in value or len(value) > 256:
        _reject("authority_key")
    try:
        key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError):
        _reject("authority_key")
    if not 32 <= len(key) <= 64:
        _reject("authority_key")
    if base64.urlsafe_b64encode(key).decode("ascii").rstrip("=") != value:
        _reject("authority_key")
    return key


@dataclass(frozen=True, slots=True)
class XenonStartConfig:
    schema_version: int
    protocol_version: int
    type: str
    authority_key_base64url: str = field(repr=False)
    permit: XenonBrokerPermit = field(repr=False)
    expected_fencing_token: int

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> XenonStartConfig:
        fields = {
            "schema_version",
            "protocol_version",
            "type",
            "authority_key_base64url",
            "permit",
            "expected_fencing_token",
        }
        if type(row) is not dict or set(row) != fields:
            _reject("start_schema")
        if row["type"] != "xenon.start":
            _reject("start_type")
        if row["schema_version"] != XENON_BROKER_SCHEMA_VERSION:
            _reject("schema_version")
        if row["protocol_version"] != XENON_BROKER_PROTOCOL_VERSION:
            _reject("protocol_version")
        _decode_key(row["authority_key_base64url"])
        try:
            permit = XenonBrokerPermit.from_dict(row["permit"])
        except XenonBrokerRejected as error:
            _reject("permit_" + error.reason_code)
        fence = row["expected_fencing_token"]
        if type(fence) is not int or not 1 <= fence <= (1 << 53) - 1:
            _reject("expected_fencing_token")
        return cls(
            XENON_BROKER_SCHEMA_VERSION,
            XENON_BROKER_PROTOCOL_VERSION,
            "xenon.start",
            row["authority_key_base64url"],
            permit,
            fence,
        )

    @property
    def authority_key(self) -> bytes:
        return _decode_key(self.authority_key_base64url)


def read_xenon_entry_frame(stream: BinaryIO) -> dict[str, Any]:
    if not hasattr(stream, "read"):
        _reject("entry_stream")
    buffer = bytearray()
    while True:
        try:
            chunk = stream.read(
                min(16_384, XENON_ENTRY_MAX_FRAME_BYTES + 1 - len(buffer))
            )
        except (OSError, AttributeError, TypeError, ValueError):
            _reject("entry_stream")
        if not chunk:
            _reject("entry_truncated")
        if type(chunk) is not bytes:
            _reject("entry_stream")
        buffer.extend(chunk)
        if len(buffer) > XENON_ENTRY_MAX_FRAME_BYTES:
            _reject("entry_frame_size")
        try:
            end = buffer.index(0)
        except ValueError:
            continue
        if end == 0 or end != len(buffer) - 1:
            _reject("entry_frame_count")
        try:
            value = json.loads(
                bytes(buffer[:end]).decode("utf-8", errors="strict"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
            )
        except UnicodeDecodeError:
            _reject("entry_utf8")
        except json.JSONDecodeError:
            _reject("entry_json")
        if type(value) is not dict:
            _reject("entry_object")
        _bound(value)
        return value


def write_xenon_entry_frame(stream: BinaryIO, row: Mapping[str, Any]) -> None:
    if type(row) is not dict:
        _reject("entry_object")
    _bound(row)
    try:
        payload = json.dumps(
            row,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\x00"
        if len(payload) > XENON_ENTRY_MAX_FRAME_BYTES:
            _reject("entry_frame_size")
        stream.write(payload)
        stream.flush()
    except XenonEntryRejected:
        raise
    except (OSError, AttributeError, TypeError, ValueError):
        _reject("entry_write")


def _resolver(host: str, port: int) -> Iterable[str]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        _reject("dns_resolution_failed")
    return tuple(str(row[4][0]) for row in rows)


def prepare_xenon_start(
    config: XenonStartConfig,
    *,
    resolver: Callable[[str, int], Iterable[str]] = _resolver,
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> tuple[XenonBrokerSession, dict[str, Any]]:
    if type(config) is not XenonStartConfig or not callable(resolver):
        _reject("entry_config")
    now_ms = clock_ms()
    try:
        permit, egress, target = verify_xenon_broker_permit(
            config.permit,
            authority_key=config.authority_key,
            resolver=resolver,
            now_ms=now_ms,
            expected_fencing_token=config.expected_fencing_token,
        )
        ca = XenonEphemeralCertificateAuthority.create(
            now_ms=now_ms,
            expires_at_ms=permit.expires_at_ms,
        )
        session = XenonBrokerSession(permit, egress, target, ca, clock_ms=clock_ms)
    except XenonBrokerRejected as error:
        _reject("start_" + error.reason_code)
    ca_pem = ca.certificate_pem
    ca_encoding = base64.urlsafe_b64encode(ca_pem).decode("ascii").rstrip("=")
    ready = {
        "schema_version": XENON_BROKER_SCHEMA_VERSION,
        "protocol_version": XENON_BROKER_PROTOCOL_VERSION,
        "type": "xenon.ready",
        "ca_pem_base64url": ca_encoding,
        "ca_pem_digest": "sha256:" + hashlib.sha256(ca_pem).hexdigest(),
        "ca_certificate_digest": ca.certificate_digest,
        "permit_id": permit.permit_id,
        "target_decision_digest": target.decision_digest,
    }
    return session, ready


def main(
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    source = stdin or sys.stdin.buffer
    target = stdout or sys.stdout.buffer
    env = dict(os.environ if environment is None else environment)
    try:
        config = XenonStartConfig.from_dict(read_xenon_entry_frame(source))
        session, ready = prepare_xenon_start(config)
        listen_address = env.get("XENON_LISTEN_ADDRESS", "")
        raw_port = env.get("XENON_LISTEN_PORT", "")
        if not raw_port.isdigit():
            _reject("listen_port")
        server = XenonBrokerServer(
            session,
            listen_address=listen_address,
            listen_port=int(raw_port),
        )
        errors: list[str] = []

        def serve() -> None:
            try:
                server.serve()
            except XenonBrokerRejected as error:
                errors.append(error.reason_code)
            except Exception:
                errors.append("server_unknown")

        thread = threading.Thread(target=serve)
        prior_sigterm: Any = None
        signal_installed = False
        if threading.current_thread() is threading.main_thread():
            prior_sigterm = signal.getsignal(signal.SIGTERM)

            def stop_server(_signum: int, _frame: Any) -> None:
                server.stop()

            signal.signal(signal.SIGTERM, stop_server)
            signal_installed = True
        thread.start()
        try:
            if not server.ready_event.wait(timeout=5):
                _reject(errors[0] if errors else "server_start_timeout")
            write_xenon_entry_frame(target, ready)
            thread.join()
        finally:
            if thread.is_alive():
                server.stop()
                thread.join(timeout=2)
            if signal_installed:
                signal.signal(signal.SIGTERM, prior_sigterm)
        if errors and errors[-1] != "broker_expired":
            _reject(errors[-1])
        evidence = session.evidence()
        write_xenon_entry_frame(
            target,
            {
                "schema_version": XENON_BROKER_SCHEMA_VERSION,
                "protocol_version": XENON_BROKER_PROTOCOL_VERSION,
                "type": "xenon.result",
                "disposition": evidence.disposition.value,
                "connection_count": evidence.connection_count,
                "active_peak": evidence.active_peak,
                "request_count": evidence.request_count,
                "redirect_count": evidence.redirect_count,
                "bytes_to_browser": evidence.bytes_to_browser,
                "target_decision_digest": evidence.target_decision_digest,
                "ca_certificate_digest": evidence.ca_digest,
                "reason_code": evidence.reason_code,
            },
        )
    except XenonEntryRejected as error:
        write_xenon_entry_frame(
            target,
            {
                "schema_version": XENON_BROKER_SCHEMA_VERSION,
                "protocol_version": XENON_BROKER_PROTOCOL_VERSION,
                "type": "xenon.error",
                "reason_code": error.reason_code,
            },
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "XENON_ENTRY_MAX_FRAME_BYTES",
    "XenonEntryRejected",
    "XenonStartConfig",
    "main",
    "prepare_xenon_start",
    "read_xenon_entry_frame",
    "write_xenon_entry_frame",
]
