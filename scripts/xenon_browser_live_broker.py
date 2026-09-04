#!/usr/bin/env python3
"""Run a narrow real-network proof of the disabled Xenon broker core.

The cell intentionally prints structural evidence only.  It proves the broker
logic and real upstream TLS path, not Docker topology, a Chrome image, or an
end-to-end public browser session.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import threading
import time
from typing import Iterable

from algo_cli.xenon_browser_broker import (
    XenonBrokerRejected,
    XenonBrokerSession,
    XenonEphemeralCertificateAuthority,
    handle_xenon_connection,
    issue_xenon_broker_permit,
    verify_xenon_broker_permit,
)


def _resolver(host: str, port: int) -> Iterable[str]:
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(str(row[4][0]) for row in rows)


def _read_all(connection: ssl.SSLSocket) -> bytes:
    output = bytearray()
    while True:
        try:
            chunk = connection.recv(65_536)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            return bytes(output)
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > 20 * 1024 * 1024:
            raise XenonBrokerRejected("probe_response_limit")


def main() -> int:
    now_ms = int(time.time() * 1000)
    key = os.urandom(32)
    permit = issue_xenon_broker_permit(
        authority_key=key,
        raw_url="https://example.com/",
        resolver=_resolver,
        issued_at_ms=now_ms,
        expires_at_ms=now_ms + 60_000,
        fencing_token=1,
        maximum_connections=1,
        maximum_active_connections=1,
        maximum_response_bytes=16 * 1024 * 1024,
        maximum_total_bytes=16 * 1024 * 1024,
        maximum_redirects=0,
    )
    canonical, egress, target = verify_xenon_broker_permit(
        permit,
        authority_key=key,
        resolver=_resolver,
        now_ms=int(time.time() * 1000),
        expected_fencing_token=1,
    )
    ca = XenonEphemeralCertificateAuthority.create(
        now_ms=int(time.time() * 1000),
        expires_at_ms=canonical.expires_at_ms,
    )
    session = XenonBrokerSession(
        canonical,
        egress,
        target,
        ca,
        clock_ms=lambda: int(time.time() * 1000),
    )

    browser, broker = socket.socketpair()
    errors: list[str] = []

    def run_broker() -> None:
        try:
            handle_xenon_connection(broker, session)
        except XenonBrokerRejected as error:
            errors.append(error.reason_code)

    thread = threading.Thread(target=run_broker)
    thread.start()
    browser.sendall(
        b"CONNECT example.com:443 HTTP/1.1\r\n"
        b"Host: example.com:443\r\n"
        b"User-Agent: Algo-Xenon-Live-Probe\r\n\r\n"
    )
    connected = browser.recv(4096)
    if connected != b"HTTP/1.1 200 Connection Established\r\n\r\n":
        raise XenonBrokerRejected("probe_connect_response")
    context = ssl.create_default_context(cadata=ca.certificate_pem.decode("ascii"))
    context.set_alpn_protocols(["http/1.1"])
    with context.wrap_socket(browser, server_hostname="example.com") as secured:
        secured.sendall(
            b"GET / HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Accept: text/html\r\n"
            b"Connection: close\r\n\r\n"
        )
        response = _read_all(secured)
    thread.join(timeout=20)
    if thread.is_alive():
        raise XenonBrokerRejected("probe_thread_timeout")
    if errors:
        raise XenonBrokerRejected(errors[0])
    if not response.startswith(b"HTTP/1.1 200 "):
        raise XenonBrokerRejected("probe_http_status")
    header, separator, body = response.partition(b"\r\n\r\n")
    if not separator or not body:
        raise XenonBrokerRejected("probe_http_framing")
    lowered = header.lower()
    if b"alt-svc:" in lowered or b"upgrade:" in lowered:
        raise XenonBrokerRejected("probe_hop_header")

    evidence = session.evidence()
    row = {
        "schema_version": 1,
        "result": "pass",
        "scope": "real broker DNS, pinned peer, upstream TLS, interception TLS, and HTTP mediation",
        "limitations": "local socketpair browser; no Docker topology, Chrome process, extension, or public readiness",
        "disposition": evidence.disposition.value,
        "connection_count": evidence.connection_count,
        "active_peak": evidence.active_peak,
        "request_count": evidence.request_count,
        "redirect_count": evidence.redirect_count,
        "bytes_to_browser": evidence.bytes_to_browser,
        "target_decision_digest": evidence.target_decision_digest,
        "ca_digest": evidence.ca_digest,
        "reason_code": evidence.reason_code,
        "response_status_family": 2,
        "response_header_count": header.count(b"\r\n") + 1,
        "response_body_bytes": len(body),
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    print(json.dumps({**row, "evidence_digest": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except XenonBrokerRejected as error:
        print(json.dumps({"result": "fail", "reason_code": error.reason_code}, sort_keys=True))
        raise SystemExit(1) from None
