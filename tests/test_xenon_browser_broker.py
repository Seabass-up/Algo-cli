from __future__ import annotations

from dataclasses import replace
import socket
import ssl
import subprocess
import threading
import time
from typing import Iterable
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes
import pytest

import algo_cli.xenon_browser_broker as broker_module
from algo_cli.xenon_browser_broker import (
    XENON_MAX_HEADER_BYTES,
    XenonBrokerDisposition,
    XenonBrokerPermit,
    XenonBrokerRejected,
    XenonBrokerServer,
    XenonBrokerSession,
    XenonEphemeralCertificateAuthority,
    handle_xenon_connection,
    issue_xenon_broker_permit,
    parse_xenon_connect_request,
    parse_xenon_http_request,
    parse_xenon_http_response,
    verify_xenon_broker_permit,
)


KEY = b"x" * 32
OTHER_KEY = b"y" * 32
NOW_MS = int(time.time() * 1000)
PUBLIC_IP = "8.8.8.8"


def _resolver(*answers: str):
    def resolve(_host: str, _port: int) -> Iterable[str]:
        return answers or (PUBLIC_IP,)

    return resolve


def _uuid_factory():
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
        "raw_url": "https://example.com/start?q=1",
        "resolver": _resolver(),
        "issued_at_ms": NOW_MS,
        "expires_at_ms": NOW_MS + 120_000,
        "fencing_token": 7,
        "uuid_factory": _uuid_factory(),
    }
    values.update(overrides)
    return issue_xenon_broker_permit(**values)


def _verified_session(
    *,
    permit: XenonBrokerPermit | None = None,
    resolver=None,
    now_ms: int = NOW_MS + 1,
) -> XenonBrokerSession:
    selected = permit or _permit()
    canonical, egress, target = verify_xenon_broker_permit(
        selected,
        authority_key=KEY,
        resolver=resolver or _resolver(),
        now_ms=now_ms,
        expected_fencing_token=7,
    )
    ca = XenonEphemeralCertificateAuthority.create(
        now_ms=NOW_MS,
        expires_at_ms=canonical.expires_at_ms,
    )
    return XenonBrokerSession(canonical, egress, target, ca, clock_ms=lambda: now_ms)


def test_signed_permit_is_canonical_bounded_and_hides_destination_in_repr() -> None:
    permit = _permit()
    assert permit.canonical_url == "https://example.com/start?q=1"
    assert permit.maximum_connections == 32
    assert permit.maximum_active_connections == 8
    assert len(permit.signature) == 43
    assert "example.com" not in repr(permit)
    assert permit.signature not in repr(permit)
    canonical, _session, target = verify_xenon_broker_permit(
        permit,
        authority_key=KEY,
        resolver=_resolver(),
        now_ms=NOW_MS + 1,
        expected_fencing_token=7,
    )
    assert canonical == permit
    assert target.origin == "https://example.com"


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda row: row.__setitem__("canonical_url", "https://other.example/"), "permit_signature_invalid"),
        (lambda row: row.__setitem__("fencing_token", 8), "permit_signature_invalid"),
        (lambda row: row.__setitem__("maximum_connections", 16), "permit_signature_invalid"),
        (lambda row: row.__setitem__("signature", "A" * 43), "permit_signature_invalid"),
        (lambda row: row.__setitem__("protocol_version", 2), "permit_protocol_version"),
        (lambda row: row.__setitem__("unexpected", 1), "permit_schema"),
    ],
)
def test_every_permit_field_and_schema_is_revalidated(mutator, reason: str) -> None:
    row = _permit().to_dict()
    mutator(row)
    if reason in {"permit_protocol_version", "permit_schema"}:
        with pytest.raises(XenonBrokerRejected, match=reason):
            XenonBrokerPermit.from_dict(row)
        return
    forged = XenonBrokerPermit.from_dict(row)
    with pytest.raises(XenonBrokerRejected, match=reason):
        verify_xenon_broker_permit(
            forged,
            authority_key=KEY,
            resolver=_resolver(),
            now_ms=NOW_MS + 1,
            expected_fencing_token=forged.fencing_token,
        )


def test_permit_rejects_wrong_key_time_fence_and_cross_budget_invariants() -> None:
    permit = _permit()
    with pytest.raises(XenonBrokerRejected, match="permit_key_changed"):
        verify_xenon_broker_permit(
            permit,
            authority_key=OTHER_KEY,
            resolver=_resolver(),
            now_ms=NOW_MS + 1,
            expected_fencing_token=7,
        )
    with pytest.raises(XenonBrokerRejected, match="permit_clock_regression"):
        verify_xenon_broker_permit(
            permit,
            authority_key=KEY,
            resolver=_resolver(),
            now_ms=NOW_MS - 1,
            expected_fencing_token=7,
        )
    with pytest.raises(XenonBrokerRejected, match="permit_expired"):
        verify_xenon_broker_permit(
            permit,
            authority_key=KEY,
            resolver=_resolver(),
            now_ms=permit.expires_at_ms,
            expected_fencing_token=7,
        )
    with pytest.raises(XenonBrokerRejected, match="permit_fence_changed"):
        verify_xenon_broker_permit(
            permit,
            authority_key=KEY,
            resolver=_resolver(),
            now_ms=NOW_MS + 1,
            expected_fencing_token=8,
        )

    row = permit.to_dict()
    row["maximum_active_connections"] = row["maximum_connections"] + 1
    with pytest.raises(XenonBrokerRejected, match="permit_active_connections"):
        XenonBrokerPermit.from_dict(row)
    row = permit.to_dict()
    row["maximum_response_bytes"] = 2
    row["maximum_total_bytes"] = 1
    with pytest.raises(XenonBrokerRejected, match="permit_total_bytes"):
        XenonBrokerPermit.from_dict(row)


def test_permit_rejects_non_public_url_and_duplicate_factory_ids() -> None:
    with pytest.raises(XenonBrokerRejected, match="permit_non_public_address"):
        _permit(resolver=_resolver("127.0.0.1"))
    same = uuid.UUID("11111111-1111-4111-8111-111111111111")
    with pytest.raises(XenonBrokerRejected, match="uuid_factory"):
        _permit(uuid_factory=lambda: same)


def test_ephemeral_ca_is_short_lived_scoped_and_can_complete_tls() -> None:
    ca = XenonEphemeralCertificateAuthority.create(
        now_ms=NOW_MS,
        expires_at_ms=NOW_MS + 60_000,
    )
    certificate = x509.load_pem_x509_certificate(ca.certificate_pem)
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert constraints.ca is True and constraints.path_length == 0
    assert ca.certificate_digest == "sha256:" + certificate.fingerprint(hashes.SHA256()).hex()
    assert b"PRIVATE KEY" not in ca.certificate_pem
    assert "PRIVATE" not in repr(ca)

    server_context = ca.server_context("example.com", now_ms=NOW_MS + 1)
    client_context = ssl.create_default_context(cadata=ca.certificate_pem.decode("ascii"))
    client_context.set_alpn_protocols(["http/1.1"])
    left, right = socket.socketpair()
    seen: list[bytes] = []

    def server() -> None:
        with server_context.wrap_socket(right, server_side=True) as secured:
            seen.append(secured.recv(4))
            secured.sendall(b"pong")

    thread = threading.Thread(target=server)
    thread.start()
    with client_context.wrap_socket(left, server_hostname="example.com") as secured:
        secured.sendall(b"ping")
        assert secured.recv(4) == b"pong"
        assert secured.selected_alpn_protocol() == "http/1.1"
    thread.join(timeout=5)
    assert seen == [b"ping"]
    with pytest.raises(XenonBrokerRejected, match="ca_expired"):
        ca.server_context("example.com", now_ms=NOW_MS + 60_000)


def test_connect_parser_accepts_only_exact_https_authority() -> None:
    request = parse_xenon_connect_request(
        b"CONNECT example.com:443 HTTP/1.1\r\n"
        b"Host: example.com:443\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"User-Agent: Chrome\r\n\r\n",
        expected_host="example.com",
    )
    assert request.host == "example.com" and request.port == 443
    assert "example.com" not in repr(request)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"GET example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n", "connect_method"),
        (b"CONNECT other.com:443 HTTP/1.1\r\nHost: other.com:443\r\n\r\n", "connect_origin"),
        (b"CONNECT example.com:80 HTTP/1.1\r\nHost: example.com:80\r\n\r\n", "connect_port"),
        (b"CONNECT example.com:443 HTTP/1.0\r\nHost: example.com:443\r\n\r\n", "connect_method"),
        (b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nHost: example.com:443\r\n\r\n", "connect_host"),
        (b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nX-Open: yes\r\n\r\n", "connect_header"),
        (b"CONNECT example.com:443 HTTP/1.1\nHost: example.com:443\n\n", "http_header_size"),
    ],
)
def test_connect_parser_rejects_tunnel_expansion(raw: bytes, reason: str) -> None:
    with pytest.raises(XenonBrokerRejected, match=reason):
        parse_xenon_connect_request(raw, expected_host="example.com")


def test_http_request_parser_strips_hop_headers_and_forces_close() -> None:
    request = parse_xenon_http_request(
        b"GET /path?q=1 HTTP/1.1\r\n"
        b"Host: example.com\r\n"
        b"Connection: keep-alive, X-Hop\r\n"
        b"X-Hop: secret\r\n"
        b"Accept: text/html\r\n\r\n",
        expected_origin="https://example.com",
        expected_host="example.com",
    )
    assert request.method == "GET"
    assert request.canonical_url == "https://example.com/path?q=1"
    assert b"x-hop" not in request.upstream_head.lower()
    assert b"connection: close" in request.upstream_head.lower()
    assert b"host: example.com" in request.upstream_head.lower()
    assert "secret" not in repr(request)


@pytest.mark.parametrize(
    ("start_or_header", "reason"),
    [
        (b"POST / HTTP/1.1\r\nHost: example.com\r\n", "request_method"),
        (b"GET https://example.com/ HTTP/1.1\r\nHost: example.com\r\n", "request_target"),
        (b"GET / HTTP/1.1\r\nHost: other.com\r\n", "request_host"),
        (b"GET / HTTP/1.1\r\nHost: example.com\r\nAuthorization: Bearer secret\r\n", "auth_handoff"),
        (b"GET / HTTP/1.1\r\nHost: example.com\r\nContent-Length: 1\r\n", "request_body"),
        (b"GET / HTTP/1.1\r\nHost: example.com\r\nTransfer-Encoding: chunked\r\n", "request_body"),
        (b"GET / HTTP/1.1\r\nHost: example.com\r\nUpgrade: websocket\r\n", "upgrade_denied"),
        (b"GET / HTTP/1.1\r\nHost: example.com\r\nSec-WebSocket-Key: x\r\n", "websocket_denied"),
        (b"GET / HTTP/1.1\r\nHost: example.com\r\nHost: example.com\r\n", "request_host"),
    ],
)
def test_http_request_parser_denies_mutation_auth_body_and_websocket(
    start_or_header: bytes, reason: str
) -> None:
    with pytest.raises(XenonBrokerRejected, match=reason):
        parse_xenon_http_request(
            start_or_header + b"\r\n",
            expected_origin="https://example.com",
            expected_host="example.com",
        )


def test_response_parser_strips_alt_svc_and_connection_nominated_headers() -> None:
    response = parse_xenon_http_response(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/html\r\n"
        b"Content-Length: 4\r\n"
        b"Alt-Svc: h3=\":443\"\r\n"
        b"Connection: X-Hop\r\n"
        b"X-Hop: secret\r\n\r\n"
    )
    assert response.status == 200
    assert response.content_length == 4
    assert response.chunked is False
    lowered = response.downstream_head.lower()
    assert b"alt-svc" not in lowered and b"x-hop" not in lowered
    assert b"connection: close" in lowered


@pytest.mark.parametrize(
    ("head", "reason"),
    [
        (b"HTTP/1.1 101 Switching\r\nUpgrade: websocket\r\n\r\n", "websocket_denied"),
        (b"HTTP/1.1 200 OK\r\nUpgrade: h2c\r\n\r\n", "upgrade_denied"),
        (b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n", "auth_handoff"),
        (b"HTTP/1.1 200 OK\r\nWWW-Authenticate: Basic\r\n\r\n", "auth_handoff"),
        (b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n\r\n", "pdf_handoff"),
        (b"HTTP/1.1 200 OK\r\nContent-Disposition: attachment\r\n\r\n", "download_handoff"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n", "response_length"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n", "response_framing"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n", "response_framing"),
        (b"HTTP/1.1 302 Found\r\nLocation: /a\r\nLocation: /b\r\n\r\n", "response_location"),
    ],
)
def test_response_parser_denies_upgrade_auth_download_and_ambiguous_framing(
    head: bytes, reason: str
) -> None:
    with pytest.raises(XenonBrokerRejected, match=reason):
        parse_xenon_http_response(head)


def test_header_bounds_and_obs_fold_fail_closed() -> None:
    with pytest.raises(XenonBrokerRejected, match="http_header_size"):
        parse_xenon_http_response(b"HTTP/1.1 200 OK\r\nX: " + b"a" * XENON_MAX_HEADER_BYTES + b"\r\n\r\n")
    with pytest.raises(XenonBrokerRejected, match="http_header"):
        parse_xenon_http_response(b"HTTP/1.1 200 OK\r\n folded\r\n\r\n")


def test_session_enforces_active_total_byte_expiry_and_redirect_budgets() -> None:
    permit = _permit(
        maximum_connections=2,
        maximum_active_connections=1,
        maximum_response_bytes=20,
        maximum_total_bytes=20,
        maximum_redirects=1,
    )
    session = _verified_session(permit=permit)
    session.begin_connection()
    with pytest.raises(XenonBrokerRejected, match="active_connection_limit"):
        session.begin_connection()
    session.finish_connection()
    session.begin_connection()
    session.finish_connection()
    with pytest.raises(XenonBrokerRejected, match="connection_limit"):
        session.begin_connection()
    session.add_bytes(20)
    with pytest.raises(XenonBrokerRejected, match="total_byte_limit"):
        session.add_bytes(1)
    session.validate_redirect("https://example.com/a", "/b")
    with pytest.raises(XenonBrokerRejected, match="redirect_redirect_limit"):
        session.validate_redirect("https://example.com/b", "/c")

    expired = _verified_session(now_ms=NOW_MS + 1)
    expired._clock_ms = lambda: expired.permit.expires_at_ms
    with pytest.raises(XenonBrokerRejected, match="broker_expired"):
        expired.assert_live()


def test_disposition_severity_never_downgrades_and_evidence_is_structural() -> None:
    session = _verified_session()
    session.mark(XenonBrokerDisposition.BLOCKED, "websocket_denied")
    session.mark(XenonBrokerDisposition.VERIFIED, "late_success")
    evidence = session.evidence()
    assert evidence.disposition is XenonBrokerDisposition.BLOCKED
    assert evidence.reason_code == "websocket_denied"
    assert "example.com" not in repr(evidence)
    assert evidence.target_decision_digest.startswith("sha256:")
    assert evidence.ca_digest.startswith("sha256:")


def _read_all(connection: ssl.SSLSocket) -> bytes:
    result = bytearray()
    while True:
        try:
            chunk = connection.recv(4096)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            # AF_UNIX TLS socketpairs on macOS can surface the broker's bounded
            # close as a ragged EOF rather than returning b"".
            return bytes(result)
        if not chunk:
            return bytes(result)
        result.extend(chunk)


def _run_double_tls(response_bytes: bytes) -> tuple[bytes, XenonBrokerSession, list[BaseException]]:
    session = _verified_session()
    server_context = session.ca.server_context("example.com", now_ms=NOW_MS + 1)
    client_context = ssl.create_default_context(cadata=session.ca.certificate_pem.decode("ascii"))
    client_context.set_alpn_protocols(["http/1.1"])

    upstream_client_raw, upstream_server_raw = socket.socketpair()
    upstream_errors: list[BaseException] = []

    def origin() -> None:
        try:
            with server_context.wrap_socket(upstream_server_raw, server_side=True) as secured:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(secured.recv(4096))
                assert request.startswith(b"GET /start?q=1 HTTP/1.1\r\n")
                assert b"Connection: close" in request
                secured.sendall(response_bytes)
        except BaseException as error:  # evidence surfaced in the caller
            upstream_errors.append(error)

    origin_thread = threading.Thread(target=origin)
    origin_thread.start()
    upstream_client = client_context.wrap_socket(
        upstream_client_raw,
        server_hostname="example.com",
    )

    browser_raw, broker_raw = socket.socketpair()
    broker_errors: list[BaseException] = []

    def connector(_target, _egress):
        return upstream_client

    def broker() -> None:
        try:
            handle_xenon_connection(broker_raw, session, connector=connector)
        except BaseException as error:  # evidence surfaced in the caller
            broker_errors.append(error)

    broker_thread = threading.Thread(target=broker)
    broker_thread.start()
    browser_raw.sendall(
        b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
    )
    connected = browser_raw.recv(4096)
    assert connected == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    with client_context.wrap_socket(browser_raw, server_hostname="example.com") as browser_tls:
        browser_tls.sendall(
            b"GET /start?q=1 HTTP/1.1\r\nHost: example.com\r\nConnection: keep-alive\r\n\r\n"
        )
        output = _read_all(browser_tls)
    broker_thread.join(timeout=5)
    origin_thread.join(timeout=5)
    return output, session, [*upstream_errors, *broker_errors]


def test_double_tls_broker_handles_coalesced_early_hints_and_fixed_body() -> None:
    output, session, errors = _run_double_tls(
        b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 4\r\n"
        b"Alt-Svc: h3=\":443\"\r\n\r\npong"
    )
    assert errors == []
    assert output.endswith(b"\r\n\r\npong")
    assert b"103 Early Hints" not in output
    assert b"Alt-Svc" not in output
    evidence = session.evidence()
    assert evidence.disposition is XenonBrokerDisposition.VERIFIED
    assert evidence.connection_count == 1
    assert evidence.request_count == 1
    assert evidence.bytes_to_browser > 4


def test_double_tls_broker_relays_valid_chunked_body() -> None:
    output, session, errors = _run_double_tls(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"4\r\npong\r\n0\r\n\r\n"
    )
    assert errors == []
    assert output.endswith(b"4\r\npong\r\n0\r\n\r\n")
    assert session.evidence().reason_code == "request_verified"


def test_double_tls_websocket_upgrade_is_blocked_and_never_forwarded() -> None:
    session = _verified_session()
    browser_raw, broker_raw = socket.socketpair()
    server_context = session.ca.server_context("example.com", now_ms=NOW_MS + 1)
    client_context = ssl.create_default_context(cadata=session.ca.certificate_pem.decode("ascii"))

    upstream_client_raw, upstream_server_raw = socket.socketpair()
    origin_seen: list[bytes] = []

    def origin() -> None:
        with server_context.wrap_socket(upstream_server_raw, server_side=True) as secured:
            secured.settimeout(2)
            try:
                origin_seen.append(secured.recv(4096))
            except OSError:
                origin_seen.append(b"")

    origin_thread = threading.Thread(target=origin)
    origin_thread.start()
    upstream_tls = client_context.wrap_socket(upstream_client_raw, server_hostname="example.com")
    errors: list[BaseException] = []

    def connector(_target, _egress):
        return upstream_tls

    def broker() -> None:
        try:
            handle_xenon_connection(broker_raw, session, connector=connector)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=broker)
    thread.start()
    browser_raw.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
    assert b"200 Connection Established" in browser_raw.recv(4096)
    with client_context.wrap_socket(browser_raw, server_hostname="example.com") as secured:
        secured.sendall(
            b"GET /socket HTTP/1.1\r\nHost: example.com\r\nConnection: Upgrade\r\n"
            b"Upgrade: websocket\r\nSec-WebSocket-Key: x\r\n\r\n"
        )
        _read_all(secured)
    thread.join(timeout=5)
    origin_thread.join(timeout=5)
    assert errors and isinstance(errors[0], XenonBrokerRejected)
    assert errors[0].reason_code in {"websocket_denied", "upgrade_denied"}
    assert origin_seen == [b""]
    assert session.evidence().disposition is XenonBrokerDisposition.BLOCKED


@pytest.mark.parametrize("address", ["127.0.0.1", "8.8.8.8", "0.0.0.0", "169.254.1.1"])
def test_server_refuses_non_private_or_special_listen_addresses(address: str) -> None:
    with pytest.raises(XenonBrokerRejected, match="listen_address"):
        XenonBrokerServer(_verified_session(), listen_address=address)


def test_server_accepts_only_exact_private_listener_without_starting_it() -> None:
    server = XenonBrokerServer(_verified_session(), listen_address="172.30.0.3")
    assert server.listen_address == "172.30.0.3"
    assert server.listen_port == 3128


def test_read_head_preserves_coalesced_final_response_bytes() -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.rows = [
                b"HTTP/1.1 103 Early\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
            ]

        def recv(self, _size: int) -> bytes:
            return self.rows.pop(0) if self.rows else b""

    first, remainder = broker_module._read_head(FakeSocket())
    assert first.startswith(b"HTTP/1.1 103")
    final, trailing = broker_module._read_head(FakeSocket(), initial=remainder)
    assert final.startswith(b"HTTP/1.1 200")
    assert trailing == b""


def test_no_subprocess_or_shell_is_used_by_broker_core(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("subprocess must not execute")

    monkeypatch.setattr(subprocess, "run", forbidden)
    permit = _permit()
    canonical, _session, _target = verify_xenon_broker_permit(
        permit,
        authority_key=KEY,
        resolver=_resolver(),
        now_ms=NOW_MS + 1,
        expected_fencing_token=7,
    )
    assert canonical.permit_id == permit.permit_id


def test_exact_dataclass_forgery_is_reconstructed_before_use() -> None:
    permit = _permit()
    forged = replace(permit, canonical_url="https://evil.example/")
    with pytest.raises(XenonBrokerRejected, match="permit_signature_invalid"):
        verify_xenon_broker_permit(
            forged,
            authority_key=KEY,
            resolver=_resolver(),
            now_ms=NOW_MS + 1,
            expected_fencing_token=7,
        )
