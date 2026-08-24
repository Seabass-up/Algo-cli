"""Tests for Track M Phase 1: daemon lifecycle and strict JSON-RPC."""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator

import pytest

from algo_cli.config import CONFIG_DIR
from algo_cli.daemon import (
    DAEMON_PROTOCOL_VERSION,
    AlgoDaemon,
    PIDLock,
    get_default_pid_path,
    get_default_socket_path,
    health_check,
    rpc_call,
)
from algo_cli.daemon_rpc import (
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE_ERROR,
    NOTIFICATION_ID,
    RPCError,
    RPCRegistry,
    make_error,
    make_response,
    make_stream_chunk,
    parse_frame,
)


@pytest.fixture
def tmp_paths() -> Iterator[tuple[Path, Path]]:
    """Use a short /tmp path to stay below macOS AF_UNIX path limits."""
    short_dir = Path(tempfile.mkdtemp(prefix="algo_d_"))
    try:
        yield short_dir / "d.sock", short_dir / "d.pid"
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


@pytest.fixture
def running_daemon(tmp_paths: tuple[Path, Path]) -> Iterator[AlgoDaemon]:
    sock_path, pid_path = tmp_paths
    daemon = AlgoDaemon(sock_path, pid_path)
    daemon.start_background()
    daemon.wait_until_ready(timeout=5.0)
    yield daemon
    daemon.stop()
    daemon.wait_for_shutdown(timeout=5.0)


def _rpc_call(
    sock_path: Path,
    method: str,
    params: dict | None = None,
    req_id: int = 1,
) -> dict:
    return rpc_call(sock_path, method, params, req_id=req_id, timeout=5.0)


def _raw_exchange(sock_path: Path, payload: bytes, *, timeout: float = 2.0) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(sock_path))
    try:
        sock.sendall(payload)
        data = sock.recv(65536)
        return json.loads(data.decode("utf-8").strip())
    finally:
        sock.close()


def _start_fake_rpc_server(
    sock_path: Path, chunks: list[tuple[float, bytes]]
) -> tuple[threading.Thread, list[BaseException]]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    sock_path.chmod(0o600)
    listener.listen(1)
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            try:
                while b"\n" not in conn.recv(65536):
                    pass
                for delay, chunk in chunks:
                    time.sleep(delay)
                    conn.sendall(chunk)
            finally:
                conn.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    return thread, errors


class TestRPCRegistry:
    def test_register_and_dispatch(self):
        reg = RPCRegistry()
        reg.register("echo", lambda msg: msg)
        assert reg.dispatch("echo", {"msg": "hello"}) == "hello"

    def test_unknown_method_raises_sanitized_error(self):
        reg = RPCRegistry()
        with pytest.raises(RPCError) as exc_info:
            reg.dispatch("secret.method", {})
        assert exc_info.value.code == ERR_METHOD_NOT_FOUND
        assert exc_info.value.message == "Method not found"
        assert "secret.method" not in exc_info.value.message

    def test_handler_exception_is_not_disclosed(self):
        reg = RPCRegistry()
        reg.register(
            "boom",
            lambda: (_ for _ in ()).throw(ValueError("/secret/path token=abc")),
        )
        with pytest.raises(RPCError) as exc_info:
            reg.dispatch("boom", {})
        assert exc_info.value.code == ERR_INTERNAL
        assert exc_info.value.message == "Internal error"
        assert "secret" not in exc_info.value.message

    def test_invalid_named_params_are_32602(self):
        reg = RPCRegistry()
        reg.register("echo", lambda msg: msg)
        with pytest.raises(RPCError) as exc_info:
            reg.dispatch("echo", {"wrong": "value"})
        assert exc_info.value.code == ERR_INVALID_PARAMS

    def test_telemetry_tracks_success_and_error(self):
        reg = RPCRegistry()
        reg.register("echo", lambda msg: msg)
        reg.dispatch("echo", {"msg": "a"})
        with pytest.raises(RPCError):
            reg.dispatch("echo", {"wrong": "b"})
        snap = reg.telemetry_snapshot()["echo"]
        assert snap["call_count"] == 2
        assert snap["error_count"] == 1
        assert snap["avg_latency_ms"] >= 0

    def test_telemetry_is_thread_safe(self):
        reg = RPCRegistry()
        reg.register("echo", lambda value: value)
        errors: list[BaseException] = []

        def call_many(offset: int) -> None:
            try:
                for index in range(100):
                    assert reg.dispatch("echo", {"value": offset + index}) >= 0
                    reg.telemetry_snapshot()
            except BaseException as exc:  # surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=call_many, args=(n * 100,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert reg.telemetry_snapshot()["echo"]["call_count"] == 800

    def test_method_names_and_presence(self):
        reg = RPCRegistry()
        reg.register("beta", lambda: None)
        reg.register("alpha", lambda: None)
        assert reg.method_names == ["alpha", "beta"]
        assert reg.has_method("alpha")
        assert not reg.has_method("gamma")


class TestFrameParsing:
    def test_parse_valid_frame(self):
        method, params, req_id, error = parse_frame(
            b'{"jsonrpc":"2.0","method":"ping","params":{},"id":1}'
        )
        assert (method, params, req_id, error) == ("ping", {}, 1, None)

    def test_valid_notification_has_sentinel_id(self):
        method, params, req_id, error = parse_frame(
            b'{"jsonrpc":"2.0","method":"ping"}'
        )
        assert method == "ping"
        assert params == {}
        assert req_id is NOTIFICATION_ID
        assert error is None

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (b"{not valid json}", ERR_PARSE_ERROR),
            (b"[1,2,3]", ERR_INVALID_REQUEST),
            (b'{"method":"ping","id":1}', ERR_INVALID_REQUEST),
            (b'{"jsonrpc":"1.0","method":"ping","id":1}', ERR_INVALID_REQUEST),
            (b'{"jsonrpc":"2.0","params":{},"id":1}', ERR_INVALID_REQUEST),
            (b'{"jsonrpc":"2.0","method":"ping","id":true}', ERR_INVALID_REQUEST),
            (b'{"jsonrpc":"2.0","method":"ping","params":[],"id":1}', ERR_INVALID_PARAMS),
        ],
    )
    def test_invalid_frames(self, payload: bytes, code: int):
        method, _params, _req_id, error = parse_frame(payload)
        assert method is None
        assert isinstance(error, RPCError)
        assert error.code == code

    def test_parse_empty_line_is_ignored(self):
        method, params, req_id, error = parse_frame(b"")
        assert (method, params, req_id, error) == (None, None, None, None)

    @pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
    def test_non_finite_json_numbers_are_parse_errors(self, constant: bytes):
        payload = (
            b'{"jsonrpc":"2.0","method":"echo","params":{"value":'
            + constant
            + b'},"id":1}'
        )
        method, params, req_id, error = parse_frame(payload)
        assert (method, params, req_id) == (None, None, None)
        assert isinstance(error, RPCError)
        assert error.code == ERR_PARSE_ERROR


class TestResponseBuilders:
    def test_make_response(self):
        obj = json.loads(make_response("pong", 1).decode())
        assert obj == {"jsonrpc": "2.0", "result": "pong", "id": 1}

    def test_make_error(self):
        obj = json.loads(make_error(ERR_METHOD_NOT_FOUND, "not found", 2).decode())
        assert obj["error"] == {"code": ERR_METHOD_NOT_FOUND, "message": "not found"}
        assert obj["id"] == 2

    def test_make_stream_chunk(self):
        obj = json.loads(make_stream_chunk("hello").decode())
        assert obj["method"] == "stream"
        assert obj["params"]["chunk"] == "hello"

    @pytest.mark.parametrize(
        "frame",
        [make_response("x", 1), make_error(-1, "err", 1), make_stream_chunk("x")],
    )
    def test_frames_end_with_newline(self, frame: bytes):
        assert frame.endswith(b"\n")


class TestPIDLock:
    def test_acquire_and_release_is_owner_only(self, tmp_path: Path):
        path = tmp_path / "state" / "test.pid"
        lock = PIDLock(path)
        assert lock.acquire() == os.getpid()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        record = json.loads(path.read_text())
        assert record["pid"] == os.getpid()
        assert record["instance_id"]
        lock.release()
        assert not path.exists()

    def test_double_acquire_fails(self, tmp_path: Path):
        path = tmp_path / "test.pid"
        first = PIDLock(path)
        first.acquire()
        with pytest.raises(RuntimeError, match="already running"):
            PIDLock(path).acquire()
        first.release()

    def test_existing_non_private_parent_is_rejected_without_chmod(
        self, tmp_path: Path
    ):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        state_dir.chmod(0o755)
        path = state_dir / "test.pid"
        with pytest.raises(RuntimeError, match="not private"):
            PIDLock(path).acquire()
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o755
        assert not path.exists()

    def test_stale_legacy_pid_is_cleaned_up(self, tmp_path: Path):
        path = tmp_path / "test.pid"
        path.write_text("999999")
        lock = PIDLock(path)
        assert lock.acquire() == os.getpid()
        lock.release()

    def test_symlink_pid_path_is_rejected_without_touching_target(self, tmp_path: Path):
        target = tmp_path / "target"
        target.write_text("preserve")
        link = tmp_path / "test.pid"
        link.symlink_to(target)
        with pytest.raises(RuntimeError, match="Unsafe daemon PID path"):
            PIDLock(link).acquire()
        assert target.read_text() == "preserve"
        assert link.is_symlink()

    def test_release_does_not_remove_replacement_file(self, tmp_path: Path):
        path = tmp_path / "test.pid"
        lock = PIDLock(path)
        lock.acquire()
        path.unlink()
        path.write_text("replacement")
        lock.release()
        assert path.read_text() == "replacement"

    def test_partial_pid_write_failure_rolls_back_created_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "test.pid"
        real_write = os.write
        calls = 0

        def failing_write(fd: int, payload) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, bytes(payload[:5]))
            raise OSError("simulated write failure")

        monkeypatch.setattr(os, "write", failing_write)
        with pytest.raises(OSError, match="simulated write failure"):
            PIDLock(path).acquire()
        assert not path.exists()
        assert not list(tmp_path.glob(".test.pid.*.tmp"))

    def test_pid_record_is_published_only_after_complete_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path = tmp_path / "test.pid"
        first = PIDLock(path)
        second = PIDLock(path)
        first_write_started = threading.Event()
        allow_first_write = threading.Event()
        real_write = os.write

        def paused_write(fd: int, payload) -> int:
            if threading.current_thread().name == "first-pid-owner":
                first_write_started.set()
                assert allow_first_write.wait(2.0)
            return real_write(fd, payload)

        monkeypatch.setattr(os, "write", paused_write)
        acquired: list[PIDLock] = []
        errors: list[BaseException] = []

        def acquire(lock: PIDLock) -> None:
            try:
                lock.acquire()
                acquired.append(lock)
            except BaseException as exc:
                errors.append(exc)

        first_thread = threading.Thread(
            target=acquire, args=(first,), name="first-pid-owner"
        )
        first_thread.start()
        assert first_write_started.wait(2.0)
        second_thread = threading.Thread(target=acquire, args=(second,))
        second_thread.start()
        second_thread.join(timeout=2.0)
        allow_first_write.set()
        first_thread.join(timeout=2.0)
        second_thread.join(timeout=2.0)
        try:
            assert not first_thread.is_alive()
            assert not second_thread.is_alive()
            assert len(acquired) == 1
            assert len(errors) == 1
            assert isinstance(errors[0], RuntimeError)
        finally:
            allow_first_write.set()
            for lock in acquired:
                lock.release()

    def test_restrictive_umask_still_creates_usable_exact_modes(
        self, tmp_path: Path
    ):
        path = tmp_path / "state" / "test.pid"
        previous = os.umask(0o777)
        lock = PIDLock(path)
        try:
            assert lock.acquire() == os.getpid()
        finally:
            os.umask(previous)
        try:
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            with pytest.raises(RuntimeError, match="already running"):
                PIDLock(path).acquire()
        finally:
            lock.release()

    def test_lock_exclusion_crosses_process_boundary(self, tmp_path: Path):
        path = tmp_path / "test.pid"
        script = """
import sys
from pathlib import Path
from algo_cli.daemon import PIDLock
lock = PIDLock(Path(sys.argv[1]))
lock.acquire()
print("ready", flush=True)
sys.stdin.readline()
lock.release()
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
            with pytest.raises(RuntimeError, match="already running"):
                PIDLock(path).acquire()
            assert child.stdin is not None
            child.stdin.write("release\n")
            child.stdin.flush()
            assert child.wait(timeout=5.0) == 0

            replacement = PIDLock(path)
            assert replacement.acquire() == os.getpid()
            replacement.release()
            assert not path.exists()
            assert not path.with_name(f".{path.name}.lock").exists()
            assert not list(tmp_path.glob(".test.pid.*.tmp"))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2.0)

    def test_release_without_acquire_is_noop(self, tmp_path: Path):
        PIDLock(tmp_path / "missing.pid").release()


class TestDaemonLifecycle:
    def test_start_creates_private_ready_socket(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        assert stat.S_ISSOCK(sock_path.stat().st_mode)
        assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
        assert running_daemon.status.ready is True

    def test_second_start_fails_without_harming_live_daemon(
        self, running_daemon, tmp_paths
    ):
        sock_path, pid_path = tmp_paths
        with pytest.raises(RuntimeError, match="already running"):
            AlgoDaemon(sock_path, pid_path).start()
        assert health_check(sock_path)

    def test_startup_failure_rolls_back_pid_lock(self, tmp_paths):
        sock_path, pid_path = tmp_paths
        sock_path.write_text("not a socket")
        with pytest.raises(RuntimeError, match="Unsafe daemon socket path"):
            AlgoDaemon(sock_path, pid_path).start()
        assert not pid_path.exists()
        assert sock_path.read_text() == "not a socket"

    def test_shutdown_removes_owned_socket_and_pid(self, running_daemon, tmp_paths):
        sock_path, pid_path = tmp_paths
        running_daemon.stop()
        running_daemon.wait_for_shutdown(timeout=5.0)
        assert not sock_path.exists()
        assert not pid_path.exists()

    def test_health_check_proves_versioned_readiness(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        assert health_check(sock_path)
        assert not health_check(sock_path, expected_protocol=DAEMON_PROTOCOL_VERSION + 1)
        assert not health_check(sock_path, expected_app_version="incompatible")

    def test_health_check_false_for_missing_or_non_socket(self, tmp_path: Path):
        assert not health_check(tmp_path / "missing.sock")
        regular = tmp_path / "regular.sock"
        regular.write_text("not a socket")
        assert not health_check(regular)

    def test_wait_for_shutdown_reports_live_thread(self, tmp_paths):
        daemon = AlgoDaemon(*tmp_paths)
        release = threading.Event()
        daemon._accept_thread = threading.Thread(target=release.wait)
        daemon._accept_thread.start()
        try:
            with pytest.raises(TimeoutError, match="daemon shutdown"):
                daemon.wait_for_shutdown(timeout=0.01)
        finally:
            release.set()
            daemon._accept_thread.join(timeout=1.0)

    def test_same_instance_cannot_start_twice_or_stop_live_daemon(
        self, running_daemon: AlgoDaemon, tmp_paths: tuple[Path, Path]
    ):
        sock_path, _ = tmp_paths
        with pytest.raises(RuntimeError, match="lifecycle is already active"):
            running_daemon.start()
        assert health_check(sock_path)

    def test_stop_during_published_start_cannot_be_erased(self, tmp_paths):
        daemon = AlgoDaemon(*tmp_paths)
        runner_entered = threading.Event()
        allow_runner = threading.Event()
        real_runner = daemon._background_runner

        def paused_runner() -> None:
            runner_entered.set()
            assert allow_runner.wait(2.0)
            real_runner()

        daemon._background_runner = paused_runner  # type: ignore[method-assign]
        daemon.start_background()
        assert runner_entered.wait(2.0)
        daemon.stop()
        allow_runner.set()
        try:
            daemon.wait_for_shutdown(timeout=0.5)
        finally:
            allow_runner.set()
            daemon.stop()
            daemon.wait_for_shutdown(timeout=2.0)
        assert daemon.status.ready is False
        assert not tmp_paths[0].exists()
        assert not tmp_paths[1].exists()

    def test_concurrent_background_starts_are_linearized(
        self, tmp_paths, monkeypatch: pytest.MonkeyPatch
    ):
        daemon = AlgoDaemon(*tmp_paths)
        release_runners = threading.Event()
        daemon._background_runner = release_runners.wait  # type: ignore[method-assign]
        real_thread_start = threading.Thread.start
        first_start_entered = threading.Event()
        release_first_start = threading.Event()
        daemon_threads: list[threading.Thread] = []
        daemon_start_count = 0
        counter_lock = threading.Lock()

        def paused_thread_start(thread: threading.Thread) -> None:
            nonlocal daemon_start_count
            if thread.name != "algo-daemon-accept":
                real_thread_start(thread)
                return
            with counter_lock:
                daemon_start_count += 1
                call_number = daemon_start_count
                daemon_threads.append(thread)
            if call_number == 1:
                first_start_entered.set()
                assert release_first_start.wait(2.0)
            real_thread_start(thread)

        monkeypatch.setattr(threading.Thread, "start", paused_thread_start)
        successes: list[None] = []
        errors: list[BaseException] = []

        def call_start() -> None:
            try:
                successes.append(daemon.start_background())
            except BaseException as exc:
                errors.append(exc)

        callers = [threading.Thread(target=call_start) for _ in range(2)]
        real_thread_start(callers[0])
        assert first_start_entered.wait(2.0)
        real_thread_start(callers[1])
        callers[1].join(timeout=1.0)
        release_first_start.set()
        callers[0].join(timeout=2.0)
        callers[1].join(timeout=2.0)
        release_runners.set()
        for thread in daemon_threads:
            thread.join(timeout=2.0)

        assert all(not caller.is_alive() for caller in callers)
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert daemon_start_count == 1

    def test_rpc_client_rejects_non_private_endpoint(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        sock_path.chmod(0o666)
        with pytest.raises(ConnectionError, match="not private"):
            rpc_call(sock_path, "ping")

    @pytest.mark.parametrize(
        "response",
        [
            b'{"jsonrpc":"2.0","result":"pong","id":2}\n',
            b'{"jsonrpc":"2.0","id":1}\n',
            b'{"jsonrpc":"2.0","result":"pong","error":{},"id":1}\n',
            b'{"jsonrpc":"2.0","result":NaN,"id":1}\n',
        ],
    )
    def test_rpc_client_rejects_uncorrelated_or_invalid_response(
        self, tmp_paths: tuple[Path, Path], response: bytes
    ):
        sock_path, _ = tmp_paths
        server, server_errors = _start_fake_rpc_server(sock_path, [(0.0, response)])
        try:
            with pytest.raises(ConnectionError, match="invalid response"):
                rpc_call(sock_path, "ping", req_id=1, timeout=1.0)
        finally:
            server.join(timeout=2.0)
        assert not server.is_alive()
        assert not server_errors

    def test_rpc_client_timeout_is_one_end_to_end_deadline(self, tmp_paths):
        sock_path, _ = tmp_paths
        server, _ = _start_fake_rpc_server(
            sock_path,
            [(0.04, b" "), (0.04, b" "), (0.04, b" ")],
        )
        started = time.monotonic()
        try:
            with pytest.raises(ConnectionError, match="RPC failed"):
                rpc_call(sock_path, "ping", timeout=0.06)
            assert time.monotonic() - started < 0.11
        finally:
            server.join(timeout=2.0)

    def test_peer_uid_is_verified_for_local_socket(self):
        server, client = socket.socketpair()
        try:
            assert AlgoDaemon._peer_uid(server) == os.getuid()
        finally:
            server.close()
            client.close()

    def test_unverified_peer_is_rejected(
        self, tmp_paths, monkeypatch: pytest.MonkeyPatch
    ):
        daemon = AlgoDaemon(*tmp_paths)
        monkeypatch.setattr(daemon, "_peer_uid", lambda _conn: None)
        server, client = socket.socketpair()
        client.settimeout(1.0)
        try:
            daemon._accept_client(server)
            try:
                assert client.recv(1) == b""
            except ConnectionResetError:
                pass
            assert not daemon._clients
        finally:
            client.close()

    def test_client_thread_start_failure_rolls_back_publication(
        self, tmp_paths, monkeypatch: pytest.MonkeyPatch
    ):
        daemon = AlgoDaemon(*tmp_paths)
        server, client = socket.socketpair()
        client.settimeout(1.0)
        real_start = threading.Thread.start

        def fail_client_start(thread: threading.Thread) -> None:
            if thread.name.startswith("algo-daemon-client-"):
                raise RuntimeError("simulated thread exhaustion")
            real_start(thread)

        monkeypatch.setattr(threading.Thread, "start", fail_client_start)
        try:
            daemon._accept_client(server)
            assert not daemon._clients
            try:
                assert client.recv(1) == b""
            except ConnectionResetError:
                pass
        finally:
            client.close()

    def test_idle_client_is_closed_and_released(self, tmp_paths):
        sock_path, pid_path = tmp_paths
        daemon = AlgoDaemon(
            sock_path,
            pid_path,
            read_timeout=0.02,
            idle_timeout=0.08,
        )
        daemon.start_background()
        daemon.wait_until_ready()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1.0)
        try:
            client.connect(str(sock_path))
            assert client.recv(1) == b""
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and daemon._clients:
                time.sleep(0.01)
            assert not daemon._clients
        finally:
            client.close()
            daemon.stop()
            daemon.wait_for_shutdown()

    def test_graceful_shutdown_drains_inflight_dispatch(self, tmp_paths):
        sock_path, pid_path = tmp_paths
        registry = RPCRegistry()
        entered = threading.Event()

        def slow() -> str:
            entered.set()
            time.sleep(0.2)
            return "done"

        registry.register("slow", slow)
        daemon = AlgoDaemon(sock_path, pid_path, registry, drain_timeout=1.0)
        daemon.start_background()
        daemon.wait_until_ready()
        result: list[dict] = []
        caller = threading.Thread(target=lambda: result.append(_rpc_call(sock_path, "slow")))
        caller.start()
        assert entered.wait(2.0)
        daemon.stop()
        caller.join(timeout=2.0)
        daemon.wait_for_shutdown(timeout=2.0)
        assert result[0]["result"] == "done"
        assert not sock_path.exists()
        assert not pid_path.exists()

    def test_shutdown_reports_handler_that_survives_one_bounded_deadline(
        self, tmp_paths
    ):
        sock_path, pid_path = tmp_paths
        registry = RPCRegistry()
        entered = threading.Event()
        release = threading.Event()

        def blocked() -> str:
            entered.set()
            release.wait(2.0)
            return "done"

        registry.register("blocked", blocked)
        daemon = AlgoDaemon(sock_path, pid_path, registry, drain_timeout=0.05)
        daemon.start_background()
        daemon.wait_until_ready()
        caller_errors: list[BaseException] = []

        def call() -> None:
            try:
                _rpc_call(sock_path, "blocked")
            except BaseException as exc:
                caller_errors.append(exc)

        caller = threading.Thread(target=call)
        caller.start()
        assert entered.wait(2.0)
        started = time.monotonic()
        daemon.stop()
        try:
            with pytest.raises(TimeoutError, match="client thread"):
                daemon.wait_for_shutdown(timeout=1.0)
            assert time.monotonic() - started < 0.2
            assert not sock_path.exists()
            assert not pid_path.exists()
        finally:
            release.set()
            caller.join(timeout=2.0)
        assert not caller.is_alive()
        assert caller_errors


class TestDaemonRPC:
    def test_ping_returns_pong(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        assert _rpc_call(sock_path, "ping")["result"] == "pong"

    def test_unknown_method_returns_32601(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        response = _rpc_call(sock_path, "nonexistent")
        assert response["error"]["code"] == ERR_METHOD_NOT_FOUND
        assert response["error"]["message"] == "Method not found"

    def test_status_includes_readiness_and_version_identity(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        result = _rpc_call(sock_path, "status")["result"]
        assert result["ready"] is True
        assert result["pid"] == os.getpid()
        assert result["instance_id"]
        assert result["protocol_version"] == DAEMON_PROTOCOL_VERSION
        assert result["app_version"]
        assert "workers_running" in result

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (b"{not valid json}\n", ERR_PARSE_ERROR),
            (b'{"jsonrpc":"1.0","method":"ping","id":1}\n', ERR_INVALID_REQUEST),
            (b'{"jsonrpc":"2.0","method":"ping","params":[],"id":1}\n', ERR_INVALID_PARAMS),
        ],
    )
    def test_invalid_request_errors(self, running_daemon, tmp_paths, payload, code):
        sock_path, _ = tmp_paths
        assert _raw_exchange(sock_path, payload)["error"]["code"] == code

    def test_notification_is_dispatched_without_response(self, tmp_paths):
        sock_path, pid_path = tmp_paths
        called = threading.Event()
        registry = RPCRegistry()
        registry.register("notice", lambda: called.set())
        daemon = AlgoDaemon(sock_path, pid_path, registry)
        daemon.start_background()
        daemon.wait_until_ready()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        sock.connect(str(sock_path))
        try:
            sock.sendall(b'{"jsonrpc":"2.0","method":"notice"}\n')
            assert called.wait(1.0)
            with pytest.raises(socket.timeout):
                sock.recv(1)
        finally:
            sock.close()
            daemon.stop()
            daemon.wait_for_shutdown()

    def test_invalid_notification_has_no_response(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        sock.connect(str(sock_path))
        try:
            sock.sendall(
                b'{"jsonrpc":"2.0","method":"ping","params":[]}\n'
            )
            with pytest.raises(socket.timeout):
                sock.recv(1)
        finally:
            sock.close()

    def test_oversized_unterminated_frame_is_rejected(self, tmp_paths):
        sock_path, pid_path = tmp_paths
        daemon = AlgoDaemon(sock_path, pid_path, max_frame_size=64)
        daemon.start_background()
        daemon.wait_until_ready()
        try:
            response = _raw_exchange(sock_path, b"x" * 65)
            assert response["error"]["code"] == ERR_INVALID_REQUEST
            assert response["error"]["message"] == "Frame too large"
        finally:
            daemon.stop()
            daemon.wait_for_shutdown()

    def test_handler_failure_is_sanitized_over_transport(self, tmp_paths):
        sock_path, pid_path = tmp_paths
        registry = RPCRegistry()
        registry.register("boom", lambda: (_ for _ in ()).throw(ValueError("token=secret")))
        daemon = AlgoDaemon(sock_path, pid_path, registry)
        daemon.start_background()
        daemon.wait_until_ready()
        try:
            response = _rpc_call(sock_path, "boom")
            assert response["error"] == {"code": ERR_INTERNAL, "message": "Internal error"}
        finally:
            daemon.stop()
            daemon.wait_for_shutdown()

    def test_unserializable_result_returns_internal_error_and_keeps_connection(
        self, tmp_paths
    ):
        sock_path, pid_path = tmp_paths
        registry = RPCRegistry()
        registry.register("bad_result", lambda: object())
        daemon = AlgoDaemon(sock_path, pid_path, registry)
        daemon.start_background()
        daemon.wait_until_ready()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(sock_path))
        try:
            sock.sendall(
                b'{"jsonrpc":"2.0","method":"bad_result","id":1}\n'
                b'{"jsonrpc":"2.0","method":"ping","id":2}\n'
            )
            reader = sock.makefile("rb")
            first = json.loads(reader.readline())
            second = json.loads(reader.readline())
            assert first == {
                "jsonrpc": "2.0",
                "error": {"code": ERR_INTERNAL, "message": "Internal error"},
                "id": 1,
            }
            assert second == {"jsonrpc": "2.0", "result": "pong", "id": 2}
        finally:
            sock.close()
            daemon.stop()
            daemon.wait_for_shutdown()

    def test_telemetry_returns_method_stats(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        _rpc_call(sock_path, "ping")
        _rpc_call(sock_path, "ping")
        result = _rpc_call(sock_path, "telemetry")["result"]
        assert result["ping"]["call_count"] >= 2

    def test_shutdown_via_rpc_returns_before_cleanup(self, running_daemon, tmp_paths):
        sock_path, pid_path = tmp_paths
        assert _rpc_call(sock_path, "shutdown")["result"] == "shutting down"
        running_daemon.wait_for_shutdown(timeout=5.0)
        assert not sock_path.exists()
        assert not pid_path.exists()

    def test_concurrent_clients_are_independent(self, running_daemon, tmp_paths):
        sock_path, _ = tmp_paths
        results: list[dict] = []
        errors: list[BaseException] = []

        def call() -> None:
            try:
                results.append(_rpc_call(sock_path, "ping"))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert len(results) == 12
        assert all(item["result"] == "pong" for item in results)


class TestDefaultPaths:
    def test_default_paths_use_canonical_config_dir(self):
        assert get_default_socket_path() == CONFIG_DIR / "daemon.sock"
        assert get_default_pid_path() == CONFIG_DIR / "daemon.pid"

    def test_custom_base_dir(self, tmp_path: Path):
        assert get_default_socket_path(tmp_path) == tmp_path / "daemon.sock"
        assert get_default_pid_path(tmp_path) == tmp_path / "daemon.pid"

class TestDaemonCLIEntry:
    def test_non_daemon_prompt_is_not_intercepted(self):
        from algo_cli import main

        assert main._run_daemon_entry("ordinary prompt") is None
        assert main._run_daemon_entry(None) is None

    @pytest.mark.parametrize("prompt", ["daemon", "daemon unknown", "daemon start extra"])
    def test_invalid_daemon_command_returns_usage(self, prompt: str):
        from algo_cli import main

        assert main._run_daemon_entry(prompt) == 64

    def test_start_uses_explicit_daemon_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        from algo_cli import daemon as daemon_module
        from algo_cli import main

        seen: dict[str, Path | None] = {}

        def fake_start(*, base_dir=None):
            seen["base_dir"] = base_dir
            return True, "Daemon started"

        monkeypatch.setenv("ALGO_CLI_DAEMON_DIR", str(tmp_path))
        monkeypatch.setattr(daemon_module, "start_daemon_process", fake_start)
        assert main._run_daemon_entry("daemon start") == 0
        assert seen["base_dir"] == tmp_path

    def test_stop_failure_propagates_nonzero(self, monkeypatch: pytest.MonkeyPatch):
        from algo_cli import daemon as daemon_module
        from algo_cli import main

        monkeypatch.setattr(
            daemon_module,
            "stop_daemon_process",
            lambda **_kwargs: (False, "Daemon is not running"),
        )
        assert main._run_daemon_entry("daemon stop") == 1

    def test_status_requires_protocol_and_package_compatibility(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from algo_cli import __version__
        from algo_cli import daemon as daemon_module
        from algo_cli import main

        compatible = {
            "ready": True,
            "pid": 123,
            "uptime_seconds": 1.0,
            "protocol_version": DAEMON_PROTOCOL_VERSION,
            "app_version": __version__,
        }
        monkeypatch.setattr(
            daemon_module,
            "rpc_call",
            lambda *_args, **_kwargs: {"result": compatible},
        )
        assert main._run_daemon_entry("daemon status") == 0

        incompatible = dict(compatible, protocol_version=DAEMON_PROTOCOL_VERSION + 1)
        monkeypatch.setattr(
            daemon_module,
            "rpc_call",
            lambda *_args, **_kwargs: {"result": incompatible},
        )
        assert main._run_daemon_entry("daemon status") == 1

    def test_status_absent_is_nonzero(self, monkeypatch: pytest.MonkeyPatch):
        from algo_cli import daemon as daemon_module
        from algo_cli import main

        def missing(*_args, **_kwargs):
            raise ConnectionError("absent")

        monkeypatch.setattr(daemon_module, "rpc_call", missing)
        assert main._run_daemon_entry("daemon status") == 1

    def test_main_dispatches_before_config_load(self, monkeypatch: pytest.MonkeyPatch):
        from algo_cli import main

        seen: list[str | None] = []
        monkeypatch.setattr(main.sys, "argv", ["algo-cli", "daemon", "status"])
        monkeypatch.setattr(main, "has_legacy_data", lambda: False)
        monkeypatch.setattr(main, "migrate_legacy_sidecar_files", lambda: [])
        monkeypatch.setattr(main, "load_runtime_env", lambda *, override: None)
        monkeypatch.setattr(
            main,
            "_run_daemon_entry",
            lambda prompt: seen.append(prompt) or 0,
        )
        monkeypatch.setattr(
            main.Config,
            "load",
            lambda: pytest.fail("Config.load must not run for daemon lifecycle commands"),
        )

        main.main()
        assert seen == ["daemon status"]
