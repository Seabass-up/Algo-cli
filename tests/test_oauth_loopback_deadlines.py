"""Exercise real loopback sockets without launching OAuth or using credentials."""

from contextlib import contextmanager
import socket
import threading
import time

import pytest

from algo_cli import chatgpt_auth, google_workspace_auth


def test_loopback_binding_does_not_spend_capture_budget_on_reverse_dns(monkeypatch):
    from algo_cli.oauth_loopback import CallbackHTTPServer, CallbackRequestHandler

    def unavailable_reverse_dns(*_args):
        raise AssertionError("callback binding must not perform reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", unavailable_reverse_dns)
    with CallbackHTTPServer(("127.0.0.1", 0), CallbackRequestHandler, timeout=1) as server:
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1]


def test_registered_localhost_redirect_uses_numeric_ipv4_binding(monkeypatch):
    from socketserver import TCPServer
    from algo_cli.oauth_loopback import CallbackHTTPServer, CallbackRequestHandler

    bind = TCPServer.server_bind

    def require_numeric_binding(server):
        assert server.server_address[0] == "127.0.0.1"
        bind(server)

    monkeypatch.setattr(TCPServer, "server_bind", require_numeric_binding)
    with CallbackHTTPServer(("localhost", 0), CallbackRequestHandler, timeout=1) as server:
        assert server.server_address[0] == "127.0.0.1"


def test_each_buffered_read_enforces_remaining_absolute_budget(monkeypatch):
    from types import SimpleNamespace
    from algo_cli import oauth_loopback

    clock = iter([0.2, 0.7, 1.1])
    monkeypatch.setattr(oauth_loopback, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    timeouts = []
    reads = []

    class Connection:
        def gettimeout(self):
            return 5

        def settimeout(self, timeout):
            timeouts.append(timeout)

        def recv_into(self, buffer):
            reads.append(True)
            buffer[0] = 71
            return 1

    reader = oauth_loopback._DeadlineSocketReader(Connection(), 1.0)
    buffer = bytearray(16)
    assert reader.readinto(buffer) == 1
    assert reader.readinto(buffer) == 1
    with pytest.raises(TimeoutError, match="deadline expired"):
        reader.readinto(buffer)
    assert timeouts == pytest.approx([0.8, 0.3])
    assert len(reads) == 2


@contextmanager
def capture_session(monkeypatch, provider, timeout=1.0):
    from algo_cli.oauth_loopback import CallbackHTTPServer

    ready = threading.Event()
    bound = {}
    activate = CallbackHTTPServer.server_activate

    def signal_listener(server):
        activate(server)
        bound["port"] = server.server_address[1]
        bound["remaining"] = server.deadline - time.monotonic()
        ready.set()

    monkeypatch.setattr(CallbackHTTPServer, "server_activate", signal_listener)
    results = []

    def capture():
        try:
            results.append(provider.run_loopback_capture(redirect_port=0, timeout=timeout))
        except Exception as exc:
            results.append(exc)

    worker = threading.Thread(target=capture, daemon=True)
    worker.start()
    try:
        assert ready.wait(3), "callback listener did not become ready"
        assert bound["remaining"] > 0, "capture deadline expired during listener setup"
        yield bound["port"], worker, results
    finally:
        worker.join(4)
        assert not worker.is_alive(), "callback worker did not terminate"


def connect(port):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(3)
        client.connect(("127.0.0.1", port))
        return client
    except BaseException:
        client.close()
        raise


def force_timer_expiry(monkeypatch, provider):
    setup = provider._CallbackHandler.setup

    def delayed_inactivity_timeout(handler):
        setup(handler)
        handler.connection.settimeout(5)

    monkeypatch.setattr(provider._CallbackHandler, "setup", delayed_inactivity_timeout)


@pytest.mark.parametrize("provider", [chatgpt_auth, google_workspace_auth], ids=["chatgpt", "google"])
@pytest.mark.parametrize("partial", [b"", b"GET /callback HTTP/1.1\r\nHost:"], ids=["silent", "headers"])
def test_incomplete_callback_cannot_exceed_deadline(monkeypatch, provider, partial):
    force_timer_expiry(monkeypatch, provider)
    with capture_session(monkeypatch, provider) as (port, worker, results), connect(port) as client:
        if partial:
            client.sendall(partial)
        worker.join(1.8)
        assert not worker.is_alive()
        if provider is chatgpt_auth:
            assert results == [{}]
        else:
            assert len(results) == 1 and isinstance(results[0], RuntimeError)
            assert "Timed out" in str(results[0])


@pytest.mark.parametrize("provider", [chatgpt_auth, google_workspace_auth], ids=["chatgpt", "google"])
def test_progress_does_not_reset_callback_deadline(monkeypatch, provider):
    with capture_session(monkeypatch, provider) as (port, worker, _), connect(port) as client:
        end = time.monotonic() + 1.8
        while worker.is_alive() and time.monotonic() < end:
            try:
                client.sendall(b"G")
            except OSError:
                break
            time.sleep(0.05)
        worker.join(0.2)
        assert not worker.is_alive()


@pytest.mark.parametrize("provider", [chatgpt_auth, google_workspace_auth], ids=["chatgpt", "google"])
def test_valid_callback_survives_an_idle_connection(monkeypatch, provider):
    force_timer_expiry(monkeypatch, provider)
    with capture_session(monkeypatch, provider, timeout=3) as (port, worker, results), connect(port) as idle:
        idle.sendall(b"GET /callback HTTP/1.1\r\nHost:")
        with connect(port) as callback:
            callback.sendall(
                b"GET /callback?code=fixture-code&state=fixture-state HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            response = b""
            while chunk := callback.recv(4096):
                response += chunk
        worker.join(1)
        assert not worker.is_alive()
        assert b"200 OK" in response and b"login complete" in response
        assert results == [{"code": "fixture-code", "state": "fixture-state"}]
