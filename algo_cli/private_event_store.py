"""Bounded, private JSONL persistence for local runtime events.

The store deliberately has a small contract:

* callers append JSON objects;
* the store owns the timestamp and on-disk envelope;
* readers receive only valid, non-expired records;
* maintenance keeps the newest suffix that satisfies record and byte limits;
* readiness exposes aggregate metadata, never event content.

The implementation uses an advisory sidecar lock so cooperating processes do
not interleave appends or race an atomic compaction.  A completed append is not
rolled back if the following compaction fails: the new line remains durable and
the aggregate result reports that maintenance is still required.  A later
append or explicit ``compact`` call can repair the file.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping


STORE_SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 1_000
DEFAULT_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_LOCK_RETRY_SECONDS = 0.025


class PrivateEventStoreError(RuntimeError):
    """Base error for private event-store operations."""


class UnsafeStorePathError(PrivateEventStoreError):
    """Raised when a store or lock path is not a regular file."""


class EventTooLargeError(ValueError):
    """Raised before writing an event that cannot fit the byte limit."""


@dataclass(frozen=True)
class RetentionPolicy:
    """Mandatory record, byte, and age bounds for a private event store."""

    max_records: int = DEFAULT_MAX_RECORDS
    max_bytes: int = DEFAULT_MAX_BYTES
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.max_records, int) or isinstance(self.max_records, bool) or self.max_records < 1:
            raise ValueError("max_records must be a positive integer")
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool) or self.max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if (
            not isinstance(self.max_age_seconds, (int, float))
            or isinstance(self.max_age_seconds, bool)
            or not math.isfinite(float(self.max_age_seconds))
            or self.max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a positive finite number")


@dataclass(frozen=True)
class StoredEvent:
    """One decoded event plus its store-controlled admission timestamp."""

    stored_at: float
    event: dict[str, Any]


@dataclass(frozen=True)
class MaintenanceResult:
    """Content-free aggregate result from append or compaction maintenance."""

    stored: bool
    compacted: bool
    retained_records: int
    retained_bytes: int
    file_bytes: int
    dropped_records: int
    malformed_records: int
    retention_satisfied: bool
    error_type: str | None = None


@dataclass
class _ScanResult:
    retained: deque[tuple[StoredEvent, bytes]]
    file_bytes: int = 0
    valid_records: int = 0
    malformed_records: int = 0
    expired_records: int = 0
    dropped_for_bounds: int = 0
    retained_bytes: int = 0

    @property
    def dropped_records(self) -> int:
        return self.expired_records + self.dropped_for_bounds

    @property
    def compaction_needed(self) -> bool:
        return (
            self.malformed_records > 0
            or self.dropped_records > 0
            or self.file_bytes != self.retained_bytes
        )


def _mode_is(path: Path, expected: int) -> bool | None:
    """Return exact POSIX mode equality, or ``None`` where it is not meaningful."""
    if os.name != "posix":
        return None
    try:
        return stat.S_IMODE(path.stat().st_mode) == expected
    except OSError:
        return False


def _ensure_regular_file(path: Path) -> None:
    """Reject symlinks and special files before following them for private I/O."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeStorePathError("private store paths must be regular files")


def _secure_open_flags(flags: int) -> int:
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        flags |= getattr(os, "O_BINARY", 0)
    return flags


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after atomic publication."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some otherwise valid filesystems do not support directory fsync.
        pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


class PrivateEventStore:
    """A cross-platform, lock-safe, retention-bounded private JSONL store."""

    def __init__(
        self,
        path: Path | str,
        *,
        policy: RetentionPolicy | None = None,
        lock_timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.policy = policy or RetentionPolicy()
        if (
            isinstance(lock_timeout_seconds, bool)
            or not math.isfinite(float(lock_timeout_seconds))
            or lock_timeout_seconds <= 0
        ):
            raise ValueError("lock_timeout_seconds must be a positive finite number")
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self._clock = clock

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _ensure_private_directory(self, *, repair_permissions: bool = True) -> None:
        parent = self.path.parent
        parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        if not parent.is_dir():
            raise UnsafeStorePathError("private store parent must be a directory")
        if repair_permissions and os.name == "posix":
            os.chmod(parent, _DIRECTORY_MODE)

    def _repair_file_mode(self, path: Path) -> None:
        _ensure_regular_file(path)
        if os.name == "posix" and path.exists():
            os.chmod(path, _FILE_MODE)

    @contextmanager
    def _locked(self, *, repair_permissions: bool = True) -> Iterator[None]:
        """Take an advisory lock without growing the sidecar on each use."""
        self._ensure_private_directory(repair_permissions=repair_permissions)
        _ensure_regular_file(self.lock_path)
        descriptor = os.open(
            self.lock_path,
            _secure_open_flags(os.O_RDWR | os.O_CREAT),
            _FILE_MODE,
        )
        try:
            if repair_permissions and os.name == "posix":
                os.fchmod(descriptor, _FILE_MODE)
            # msvcrt locks a byte range and needs at least one byte to exist.
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)

            deadline = time.monotonic() + self.lock_timeout_seconds
            if os.name == "nt":
                import msvcrt

                lock_region = getattr(msvcrt, "locking")
                lock_nonblocking = getattr(msvcrt, "LK_NBLCK")
                unlock = getattr(msvcrt, "LK_UNLCK")
                while True:
                    try:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        lock_region(descriptor, lock_nonblocking, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("timed out waiting for private event-store lock")
                        time.sleep(_LOCK_RETRY_SECONDS)
                try:
                    if os.fstat(descriptor).st_size != 1:
                        os.ftruncate(descriptor, 1)
                        os.fsync(descriptor)
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    lock_region(descriptor, unlock, 1)
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("timed out waiting for private event-store lock")
                        time.sleep(_LOCK_RETRY_SECONDS)
                try:
                    if os.fstat(descriptor).st_size != 1:
                        os.ftruncate(descriptor, 1)
                        os.fsync(descriptor)
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def initialize(self) -> None:
        """Create an empty private store and repair its directory/file modes."""
        with self._locked():
            self._repair_file_mode(self.path)
            created = not self.path.exists()
            descriptor = os.open(
                self.path,
                _secure_open_flags(os.O_WRONLY | os.O_APPEND | os.O_CREAT),
                _FILE_MODE,
            )
            try:
                if os.name == "posix":
                    os.fchmod(descriptor, _FILE_MODE)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if created:
                _fsync_directory(self.path.parent)

    def repair_permissions(self) -> None:
        """Repair the store directory, data file, and lock file when present."""
        with self._locked():
            self._repair_file_mode(self.path)
            self._repair_file_mode(self.lock_path)

    @staticmethod
    def _encode_event(event: Mapping[str, Any], stored_at: float) -> bytes:
        envelope = {
            "version": STORE_SCHEMA_VERSION,
            "stored_at": stored_at,
            "event": dict(event),
        }
        try:
            text = json.dumps(
                envelope,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("event must be a finite JSON object") from exc
        return text.encode("utf-8") + b"\n"

    @staticmethod
    def _decode_line(raw: bytes) -> StoredEvent | None:
        try:
            item = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(item, dict) or item.get("version") != STORE_SCHEMA_VERSION:
            return None
        stored_at = item.get("stored_at")
        event = item.get("event")
        if (
            isinstance(stored_at, bool)
            or not isinstance(stored_at, (int, float))
            or not math.isfinite(float(stored_at))
            or float(stored_at) < 0
            or not isinstance(event, dict)
        ):
            return None
        return StoredEvent(stored_at=float(stored_at), event=event)

    @classmethod
    def _encode_stored(cls, stored: StoredEvent) -> bytes:
        return cls._encode_event(stored.event, stored.stored_at)

    def _scan(self, *, now: float) -> _ScanResult:
        result = _ScanResult(retained=deque())
        _ensure_regular_file(self.path)
        try:
            result.file_bytes = self.path.stat().st_size
        except FileNotFoundError:
            return result

        cutoff = now - float(self.policy.max_age_seconds)
        retained_bytes = 0
        with self.path.open("rb") as handle:
            for raw in handle:
                if not raw.strip():
                    # Empty separators can safely delimit a previously torn tail.
                    continue
                stored = self._decode_line(raw)
                if stored is None:
                    result.malformed_records += 1
                    continue
                result.valid_records += 1
                try:
                    # Python's decoder accepts non-standard NaN/Infinity
                    # tokens.  Requiring canonical finite JSON here keeps such
                    # externally injected lines in the malformed bucket.
                    encoded = self._encode_stored(stored)
                except (TypeError, ValueError, RecursionError):
                    result.valid_records -= 1
                    result.malformed_records += 1
                    continue
                if stored.stored_at < cutoff:
                    result.expired_records += 1
                    continue

                result.retained.append((stored, encoded))
                retained_bytes += len(encoded)
                while (
                    len(result.retained) > self.policy.max_records
                    or retained_bytes > self.policy.max_bytes
                ):
                    _discarded, discarded_bytes = result.retained.popleft()
                    retained_bytes -= len(discarded_bytes)
                    result.dropped_for_bounds += 1

        result.retained_bytes = retained_bytes
        return result

    def _append_bytes(self, payload: bytes) -> None:
        """Append all bytes and fsync; a torn failure is tolerated on next use."""
        self._repair_file_mode(self.path)
        created = not self.path.exists()
        descriptor = os.open(
            self.path,
            _secure_open_flags(os.O_WRONLY | os.O_APPEND | os.O_CREAT),
            _FILE_MODE,
        )
        try:
            if os.name == "posix":
                os.fchmod(descriptor, _FILE_MODE)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("private event-store append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            _fsync_directory(self.path.parent)

    def _needs_separator(self) -> bool:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return False
        if size == 0:
            return False
        with self.path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            # JSONL records are LF-delimited.  A lone CR may be the first half
            # of a torn CRLF, so prefix LF to close it before the new record.
            return handle.read(1) != b"\n"

    def _atomic_replace(self, lines: Iterator[bytes]) -> None:
        """Publish compacted JSONL without exposing a partially written file."""
        self._ensure_private_directory()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        temporary = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, _FILE_MODE)
            with os.fdopen(descriptor, "wb") as handle:
                for line in lines:
                    handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            # The temporary inode already has the final private mode.  Keep
            # publication as the last fallible state transition so an error
            # before replace always leaves the old file intact.
            _fsync_directory(self.path.parent)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def _maintenance_result(
        self,
        scan: _ScanResult,
        *,
        stored: bool,
        compacted: bool,
        error_type: str | None = None,
    ) -> MaintenanceResult:
        if compacted:
            file_bytes = scan.retained_bytes
        else:
            try:
                file_bytes = self.path.stat().st_size
            except OSError:
                file_bytes = scan.file_bytes
        retention_satisfied = (
            error_type is None
            and len(scan.retained) <= self.policy.max_records
            and scan.retained_bytes <= self.policy.max_bytes
            and (compacted or not scan.compaction_needed)
        )
        return MaintenanceResult(
            stored=stored,
            compacted=compacted,
            retained_records=len(scan.retained),
            retained_bytes=scan.retained_bytes,
            file_bytes=file_bytes,
            dropped_records=scan.dropped_records,
            malformed_records=scan.malformed_records,
            retention_satisfied=retention_satisfied,
            error_type=error_type,
        )

    def append(self, event: Mapping[str, Any]) -> MaintenanceResult:
        """Durably append one event, then atomically enforce retention.

        Serialization and single-record size validation happen before touching
        the file.  If append succeeds but compaction raises an ``OSError``, the
        append is kept and the returned result names only the exception type.
        """
        if not isinstance(event, Mapping):
            raise TypeError("event must be a mapping")
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite non-negative timestamp")
        encoded = self._encode_event(event, now)
        if len(encoded) > self.policy.max_bytes:
            raise EventTooLargeError("encoded event exceeds private store max_bytes")

        with self._locked():
            # Delimit an old torn tail so this complete event remains recoverable.
            payload = (b"\n" if self._needs_separator() else b"") + encoded
            self._append_bytes(payload)
            scan = self._scan(now=now)
            if not scan.compaction_needed:
                return self._maintenance_result(scan, stored=True, compacted=False)
            try:
                self._atomic_replace(line for _stored, line in scan.retained)
            except Exception as exc:
                # Do not report append failure after its fsync: that invites a
                # retry and duplicate.  Surface bounded, content-free state.
                return self._maintenance_result(
                    scan,
                    stored=True,
                    compacted=False,
                    error_type=type(exc).__name__,
                )
            return self._maintenance_result(scan, stored=True, compacted=True)

    def compact(self) -> MaintenanceResult:
        """Atomically remove malformed, expired, and out-of-bound records."""
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite non-negative timestamp")
        with self._locked():
            self._repair_file_mode(self.path)
            scan = self._scan(now=now)
            if not scan.compaction_needed:
                return self._maintenance_result(scan, stored=False, compacted=False)
            self._atomic_replace(line for _stored, line in scan.retained)
            return self._maintenance_result(scan, stored=False, compacted=True)

    def read_records(self, *, limit: int | None = None) -> list[StoredEvent]:
        """Read the valid retained suffix without returning malformed content."""
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        if limit == 0:
            return []
        now = float(self._clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError("clock must return a finite non-negative timestamp")
        with self._locked():
            self._repair_file_mode(self.path)
            scan = self._scan(now=now)
            records = [stored for stored, _line in scan.retained]
        if limit is not None:
            return records[-limit:]
        return records

    def read_events(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Convenience view returning event objects without store metadata."""
        return [record.event for record in self.read_records(limit=limit)]

    def readiness(self, *, repair_permissions: bool = True) -> dict[str, Any]:
        """Return content-free aggregate health and retention metadata.

        Permission repair is enabled by default because broad permissions on a
        private store are a live safety defect.  No data file is created merely
        by checking readiness; the private directory and lock sidecar may be.
        """
        base: dict[str, Any] = {
            "schema_version": STORE_SCHEMA_VERSION,
            "initialized": False,
            "records": 0,
            "retained_records": 0,
            "file_bytes": 0,
            "retained_bytes": 0,
            "malformed_records": 0,
            "expired_records": 0,
            "dropped_for_bounds": 0,
            "compaction_needed": False,
            "permissions_enforced": os.name == "posix",
            "directory_private": None,
            "file_private": None,
            "lock_private": None,
            "limits": {
                "max_records": self.policy.max_records,
                "max_bytes": self.policy.max_bytes,
                "max_age_seconds": self.policy.max_age_seconds,
            },
            "error_type": None,
        }
        try:
            if repair_permissions:
                self._ensure_private_directory()
            elif not self.path.parent.exists():
                return {**base, "status": "empty"}

            with self._locked(repair_permissions=repair_permissions):
                if repair_permissions:
                    self._repair_file_mode(self.path)
                    self._repair_file_mode(self.lock_path)
                now = float(self._clock())
                if not math.isfinite(now) or now < 0:
                    raise ValueError("clock must return a finite non-negative timestamp")
                scan = self._scan(now=now)

            initialized = self.path.exists()
            directory_private = _mode_is(self.path.parent, _DIRECTORY_MODE)
            file_private = _mode_is(self.path, _FILE_MODE) if initialized else None
            lock_private = _mode_is(self.lock_path, _FILE_MODE)
            permission_problem = os.name == "posix" and (
                directory_private is not True
                or lock_private is not True
                or (initialized and file_private is not True)
            )
            degraded = scan.compaction_needed or permission_problem
            status = "degraded" if degraded else ("ready" if initialized else "empty")
            return {
                **base,
                "status": status,
                "initialized": initialized,
                "records": scan.valid_records,
                "retained_records": len(scan.retained),
                "file_bytes": scan.file_bytes,
                "retained_bytes": scan.retained_bytes,
                "malformed_records": scan.malformed_records,
                "expired_records": scan.expired_records,
                "dropped_for_bounds": scan.dropped_for_bounds,
                "compaction_needed": scan.compaction_needed,
                "directory_private": directory_private,
                "file_private": file_private,
                "lock_private": lock_private,
            }
        except Exception as exc:
            return {
                **base,
                "status": "error",
                "error_type": type(exc).__name__,
            }


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_RECORDS",
    "EventTooLargeError",
    "MaintenanceResult",
    "PrivateEventStore",
    "PrivateEventStoreError",
    "RetentionPolicy",
    "STORE_SCHEMA_VERSION",
    "StoredEvent",
    "UnsafeStorePathError",
]
