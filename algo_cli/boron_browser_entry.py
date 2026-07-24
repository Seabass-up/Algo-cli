"""Single-plan stdin entry protocol for the Boron browser container."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import re
import sys
import time
from typing import Any, BinaryIO, Callable, Mapping, NoReturn
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from .boron_browser_wrapper import (
    BoronNavigationEvidence,
    BoronNavigationPlan,
    BoronPipeRejected,
    decode_boron_pipe_message,
    encode_boron_pipe_message,
    install_ephemeral_xenon_ca,
    run_boron_navigation,
)


BORON_ENTRY_SCHEMA_VERSION = 1
BORON_ENTRY_PROTOCOL_VERSION = 1
BORON_ENTRY_MAX_FRAME_BYTES = 131_072

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class BoronEntryRejected(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject(reason_code: str) -> NoReturn:
    raise BoronEntryRejected(reason_code)


def _uuid(value: Any, label: str) -> str:
    if type(value) is not str or not _UUID_RE.fullmatch(value):
        _reject(label)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        _reject(label)
    if str(parsed) != value or parsed.int == 0:
        _reject(label)
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(label)
    return value


def _decode_base64url(value: Any, label: str, maximum: int) -> bytes:
    if type(value) is not str or not value or "=" in value or len(value) > 200_000:
        _reject(label)
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError):
        _reject(label)
    if not decoded or len(decoded) > maximum:
        _reject(label)
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != value:
        _reject(label)
    return decoded


@dataclass(frozen=True, slots=True)
class BoronStartConfig:
    schema_version: int
    protocol_version: int
    type: str
    session_id: str
    canonical_url: str = field(repr=False)
    expected_browser_version: str
    proxy_host: str
    proxy_port: int
    maximum_duration_ms: int
    ca_pem_base64url: str = field(repr=False)
    ca_pem_digest: str
    ca_certificate_digest: str

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> BoronStartConfig:
        if type(row) is not dict or frozenset(row) != frozenset(cls.__dataclass_fields__):
            _reject("start_schema")
        if row["type"] != "boron.start":
            _reject("start_type")
        ca = _decode_base64url(row["ca_pem_base64url"], "ca_encoding", 16_384)
        ca_pem_digest = row["ca_pem_digest"]
        if type(ca_pem_digest) is not str or not _DIGEST_RE.fullmatch(ca_pem_digest):
            _reject("ca_pem_digest")
        actual_pem_digest = "sha256:" + hashlib.sha256(ca).hexdigest()
        if actual_pem_digest != ca_pem_digest:
            _reject("ca_pem_digest_mismatch")
        ca_certificate_digest = row["ca_certificate_digest"]
        if (
            type(ca_certificate_digest) is not str
            or not _DIGEST_RE.fullmatch(ca_certificate_digest)
        ):
            _reject("ca_certificate_digest")
        try:
            certificate = x509.load_pem_x509_certificate(ca)
        except ValueError:
            _reject("ca_certificate")
        actual_certificate_digest = (
            "sha256:" + certificate.fingerprint(hashes.SHA256()).hex()
        )
        if actual_certificate_digest != ca_certificate_digest:
            _reject("ca_certificate_digest_mismatch")
        try:
            plan = BoronNavigationPlan(
                row["canonical_url"],
                row["expected_browser_version"],
                row["proxy_host"],
                row["proxy_port"],
                row["maximum_duration_ms"],
            )
        except BoronPipeRejected as error:
            _reject("plan_" + error.reason_code)
        return cls(
            schema_version=_integer(
                row["schema_version"],
                "schema_version",
                BORON_ENTRY_SCHEMA_VERSION,
                BORON_ENTRY_SCHEMA_VERSION,
            ),
            protocol_version=_integer(
                row["protocol_version"],
                "protocol_version",
                BORON_ENTRY_PROTOCOL_VERSION,
                BORON_ENTRY_PROTOCOL_VERSION,
            ),
            type="boron.start",
            session_id=_uuid(row["session_id"], "session_id"),
            canonical_url=plan.canonical_url,
            expected_browser_version=plan.expected_browser_version,
            proxy_host=plan.proxy_host,
            proxy_port=plan.proxy_port,
            maximum_duration_ms=plan.maximum_duration_ms,
            ca_pem_base64url=row["ca_pem_base64url"],
            ca_pem_digest=ca_pem_digest,
            ca_certificate_digest=ca_certificate_digest,
        )

    @property
    def ca_pem(self) -> bytes:
        return _decode_base64url(self.ca_pem_base64url, "ca_encoding", 16_384)

    @property
    def plan(self) -> BoronNavigationPlan:
        return BoronNavigationPlan(
            self.canonical_url,
            self.expected_browser_version,
            self.proxy_host,
            self.proxy_port,
            self.maximum_duration_ms,
        )


def read_boron_entry_frame(stream: BinaryIO) -> dict[str, Any]:
    if not hasattr(stream, "read"):
        _reject("entry_stream")
    buffer = bytearray()
    while True:
        try:
            chunk = stream.read(
                min(16_384, BORON_ENTRY_MAX_FRAME_BYTES + 1 - len(buffer))
            )
        except (OSError, AttributeError, TypeError, ValueError):
            _reject("entry_stream")
        if not chunk:
            _reject("entry_truncated")
        if type(chunk) is not bytes:
            _reject("entry_stream")
        buffer.extend(chunk)
        if len(buffer) > BORON_ENTRY_MAX_FRAME_BYTES:
            _reject("entry_frame_size")
        try:
            end = buffer.index(0)
        except ValueError:
            continue
        if end == 0 or end != len(buffer) - 1:
            _reject("entry_frame_count")
        try:
            return decode_boron_pipe_message(bytes(buffer[:end]))
        except BoronPipeRejected as error:
            _reject("entry_" + error.reason_code)


def write_boron_entry_frame(stream: BinaryIO, row: Mapping[str, Any]) -> None:
    try:
        payload = encode_boron_pipe_message(row)
        stream.write(payload)
        stream.flush()
    except BoronPipeRejected as error:
        _reject("entry_" + error.reason_code)
    except (OSError, AttributeError):
        _reject("entry_write")


NavigationRunner = Callable[[BoronNavigationPlan], BoronNavigationEvidence]
CaInstaller = Callable[..., str]


def execute_boron_start(
    config: BoronStartConfig,
    *,
    navigation_runner: NavigationRunner = run_boron_navigation,
    ca_installer: CaInstaller = install_ephemeral_xenon_ca,
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> dict[str, Any]:
    if type(config) is not BoronStartConfig or not callable(navigation_runner):
        _reject("entry_config")
    try:
        installed_digest = ca_installer(config.ca_pem, now_ms=clock_ms())
    except BoronPipeRejected as error:
        _reject("ca_" + error.reason_code)
    if installed_digest != config.ca_certificate_digest:
        _reject("ca_install_digest")
    try:
        evidence = navigation_runner(config.plan)
    except BoronPipeRejected as error:
        _reject("navigation_" + error.reason_code)
    if type(evidence) is not BoronNavigationEvidence:
        _reject("navigation_evidence")
    return {
        "schema_version": BORON_ENTRY_SCHEMA_VERSION,
        "protocol_version": BORON_ENTRY_PROTOCOL_VERSION,
        "type": "boron.result",
        "state": evidence.state.value,
        "browser_major": evidence.browser_major,
        "command_count": evidence.command_count,
        "event_count": evidence.event_count,
        "child_frame_count": evidence.child_frame_count,
        "origin_digest": evidence.origin_digest,
        "frame_digest": evidence.frame_digest,
        "loader_digest": evidence.loader_digest,
        "reason_code": evidence.reason_code,
        "ca_pem_digest": config.ca_pem_digest,
        "ca_certificate_digest": config.ca_certificate_digest,
    }


def main(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    source = stdin or sys.stdin.buffer
    target = stdout or sys.stdout.buffer
    try:
        config = BoronStartConfig.from_dict(read_boron_entry_frame(source))
        result = execute_boron_start(config)
    except BoronEntryRejected as error:
        write_boron_entry_frame(
            target,
            {
                "schema_version": BORON_ENTRY_SCHEMA_VERSION,
                "protocol_version": BORON_ENTRY_PROTOCOL_VERSION,
                "type": "boron.error",
                "reason_code": error.reason_code,
            },
        )
        return 2
    write_boron_entry_frame(target, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BORON_ENTRY_MAX_FRAME_BYTES",
    "BORON_ENTRY_PROTOCOL_VERSION",
    "BORON_ENTRY_SCHEMA_VERSION",
    "BoronEntryRejected",
    "BoronStartConfig",
    "execute_boron_start",
    "main",
    "read_boron_entry_frame",
    "write_boron_entry_frame",
]
