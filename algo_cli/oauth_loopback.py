"""Bound the lifetime of each local OAuth HTTP connection and capture session."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
from socketserver import TCPServer
from threading import Timer
import time
from typing import Any


class CallbackHTTPServer(HTTPServer):
    def __init__(
        self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], *, timeout: float,
    ) -> None:
        self.deadline = time.monotonic() + max(0.0, timeout)
        super().__init__(address, handler)

    def server_bind(self) -> None:
        # Fixed loopback listeners need no reverse DNS, which can consume the deadline.
        TCPServer.server_bind(self)
        host, self.server_port = self.server_address[:2]
        self.server_name = str(host)

    def handle_request(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return
        self.timeout = min(1.0, remaining)
        super().handle_request()

    def process_request(self, request: Any, client_address: Any) -> None:
        now = time.monotonic()
        remaining = self.deadline - now
        if remaining <= 0:
            self.shutdown_request(request)
            return
        lifetime = min(1.0, remaining)
        self.connection_deadline = now + lifetime
        request.settimeout(lifetime)

        def expire() -> None:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # Socket timeouts alone restart after each byte; the timer also bounds drip traffic.
        timer = Timer(lifetime, expire)
        timer.daemon = True
        timer.start()
        try:
            super().process_request(request, client_address)
        finally:
            timer.cancel()
            timer.join()


class CallbackRequestHandler(BaseHTTPRequestHandler):
    server: CallbackHTTPServer

    def parse_request(self) -> bool:
        # HTTP parsing accepts EOF as the end of headers, including timer-closed partial requests.
        return super().parse_request() and time.monotonic() < self.server.connection_deadline
