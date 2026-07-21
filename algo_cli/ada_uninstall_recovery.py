"""Signed write-ahead state for crash-safe Oliver uninstall recovery.

The record contains only content-free identifiers.  It is durably published
after explicit confirmation and before the first mutation, so a later process
can distinguish an authorized interrupted lifecycle from an unconfirmed plan.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hmac
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator, Mapping, NoReturn
import uuid

from .david_control_kernel import (
    MAX_SAFE_INTEGER,
    AuthorityRejected,
    ControlSigner,
    ControlVerifier,
    FrameRejected,
    canonical_json_bytes,
    decode_json_payload,
)


ADA_UNINSTALL_RECOVERY_SCHEMA_VERSION = 1
ADA_UNINSTALL_RECOVERY_SIGNATURE_KIND = "control_uninstall_recovery"
MAX_ADA_UNINSTALL_RECOVERY_BYTES = 128 * 1024
MAX_ADA_UNINSTALL_RECOVERY_IDS = 512

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MODES = frozenset({"runtime_only", "purge_private_state"})
_PHASES = frozenset({"authorized", "commit_ready", "terminal"})
_LAUNCH_STATES = frozenset({"absent", "loaded"})
_LOCAL_RECOVERY_LOCK = threading.RLock()


class AdaUninstallRecoveryError(RuntimeError):
    """A content-free recovery-record or durable-store failure."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "uninstall_recovery_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise AdaUninstallRecoveryError(reason_code)


def _exact_dict(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        _reject("uninstall_recovery_schema")
    if not all(type(key) is str for key in value):
        _reject("uninstall_recovery_schema")
    return value


def _pattern(value: Any, pattern: re.Pattern[str], reason_code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _reject(reason_code)
    return value


def _bounded_int(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        _reject("uninstall_recovery_integer")
    return value


def _ordered_ids(value: Any, reason_code: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > MAX_ADA_UNINSTALL_RECOVERY_IDS:
        _reject(reason_code)
    selected = tuple(_pattern(item, _DIGEST_RE, reason_code) for item in value)
    if selected != tuple(sorted(set(selected))):
        _reject(reason_code)
    return selected


def _terminal_receipt(value: Any, *, phase: str) -> dict[str, Any]:
    if phase == "authorized":
        if value != {}:
            _reject("uninstall_recovery_terminal")
        return {}
    if type(value) is not dict or not value or not all(type(key) is str for key in value):
        _reject("uninstall_recovery_terminal")
    try:
        encoded = canonical_json_bytes(value)
    except FrameRejected:
        _reject("uninstall_recovery_terminal")
    if len(encoded) > 64 * 1024:
        _reject("uninstall_recovery_terminal")
    return dict(value)


@dataclass(frozen=True, slots=True)
class AdaUninstallRecoveryRecord:
    install_id: str
    inventory_digest: str
    plan_digest: str
    mode: str
    present_entry_ids: tuple[str, ...]
    present_credential_ids: tuple[str, ...]
    launch_agent_state: str
    created_at_ms: int
    updated_at_ms: int
    revision: int
    phase: str
    terminal_receipt: dict[str, Any]
    authority_key_id: str
    signature: str
    schema_version: int = ADA_UNINSTALL_RECOVERY_SCHEMA_VERSION

    @property
    def unsigned(self) -> dict[str, Any]:
        return {
            "authority_key_id": self.authority_key_id,
            "created_at_ms": self.created_at_ms,
            "install_id": self.install_id,
            "inventory_digest": self.inventory_digest,
            "launch_agent_state": self.launch_agent_state,
            "mode": self.mode,
            "phase": self.phase,
            "plan_digest": self.plan_digest,
            "present_credential_ids": list(self.present_credential_ids),
            "present_entry_ids": list(self.present_entry_ids),
            "revision": self.revision,
            "schema_version": self.schema_version,
            "terminal_receipt": self.terminal_receipt,
            "updated_at_ms": self.updated_at_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned, "signature": self.signature}

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_dict())
        except FrameRejected:
            _reject("uninstall_recovery_encoding")
        if not 1 <= len(payload) <= MAX_ADA_UNINSTALL_RECOVERY_BYTES:
            _reject("uninstall_recovery_size")
        return payload

    def verify(self, verifier: ControlVerifier) -> None:
        if type(verifier) is not ControlVerifier or self.authority_key_id != verifier.key_id:
            _reject("uninstall_recovery_authority")
        try:
            verifier.verify(
                ADA_UNINSTALL_RECOVERY_SIGNATURE_KIND,
                self.unsigned,
                self.signature,
            )
        except AuthorityRejected:
            _reject("uninstall_recovery_signature")

    def verify_context(
        self,
        *,
        install_id: str,
        inventory_digest: str,
        mode: str,
    ) -> None:
        if self.install_id != install_id:
            _reject("uninstall_recovery_install")
        if not hmac.compare_digest(self.inventory_digest, inventory_digest):
            _reject("uninstall_recovery_inventory")
        if self.mode != mode:
            _reject("uninstall_recovery_mode")

    @classmethod
    def authorize(
        cls,
        *,
        install_id: str,
        inventory_digest: str,
        plan_digest: str,
        mode: str,
        present_entry_ids: tuple[str, ...],
        present_credential_ids: tuple[str, ...],
        launch_agent_state: str,
        created_at_ms: int,
        signer: ControlSigner,
    ) -> "AdaUninstallRecoveryRecord":
        unsigned = {
            "authority_key_id": signer.key_id,
            "created_at_ms": created_at_ms,
            "install_id": install_id,
            "inventory_digest": inventory_digest,
            "launch_agent_state": launch_agent_state,
            "mode": mode,
            "phase": "authorized",
            "plan_digest": plan_digest,
            "present_credential_ids": list(present_credential_ids),
            "present_entry_ids": list(present_entry_ids),
            "revision": 1,
            "schema_version": ADA_UNINSTALL_RECOVERY_SCHEMA_VERSION,
            "terminal_receipt": {},
            "updated_at_ms": created_at_ms,
        }
        signature = signer.sign(ADA_UNINSTALL_RECOVERY_SIGNATURE_KIND, unsigned)
        return cls.from_dict({**unsigned, "signature": signature})

    def complete(
        self,
        *,
        terminal_receipt: Mapping[str, Any],
        updated_at_ms: int,
        signer: ControlSigner,
    ) -> "AdaUninstallRecoveryRecord":
        self.verify(signer.verifier)
        if self.phase != "authorized" or self.revision != 1:
            _reject("uninstall_recovery_transition")
        selected_time = _bounded_int(updated_at_ms, minimum=self.created_at_ms)
        receipt = dict(terminal_receipt)
        unsigned = {
            **self.unsigned,
            "phase": "terminal",
            "revision": 2,
            "terminal_receipt": receipt,
            "updated_at_ms": selected_time,
        }
        signature = signer.sign(ADA_UNINSTALL_RECOVERY_SIGNATURE_KIND, unsigned)
        return self.from_dict({**unsigned, "signature": signature})

    def prepare_commit(
        self,
        *,
        terminal_receipt: Mapping[str, Any],
        updated_at_ms: int,
        signer: ControlSigner,
    ) -> "AdaUninstallRecoveryRecord":
        """Persist a pre-signed receipt before the final signer is deleted."""

        self.verify(signer.verifier)
        if (
            self.phase != "authorized"
            or self.revision != 1
            or self.mode != "purge_private_state"
        ):
            _reject("uninstall_recovery_transition")
        selected_time = _bounded_int(updated_at_ms, minimum=self.created_at_ms)
        unsigned = {
            **self.unsigned,
            "phase": "commit_ready",
            "revision": 2,
            "terminal_receipt": dict(terminal_receipt),
            "updated_at_ms": selected_time,
        }
        signature = signer.sign(ADA_UNINSTALL_RECOVERY_SIGNATURE_KIND, unsigned)
        return self.from_dict({**unsigned, "signature": signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> "AdaUninstallRecoveryRecord":
        if type(payload) is not bytes or not 1 <= len(payload) <= MAX_ADA_UNINSTALL_RECOVERY_BYTES:
            _reject("uninstall_recovery_size")
        try:
            value = decode_json_payload(payload)
        except FrameRejected:
            _reject("uninstall_recovery_encoding")
        parsed = cls.from_dict(value)
        if not hmac.compare_digest(parsed.to_bytes(), payload):
            _reject("uninstall_recovery_noncanonical")
        return parsed

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdaUninstallRecoveryRecord":
        row = _exact_dict(
            value,
            frozenset(
                {
                    "authority_key_id",
                    "created_at_ms",
                    "install_id",
                    "inventory_digest",
                    "launch_agent_state",
                    "mode",
                    "phase",
                    "plan_digest",
                    "present_credential_ids",
                    "present_entry_ids",
                    "revision",
                    "schema_version",
                    "signature",
                    "terminal_receipt",
                    "updated_at_ms",
                }
            ),
        )
        if (
            type(row["schema_version"]) is not int
            or row["schema_version"] != ADA_UNINSTALL_RECOVERY_SCHEMA_VERSION
        ):
            _reject("uninstall_recovery_version")
        mode = row["mode"]
        phase = row["phase"]
        launch_state = row["launch_agent_state"]
        if type(mode) is not str or mode not in _MODES:
            _reject("uninstall_recovery_mode")
        if type(phase) is not str or phase not in _PHASES:
            _reject("uninstall_recovery_phase")
        if phase == "commit_ready" and mode != "purge_private_state":
            _reject("uninstall_recovery_transition")
        if type(launch_state) is not str or launch_state not in _LAUNCH_STATES:
            _reject("uninstall_recovery_launch")
        revision = _bounded_int(row["revision"], minimum=1)
        if (phase, revision) not in {
            ("authorized", 1),
            ("commit_ready", 2),
            ("terminal", 2),
        }:
            _reject("uninstall_recovery_revision")
        created_at_ms = _bounded_int(row["created_at_ms"])
        updated_at_ms = _bounded_int(row["updated_at_ms"], minimum=created_at_ms)
        install_id = _pattern(row["install_id"], _UUID_RE, "uninstall_recovery_install")
        try:
            parsed_uuid = uuid.UUID(install_id)
        except ValueError:
            _reject("uninstall_recovery_install")
        if parsed_uuid.int == 0 or parsed_uuid.variant != uuid.RFC_4122:
            _reject("uninstall_recovery_install")
        return cls(
            install_id=install_id,
            inventory_digest=_pattern(
                row["inventory_digest"], _DIGEST_RE, "uninstall_recovery_inventory"
            ),
            plan_digest=_pattern(
                row["plan_digest"], _DIGEST_RE, "uninstall_recovery_plan"
            ),
            mode=mode,
            present_entry_ids=_ordered_ids(
                row["present_entry_ids"], "uninstall_recovery_entries"
            ),
            present_credential_ids=_ordered_ids(
                row["present_credential_ids"], "uninstall_recovery_credentials"
            ),
            launch_agent_state=launch_state,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
            revision=revision,
            phase=phase,
            terminal_receipt=_terminal_receipt(row["terminal_receipt"], phase=phase),
            authority_key_id=_pattern(
                row["authority_key_id"], _KEY_ID_RE, "uninstall_recovery_authority"
            ),
            signature=_pattern(
                row["signature"], _SIGNATURE_RE, "uninstall_recovery_signature"
            ),
        )


class AdaUninstallRecoveryStore:
    """One owner-only, atomically replaced recovery record."""

    def __init__(self, path: Path, *, uid: int) -> None:
        selected = Path(path)
        if not selected.is_absolute() or not selected.name or ".." in selected.parts:
            _reject("uninstall_recovery_path")
        if type(uid) is not int or uid < 0:
            _reject("uninstall_recovery_owner")
        self.path = selected
        self.uid = uid

    def _directory_fd(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(self.path.anchor, flags)
            for component in self.path.parent.parts[1:]:
                next_descriptor = os.open(
                    component,
                    flags | nofollow,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            value = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(value.st_mode)
                or value.st_uid != self.uid
                or stat.S_IMODE(value.st_mode) != 0o700
            ):
                _reject("uninstall_recovery_directory")
            return descriptor
        except AdaUninstallRecoveryError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            _reject("uninstall_recovery_directory")

    @contextmanager
    def _lease(self) -> Iterator[int]:
        with _LOCAL_RECOVERY_LOCK:
            directory_fd = self._directory_fd()
            lock_fd = -1
            try:
                flags = os.O_RDWR | os.O_CREAT
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                lock_fd = os.open(
                    ".AdaUninstallRecovery.lock",
                    flags,
                    0o600,
                    dir_fd=directory_fd,
                )
                value = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(value.st_mode)
                    or value.st_nlink != 1
                    or value.st_uid != self.uid
                    or stat.S_IMODE(value.st_mode) != 0o600
                ):
                    _reject("uninstall_recovery_lock")
                named = os.stat(
                    ".AdaUninstallRecovery.lock",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (named.st_dev, named.st_ino) != (value.st_dev, value.st_ino):
                    _reject("uninstall_recovery_lock")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                named = os.stat(
                    ".AdaUninstallRecovery.lock",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (named.st_dev, named.st_ino) != (value.st_dev, value.st_ino):
                    _reject("uninstall_recovery_lock")
                yield directory_fd
            except AdaUninstallRecoveryError:
                raise
            except OSError:
                _reject("uninstall_recovery_lock")
            finally:
                if lock_fd >= 0:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(lock_fd)
                os.close(directory_fd)

    def _load_at(self, directory_fd: int) -> AdaUninstallRecoveryRecord | None:
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path.name, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            value = os.fstat(descriptor)
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_nlink != 1
                or value.st_uid != self.uid
                or stat.S_IMODE(value.st_mode) != 0o600
                or not 1 <= value.st_size <= MAX_ADA_UNINSTALL_RECOVERY_BYTES
            ):
                _reject("uninstall_recovery_file")
            payload = bytearray()
            remaining = value.st_size
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    _reject("uninstall_recovery_file")
                payload.extend(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                _reject("uninstall_recovery_file")
            after = os.fstat(descriptor)
            if (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                _reject("uninstall_recovery_race")
            return AdaUninstallRecoveryRecord.from_bytes(bytes(payload))
        except AdaUninstallRecoveryError:
            raise
        except OSError:
            _reject("uninstall_recovery_read")
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def load(self) -> AdaUninstallRecoveryRecord | None:
        directory_fd = self._directory_fd()
        try:
            return self._load_at(directory_fd)
        finally:
            os.close(directory_fd)

    def publish(
        self,
        record: AdaUninstallRecoveryRecord,
        *,
        verifier: ControlVerifier,
    ) -> AdaUninstallRecoveryRecord:
        record.verify(verifier)
        payload = record.to_bytes()
        temporary = f".AdaUninstallRecovery.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        with self._lease() as directory_fd:
            current = self._load_at(directory_fd)
            if current is None:
                if record.phase != "authorized" or record.revision != 1:
                    _reject("uninstall_recovery_conflict")
            else:
                current.verify(verifier)
                if current == record:
                    return current
                if (
                    current.install_id != record.install_id
                    or current.inventory_digest != record.inventory_digest
                    or current.plan_digest != record.plan_digest
                    or current.authority_key_id != record.authority_key_id
                    or current.mode != record.mode
                    or current.present_entry_ids != record.present_entry_ids
                    or current.present_credential_ids != record.present_credential_ids
                    or current.launch_agent_state != record.launch_agent_state
                    or current.created_at_ms != record.created_at_ms
                    or current.phase != "authorized"
                    or record.phase not in {"commit_ready", "terminal"}
                    or record.revision != current.revision + 1
                ):
                    _reject("uninstall_recovery_conflict")
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
                os.fchmod(descriptor, 0o600)
                written = 0
                while written < len(payload):
                    count = os.write(descriptor, payload[written:])
                    if count <= 0:
                        _reject("uninstall_recovery_write")
                    written += count
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.replace(
                    temporary,
                    self.path.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            except AdaUninstallRecoveryError:
                raise
            except OSError:
                _reject("uninstall_recovery_write")
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    _reject("uninstall_recovery_cleanup")
            confirmed = self._load_at(directory_fd)
            if confirmed != record:
                _reject("uninstall_recovery_write_lost")
        return record


__all__ = [
    "ADA_UNINSTALL_RECOVERY_SCHEMA_VERSION",
    "ADA_UNINSTALL_RECOVERY_SIGNATURE_KIND",
    "AdaUninstallRecoveryError",
    "AdaUninstallRecoveryRecord",
    "AdaUninstallRecoveryStore",
    "MAX_ADA_UNINSTALL_RECOVERY_BYTES",
]
