"""Cross-process single-writer effect leases with persistent fencing tokens."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Any, Callable
import uuid


_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_RETRY_SECONDS = 0.01
_SAFE_OWNER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class EffectLeaseError(RuntimeError):
    pass


class EffectLeaseBusy(EffectLeaseError):
    pass


class EffectLeaseStateError(EffectLeaseError):
    pass


def target_digest(target: str) -> str:
    return hashlib.sha256(str(target).encode("utf-8")).hexdigest()


def _ensure_regular_or_missing(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EffectLeaseStateError("effect lease paths must be regular files")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TargetLease:
    """A held OS/thread lock plus its monotonically increasing fence."""

    def __init__(
        self,
        manager: "TargetLeaseManager",
        *,
        target_hash: str,
        owner_id: str,
        fencing_token: int,
        issued_at: float,
        expires_at: float,
        descriptor: int,
        thread_lock: threading.Lock,
    ) -> None:
        self.manager = manager
        self.target_hash = target_hash
        self.owner_id = owner_id
        self.fencing_token = fencing_token
        self.issued_at = issued_at
        self.expires_at = expires_at
        self._descriptor = descriptor
        self._thread_lock = thread_lock
        self.released = False

    def validate(self) -> bool:
        return self.manager.validate(self)

    def release(self) -> None:
        self.manager.release(self)

    def __enter__(self) -> "TargetLease":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


class TargetLeaseManager:
    """Serialize effects on a target across threads and cooperating processes."""

    _thread_map_guard = threading.Lock()
    _thread_locks: dict[str, threading.Lock] = {}

    def __init__(
        self,
        root: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self.root = Path(root)
        self._clock = clock
        if not math.isfinite(float(lock_timeout_seconds)) or lock_timeout_seconds <= 0:
            raise ValueError("lock_timeout_seconds must be positive and finite")
        self.lock_timeout_seconds = float(lock_timeout_seconds)

    def _paths(self, target_hash: str) -> tuple[Path, Path]:
        return self.root / f"{target_hash}.json", self.root / f"{target_hash}.lock"

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise EffectLeaseStateError("effect lease root is not a directory")
        if os.name == "posix":
            os.chmod(self.root, _DIRECTORY_MODE)

    def _thread_lock(self, target_hash: str) -> threading.Lock:
        key = f"{self.root.expanduser().resolve()}:{target_hash}"
        with self._thread_map_guard:
            return self._thread_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _lock_descriptor(descriptor: int, deadline: float) -> None:
        if os.name == "nt":
            import msvcrt

            lock_region = getattr(msvcrt, "locking")
            lock_nonblocking = getattr(msvcrt, "LK_NBLCK")
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    lock_region(descriptor, lock_nonblocking, 1)
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise EffectLeaseBusy("timed out waiting for target effect lease")
                    time.sleep(_LOCK_RETRY_SECONDS)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise EffectLeaseBusy("timed out waiting for target effect lease")
                    time.sleep(_LOCK_RETRY_SECONDS)

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            lock_region = getattr(msvcrt, "locking")
            unlock = getattr(msvcrt, "LK_UNLCK")
            os.lseek(descriptor, 0, os.SEEK_SET)
            lock_region(descriptor, unlock, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)

    @staticmethod
    def _read_state(path: Path, target_hash: str) -> dict[str, Any]:
        _ensure_regular_or_missing(path)
        if not path.exists():
            return {
                "version": 1,
                "target_hash": target_hash,
                "last_fencing_token": 0,
                "owner_id": "",
                "issued_at": 0.0,
                "expires_at": 0.0,
                "released": True,
            }
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise EffectLeaseStateError("effect lease state is unreadable") from exc
        token = value.get("last_fencing_token") if isinstance(value, dict) else None
        owner_id = value.get("owner_id") if isinstance(value, dict) else None
        released = value.get("released") if isinstance(value, dict) else None
        issued_at = value.get("issued_at", 0.0) if isinstance(value, dict) else None
        expires_at = value.get("expires_at") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or value.get("target_hash") != target_hash
            or isinstance(token, bool)
            or not isinstance(token, int)
            or token < 0
            or not isinstance(owner_id, str)
            or bool(owner_id) != (released is False)
            or (owner_id and not _SAFE_OWNER_RE.fullmatch(owner_id))
            or not isinstance(released, bool)
            or isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or not math.isfinite(float(issued_at))
            or float(issued_at) < 0
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) < float(issued_at)
        ):
            raise EffectLeaseStateError("effect lease state is invalid")
        return value

    def _write_state(self, path: Path, state: dict[str, Any]) -> None:
        _ensure_regular_or_missing(path)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(self.root)
        )
        temporary = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, _FILE_MODE)
            payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(self.root)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    def acquire(
        self,
        target: str,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 60.0,
    ) -> TargetLease:
        if not str(target).strip():
            raise ValueError("effect target must not be empty")
        if not math.isfinite(float(lease_seconds)) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        self._ensure_root()
        digest = target_digest(target)
        state_path, lock_path = self._paths(digest)
        _ensure_regular_or_missing(lock_path)
        thread_lock = self._thread_lock(digest)
        if not thread_lock.acquire(timeout=self.lock_timeout_seconds):
            raise EffectLeaseBusy("timed out waiting for in-process target effect lease")
        descriptor = -1
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                _FILE_MODE,
            )
            if os.name == "posix":
                os.fchmod(descriptor, _FILE_MODE)
            self._lock_descriptor(descriptor, time.monotonic() + self.lock_timeout_seconds)
            state = self._read_state(state_path, digest)
            fencing_token = int(state["last_fencing_token"]) + 1
            now = float(self._clock())
            if not math.isfinite(now) or now < 0:
                raise EffectLeaseStateError("effect lease clock is invalid")
            owner = owner_id or f"owner-{uuid.uuid4().hex}"
            if not _SAFE_OWNER_RE.fullmatch(owner):
                raise ValueError("owner_id must be a bounded non-sensitive identifier")
            expires_at = now + float(lease_seconds)
            self._write_state(
                state_path,
                {
                    "version": 1,
                    "target_hash": digest,
                    "last_fencing_token": fencing_token,
                    "owner_id": owner,
                    "issued_at": now,
                    "expires_at": expires_at,
                    "released": False,
                },
            )
            return TargetLease(
                self,
                target_hash=digest,
                owner_id=owner,
                fencing_token=fencing_token,
                issued_at=now,
                expires_at=expires_at,
                descriptor=descriptor,
                thread_lock=thread_lock,
            )
        except BaseException:
            if descriptor >= 0:
                try:
                    self._unlock_descriptor(descriptor)
                except OSError:
                    pass
                os.close(descriptor)
            thread_lock.release()
            raise

    def validate(self, lease: TargetLease) -> bool:
        if lease.manager is not self or lease.released:
            return False
        state_path, _lock_path = self._paths(lease.target_hash)
        state = self._read_state(state_path, lease.target_hash)
        try:
            now = float(self._clock())
        except Exception:
            return False
        if not math.isfinite(now) or now < lease.issued_at or now >= lease.expires_at:
            return False
        return (
            state.get("released") is False
            and state.get("owner_id") == lease.owner_id
            and state.get("last_fencing_token") == lease.fencing_token
            and float(state.get("issued_at", 0.0)) == lease.issued_at
            and float(state.get("expires_at", 0.0)) == lease.expires_at
        )

    def release(self, lease: TargetLease) -> None:
        if lease.manager is not self or lease.released:
            return
        try:
            state_path, _lock_path = self._paths(lease.target_hash)
            state = self._read_state(state_path, lease.target_hash)
            if (
                state.get("owner_id") == lease.owner_id
                and state.get("last_fencing_token") == lease.fencing_token
            ):
                self._write_state(
                    state_path,
                    {
                        **state,
                        "owner_id": "",
                        "issued_at": 0.0,
                        "expires_at": 0.0,
                        "released": True,
                    },
                )
        finally:
            try:
                self._unlock_descriptor(lease._descriptor)
            finally:
                os.close(lease._descriptor)
                lease.released = True
                lease._thread_lock.release()


__all__ = [
    "EffectLeaseBusy",
    "EffectLeaseError",
    "EffectLeaseStateError",
    "TargetLease",
    "TargetLeaseManager",
    "target_digest",
]
