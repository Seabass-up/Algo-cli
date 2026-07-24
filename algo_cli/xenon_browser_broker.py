"""Bounded TLS-mediating egress broker for the disabled browser foundation.

An ordinary HTTPS CONNECT proxy cannot see redirects or WebSocket upgrades.
Xenon therefore terminates one ephemeral browser-facing TLS connection, checks
and forwards only finite HTTP/1.1 GET/HEAD requests, and independently validates
the upstream certificate and pinned peer.  The CA private key lives only in the
broker process and its temporary cert-chain files live only on tmpfs long enough
for ``SSLContext.load_cert_chain``.

The broker is not registered as an Algo CLI action and has no generic proxy or
caller-supplied command mode.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, NoReturn, Protocol, Sequence
from urllib.parse import urljoin
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .xenon_browser_egress import (
    Resolver,
    XenonEgressPolicy,
    XenonEgressRejected,
    XenonEgressSession,
    XenonResolvedTarget,
    resolve_public_url,
)


XENON_BROKER_SCHEMA_VERSION = 1
XENON_BROKER_PROTOCOL_VERSION = 1
XENON_MAX_HEADER_BYTES = 65_536
XENON_MAX_HEADER_COUNT = 128
XENON_MAX_LINE_BYTES = 8_192
XENON_MAX_CONNECTIONS = 64
XENON_MAX_ACTIVE_CONNECTIONS = 16
XENON_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
XENON_MAX_TOTAL_BYTES = 128 * 1024 * 1024
XENON_MAX_LIFETIME_MS = 300_000
XENON_SOCKET_TIMEOUT_SECONDS = 15.0

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_KEY_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TOKEN_RE = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")

_REQUEST_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_RESPONSE_STRIP_HEADERS = frozenset(
    {
        "alt-svc",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-connection",
        "upgrade",
    }
)
_WEBSOCKET_HEADERS = frozenset(
    {
        "sec-websocket-accept",
        "sec-websocket-extensions",
        "sec-websocket-key",
        "sec-websocket-protocol",
        "sec-websocket-version",
    }
)


class XenonBrokerRejected(ValueError):
    """A permit, HTTP message, TLS peer, or broker transition failed closed."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class XenonBrokerDisposition(str, Enum):
    VERIFIED = "verified"
    BLOCKED = "blocked"
    HANDOFF = "handoff"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _reject(reason_code: str) -> NoReturn:
    raise XenonBrokerRejected(reason_code)


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(label)
    return value


def _canonical_uuid(value: Any, label: str) -> str:
    if type(value) is not str or not _UUID_RE.fullmatch(value):
        _reject(label)
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        _reject(label)
    if str(parsed) != value or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        _reject(label)
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        _reject("permit_json")


def _b64_signature(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _signature_bytes(value: Any) -> bytes:
    if type(value) is not str or not _SIGNATURE_RE.fullmatch(value):
        _reject("permit_signature")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, UnicodeError):
        _reject("permit_signature")
    if len(decoded) != 32 or _b64_signature(decoded) != value:
        _reject("permit_signature")
    return decoded


def _key_id(authority_key: bytes) -> str:
    return "hmac-sha256:" + hmac.new(
        authority_key,
        b"algo-cli/xenon-browser-broker/key-id/v1",
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class XenonBrokerPermit:
    schema_version: int
    protocol_version: int
    permit_id: str
    session_id: str
    canonical_url: str = field(repr=False)
    issued_at_ms: int
    expires_at_ms: int
    fencing_token: int
    maximum_connections: int
    maximum_active_connections: int
    maximum_response_bytes: int
    maximum_total_bytes: int
    maximum_redirects: int
    key_id: str
    signature: str = field(repr=False)

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> XenonBrokerPermit:
        if type(row) is not dict or frozenset(row) != frozenset(cls.__dataclass_fields__):
            _reject("permit_schema")
        issued = _integer(row["issued_at_ms"], "permit_issued", 1, (1 << 53) - 1)
        expires = _integer(row["expires_at_ms"], "permit_expires", 1, (1 << 53) - 1)
        if expires <= issued or expires - issued > XENON_MAX_LIFETIME_MS:
            _reject("permit_lifetime")
        canonical_url = row["canonical_url"]
        if type(canonical_url) is not str or not canonical_url:
            _reject("permit_url")
        key_id = row["key_id"]
        if type(key_id) is not str or not _KEY_ID_RE.fullmatch(key_id):
            _reject("permit_key_id")
        signature = row["signature"]
        _signature_bytes(signature)
        maximum_connections = _integer(
            row["maximum_connections"],
            "permit_connections",
            1,
            XENON_MAX_CONNECTIONS,
        )
        maximum_active_connections = _integer(
            row["maximum_active_connections"],
            "permit_active_connections",
            1,
            XENON_MAX_ACTIVE_CONNECTIONS,
        )
        maximum_response_bytes = _integer(
            row["maximum_response_bytes"],
            "permit_response_bytes",
            1,
            XENON_MAX_RESPONSE_BYTES,
        )
        maximum_total_bytes = _integer(
            row["maximum_total_bytes"],
            "permit_total_bytes",
            1,
            XENON_MAX_TOTAL_BYTES,
        )
        if maximum_active_connections > maximum_connections:
            _reject("permit_active_connections")
        if maximum_response_bytes > maximum_total_bytes:
            _reject("permit_total_bytes")
        return cls(
            schema_version=_integer(
                row["schema_version"],
                "permit_schema_version",
                XENON_BROKER_SCHEMA_VERSION,
                XENON_BROKER_SCHEMA_VERSION,
            ),
            protocol_version=_integer(
                row["protocol_version"],
                "permit_protocol_version",
                XENON_BROKER_PROTOCOL_VERSION,
                XENON_BROKER_PROTOCOL_VERSION,
            ),
            permit_id=_canonical_uuid(row["permit_id"], "permit_id"),
            session_id=_canonical_uuid(row["session_id"], "session_id"),
            canonical_url=canonical_url,
            issued_at_ms=issued,
            expires_at_ms=expires,
            fencing_token=_integer(row["fencing_token"], "permit_fence", 1, (1 << 53) - 1),
            maximum_connections=maximum_connections,
            maximum_active_connections=maximum_active_connections,
            maximum_response_bytes=maximum_response_bytes,
            maximum_total_bytes=maximum_total_bytes,
            maximum_redirects=_integer(row["maximum_redirects"], "permit_redirects", 0, 20),
            key_id=key_id,
            signature=signature,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "permit_id": self.permit_id,
            "session_id": self.session_id,
            "canonical_url": self.canonical_url,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "fencing_token": self.fencing_token,
            "maximum_connections": self.maximum_connections,
            "maximum_active_connections": self.maximum_active_connections,
            "maximum_response_bytes": self.maximum_response_bytes,
            "maximum_total_bytes": self.maximum_total_bytes,
            "maximum_redirects": self.maximum_redirects,
            "key_id": self.key_id,
            "signature": self.signature,
        }

    def unsigned_dict(self) -> dict[str, Any]:
        row = self.to_dict()
        del row["signature"]
        return row


def issue_xenon_broker_permit(
    *,
    authority_key: bytes,
    raw_url: str,
    resolver: Resolver,
    issued_at_ms: int,
    expires_at_ms: int,
    fencing_token: int,
    maximum_connections: int = 32,
    maximum_active_connections: int = 8,
    maximum_response_bytes: int = 16 * 1024 * 1024,
    maximum_total_bytes: int = 64 * 1024 * 1024,
    maximum_redirects: int = 5,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> XenonBrokerPermit:
    """Issue one exact same-origin HTTPS browsing permit."""

    if type(authority_key) is not bytes or len(authority_key) < 32:
        _reject("authority_key")
    if not callable(resolver) or not callable(uuid_factory):
        _reject("permit_dependency")
    policy = XenonEgressPolicy(maximum_redirects=maximum_redirects)
    try:
        target = resolve_public_url(raw_url, policy, resolver=resolver)
    except XenonEgressRejected as error:
        _reject("permit_" + error.reason_code)
    permit_id = uuid_factory()
    session_id = uuid_factory()
    if type(permit_id) is not uuid.UUID or type(session_id) is not uuid.UUID:
        _reject("uuid_factory")
    if permit_id.int == 0 or session_id.int == 0 or permit_id == session_id:
        _reject("uuid_factory")
    row = {
        "schema_version": XENON_BROKER_SCHEMA_VERSION,
        "protocol_version": XENON_BROKER_PROTOCOL_VERSION,
        "permit_id": str(permit_id),
        "session_id": str(session_id),
        "canonical_url": target.canonical_url,
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": expires_at_ms,
        "fencing_token": fencing_token,
        "maximum_connections": maximum_connections,
        "maximum_active_connections": maximum_active_connections,
        "maximum_response_bytes": maximum_response_bytes,
        "maximum_total_bytes": maximum_total_bytes,
        "maximum_redirects": maximum_redirects,
        "key_id": _key_id(authority_key),
        "signature": "A" * 43,
    }
    unsigned = XenonBrokerPermit.from_dict(row).unsigned_dict()
    row["signature"] = _b64_signature(
        hmac.new(authority_key, _canonical_json(unsigned), hashlib.sha256).digest()
    )
    return XenonBrokerPermit.from_dict(row)


def verify_xenon_broker_permit(
    permit: XenonBrokerPermit,
    *,
    authority_key: bytes,
    resolver: Resolver,
    now_ms: int,
    expected_fencing_token: int,
) -> tuple[XenonBrokerPermit, XenonEgressSession, XenonResolvedTarget]:
    """Reconstruct and verify even exact-class dataclass instances."""

    if type(permit) is not XenonBrokerPermit:
        _reject("permit_type")
    if type(authority_key) is not bytes or len(authority_key) < 32:
        _reject("authority_key")
    if not callable(resolver):
        _reject("resolver")
    canonical = XenonBrokerPermit.from_dict(permit.to_dict())
    if canonical.key_id != _key_id(authority_key):
        _reject("permit_key_changed")
    expected = hmac.new(
        authority_key,
        _canonical_json(canonical.unsigned_dict()),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, _signature_bytes(canonical.signature)):
        _reject("permit_signature_invalid")
    now = _integer(now_ms, "permit_clock", 1, (1 << 53) - 1)
    if now < canonical.issued_at_ms:
        _reject("permit_clock_regression")
    if now >= canonical.expires_at_ms:
        _reject("permit_expired")
    if canonical.fencing_token != _integer(
        expected_fencing_token, "permit_expected_fence", 1, (1 << 53) - 1
    ):
        _reject("permit_fence_changed")
    policy = XenonEgressPolicy(maximum_redirects=canonical.maximum_redirects)
    session = XenonEgressSession(policy, resolver)
    try:
        target = session.begin(canonical.canonical_url)
    except XenonEgressRejected as error:
        _reject("permit_" + error.reason_code)
    if target.canonical_url != canonical.canonical_url:
        _reject("permit_url_changed")
    return canonical, session, target


@dataclass(slots=True)
class XenonEphemeralCertificateAuthority:
    """One-process CA whose private keys never appear in repr or evidence."""

    _ca_key: ec.EllipticCurvePrivateKey = field(repr=False)
    _leaf_key: ec.EllipticCurvePrivateKey = field(repr=False)
    _certificate: x509.Certificate = field(repr=False)
    expires_at_ms: int
    _contexts: dict[str, ssl.SSLContext] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def create(cls, *, now_ms: int, expires_at_ms: int) -> XenonEphemeralCertificateAuthority:
        now_value = _integer(now_ms, "ca_clock", 1, (1 << 53) - 1)
        expires = _integer(expires_at_ms, "ca_expires", 1, (1 << 53) - 1)
        if expires <= now_value or expires - now_value > XENON_MAX_LIFETIME_MS:
            _reject("ca_lifetime")
        now = datetime.fromtimestamp(now_value / 1000, tz=timezone.utc)
        expiry = datetime.fromtimestamp(expires / 1000, tz=timezone.utc)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Algo Xenon Session CA")])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(seconds=30))
            .not_valid_after(expiry)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
            .sign(ca_key, hashes.SHA256())
        )
        return cls(ca_key, leaf_key, certificate, expires)

    @property
    def certificate_pem(self) -> bytes:
        return self._certificate.public_bytes(serialization.Encoding.PEM)

    @property
    def certificate_digest(self) -> str:
        return "sha256:" + self._certificate.fingerprint(hashes.SHA256()).hex()

    def server_context(self, host: str, *, now_ms: int) -> ssl.SSLContext:
        if type(host) is not str or not _HOST_RE.fullmatch(host):
            _reject("leaf_host")
        now_value = _integer(now_ms, "leaf_clock", 1, (1 << 53) - 1)
        if now_value >= self.expires_at_ms:
            _reject("ca_expired")
        with self._lock:
            cached = self._contexts.get(host)
            if cached is not None:
                return cached
            now = datetime.fromtimestamp(now_value / 1000, tz=timezone.utc)
            expiry = datetime.fromtimestamp(self.expires_at_ms / 1000, tz=timezone.utc)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
            certificate = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(self._certificate.subject)
                .public_key(self._leaf_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(seconds=30))
                .not_valid_after(expiry)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
                )
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        content_commitment=False,
                        key_encipherment=False,
                        data_encipherment=False,
                        key_agreement=False,
                        key_cert_sign=False,
                        crl_sign=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .sign(self._ca_key, hashes.SHA256())
            )
            cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
            key_pem = self._leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.maximum_version = ssl.TLSVersion.TLSv1_3
            context.set_alpn_protocols(["http/1.1"])
            directory: str | None = None
            try:
                directory = tempfile.mkdtemp(prefix="xenon-cert-", dir="/tmp")
                os.chmod(directory, 0o700)
                cert_fd, cert_path = tempfile.mkstemp(prefix="cert-", dir=directory)
                key_fd, key_path = tempfile.mkstemp(prefix="key-", dir=directory)
                os.fchmod(cert_fd, 0o600)
                os.fchmod(key_fd, 0o600)
                with os.fdopen(cert_fd, "wb", closefd=True) as cert_file:
                    cert_file.write(cert_pem)
                    cert_file.write(self.certificate_pem)
                    cert_file.flush()
                    os.fsync(cert_file.fileno())
                with os.fdopen(key_fd, "wb", closefd=True) as key_file:
                    key_file.write(key_pem)
                    key_file.flush()
                    os.fsync(key_file.fileno())
                context.load_cert_chain(cert_path, key_path)
            except OSError:
                _reject("leaf_context")
            finally:
                for path in (locals().get("cert_path"), locals().get("key_path")):
                    if isinstance(path, str):
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                if directory is not None:
                    try:
                        os.rmdir(directory)
                    except OSError:
                        pass
            self._contexts[host] = context
            return context


def _header_lines(raw: bytes) -> tuple[bytes, tuple[tuple[str, bytes], ...]]:
    if type(raw) is not bytes or not raw.endswith(b"\r\n\r\n") or len(raw) > XENON_MAX_HEADER_BYTES:
        _reject("http_header_size")
    if b"\x00" in raw or b"\n" in raw.replace(b"\r\n", b""):
        _reject("http_line_ending")
    lines = raw[:-4].split(b"\r\n")
    if not lines or not lines[0] or len(lines[0]) > XENON_MAX_LINE_BYTES:
        _reject("http_start_line")
    if len(lines) - 1 > XENON_MAX_HEADER_COUNT:
        _reject("http_header_count")
    headers: list[tuple[str, bytes]] = []
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            _reject("http_header")
        name, value = line.split(b":", 1)
        if not _TOKEN_RE.fullmatch(name):
            _reject("http_header_name")
        value = value.strip(b" \t")
        if any(byte < 32 and byte != 9 or byte == 127 for byte in value):
            _reject("http_header_value")
        headers.append((name.decode("ascii").casefold(), value))
    return lines[0], tuple(headers)


def _header_map(headers: Sequence[tuple[str, bytes]]) -> dict[str, list[bytes]]:
    result: dict[str, list[bytes]] = {}
    for name, value in headers:
        result.setdefault(name, []).append(value)
    return result


def _authority(value: bytes) -> tuple[str, int]:
    if not value or len(value) > 512 or b"@" in value or value.startswith(b"["):
        _reject("connect_authority")
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        _reject("connect_authority")
    if text.count(":") != 1:
        _reject("connect_authority")
    host, raw_port = text.rsplit(":", 1)
    host = host.rstrip(".").casefold()
    if not _HOST_RE.fullmatch(host) or ".." in host or not raw_port.isdigit():
        _reject("connect_authority")
    if int(raw_port) != 443 or raw_port != "443":
        _reject("connect_port")
    return host, 443


@dataclass(frozen=True, slots=True)
class XenonConnectRequest:
    host: str = field(repr=False)
    port: int


def parse_xenon_connect_request(raw: bytes, *, expected_host: str) -> XenonConnectRequest:
    start, headers = _header_lines(raw)
    parts = start.split(b" ")
    if len(parts) != 3 or parts[0] != b"CONNECT" or parts[2] != b"HTTP/1.1":
        _reject("connect_method")
    host, port = _authority(parts[1])
    if host != expected_host:
        _reject("connect_origin")
    mapped = _header_map(headers)
    if set(mapped) - {"host", "proxy-connection", "user-agent"}:
        _reject("connect_header")
    if len(mapped.get("host", [])) != 1:
        _reject("connect_host")
    header_host, header_port = _authority(mapped["host"][0])
    if (header_host, header_port) != (host, port):
        _reject("connect_host")
    if "proxy-connection" in mapped and mapped["proxy-connection"] != [b"keep-alive"]:
        _reject("connect_header")
    return XenonConnectRequest(host, port)


@dataclass(frozen=True, slots=True)
class XenonHttpRequest:
    method: str
    path: str = field(repr=False)
    canonical_url: str = field(repr=False)
    upstream_head: bytes = field(repr=False)


def parse_xenon_http_request(
    raw: bytes,
    *,
    expected_origin: str,
    expected_host: str,
) -> XenonHttpRequest:
    start, headers = _header_lines(raw)
    parts = start.split(b" ")
    if len(parts) != 3 or parts[0] not in {b"GET", b"HEAD"} or parts[2] != b"HTTP/1.1":
        _reject("request_method")
    target = parts[1]
    if not target.startswith(b"/") or target.startswith(b"//") or len(target) > XENON_MAX_LINE_BYTES:
        _reject("request_target")
    try:
        path = target.decode("ascii")
    except UnicodeDecodeError:
        _reject("request_target")
    if "#" in path or any(character in path for character in ("\r", "\n", "\x00", "\\")):
        _reject("request_target")
    mapped = _header_map(headers)
    if len(mapped.get("host", [])) != 1:
        _reject("request_host")
    host_value = mapped["host"][0]
    try:
        host_text = host_value.decode("ascii").rstrip(".").casefold()
    except UnicodeDecodeError:
        _reject("request_host")
    if host_text not in {expected_host, expected_host + ":443"}:
        _reject("request_host")
    if "authorization" in mapped or "proxy-authorization" in mapped:
        _reject("auth_handoff")
    if "content-length" in mapped and mapped["content-length"] not in ([b"0"],):
        _reject("request_body")
    if "transfer-encoding" in mapped or "expect" in mapped:
        _reject("request_body")
    if set(mapped) & _WEBSOCKET_HEADERS:
        _reject("websocket_denied")
    connection_tokens = b",".join(mapped.get("connection", [])).lower().split(b",")
    nominated = {token.strip() for token in connection_tokens if token.strip()}
    if b"upgrade" in nominated or "upgrade" in mapped:
        _reject("upgrade_denied")
    if any(not _TOKEN_RE.fullmatch(token) for token in nominated):
        _reject("request_connection")
    nominated_names = {token.decode("ascii") for token in nominated}

    forwarded: list[bytes] = [parts[0] + b" " + target + b" HTTP/1.1"]
    for name, value in headers:
        if (
            name in _REQUEST_HOP_HEADERS
            or name in nominated_names
            or name == "host"
            or name == "content-length"
        ):
            continue
        forwarded.append(name.encode("ascii") + b": " + value)
    forwarded.append(b"Host: " + expected_host.encode("ascii"))
    forwarded.append(b"Connection: close")
    upstream = b"\r\n".join(forwarded) + b"\r\n\r\n"
    return XenonHttpRequest(
        method=parts[0].decode("ascii"),
        path=path,
        canonical_url=expected_origin + path,
        upstream_head=upstream,
    )


@dataclass(frozen=True, slots=True)
class XenonHttpResponse:
    status: int
    downstream_head: bytes = field(repr=False)
    content_length: int | None
    chunked: bool
    location: str | None = field(default=None, repr=False)


def parse_xenon_http_response(raw: bytes) -> XenonHttpResponse:
    start, headers = _header_lines(raw)
    parts = start.split(b" ", 2)
    if len(parts) < 2 or parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}:
        _reject("response_status")
    try:
        status = int(parts[1])
    except ValueError:
        _reject("response_status")
    if not 100 <= status <= 599:
        _reject("response_status")
    if status == 101:
        _reject("websocket_denied")
    mapped = _header_map(headers)
    if set(mapped) & _WEBSOCKET_HEADERS or "upgrade" in mapped:
        _reject("upgrade_denied")
    if status in {401, 407} or "www-authenticate" in mapped:
        _reject("auth_handoff")
    dispositions = mapped.get("content-disposition", [])
    if any(b"attachment" in value.lower() for value in dispositions):
        _reject("download_handoff")
    content_types = mapped.get("content-type", [])
    if any(value.lower().split(b";", 1)[0].strip() == b"application/pdf" for value in content_types):
        _reject("pdf_handoff")

    lengths = mapped.get("content-length", [])
    content_length: int | None = None
    if lengths:
        if len(set(lengths)) != 1:
            _reject("response_length")
        try:
            content_length = int(lengths[0])
        except ValueError:
            _reject("response_length")
        if content_length < 0 or content_length > XENON_MAX_RESPONSE_BYTES:
            _reject("response_length")
    encodings = mapped.get("transfer-encoding", [])
    chunked = False
    if encodings:
        if content_length is not None:
            _reject("response_framing")
        tokens = [token.strip().lower() for token in b",".join(encodings).split(b",")]
        if tokens != [b"chunked"]:
            _reject("response_framing")
        chunked = True
    locations = mapped.get("location", [])
    if len(locations) > 1:
        _reject("response_location")
    location: str | None = None
    if locations:
        try:
            location = locations[0].decode("ascii")
        except UnicodeDecodeError:
            _reject("response_location")
        if len(location.encode("ascii")) > 4096:
            _reject("response_location")

    response_connection_tokens = {
        token.strip()
        for token in b",".join(mapped.get("connection", [])).lower().split(b",")
        if token.strip()
    }
    if any(not _TOKEN_RE.fullmatch(token) for token in response_connection_tokens):
        _reject("response_connection")
    response_nominated = {token.decode("ascii") for token in response_connection_tokens}

    forwarded: list[bytes] = [start]
    for name, value in headers:
        if name in _RESPONSE_STRIP_HEADERS or name in response_nominated:
            continue
        forwarded.append(name.encode("ascii") + b": " + value)
    forwarded.append(b"Connection: close")
    downstream = b"\r\n".join(forwarded) + b"\r\n\r\n"
    return XenonHttpResponse(status, downstream, content_length, chunked, location)


class SocketLike(Protocol):
    def recv(self, size: int) -> bytes: ...

    def sendall(self, data: bytes) -> None: ...

    def settimeout(self, value: float | None) -> None: ...

    def close(self) -> None: ...


def _read_head(connection: SocketLike, *, initial: bytes = b"") -> tuple[bytes, bytes]:
    if type(initial) is not bytes or len(initial) > XENON_MAX_HEADER_BYTES:
        _reject("http_header_size")
    buffer = bytearray(initial)
    while True:
        end = buffer.find(b"\r\n\r\n")
        if end >= 0:
            head_end = end + 4
            return bytes(buffer[:head_end]), bytes(buffer[head_end:])
        if len(buffer) >= XENON_MAX_HEADER_BYTES:
            _reject("http_header_size")
        try:
            chunk = connection.recv(min(16_384, XENON_MAX_HEADER_BYTES - len(buffer)))
        except (OSError, TimeoutError):
            _reject("socket_read")
        if not chunk:
            _reject("socket_eof")
        buffer.extend(chunk)


def _socket_family(address: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET


Connector = Callable[[XenonResolvedTarget, XenonEgressSession], ssl.SSLSocket]


def connect_xenon_upstream(
    target: XenonResolvedTarget,
    session: XenonEgressSession,
    *,
    timeout_seconds: float = XENON_SOCKET_TIMEOUT_SECONDS,
) -> ssl.SSLSocket:
    """Connect directly to a pinned IP, validate its peer and origin TLS cert."""

    if type(target) is not XenonResolvedTarget or type(session) is not XenonEgressSession:
        _reject("upstream_target")
    if type(timeout_seconds) is not float or not 0.1 <= timeout_seconds <= 30.0:
        _reject("upstream_timeout")
    try:
        session.verify_dns_pin(target)
    except XenonEgressRejected as error:
        _reject("upstream_" + error.reason_code)
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["http/1.1"])
    for address in target.addresses:
        raw: socket.socket | None = None
        tls: ssl.SSLSocket | None = None
        try:
            raw = socket.socket(_socket_family(address), socket.SOCK_STREAM)
            raw.settimeout(timeout_seconds)
            destination: Any = (address, target.port, 0, 0) if ":" in address else (address, target.port)
            raw.connect(destination)
            peer = str(raw.getpeername()[0])
            session.verify_connected_peer(target, peer)
            tls = context.wrap_socket(raw, server_hostname=target.host)
            raw = None
            if tls.selected_alpn_protocol() not in {None, "http/1.1"}:
                _reject("upstream_alpn")
            return tls
        except XenonBrokerRejected:
            if tls is not None:
                tls.close()
            if raw is not None:
                raw.close()
            raise
        except (OSError, ssl.SSLError, XenonEgressRejected):
            if tls is not None:
                tls.close()
            if raw is not None:
                raw.close()
    _reject("upstream_connect")


@dataclass(frozen=True, slots=True)
class XenonBrokerEvidence:
    disposition: XenonBrokerDisposition
    connection_count: int
    active_peak: int
    request_count: int
    redirect_count: int
    bytes_to_browser: int
    target_decision_digest: str
    ca_digest: str
    reason_code: str


class XenonBrokerSession:
    """Thread-safe budgets and policy for one signed broker permit."""

    def __init__(
        self,
        permit: XenonBrokerPermit,
        egress_session: XenonEgressSession,
        target: XenonResolvedTarget,
        ca: XenonEphemeralCertificateAuthority,
        *,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        if (
            type(permit) is not XenonBrokerPermit
            or type(egress_session) is not XenonEgressSession
            or type(target) is not XenonResolvedTarget
            or type(ca) is not XenonEphemeralCertificateAuthority
            or not callable(clock_ms)
        ):
            _reject("broker_session_type")
        self.permit = XenonBrokerPermit.from_dict(permit.to_dict())
        self.egress = egress_session
        self.target = target
        self.ca = ca
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._connections = 0
        self._active = 0
        self._active_peak = 0
        self._requests = 0
        self._redirects = 0
        self._bytes = 0
        self._disposition = XenonBrokerDisposition.VERIFIED
        self._reason = "ready"

    def assert_live(self) -> None:
        now = self._clock_ms()
        if type(now) is not int or now < self.permit.issued_at_ms:
            _reject("broker_clock")
        if now >= self.permit.expires_at_ms:
            _reject("broker_expired")

    def begin_connection(self) -> None:
        self.assert_live()
        with self._lock:
            if self._connections >= self.permit.maximum_connections:
                _reject("connection_limit")
            if self._active >= self.permit.maximum_active_connections:
                _reject("active_connection_limit")
            self._connections += 1
            self._active += 1
            self._active_peak = max(self._active_peak, self._active)

    def finish_connection(self) -> None:
        with self._lock:
            if self._active <= 0:
                _reject("connection_accounting")
            self._active -= 1

    def add_request(self) -> None:
        self.assert_live()
        with self._lock:
            self._requests += 1

    def add_bytes(self, amount: int) -> None:
        value = _integer(amount, "response_bytes", 0, self.permit.maximum_response_bytes)
        with self._lock:
            if self._bytes + value > self.permit.maximum_total_bytes:
                _reject("total_byte_limit")
            self._bytes += value

    def validate_redirect(self, request_url: str, location: str | None) -> None:
        if location is None:
            return
        absolute = urljoin(request_url, location)
        try:
            target = self.egress.redirect(self.target, absolute)
        except XenonEgressRejected as error:
            _reject("redirect_" + error.reason_code)
        if target.origin != self.target.origin:
            _reject("redirect_origin")
        with self._lock:
            self._redirects += 1

    def mark(self, disposition: XenonBrokerDisposition, reason_code: str) -> None:
        if type(disposition) is not XenonBrokerDisposition or type(reason_code) is not str:
            _reject("broker_disposition")
        with self._lock:
            severity = {
                XenonBrokerDisposition.VERIFIED: 0,
                XenonBrokerDisposition.BLOCKED: 1,
                XenonBrokerDisposition.HANDOFF: 2,
                XenonBrokerDisposition.FAILED: 3,
                XenonBrokerDisposition.UNKNOWN: 4,
            }
            if severity[disposition] >= severity[self._disposition]:
                self._disposition = disposition
                self._reason = reason_code

    def evidence(self) -> XenonBrokerEvidence:
        with self._lock:
            return XenonBrokerEvidence(
                disposition=self._disposition,
                connection_count=self._connections,
                active_peak=self._active_peak,
                request_count=self._requests,
                redirect_count=self._redirects,
                bytes_to_browser=self._bytes,
                target_decision_digest=self.target.decision_digest,
                ca_digest=self.ca.certificate_digest,
                reason_code=self._reason,
            )


def _error_disposition(reason_code: str) -> XenonBrokerDisposition:
    if reason_code.endswith("handoff") or reason_code in {"auth_handoff", "pdf_handoff"}:
        return XenonBrokerDisposition.HANDOFF
    if reason_code in {
        "connect_origin",
        "connect_port",
        "request_method",
        "request_body",
        "websocket_denied",
        "upgrade_denied",
    } or reason_code.startswith("redirect_"):
        return XenonBrokerDisposition.BLOCKED
    return XenonBrokerDisposition.FAILED


def _send_error(connection: SocketLike, status: int) -> None:
    if status not in {403, 408, 413, 429, 502}:
        status = 502
    reasons = {403: b"Forbidden", 408: b"Request Timeout", 413: b"Payload Too Large", 429: b"Too Many Requests", 502: b"Bad Gateway"}
    body = b"Algo Xenon request blocked.\n"
    response = (
        b"HTTP/1.1 "
        + str(status).encode("ascii")
        + b" "
        + reasons[status]
        + b"\r\nContent-Type: text/plain\r\nContent-Length: "
        + str(len(body)).encode("ascii")
        + b"\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
        + body
    )
    try:
        connection.sendall(response)
    except OSError:
        pass


def _relay_fixed(
    upstream: SocketLike,
    downstream: SocketLike,
    *,
    initial: bytes,
    length: int,
    session: XenonBrokerSession,
) -> None:
    if len(initial) > length:
        _reject("response_framing")
    remaining = length
    if initial:
        downstream.sendall(initial)
        session.add_bytes(len(initial))
        remaining -= len(initial)
    while remaining:
        try:
            chunk = upstream.recv(min(65_536, remaining))
        except (OSError, TimeoutError):
            _reject("upstream_read")
        if not chunk:
            _reject("response_truncated")
        downstream.sendall(chunk)
        session.add_bytes(len(chunk))
        remaining -= len(chunk)


def _relay_to_eof(
    upstream: SocketLike,
    downstream: SocketLike,
    *,
    initial: bytes,
    session: XenonBrokerSession,
) -> None:
    transferred = 0
    pending = initial
    while True:
        if pending:
            transferred += len(pending)
            if transferred > session.permit.maximum_response_bytes:
                _reject("response_byte_limit")
            downstream.sendall(pending)
            session.add_bytes(len(pending))
        try:
            pending = upstream.recv(65_536)
        except (OSError, TimeoutError):
            _reject("upstream_read")
        if not pending:
            return


def _relay_chunked(
    upstream: SocketLike,
    downstream: SocketLike,
    *,
    initial: bytes,
    session: XenonBrokerSession,
) -> None:
    buffer = bytearray(initial)
    transferred = 0

    def need_line() -> bytes:
        while True:
            end = buffer.find(b"\r\n")
            if end >= 0:
                if end > XENON_MAX_LINE_BYTES:
                    _reject("chunk_line")
                line = bytes(buffer[:end])
                del buffer[: end + 2]
                return line
            if len(buffer) > XENON_MAX_LINE_BYTES:
                _reject("chunk_line")
            chunk = upstream.recv(4096)
            if not chunk:
                _reject("response_truncated")
            buffer.extend(chunk)

    def need_bytes(amount: int) -> bytes:
        while len(buffer) < amount:
            chunk = upstream.recv(min(65_536, amount - len(buffer)))
            if not chunk:
                _reject("response_truncated")
            buffer.extend(chunk)
        value = bytes(buffer[:amount])
        del buffer[:amount]
        return value

    while True:
        line = need_line()
        raw_size = line.split(b";", 1)[0]
        if not raw_size or len(raw_size) > 16:
            _reject("chunk_size")
        try:
            size = int(raw_size, 16)
        except ValueError:
            _reject("chunk_size")
        if size < 0 or transferred + size > session.permit.maximum_response_bytes:
            _reject("response_byte_limit")
        downstream.sendall(line + b"\r\n")
        session.add_bytes(len(line) + 2)
        if size == 0:
            # Bounded trailers terminate at an empty line.
            trailer_bytes = 0
            while True:
                trailer = need_line()
                trailer_bytes += len(trailer) + 2
                if trailer_bytes > XENON_MAX_HEADER_BYTES:
                    _reject("trailer_size")
                downstream.sendall(trailer + b"\r\n")
                session.add_bytes(len(trailer) + 2)
                if not trailer:
                    if buffer:
                        _reject("response_pipelining")
                    return
        data = need_bytes(size + 2)
        if not data.endswith(b"\r\n"):
            _reject("chunk_framing")
        downstream.sendall(data)
        session.add_bytes(len(data))
        transferred += size


def handle_xenon_connection(
    client: socket.socket,
    session: XenonBrokerSession,
    *,
    connector: Connector = connect_xenon_upstream,
) -> None:
    """Handle one CONNECT and one TLS HTTP request, then force connection close."""

    if not isinstance(client, socket.socket) or type(session) is not XenonBrokerSession:
        _reject("connection_type")
    upstream: ssl.SSLSocket | None = None
    browser_tls: ssl.SSLSocket | None = None
    begun = False
    try:
        session.begin_connection()
        begun = True
        client.settimeout(XENON_SOCKET_TIMEOUT_SECONDS)
        connect_head, connect_extra = _read_head(client)
        if connect_extra:
            _reject("connect_pipelining")
        parse_xenon_connect_request(connect_head, expected_host=session.target.host)
        try:
            upstream = connector(session.target, session.egress)
        except TypeError:
            # The default connector uses its second positional session argument;
            # normalize arbitrary injected callable signature failures.
            _reject("upstream_connector")
        if not isinstance(upstream, ssl.SSLSocket):
            _reject("upstream_socket")
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        context = session.ca.server_context(session.target.host, now_ms=session._clock_ms())
        try:
            browser_tls = context.wrap_socket(client, server_side=True)
        except (OSError, ssl.SSLError):
            _reject("browser_tls")
        client = browser_tls
        request_head, request_extra = _read_head(browser_tls)
        if request_extra:
            _reject("request_pipelining")
        request = parse_xenon_http_request(
            request_head,
            expected_origin=session.target.origin,
            expected_host=session.target.host,
        )
        session.add_request()
        upstream.sendall(request.upstream_head)
        response_head, response_extra = _read_head(upstream)
        response = parse_xenon_http_response(response_head)
        while 100 <= response.status < 200:
            if response.status == 101:
                _reject("websocket_denied")
            response_head, response_extra = _read_head(upstream, initial=response_extra)
            response = parse_xenon_http_response(response_head)
        session.validate_redirect(request.canonical_url, response.location)
        browser_tls.sendall(response.downstream_head)
        session.add_bytes(len(response.downstream_head))
        if request.method == "HEAD" or response.status in {204, 304}:
            if response_extra:
                _reject("response_unexpected_body")
        elif response.chunked:
            _relay_chunked(upstream, browser_tls, initial=response_extra, session=session)
        elif response.content_length is not None:
            _relay_fixed(
                upstream,
                browser_tls,
                initial=response_extra,
                length=response.content_length,
                session=session,
            )
        else:
            _relay_to_eof(upstream, browser_tls, initial=response_extra, session=session)
        session.mark(XenonBrokerDisposition.VERIFIED, "request_verified")
    except XenonBrokerRejected as error:
        session.mark(_error_disposition(error.reason_code), error.reason_code)
        if browser_tls is None:
            _send_error(client, 403)
        raise
    except (OSError, ssl.SSLError):
        session.mark(XenonBrokerDisposition.UNKNOWN, "connection_unknown")
        raise XenonBrokerRejected("connection_unknown") from None
    finally:
        if upstream is not None:
            upstream.close()
        if browser_tls is not None:
            browser_tls.close()
        else:
            client.close()
        if begun:
            session.finish_connection()


class XenonBrokerServer:
    """Private-address-only bounded thread server for one broker session."""

    def __init__(
        self,
        session: XenonBrokerSession,
        *,
        listen_address: str,
        listen_port: int = 3128,
        connector: Connector = connect_xenon_upstream,
    ) -> None:
        if type(session) is not XenonBrokerSession:
            _reject("server_session")
        try:
            address = ipaddress.ip_address(listen_address)
        except ValueError:
            _reject("listen_address")
        if (
            not address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
        ):
            _reject("listen_address")
        self.session = session
        self.listen_address = address.compressed
        self.listen_port = _integer(listen_port, "listen_port", 1024, 65535)
        if not callable(connector):
            _reject("connector")
        self.connector = connector
        self._stop = threading.Event()
        self.ready_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._listener: socket.socket | None = None

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

    def _worker(self, client: socket.socket) -> None:
        try:
            handle_xenon_connection(client, self.session, connector=self.connector)
        except XenonBrokerRejected:
            pass

    def serve(self) -> XenonBrokerEvidence:
        family = _socket_family(self.listen_address)
        listener = socket.socket(family, socket.SOCK_STREAM)
        self._listener = listener
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.listen_address, self.listen_port))
        listener.listen(self.session.permit.maximum_active_connections)
        listener.settimeout(0.5)
        self.ready_event.set()
        try:
            while not self._stop.is_set():
                self.session.assert_live()
                try:
                    client, _peer = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    _reject("listener_accept")
                thread = threading.Thread(target=self._worker, args=(client,), daemon=False)
                self._threads.append(thread)
                thread.start()
        finally:
            self.stop()
            for thread in self._threads:
                thread.join(timeout=XENON_SOCKET_TIMEOUT_SECONDS + 2)
            self._listener = None
        return self.session.evidence()


__all__ = [
    "XENON_BROKER_PROTOCOL_VERSION",
    "XENON_BROKER_SCHEMA_VERSION",
    "XENON_MAX_HEADER_BYTES",
    "XenonBrokerDisposition",
    "XenonBrokerEvidence",
    "XenonBrokerPermit",
    "XenonBrokerRejected",
    "XenonBrokerServer",
    "XenonBrokerSession",
    "XenonConnectRequest",
    "XenonEphemeralCertificateAuthority",
    "XenonHttpRequest",
    "XenonHttpResponse",
    "connect_xenon_upstream",
    "handle_xenon_connection",
    "issue_xenon_broker_permit",
    "parse_xenon_connect_request",
    "parse_xenon_http_request",
    "parse_xenon_http_response",
    "verify_xenon_broker_permit",
]
