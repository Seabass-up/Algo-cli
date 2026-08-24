"""DM1. Local always-on daemon lifecycle and Unix-socket transport.

The daemon is an optional optimization.  Normal Algo CLI startup remains
in-process unless the user explicitly invokes a daemon lifecycle command.
Source: ``docs/ALGO.md`` Track M, patterns DM1 and DM2.
"""
from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import CONFIG_DIR
from .daemon_rpc import (
    ERR_INTERNAL,
    ERR_INVALID_REQUEST,
    NOTIFICATION_ID,
    RPCError,
    RPCRegistry,
    make_error,
    make_response,
    parse_frame,
)

logger = logging.getLogger(__name__)

DEFAULT_SOCKET_NAME = "daemon.sock"
DEFAULT_PID_NAME = "daemon.pid"
DEFAULT_LOG_NAME = "daemon.log"
SOCKET_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
PID_MODE = 0o600
DAEMON_PROTOCOL_VERSION = 1
DRAIN_TIMEOUT = 5.0
READ_TIMEOUT = 0.5
IDLE_TIMEOUT = 30.0
WRITE_TIMEOUT = 30.0
START_TIMEOUT = 10.0
MAX_FRAME_SIZE = 10 * 1024 * 1024
MAX_CLIENTS = 32


@dataclass
class DaemonStatus:
    """Snapshot of daemon runtime state safe to expose to local clients."""

    pid: int = 0
    started_at: float = field(default_factory=time.time)
    socket_path: str = ""
    pid_path: str = ""
    instance_id: str = ""
    client_count: int = 0
    workers_running: int = 0
    shutdown_requested: bool = False
    ready: bool = False

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "socket_path": self.socket_path,
            "pid_path": self.pid_path,
            "instance_id": self.instance_id,
            "client_count": self.client_count,
            "workers_running": self.workers_running,
            "shutdown_requested": self.shutdown_requested,
            "ready": self.ready,
            "protocol_version": DAEMON_PROTOCOL_VERSION,
            "app_version": __version__,
        }


@dataclass(frozen=True)
class _PIDRecord:
    pid: int
    uid: int | None = None
    process_start: str | None = None
    instance_id: str | None = None


@dataclass
class _Client:
    conn: socket.socket
    thread: threading.Thread


def _current_uid() -> int:
    if not hasattr(os, "getuid"):
        raise RuntimeError("The always-on daemon currently requires a Unix platform")
    return os.getuid()


def _path_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino)


def _ensure_private_parent(path: Path) -> None:
    """Create or validate an exact owner-only parent directory.

    ``mkdir(mode=...)`` is filtered through the process umask.  Create each
    missing component separately and explicitly chmod only components created
    by this call; pre-existing caller-owned directories are validated without
    being rewritten.
    """
    parent = path.parent
    missing: list[Path] = []
    cursor = parent
    while True:
        try:
            cursor.lstat()
            break
        except FileNotFoundError:
            missing.append(cursor)
            if cursor.parent == cursor:
                raise RuntimeError(f"Could not resolve daemon directory: {parent}")
            cursor = cursor.parent

    for directory in reversed(missing):
        created = False
        try:
            directory.mkdir(mode=PRIVATE_DIR_MODE)
            created = True
        except FileExistsError:
            # Another same-user starter may have won the creation race.  Never
            # chmod its object; validate it exactly like any existing path.
            pass
        if created:
            os.chmod(directory, PRIVATE_DIR_MODE)
        info = directory.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != _current_uid()
            or stat.S_IMODE(info.st_mode) != PRIVATE_DIR_MODE
        ):
            raise RuntimeError(f"Daemon directory is not private and usable: {directory}")

    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"Daemon directory is not a real directory: {parent}")
    if info.st_uid != _current_uid():
        raise RuntimeError(f"Daemon directory is not owned by the current user: {parent}")
    if stat.S_IMODE(info.st_mode) != PRIVATE_DIR_MODE:
        raise RuntimeError(f"Daemon directory is not private to the current user: {parent}")


def _process_identity(pid: int) -> tuple[int | None, str | None]:
    """Return process uid and a start-time token, when the OS can provide them."""
    if pid <= 0:
        return None, None
    if sys.platform.startswith("linux"):
        try:
            proc_info = Path(f"/proc/{pid}").stat()
            fields = Path(f"/proc/{pid}/stat").read_text().split()
            return proc_info.st_uid, fields[21]
        except (FileNotFoundError, IndexError, OSError, ValueError):
            return None, None
    try:
        result = subprocess.run(
            ["ps", "-o", "uid=,lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode != 0 or not result.stdout.strip():
        return None, None
    pieces = result.stdout.strip().split(maxsplit=1)
    try:
        uid = int(pieces[0])
    except (IndexError, ValueError):
        uid = None
    started = pieces[1].strip() if len(pieces) == 2 else None
    return uid, started or None


def _pid_alive(pid: int) -> bool:
    """Return whether a process exists without sending it a real signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PIDLock:
    """Atomic, owner-only PID lock with identity-safe stale cleanup."""

    def __init__(self, path: str | Path, *, instance_id: str | None = None) -> None:
        self.path = Path(path)
        self.instance_id = instance_id or secrets.token_hex(16)
        self._owned_identity: tuple[int, int] | None = None
        self._guard_path = self.path.with_name(f".{self.path.name}.lock")
        self._guard_fd: int | None = None

    def _acquire_guard(self) -> None:
        """Hold a stable advisory lock across PID inspection and ownership."""
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        created = False
        try:
            fd = os.open(
                str(self._guard_path),
                flags | os.O_CREAT | os.O_EXCL,
                PID_MODE,
            )
            created = True
        except FileExistsError:
            fd = os.open(str(self._guard_path), flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != _current_uid():
                raise RuntimeError(f"Unsafe daemon PID guard path: {self._guard_path}")
            if created:
                os.fchmod(fd, PID_MODE)
                os.fsync(fd)
                info = os.fstat(fd)
            if stat.S_IMODE(info.st_mode) != PID_MODE:
                raise RuntimeError(f"Unsafe daemon PID guard mode: {self._guard_path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Daemon already running or startup is active") from exc
            path_info = self._guard_path.lstat()
            if (
                (path_info.st_dev, path_info.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISREG(path_info.st_mode)
                or path_info.st_uid != _current_uid()
                or stat.S_IMODE(path_info.st_mode) != PID_MODE
            ):
                raise RuntimeError("Daemon PID guard changed during acquisition")
            self._guard_fd = fd
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
            # Never unlink the guard: its stable inode is the synchronization
            # point across normal release, crashes, and stale-record cleanup.
            raise

    def _release_guard(self) -> None:
        fd, self._guard_fd = self._guard_fd, None
        if fd is None:
            return
        try:
            # Unlink only this locked inode before unlocking it.  A contender
            # that already opened the old inode will fail _acquire_guard's
            # pathname-identity check; a new contender may safely create the
            # next guard only after this owner has removed its PID record.
            info = os.fstat(fd)
            try:
                current = self._guard_path.lstat()
                if (
                    (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)
                    and stat.S_ISREG(current.st_mode)
                    and current.st_uid == _current_uid()
                ):
                    self._guard_path.unlink()
            except (FileNotFoundError, OSError):
                pass
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read_record(self) -> _PIDRecord | None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        # Backward-compatible read of the original integer-only lock.  JSON
        # integers parse successfully, so this must precede dict validation.
        if isinstance(obj, int) and not isinstance(obj, bool):
            return _PIDRecord(pid=obj)
        if not isinstance(obj, dict) or isinstance(obj.get("pid"), bool):
            return None
        try:
            pid = int(obj["pid"])
        except (KeyError, TypeError, ValueError):
            return None
        uid = obj.get("uid")
        if not isinstance(uid, int) or isinstance(uid, bool):
            uid = None
        started = obj.get("process_start")
        instance_id = obj.get("instance_id")
        return _PIDRecord(
            pid=pid,
            uid=uid,
            process_start=started if isinstance(started, str) else None,
            instance_id=instance_id if isinstance(instance_id, str) else None,
        )

    def _validate_existing_file(self) -> os.stat_result | None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Unsafe daemon PID path: {self.path}")
        if info.st_uid != _current_uid():
            raise RuntimeError(f"Daemon PID file is not owned by the current user: {self.path}")
        return info

    @staticmethod
    def _record_is_live(record: _PIDRecord | None) -> bool:
        if record is None or not _pid_alive(record.pid):
            return False
        actual_uid, actual_start = _process_identity(record.pid)
        if record.uid is not None and actual_uid is not None and record.uid != actual_uid:
            return False
        if (
            record.process_start is not None
            and actual_start is not None
            and record.process_start != actual_start
        ):
            return False
        # If identity metadata is unavailable, fail closed: a false "already
        # running" is safer than removing a lock for a live process.
        return True

    def acquire(self) -> int:
        """Atomically publish a complete lock record or fail closed.

        The record is written and synced under a private temporary name, then
        hard-linked into place.  Unlike creating the final path before writing,
        this never exposes an empty/partial record that a concurrent starter
        could misclassify as stale and unlink.
        """
        _ensure_private_parent(self.path)
        self._acquire_guard()
        pid = os.getpid()
        temp_path = self.path.with_name(
            f".{self.path.name}.{pid}.{secrets.token_hex(8)}.tmp"
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        temp_identity: tuple[int, int] | None = None
        published = False
        try:
            fd = os.open(str(temp_path), flags, PID_MODE)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != _current_uid():
                raise RuntimeError(f"Unsafe temporary daemon PID path: {temp_path}")
            temp_identity = (info.st_dev, info.st_ino)
            os.fchmod(fd, PID_MODE)
            uid, started = _process_identity(pid)
            payload = json.dumps(
                {
                    "pid": pid,
                    "uid": _current_uid() if uid is None else uid,
                    "process_start": started,
                    "instance_id": self.instance_id,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("Could not write daemon PID record")
                remaining = remaining[written:]
            os.fsync(fd)

            while True:
                try:
                    os.link(temp_path, self.path, follow_symlinks=False)
                    published = True
                    break
                except FileExistsError:
                    existing = self._validate_existing_file()
                    if existing is None:
                        continue
                    identity = (existing.st_dev, existing.st_ino)
                    record = self._read_record()
                    # Re-read when another same-user process replaced the path
                    # during inspection; never act on metadata from two files.
                    if _path_identity(self.path) != identity:
                        continue
                    if record is None:
                        raise RuntimeError(
                            "Daemon PID file is incomplete or invalid; refusing unsafe cleanup"
                        )
                    if self._record_is_live(record):
                        raise RuntimeError(f"Daemon already running (PID {record.pid})")
                    # Remove only the exact dead record inspected above.  A
                    # competing starter may claim the name before our retry;
                    # the next link/inspection iteration handles that safely.
                    if _path_identity(self.path) != identity:
                        continue
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass

            final = self.path.lstat()
            if (
                temp_identity is None
                or (final.st_dev, final.st_ino) != temp_identity
                or not stat.S_ISREG(final.st_mode)
                or final.st_uid != _current_uid()
                or stat.S_IMODE(final.st_mode) != PID_MODE
            ):
                raise RuntimeError("Daemon PID file changed during atomic publication")
            self._owned_identity = temp_identity
            return pid
        except BaseException:
            # If publication succeeded but validation failed, remove only our
            # exact inode.  Never unlink a replacement at the final path.
            if published and temp_identity is not None:
                try:
                    final = self.path.lstat()
                    if (
                        (final.st_dev, final.st_ino) == temp_identity
                        and stat.S_ISREG(final.st_mode)
                        and final.st_uid == _current_uid()
                    ):
                        self.path.unlink()
                except (FileNotFoundError, OSError):
                    pass
            self._release_guard()
            raise
        finally:
            if fd is not None:
                os.close(fd)
            if temp_identity is not None:
                try:
                    temp = temp_path.lstat()
                    if (
                        (temp.st_dev, temp.st_ino) == temp_identity
                        and stat.S_ISREG(temp.st_mode)
                        and temp.st_uid == _current_uid()
                    ):
                        temp_path.unlink()
                except (FileNotFoundError, OSError):
                    pass

    def release(self) -> None:
        """Remove only this instance's PID record, then release its guard."""
        try:
            if self._owned_identity is None:
                return
            info = self.path.lstat()
            record = self._read_record()
            if (
                (info.st_dev, info.st_ino) == self._owned_identity
                and stat.S_ISREG(info.st_mode)
                and info.st_uid == _current_uid()
                and record is not None
                and record.instance_id == self.instance_id
            ):
                self.path.unlink()
        except (FileNotFoundError, OSError):
            pass
        finally:
            self._owned_identity = None
            self._release_guard()


class AlgoDaemon:
    """Optional local daemon with strict JSON-RPC dispatch."""

    def __init__(
        self,
        socket_path: str | Path,
        pid_path: str | Path,
        registry: RPCRegistry | None = None,
        *,
        drain_timeout: float = DRAIN_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
        idle_timeout: float = IDLE_TIMEOUT,
        write_timeout: float = WRITE_TIMEOUT,
        max_frame_size: int = MAX_FRAME_SIZE,
        max_clients: int = MAX_CLIENTS,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.pid_path = Path(pid_path)
        self.registry = registry or RPCRegistry()
        self.instance_id = secrets.token_hex(16)
        self.status = DaemonStatus(
            socket_path=str(self.socket_path),
            pid_path=str(self.pid_path),
            instance_id=self.instance_id,
        )
        self.drain_timeout = max(0.0, drain_timeout)
        self.read_timeout = max(0.01, read_timeout)
        self.idle_timeout = max(self.read_timeout, idle_timeout)
        self.write_timeout = max(0.01, write_timeout)
        self.max_frame_size = max(1, max_frame_size)
        self.max_clients = max(1, max_clients)
        self._pid_lock = PIDLock(self.pid_path, instance_id=self.instance_id)
        self._sock: socket.socket | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._clients: dict[int, _Client] = {}
        self._client_counter = 0
        self._inflight = 0
        self._client_condition = threading.Condition(threading.RLock())
        self._shutdown_event = threading.Event()
        self._ready_event = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._shutdown_error: TimeoutError | None = None
        self._lifecycle_lock = threading.RLock()
        self._lifecycle_state = "stopped"
        self._signal_handlers: dict[int, Any] = {}
        self._register_builtin_methods()

    def _register_builtin_methods(self) -> None:
        self.registry.register("ping", lambda: "pong")
        self.registry.register("status", self._handle_status)
        self.registry.register("shutdown", self._handle_shutdown)
        self.registry.register("telemetry", self.registry.telemetry_snapshot)

    def _handle_status(self) -> dict[str, Any]:
        with self._client_condition:
            self.status.client_count = len(self._clients)
        return self.status.to_dict()

    def _handle_shutdown(self) -> str:
        self.stop()
        return "shutting down"

    def _prepare_socket_path(self) -> None:
        _ensure_private_parent(self.socket_path)
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError(f"Unsafe daemon socket path: {self.socket_path}")
        if info.st_uid != _current_uid():
            raise RuntimeError(
                f"Daemon socket is not owned by the current user: {self.socket_path}"
            )
        # A successful connect means something is actively listening.  Never
        # unlink such a socket even if the PID file was stale or missing.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            pass
        else:
            raise RuntimeError(f"Daemon socket is already active: {self.socket_path}")
        finally:
            probe.close()
        if _path_identity(self.socket_path) != (info.st_dev, info.st_ino):
            raise RuntimeError("Daemon socket changed during stale-socket cleanup")
        self.socket_path.unlink()

    def _reserve_start(self) -> None:
        """Reserve the single lifecycle transition into ``starting``."""
        with self._lifecycle_lock:
            if self._lifecycle_state != "stopped":
                raise RuntimeError("Daemon lifecycle is already active")
            self._lifecycle_state = "starting"
            self._shutdown_event.clear()
            self._ready_event.clear()
            self._startup_error = None
            self._shutdown_error = None
            self.status.shutdown_requested = False
            self.status.ready = False

    def start(self) -> None:
        """Run the daemon in the current thread until shutdown."""
        self._reserve_start()
        self._run_reserved_start()

    def _run_reserved_start(self) -> None:
        """Run a lifecycle already reserved by ``start`` or ``start_background``."""
        lock_acquired = False
        try:
            # ``stop`` may win immediately after a background start is
            # published.  Never erase that request or acquire resources for a
            # lifecycle that is already stopping.
            if self._shutdown_event.is_set():
                self._ready_event.set()
                return

            pid = self._pid_lock.acquire()
            lock_acquired = True
            self.status.pid = pid
            self.status.started_at = time.time()
            logger.info("Daemon starting (PID %d), socket=%s", pid, self.socket_path)

            if self._shutdown_event.is_set():
                self._ready_event.set()
                return
            self._install_signal_handlers()
            self._prepare_socket_path()
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(self.socket_path))
            info = self.socket_path.lstat()
            self._socket_identity = (info.st_dev, info.st_ino)
            os.chmod(self.socket_path, SOCKET_MODE)
            self._sock.listen(min(self.max_clients, 128))
            self._sock.settimeout(0.25)

            with self._lifecycle_lock:
                if self._shutdown_event.is_set():
                    self._lifecycle_state = "stopping"
                else:
                    self._lifecycle_state = "running"
                    self.status.ready = True
            self._ready_event.set()
            if self.status.ready:
                logger.info("Daemon ready on %s", self.socket_path)

            while not self._shutdown_event.is_set():
                listener = self._sock
                if listener is None:
                    break
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if self._shutdown_event.is_set():
                        break
                    if exc.errno == errno.EINTR:
                        continue
                    raise
                if self._shutdown_event.is_set():
                    # ``stop`` uses one local connection to wake a blocking
                    # macOS accept().  Never publish it as a client worker.
                    conn.close()
                    break
                self._accept_client(conn)
        except BaseException as exc:
            with self._lifecycle_lock:
                self._startup_error = exc
            self._ready_event.set()
            raise
        finally:
            with self._lifecycle_lock:
                self.status.ready = False
                self._shutdown_event.set()
                self._lifecycle_state = "stopping"
            try:
                self._drain_and_cleanup(release_pid=lock_acquired)
            finally:
                with self._lifecycle_lock:
                    self._lifecycle_state = "stopped"
                # Wake readiness waiters even when a stop arrived before bind.
                self._ready_event.set()

    def _background_runner(self) -> None:
        try:
            self._run_reserved_start()
        except BaseException:
            # ``wait_until_ready`` exposes the original failure to the owner.
            logger.exception("Daemon background thread exited")

    def start_background(self) -> None:
        """Start in a test/application thread; use ``wait_until_ready`` for proof."""
        with self._lifecycle_lock:
            self._reserve_start()
            thread = threading.Thread(
                target=self._background_runner,
                name="algo-daemon-accept",
                daemon=True,
            )
            self._accept_thread = thread
            try:
                thread.start()
            except BaseException as exc:
                self._accept_thread = None
                self._startup_error = exc
                self._lifecycle_state = "stopped"
                self._ready_event.set()
                raise

    def wait_until_ready(self, timeout: float = START_TIMEOUT) -> None:
        """Wait for a successful bind/listen or raise the startup exception."""
        if not self._ready_event.wait(timeout):
            raise TimeoutError("Timed out waiting for daemon readiness")
        if self._startup_error is not None:
            raise RuntimeError(f"Daemon startup failed: {self._startup_error}") from self._startup_error
        if not self.status.ready:
            raise RuntimeError("Daemon stopped before becoming ready")

    def stop(self) -> None:
        """Stop accepting new work and begin a bounded graceful drain."""
        wake_listener = False
        with self._lifecycle_lock:
            self.status.shutdown_requested = True
            self._shutdown_event.set()
            if self._lifecycle_state == "running":
                wake_listener = True
                self._lifecycle_state = "stopping"
            elif self._lifecycle_state == "starting":
                self._lifecycle_state = "stopping"
        if wake_listener:
            # Closing a listening AF_UNIX socket from another thread does not
            # promptly wake accept() on macOS.  A bounded same-user connection
            # does; the accept loop sees the event and closes it immediately.
            wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wake.settimeout(min(0.1, self.read_timeout))
            try:
                wake.connect(str(self.socket_path))
            except OSError:
                # The listener may already be exiting.  Its own timeout still
                # bounds this fallback; cleanup owns final listener closure.
                pass
            finally:
                wake.close()

    def wait_for_shutdown(self, timeout: float = 10.0) -> None:
        if self._accept_thread:
            self._accept_thread.join(timeout=timeout)
            if self._accept_thread.is_alive():
                raise TimeoutError("Timed out waiting for daemon shutdown")
        if self._shutdown_error is not None:
            raise self._shutdown_error

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._signal_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._signal_handler)

    def _restore_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, handler in self._signal_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                logger.debug("Could not restore signal %s", sig, exc_info=True)
        self._signal_handlers.clear()

    def _signal_handler(self, signum: int, frame: Any) -> None:
        del frame
        logger.info("Received signal %d; beginning graceful shutdown", signum)
        self.stop()

    @staticmethod
    def _peer_uid(conn: socket.socket) -> int | None:
        """Return the authenticated peer UID for supported Unix transports."""
        getter = getattr(conn, "getpeereid", None)
        if callable(getter):
            try:
                uid, _gid = getter()
                return int(uid)
            except OSError:
                return None
        if sys.platform == "darwin" and hasattr(socket, "LOCAL_PEERCRED"):
            # macOS exposes LOCAL_PEERCRED but CPython does not expose
            # getpeereid(). struct xucred begins with version then cr_uid.
            try:
                raw = conn.getsockopt(
                    getattr(socket, "SOL_LOCAL", 0),
                    socket.LOCAL_PEERCRED,
                    256,
                )
                version, uid = struct.unpack_from("@II", raw)
                if version != 0:  # XUCRED_VERSION from <sys/ucred.h>
                    return None
                return int(uid)
            except (OSError, struct.error):
                return None
        if hasattr(socket, "SO_PEERCRED"):
            try:
                raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                _pid, uid, _gid = struct.unpack("3i", raw)
                return int(uid)
            except (OSError, struct.error):
                return None
        return None

    def _accept_client(self, conn: socket.socket) -> None:
        peer_uid = self._peer_uid(conn)
        if peer_uid is None or peer_uid != _current_uid():
            conn.close()
            logger.warning("Rejected daemon client with unverified uid %s", peer_uid)
            return
        conn.settimeout(self.read_timeout)
        with self._client_condition:
            if len(self._clients) >= self.max_clients:
                try:
                    conn.settimeout(self.write_timeout)
                    conn.sendall(make_error(ERR_INVALID_REQUEST, "Server busy", None))
                except OSError:
                    pass
                finally:
                    conn.close()
                return
            self._client_counter += 1
            client_id = self._client_counter
            thread = threading.Thread(
                target=self._handle_client,
                args=(client_id, conn),
                name=f"algo-daemon-client-{client_id}",
                daemon=True,
            )
            client = _Client(conn=conn, thread=thread)
            self._clients[client_id] = client
            try:
                thread.start()
            except RuntimeError:
                # Thread exhaustion/start failures must not leak a published
                # client slot or crash the accept loop.
                if self._clients.get(client_id) is client:
                    self._clients.pop(client_id, None)
                self._client_condition.notify_all()
                try:
                    conn.close()
                except OSError:
                    pass
                logger.exception("Could not start daemon client thread %d", client_id)

    def _send(self, conn: socket.socket, payload: bytes) -> bool:
        try:
            conn.settimeout(self.write_timeout)
            conn.sendall(payload)
            conn.settimeout(self.read_timeout)
            return True
        except (OSError, socket.timeout):
            return False

    def _handle_client(self, client_id: int, conn: socket.socket) -> None:
        logger.debug("Client %d connected", client_id)
        buf = bytearray()
        last_activity = time.monotonic()
        try:
            while not self._shutdown_event.is_set():
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    if time.monotonic() - last_activity >= self.idle_timeout:
                        break
                    continue
                except OSError:
                    break
                if not data:
                    break
                last_activity = time.monotonic()
                buf.extend(data)
                # Enforce the cap before waiting for a delimiter.  One recv may
                # include the newline just beyond the maximum allowed frame.
                newline = buf.find(b"\n")
                if newline < 0 and len(buf) > self.max_frame_size:
                    self._send(
                        conn,
                        make_error(ERR_INVALID_REQUEST, "Frame too large", None),
                    )
                    break

                while True:
                    newline = buf.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buf[:newline])
                    del buf[: newline + 1]
                    if len(line) > self.max_frame_size:
                        self._send(
                            conn,
                            make_error(ERR_INVALID_REQUEST, "Frame too large", None),
                        )
                        return
                    with self._client_condition:
                        self._inflight += 1
                    try:
                        response = self._process_frame(line)
                        if response is not None and not self._send(conn, response):
                            return
                    finally:
                        with self._client_condition:
                            self._inflight -= 1
                            self._client_condition.notify_all()
                    if self._shutdown_event.is_set():
                        return
                if len(buf) > self.max_frame_size:
                    self._send(
                        conn,
                        make_error(ERR_INVALID_REQUEST, "Frame too large", None),
                    )
                    break
        except Exception:
            logger.exception("Error handling client %d", client_id)
        finally:
            with self._client_condition:
                self._clients.pop(client_id, None)
                self._client_condition.notify_all()
            try:
                conn.close()
            except OSError:
                pass
            logger.debug("Client %d disconnected", client_id)

    def _process_frame(self, line: bytes) -> bytes | None:
        method, params, req_id, error = parse_frame(line)
        if error is not None:
            # A syntactically valid notification never receives a response,
            # including when its method/params fail validation. Parse errors
            # and requests whose identity is unknowable still use a null id.
            if req_id is NOTIFICATION_ID:
                return None
            return make_error(error.code, error.message, req_id)  # type: ignore[arg-type]
        if method is None:
            return None
        notification = req_id is NOTIFICATION_ID
        try:
            result = self.registry.dispatch(method, params)
            if notification:
                return None
            try:
                return make_response(result, req_id)  # type: ignore[arg-type]
            except (TypeError, ValueError, UnicodeError):
                # A handler result is not successful until it can be encoded as
                # strict JSON.  Keep details in the private daemon log and
                # preserve the connection with a sanitized protocol response.
                logger.exception("RPC method %s returned an invalid result", method)
                return make_error(ERR_INTERNAL, "Internal error", req_id)  # type: ignore[arg-type]
        except RPCError as exc:
            if notification:
                return None
            return make_error(exc.code, exc.message, req_id)  # type: ignore[arg-type]

    def _close_listener(self) -> None:
        listener, self._sock = self._sock, None
        if listener is not None:
            try:
                # On macOS, close() from another thread may not wake accept()
                # until its socket timeout; shutdown() makes the wake prompt.
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                listener.close()
            except OSError:
                pass

    def _drain_and_cleanup(self, *, release_pid: bool) -> None:
        logger.info("Draining connections (timeout %.1fs)", self.drain_timeout)
        started = time.monotonic()
        self._close_listener()
        deadline = started + self.drain_timeout

        # Client recv calls have a short timeout and observe shutdown.  Active
        # dispatch/send operations receive the full bounded drain window.
        while time.monotonic() < deadline:
            with self._client_condition:
                clients = list(self._clients.values())
                if self._inflight == 0 and not clients:
                    break
                remaining = max(0.0, deadline - time.monotonic())
                self._client_condition.wait(timeout=min(0.05, remaining))

        with self._client_condition:
            remaining_clients = list(self._clients.values())
        for client in remaining_clients:
            try:
                client.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.conn.close()
            except OSError:
                pass

        # The drain timeout is one end-to-end deadline, not a fresh allowance
        # per client.  At the deadline, force-close transports and report every
        # handler that still survives instead of silently claiming success.
        surviving_threads = [
            client.thread
            for client in remaining_clients
            if client.thread is not threading.current_thread()
            and client.thread.is_alive()
        ]
        if surviving_threads:
            self._shutdown_error = TimeoutError(
                f"{len(surviving_threads)} daemon client thread(s) survived "
                f"the {self.drain_timeout:.2f}s shutdown drain"
            )
            logger.error("%s", self._shutdown_error)

        if self._socket_identity is not None:
            try:
                info = self.socket_path.lstat()
                if (
                    (info.st_dev, info.st_ino) == self._socket_identity
                    and stat.S_ISSOCK(info.st_mode)
                    and info.st_uid == _current_uid()
                ):
                    self.socket_path.unlink()
            except (FileNotFoundError, OSError):
                pass
            finally:
                self._socket_identity = None
        if release_pid:
            self._pid_lock.release()
        self._restore_signal_handlers()
        logger.info(
            "Daemon shutdown complete (drain %.2fs, uptime %.1fs)",
            time.monotonic() - started,
            self.status.uptime_seconds,
        )


def _validate_client_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ConnectionError("Daemon is not running") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise ConnectionError("Daemon endpoint is not a Unix socket")
    if info.st_uid != _current_uid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ConnectionError("Daemon endpoint is not private to the current user")


def rpc_call(
    socket_path: str | Path,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    req_id: int | str | None = 1,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Perform one correlated RPC call within one end-to-end deadline."""
    if not isinstance(method, str) or not method:
        raise ValueError("RPC method must be a non-empty string")
    if params is not None and not isinstance(params, dict):
        raise TypeError("RPC params must be a dictionary")
    if not (
        req_id is None
        or isinstance(req_id, str)
        or (isinstance(req_id, int) and not isinstance(req_id, bool))
    ):
        raise ValueError("RPC request id must be a string, integer, or null")
    if timeout <= 0:
        raise ValueError("RPC timeout must be positive")

    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise socket.timeout("Daemon RPC deadline expired")
        return value

    def reject_non_finite(value: str) -> Any:
        raise ValueError(f"Non-finite JSON number: {value}")

    path = Path(socket_path)
    _validate_client_socket(path)
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params if params is not None else {},
        "id": req_id,
    }
    try:
        payload = (
            json.dumps(
                request,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("RPC request is not valid JSON") from exc

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(remaining())
        sock.connect(str(path))
        sock.settimeout(remaining())
        sock.sendall(payload)
        buf = bytearray()
        while b"\n" not in buf:
            sock.settimeout(remaining())
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("Daemon closed the connection without a response")
            buf.extend(chunk)
            if len(buf) > MAX_FRAME_SIZE:
                raise ConnectionError("Daemon response exceeded the frame limit")
        line = bytes(buf).split(b"\n", 1)[0]
        try:
            response = json.loads(
                line.decode("utf-8"),
                parse_constant=reject_non_finite,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ConnectionError("Daemon returned an invalid response") from exc
        has_result = isinstance(response, dict) and "result" in response
        has_error = isinstance(response, dict) and "error" in response
        if (
            not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0"
            or response.get("id") != req_id
            or has_result == has_error
        ):
            raise ConnectionError("Daemon returned an invalid response")
        if has_error:
            error = response.get("error")
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), int)
                or isinstance(error.get("code"), bool)
                or not isinstance(error.get("message"), str)
            ):
                raise ConnectionError("Daemon returned an invalid response")
        return response
    except ConnectionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConnectionError("Daemon RPC failed") from exc
    finally:
        sock.close()


def health_check(
    socket_path: str | Path,
    timeout: float = 2.0,
    *,
    expected_protocol: int = DAEMON_PROTOCOL_VERSION,
    expected_app_version: str | None = __version__,
) -> bool:
    """Prove daemon readiness and protocol/application compatibility."""
    try:
        response = rpc_call(socket_path, "status", timeout=timeout)
        result = response.get("result")
        if not isinstance(result, dict):
            return False
        if result.get("ready") is not True:
            return False
        if result.get("protocol_version") != expected_protocol:
            return False
        if expected_app_version is not None and result.get("app_version") != expected_app_version:
            return False
        return True
    except (ConnectionError, TimeoutError):
        return False


def _daemon_base_dir(base_dir: str | Path | None = None) -> Path:
    return Path(base_dir).expanduser() if base_dir is not None else CONFIG_DIR


def get_default_socket_path(base_dir: str | Path | None = None) -> Path:
    return _daemon_base_dir(base_dir) / DEFAULT_SOCKET_NAME


def get_default_pid_path(base_dir: str | Path | None = None) -> Path:
    return _daemon_base_dir(base_dir) / DEFAULT_PID_NAME


def get_default_log_path(base_dir: str | Path | None = None) -> Path:
    return _daemon_base_dir(base_dir) / DEFAULT_LOG_NAME


def start_daemon_process(
    *,
    base_dir: str | Path | None = None,
    timeout: float = START_TIMEOUT,
) -> tuple[bool, str]:
    """Start a detached daemon only when explicitly requested by the user."""
    socket_path = get_default_socket_path(base_dir)
    if health_check(socket_path, timeout=min(timeout, 1.0)):
        return True, "Daemon is already running"
    log_path = get_default_log_path(base_dir)
    _ensure_private_parent(log_path)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    log_fd = os.open(str(log_path), flags, PID_MODE)
    try:
        log_info = os.fstat(log_fd)
        if not stat.S_ISREG(log_info.st_mode) or log_info.st_uid != _current_uid():
            raise RuntimeError(f"Unsafe daemon log path: {log_path}")
        os.fchmod(log_fd, PID_MODE)
        command = [sys.executable, "-I", "-m", "algo_cli", "daemon", "run"]
        env = os.environ.copy()
        env["ALGO_CLI_DAEMON_MODE"] = "1"
        # Make the child use the exact directory this parent is polling.  This
        # also prevents a pre-existing environment value from redirecting it.
        env["ALGO_CLI_DAEMON_DIR"] = str(socket_path.parent.resolve())
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
    finally:
        os.close(log_fd)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_check(socket_path, timeout=0.25):
            return True, f"Daemon started (PID {process.pid})"
        code = process.poll()
        if code is not None:
            return False, f"Daemon failed to start (exit {code}); see {log_path}"
        time.sleep(0.05)

    # A child that did not prove readiness must not be left detached.  Ask it
    # to terminate, then escalate only if it ignores the bounded grace period.
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
    return False, f"Timed out waiting for daemon; see {log_path}"


def stop_daemon_process(
    *,
    base_dir: str | Path | None = None,
    timeout: float = DRAIN_TIMEOUT + 2.0,
) -> tuple[bool, str]:
    """Request bounded shutdown through the owner-only socket."""
    socket_path = get_default_socket_path(base_dir)
    try:
        response = rpc_call(socket_path, "shutdown", timeout=min(timeout, 2.0))
    except ConnectionError:
        return False, "Daemon is not running"
    if response.get("result") != "shutting down":
        return False, "Daemon rejected the shutdown request"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not socket_path.exists():
            return True, "Daemon stopped"
        time.sleep(0.05)
    return False, "Timed out waiting for daemon shutdown"
