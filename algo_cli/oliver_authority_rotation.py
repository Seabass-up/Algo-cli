"""Rollback-forbidden authority-rotation recovery foundation.

This module does not replace an application bundle, touch Keychain, create an
Ada replay database, or enable the Austin dispatcher.  It defines the closed,
signed state machine that an installer must durably anchor *before* any such
mutation.  The external anchor is authoritative; the owner-only file is a
recoverable local cache and may never silently bootstrap a missing anchor.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator, Mapping, NoReturn, Protocol
import uuid

try:  # pragma: no cover - exercised by hosted platform cells
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - Windows is fail-closed
    fcntl = None  # type: ignore[assignment]

from .david_control_kernel import (
    MAX_SAFE_INTEGER,
    AuthorityRejected,
    ControlSigner,
    ControlVerifier,
    FrameRejected,
    canonical_json_bytes,
    decode_json_payload,
)


OLIVER_AUTHORITY_ROTATION_SCHEMA_VERSION = 1
OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND = "control_authority_rotation"
MAX_OLIVER_AUTHORITY_ROTATION_BYTES = 16 * 1024
MAX_OLIVER_AUTHORITY_ROTATION_WINDOW_MS = 60 * 60 * 1000

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$"
)
_BUILD_RE = re.compile(r"^[1-9][0-9]{0,8}$")
_REASONS = frozenset({"capacity", "clock_floor", "integrity", "page_limit"})
_PHASES = frozenset({"authorized", "commit_ready", "terminal"})
_ZERO_DIGEST = "sha256:" + ("0" * 64)
_LOCAL_ROTATION_LOCK = threading.RLock()


class OliverAuthorityRotationError(RuntimeError):
    """A content-free authority-rotation validation or durability failure."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "authority_rotation_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise OliverAuthorityRotationError(reason_code)


def _exact_dict(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        _reject("authority_rotation_schema")
    if not all(type(key) is str for key in value):
        _reject("authority_rotation_schema")
    return value


def _pattern(value: Any, pattern: re.Pattern[str], reason_code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _reject(reason_code)
    return value


def _bounded_int(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        _reject("authority_rotation_integer")
    return value


def _uuid(value: Any, reason_code: str) -> str:
    selected = _pattern(value, _UUID_RE, reason_code)
    try:
        parsed = uuid.UUID(selected)
    except ValueError:
        _reject(reason_code)
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != selected:
        _reject(reason_code)
    return selected


def _version(value: Any, reason_code: str) -> tuple[str, tuple[int, int, int]]:
    selected = _pattern(value, _VERSION_RE, reason_code)
    return selected, tuple(int(part) for part in selected.split("."))  # type: ignore[return-value]


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def authority_rotation_anchor_id(
    old_install_id: str,
    old_inventory_digest: str,
) -> str:
    """Derive the one anchor namespace for an exact signed installation."""

    selected_install = _uuid(
        old_install_id,
        "authority_rotation_old_install",
    )
    selected_inventory = _pattern(
        old_inventory_digest,
        _DIGEST_RE,
        "authority_rotation_old_inventory_digest",
    )
    return _digest_bytes(
        b"algo-cli-authority-rotation-anchor-v1\0"
        + selected_install.encode("ascii")
        + b"\0"
        + selected_inventory.encode("ascii")
    )


def authority_rotation_path_digest(path: str | Path) -> str:
    """Bind one exact normalized absolute path without retaining user path text."""

    try:
        raw = os.fspath(path)
    except TypeError:
        _reject("authority_rotation_path")
    if type(raw) is not str or not raw or "\x00" in raw or not os.path.isabs(raw):
        _reject("authority_rotation_path")
    normalized = os.path.normpath(raw)
    if normalized != raw or normalized == os.path.sep:
        _reject("authority_rotation_path")
    try:
        encoded = os.fsencode(normalized)
    except (TypeError, UnicodeError):
        _reject("authority_rotation_path")
    return _digest_bytes(b"algo-cli-authority-rotation-path-v1\0" + encoded)


class AuthorityRotationAnchorStore(Protocol):
    """External compare-and-set head, normally backed by an OS credential store."""

    def load(self, anchor_id: str) -> bytes | None: ...

    def compare_and_set(
        self,
        anchor_id: str,
        *,
        expected_digest: str | None,
        value: bytes,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class OliverAuthorityRotationRecord:
    rotation_id: str
    anchor_id: str
    old_install_id: str
    new_install_id: str
    old_inventory_digest: str
    new_inventory_digest: str
    old_app_digest: str
    new_app_digest: str
    database_path_digest: str
    evidence_path_digest: str
    old_database_digest: str
    old_samuel_key_id: str
    new_samuel_key_id: str
    old_app_version: str
    old_app_build_number: str
    new_app_version: str
    new_app_build_number: str
    reason_code: str
    authorized_at_ms: int
    expires_at_ms: int
    updated_at_ms: int
    phase: str
    revision: int
    previous_record_digest: str
    quiescence_evidence_digest: str
    retained_evidence_digest: str
    new_empty_store_digest: str
    qualification_evidence_digest: str
    control_authority_key_id: str
    signature: str
    schema_version: int = OLIVER_AUTHORITY_ROTATION_SCHEMA_VERSION

    @property
    def unsigned(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "authorized_at_ms": self.authorized_at_ms,
            "control_authority_key_id": self.control_authority_key_id,
            "database_path_digest": self.database_path_digest,
            "evidence_path_digest": self.evidence_path_digest,
            "expires_at_ms": self.expires_at_ms,
            "new_app_build_number": self.new_app_build_number,
            "new_app_digest": self.new_app_digest,
            "new_app_version": self.new_app_version,
            "new_empty_store_digest": self.new_empty_store_digest,
            "new_install_id": self.new_install_id,
            "new_inventory_digest": self.new_inventory_digest,
            "new_samuel_key_id": self.new_samuel_key_id,
            "old_app_build_number": self.old_app_build_number,
            "old_app_digest": self.old_app_digest,
            "old_app_version": self.old_app_version,
            "old_database_digest": self.old_database_digest,
            "old_install_id": self.old_install_id,
            "old_inventory_digest": self.old_inventory_digest,
            "old_samuel_key_id": self.old_samuel_key_id,
            "phase": self.phase,
            "previous_record_digest": self.previous_record_digest,
            "qualification_evidence_digest": self.qualification_evidence_digest,
            "quiescence_evidence_digest": self.quiescence_evidence_digest,
            "reason_code": self.reason_code,
            "retained_evidence_digest": self.retained_evidence_digest,
            "revision": self.revision,
            "rotation_id": self.rotation_id,
            "schema_version": self.schema_version,
            "updated_at_ms": self.updated_at_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned, "signature": self.signature}

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_dict())
        except FrameRejected:
            _reject("authority_rotation_encoding")
        if not 1 <= len(payload) <= MAX_OLIVER_AUTHORITY_ROTATION_BYTES:
            _reject("authority_rotation_size")
        return payload

    @property
    def digest(self) -> str:
        return _digest_bytes(self.to_bytes())

    def verify(self, verifier: ControlVerifier) -> None:
        validated = type(self).from_dict(self.to_dict())
        if validated != self:
            _reject("authority_rotation_schema")
        if (
            type(verifier) is not ControlVerifier
            or validated.control_authority_key_id != verifier.key_id
        ):
            _reject("authority_rotation_control_authority")
        try:
            verifier.verify(
                OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND,
                validated.unsigned,
                validated.signature,
            )
        except AuthorityRejected:
            _reject("authority_rotation_signature")

    @classmethod
    def authorize(
        cls,
        *,
        rotation_id: str,
        anchor_id: str,
        old_install_id: str,
        new_install_id: str,
        old_inventory_digest: str,
        new_inventory_digest: str,
        old_app_digest: str,
        new_app_digest: str,
        database_path_digest: str,
        evidence_path_digest: str,
        old_database_digest: str,
        old_samuel_key_id: str,
        new_samuel_key_id: str,
        old_app_version: str,
        old_app_build_number: str,
        new_app_version: str,
        new_app_build_number: str,
        reason_code: str,
        authorized_at_ms: int,
        expires_at_ms: int,
        signer: ControlSigner,
    ) -> "OliverAuthorityRotationRecord":
        if type(signer) is not ControlSigner:
            _reject("authority_rotation_control_authority")
        unsigned = {
            "anchor_id": anchor_id,
            "authorized_at_ms": authorized_at_ms,
            "control_authority_key_id": signer.key_id,
            "database_path_digest": database_path_digest,
            "evidence_path_digest": evidence_path_digest,
            "expires_at_ms": expires_at_ms,
            "new_app_build_number": new_app_build_number,
            "new_app_digest": new_app_digest,
            "new_app_version": new_app_version,
            "new_empty_store_digest": _ZERO_DIGEST,
            "new_install_id": new_install_id,
            "new_inventory_digest": new_inventory_digest,
            "new_samuel_key_id": new_samuel_key_id,
            "old_app_build_number": old_app_build_number,
            "old_app_digest": old_app_digest,
            "old_app_version": old_app_version,
            "old_database_digest": old_database_digest,
            "old_install_id": old_install_id,
            "old_inventory_digest": old_inventory_digest,
            "old_samuel_key_id": old_samuel_key_id,
            "phase": "authorized",
            "previous_record_digest": _ZERO_DIGEST,
            "qualification_evidence_digest": _ZERO_DIGEST,
            "quiescence_evidence_digest": _ZERO_DIGEST,
            "reason_code": reason_code,
            "retained_evidence_digest": _ZERO_DIGEST,
            "revision": 1,
            "rotation_id": rotation_id,
            "schema_version": OLIVER_AUTHORITY_ROTATION_SCHEMA_VERSION,
            "updated_at_ms": authorized_at_ms,
        }
        signature = signer.sign(OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND, unsigned)
        return cls.from_dict({**unsigned, "signature": signature})

    def prepare_commit(
        self,
        *,
        quiescence_evidence_digest: str,
        retained_evidence_digest: str,
        updated_at_ms: int,
        signer: ControlSigner,
    ) -> "OliverAuthorityRotationRecord":
        if type(signer) is not ControlSigner:
            _reject("authority_rotation_control_authority")
        self.verify(signer.verifier)
        if self.phase != "authorized" or self.revision != 1:
            _reject("authority_rotation_transition")
        selected_time = _bounded_int(updated_at_ms, minimum=self.updated_at_ms + 1)
        if selected_time > self.expires_at_ms:
            _reject("authority_rotation_expired")
        unsigned = {
            **self.unsigned,
            "phase": "commit_ready",
            "previous_record_digest": self.digest,
            "quiescence_evidence_digest": quiescence_evidence_digest,
            "retained_evidence_digest": retained_evidence_digest,
            "revision": 2,
            "updated_at_ms": selected_time,
        }
        signature = signer.sign(OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND, unsigned)
        return type(self).from_dict({**unsigned, "signature": signature})

    def complete(
        self,
        *,
        new_empty_store_digest: str,
        qualification_evidence_digest: str,
        updated_at_ms: int,
        signer: ControlSigner,
    ) -> "OliverAuthorityRotationRecord":
        if type(signer) is not ControlSigner:
            _reject("authority_rotation_control_authority")
        self.verify(signer.verifier)
        if self.phase != "commit_ready" or self.revision != 2:
            _reject("authority_rotation_transition")
        selected_time = _bounded_int(updated_at_ms, minimum=self.updated_at_ms + 1)
        if selected_time > self.expires_at_ms:
            _reject("authority_rotation_expired")
        unsigned = {
            **self.unsigned,
            "new_empty_store_digest": new_empty_store_digest,
            "phase": "terminal",
            "previous_record_digest": self.digest,
            "qualification_evidence_digest": qualification_evidence_digest,
            "revision": 3,
            "updated_at_ms": selected_time,
        }
        signature = signer.sign(OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND, unsigned)
        return type(self).from_dict({**unsigned, "signature": signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> "OliverAuthorityRotationRecord":
        if (
            type(payload) is not bytes
            or not 1 <= len(payload) <= MAX_OLIVER_AUTHORITY_ROTATION_BYTES
        ):
            _reject("authority_rotation_size")
        try:
            value = decode_json_payload(payload)
        except FrameRejected:
            _reject("authority_rotation_encoding")
        parsed = cls.from_dict(value)
        if not hmac.compare_digest(parsed.to_bytes(), payload):
            _reject("authority_rotation_noncanonical")
        return parsed

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OliverAuthorityRotationRecord":
        fields = frozenset(
            {
                "anchor_id",
                "authorized_at_ms",
                "control_authority_key_id",
                "database_path_digest",
                "evidence_path_digest",
                "expires_at_ms",
                "new_app_build_number",
                "new_app_digest",
                "new_app_version",
                "new_empty_store_digest",
                "new_install_id",
                "new_inventory_digest",
                "new_samuel_key_id",
                "old_app_build_number",
                "old_app_digest",
                "old_app_version",
                "old_database_digest",
                "old_install_id",
                "old_inventory_digest",
                "old_samuel_key_id",
                "phase",
                "previous_record_digest",
                "qualification_evidence_digest",
                "quiescence_evidence_digest",
                "reason_code",
                "retained_evidence_digest",
                "revision",
                "rotation_id",
                "schema_version",
                "signature",
                "updated_at_ms",
            }
        )
        row = _exact_dict(value, fields)
        if row["schema_version"] != OLIVER_AUTHORITY_ROTATION_SCHEMA_VERSION:
            _reject("authority_rotation_version")
        phase = row["phase"]
        if type(phase) is not str or phase not in _PHASES:
            _reject("authority_rotation_phase")
        revision = _bounded_int(row["revision"], minimum=1)
        if (phase, revision) not in {
            ("authorized", 1),
            ("commit_ready", 2),
            ("terminal", 3),
        }:
            _reject("authority_rotation_revision")
        reason_code = row["reason_code"]
        if type(reason_code) is not str or reason_code not in _REASONS:
            _reject("authority_rotation_reason")
        authorized_at_ms = _bounded_int(row["authorized_at_ms"])
        expires_at_ms = _bounded_int(row["expires_at_ms"], minimum=authorized_at_ms + 1)
        if expires_at_ms - authorized_at_ms > MAX_OLIVER_AUTHORITY_ROTATION_WINDOW_MS:
            _reject("authority_rotation_expiry")
        updated_at_ms = _bounded_int(row["updated_at_ms"], minimum=authorized_at_ms)
        if updated_at_ms > expires_at_ms:
            _reject("authority_rotation_expired")

        old_install_id = _uuid(row["old_install_id"], "authority_rotation_old_install")
        new_install_id = _uuid(row["new_install_id"], "authority_rotation_new_install")
        if old_install_id == new_install_id:
            _reject("authority_rotation_install_reuse")

        old_version, old_version_tuple = _version(
            row["old_app_version"], "authority_rotation_old_version"
        )
        new_version, new_version_tuple = _version(
            row["new_app_version"], "authority_rotation_new_version"
        )
        old_build = _pattern(
            row["old_app_build_number"], _BUILD_RE, "authority_rotation_old_build"
        )
        new_build = _pattern(
            row["new_app_build_number"], _BUILD_RE, "authority_rotation_new_build"
        )
        if new_version_tuple < old_version_tuple or int(new_build) <= int(old_build):
            _reject("authority_rotation_rollback")

        old_key = _pattern(
            row["old_samuel_key_id"], _KEY_ID_RE, "authority_rotation_old_key"
        )
        new_key = _pattern(
            row["new_samuel_key_id"], _KEY_ID_RE, "authority_rotation_new_key"
        )
        if hmac.compare_digest(old_key, new_key):
            _reject("authority_rotation_key_reuse")

        digests = {
            key: _pattern(row[key], _DIGEST_RE, f"authority_rotation_{key}")
            for key in (
                "anchor_id",
                "database_path_digest",
                "evidence_path_digest",
                "new_app_digest",
                "new_empty_store_digest",
                "new_inventory_digest",
                "old_app_digest",
                "old_database_digest",
                "old_inventory_digest",
                "previous_record_digest",
                "qualification_evidence_digest",
                "quiescence_evidence_digest",
                "retained_evidence_digest",
            )
        }
        if digests["database_path_digest"] == digests["evidence_path_digest"]:
            _reject("authority_rotation_path_reuse")
        if (
            digests["old_app_digest"] == digests["new_app_digest"]
            or digests["old_inventory_digest"] == digests["new_inventory_digest"]
            or digests["old_database_digest"] == _ZERO_DIGEST
        ):
            _reject("authority_rotation_identity_reuse")
        if not hmac.compare_digest(
            digests["anchor_id"],
            authority_rotation_anchor_id(
                old_install_id,
                digests["old_inventory_digest"],
            ),
        ):
            _reject("authority_rotation_anchor_context")

        transient = (
            digests["quiescence_evidence_digest"],
            digests["retained_evidence_digest"],
            digests["new_empty_store_digest"],
            digests["qualification_evidence_digest"],
        )
        if phase == "authorized" and (
            digests["previous_record_digest"] != _ZERO_DIGEST
            or transient != (_ZERO_DIGEST,) * 4
            or updated_at_ms != authorized_at_ms
        ):
            _reject("authority_rotation_authorized_state")
        if phase == "commit_ready" and (
            digests["previous_record_digest"] == _ZERO_DIGEST
            or transient[0] == _ZERO_DIGEST
            or transient[1] == _ZERO_DIGEST
            or transient[2:] != (_ZERO_DIGEST, _ZERO_DIGEST)
        ):
            _reject("authority_rotation_commit_state")
        if phase == "terminal" and (
            digests["previous_record_digest"] == _ZERO_DIGEST
            or any(item == _ZERO_DIGEST for item in transient)
        ):
            _reject("authority_rotation_terminal_state")

        return cls(
            rotation_id=_uuid(row["rotation_id"], "authority_rotation_id"),
            anchor_id=digests["anchor_id"],
            old_install_id=old_install_id,
            new_install_id=new_install_id,
            old_inventory_digest=digests["old_inventory_digest"],
            new_inventory_digest=digests["new_inventory_digest"],
            old_app_digest=digests["old_app_digest"],
            new_app_digest=digests["new_app_digest"],
            database_path_digest=digests["database_path_digest"],
            evidence_path_digest=digests["evidence_path_digest"],
            old_database_digest=digests["old_database_digest"],
            old_samuel_key_id=old_key,
            new_samuel_key_id=new_key,
            old_app_version=old_version,
            old_app_build_number=old_build,
            new_app_version=new_version,
            new_app_build_number=new_build,
            reason_code=reason_code,
            authorized_at_ms=authorized_at_ms,
            expires_at_ms=expires_at_ms,
            updated_at_ms=updated_at_ms,
            phase=phase,
            revision=revision,
            previous_record_digest=digests["previous_record_digest"],
            quiescence_evidence_digest=digests["quiescence_evidence_digest"],
            retained_evidence_digest=digests["retained_evidence_digest"],
            new_empty_store_digest=digests["new_empty_store_digest"],
            qualification_evidence_digest=digests["qualification_evidence_digest"],
            control_authority_key_id=_pattern(
                row["control_authority_key_id"],
                _KEY_ID_RE,
                "authority_rotation_control_authority",
            ),
            signature=_pattern(
                row["signature"], _SIGNATURE_RE, "authority_rotation_signature"
            ),
        )


_IMMUTABLE_RECORD_FIELDS = tuple(
    key
    for key in OliverAuthorityRotationRecord.__dataclass_fields__
    if key
    not in {
        "new_empty_store_digest",
        "phase",
        "previous_record_digest",
        "qualification_evidence_digest",
        "quiescence_evidence_digest",
        "retained_evidence_digest",
        "revision",
        "signature",
        "updated_at_ms",
    }
)


def _is_successor(
    current: OliverAuthorityRotationRecord,
    candidate: OliverAuthorityRotationRecord,
) -> bool:
    if any(getattr(current, key) != getattr(candidate, key) for key in _IMMUTABLE_RECORD_FIELDS):
        return False
    if candidate.previous_record_digest != current.digest:
        return False
    if candidate.updated_at_ms <= current.updated_at_ms:
        return False
    if (current.phase, candidate.phase, current.revision, candidate.revision) == (
        "authorized",
        "commit_ready",
        1,
        2,
    ):
        return (
            current.quiescence_evidence_digest == _ZERO_DIGEST
            and current.retained_evidence_digest == _ZERO_DIGEST
            and candidate.new_empty_store_digest == _ZERO_DIGEST
            and candidate.qualification_evidence_digest == _ZERO_DIGEST
        )
    if (current.phase, candidate.phase, current.revision, candidate.revision) == (
        "commit_ready",
        "terminal",
        2,
        3,
    ):
        return (
            candidate.quiescence_evidence_digest
            == current.quiescence_evidence_digest
            and candidate.retained_evidence_digest == current.retained_evidence_digest
        )
    return False


@dataclass(frozen=True, slots=True)
class OliverAuthorityRotationPermit:
    """Content-free, time-bounded authority for the exact prepared transition."""

    rotation_id: str
    record_digest: str
    old_install_id: str
    new_install_id: str
    old_samuel_key_id: str
    new_samuel_key_id: str
    database_path_digest: str
    evidence_path_digest: str
    retained_evidence_digest: str
    expires_at_ms: int


class AdaAuthorityRotationStore:
    """Externally anchored signed state with an owner-only recoverable cache."""

    def __init__(
        self,
        path: str | Path,
        *,
        uid: int,
        anchor_id: str,
        anchor_store: AuthorityRotationAnchorStore,
    ) -> None:
        selected = Path(path)
        if not selected.is_absolute() or not selected.name or ".." in selected.parts:
            _reject("authority_rotation_store_path")
        if type(uid) is not int or uid < 0:
            _reject("authority_rotation_store_owner")
        if any(
            not callable(getattr(anchor_store, method, None))
            for method in ("load", "compare_and_set")
        ):
            _reject("authority_rotation_anchor_store")
        self.path = selected
        self.uid = uid
        self.anchor_id = _pattern(
            anchor_id, _DIGEST_RE, "authority_rotation_anchor_id"
        )
        self._anchor_store = anchor_store

    def _directory_fd(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(self.path.anchor, flags)
            for component in self.path.parent.parts[1:]:
                next_descriptor = os.open(component, flags | nofollow, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            value = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(value.st_mode)
                or value.st_uid != self.uid
                or stat.S_IMODE(value.st_mode) != 0o700
            ):
                _reject("authority_rotation_store_directory")
            return descriptor
        except OliverAuthorityRotationError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            _reject("authority_rotation_store_directory")

    @contextmanager
    def _lease(self) -> Iterator[int]:
        if fcntl is None:
            _reject("authority_rotation_lock_unsupported")
        with _LOCAL_ROTATION_LOCK:
            directory_fd = self._directory_fd()
            lock_fd = -1
            try:
                flags = os.O_RDWR | os.O_CREAT
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                lock_fd = os.open(
                    ".AdaAuthorityRotation.lock",
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
                    _reject("authority_rotation_store_lock")
                named = os.stat(
                    ".AdaAuthorityRotation.lock",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (named.st_dev, named.st_ino) != (value.st_dev, value.st_ino):
                    _reject("authority_rotation_store_lock")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                named = os.stat(
                    ".AdaAuthorityRotation.lock",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if (named.st_dev, named.st_ino) != (value.st_dev, value.st_ino):
                    _reject("authority_rotation_store_lock")
                yield directory_fd
            except OliverAuthorityRotationError:
                raise
            except OSError:
                _reject("authority_rotation_store_lock")
            finally:
                if lock_fd >= 0:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(lock_fd)
                os.close(directory_fd)

    def _load_at(self, directory_fd: int) -> OliverAuthorityRotationRecord | None:
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path.name, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != self.uid
                or stat.S_IMODE(before.st_mode) != 0o600
                or not 1 <= before.st_size <= MAX_OLIVER_AUTHORITY_ROTATION_BYTES
            ):
                _reject("authority_rotation_store_file")
            payload = bytearray()
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    _reject("authority_rotation_store_file")
                payload.extend(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                _reject("authority_rotation_store_file")
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                _reject("authority_rotation_store_race")
            return OliverAuthorityRotationRecord.from_bytes(bytes(payload))
        except OliverAuthorityRotationError:
            raise
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _reject("authority_rotation_store_file")
            _reject("authority_rotation_store_read")
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _write_at(
        self,
        directory_fd: int,
        record: OliverAuthorityRotationRecord,
    ) -> None:
        payload = record.to_bytes()
        temporary = f".AdaAuthorityRotation.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    _reject("authority_rotation_store_write")
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
        except OliverAuthorityRotationError:
            raise
        except OSError:
            _reject("authority_rotation_store_write")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                _reject("authority_rotation_store_cleanup")

    def _load_anchor(self, verifier: ControlVerifier) -> OliverAuthorityRotationRecord | None:
        try:
            payload = self._anchor_store.load(self.anchor_id)
        except Exception:
            _reject("authority_rotation_anchor_unavailable")
        if payload is None:
            return None
        if type(payload) is not bytes:
            _reject("authority_rotation_anchor_value")
        try:
            record = OliverAuthorityRotationRecord.from_bytes(payload)
            record.verify(verifier)
        except OliverAuthorityRotationError:
            _reject("authority_rotation_anchor_value")
        if record.anchor_id != self.anchor_id:
            _reject("authority_rotation_anchor_context")
        return record

    def _reconcile_at(
        self,
        directory_fd: int,
        verifier: ControlVerifier,
    ) -> OliverAuthorityRotationRecord | None:
        cached = self._load_at(directory_fd)
        anchored = self._load_anchor(verifier)
        if anchored is None:
            if cached is not None:
                _reject("authority_rotation_anchor_missing")
            return None
        if cached is None:
            self._write_at(directory_fd, anchored)
            return anchored
        cached.verify(verifier)
        if cached.anchor_id != self.anchor_id:
            _reject("authority_rotation_store_context")
        if cached == anchored:
            return anchored
        if _is_successor(cached, anchored):
            self._write_at(directory_fd, anchored)
            return anchored
        _reject("authority_rotation_rollback")

    def load(self, verifier: ControlVerifier) -> OliverAuthorityRotationRecord | None:
        if type(verifier) is not ControlVerifier:
            _reject("authority_rotation_control_authority")
        with self._lease() as directory_fd:
            return self._reconcile_at(directory_fd, verifier)

    def publish(
        self,
        record: OliverAuthorityRotationRecord,
        *,
        verifier: ControlVerifier,
    ) -> OliverAuthorityRotationRecord:
        record.verify(verifier)
        if record.anchor_id != self.anchor_id:
            _reject("authority_rotation_anchor_context")
        payload = record.to_bytes()
        with self._lease() as directory_fd:
            current = self._reconcile_at(directory_fd, verifier)
            if current is None:
                if record.phase != "authorized" or record.revision != 1:
                    _reject("authority_rotation_conflict")
                expected_digest = None
            else:
                if current == record:
                    return current
                if not _is_successor(current, record):
                    _reject("authority_rotation_conflict")
                expected_digest = current.digest
            try:
                changed = self._anchor_store.compare_and_set(
                    self.anchor_id,
                    expected_digest=expected_digest,
                    value=payload,
                )
            except Exception:
                _reject("authority_rotation_anchor_unavailable")
            if type(changed) is not bool:
                _reject("authority_rotation_anchor_unavailable")
            if not changed:
                observed = self._load_anchor(verifier)
                if observed != record:
                    _reject("authority_rotation_conflict")
            self._write_at(directory_fd, record)
            confirmed = self._reconcile_at(directory_fd, verifier)
            if confirmed != record:
                _reject("authority_rotation_write_lost")
        return record

    def commit_permit(
        self,
        verifier: ControlVerifier,
        *,
        now_ms: int,
    ) -> OliverAuthorityRotationPermit:
        selected_now = _bounded_int(now_ms)
        record = self.load(verifier)
        if record is None or record.phase != "commit_ready" or record.revision != 2:
            _reject("authority_rotation_not_commit_ready")
        if selected_now < record.updated_at_ms or selected_now > record.expires_at_ms:
            _reject("authority_rotation_expired")
        return OliverAuthorityRotationPermit(
            rotation_id=record.rotation_id,
            record_digest=record.digest,
            old_install_id=record.old_install_id,
            new_install_id=record.new_install_id,
            old_samuel_key_id=record.old_samuel_key_id,
            new_samuel_key_id=record.new_samuel_key_id,
            database_path_digest=record.database_path_digest,
            evidence_path_digest=record.evidence_path_digest,
            retained_evidence_digest=record.retained_evidence_digest,
            expires_at_ms=record.expires_at_ms,
        )


__all__ = [
    "AdaAuthorityRotationStore",
    "AuthorityRotationAnchorStore",
    "MAX_OLIVER_AUTHORITY_ROTATION_BYTES",
    "MAX_OLIVER_AUTHORITY_ROTATION_WINDOW_MS",
    "OLIVER_AUTHORITY_ROTATION_SCHEMA_VERSION",
    "OLIVER_AUTHORITY_ROTATION_SIGNATURE_KIND",
    "OliverAuthorityRotationError",
    "OliverAuthorityRotationPermit",
    "OliverAuthorityRotationRecord",
    "authority_rotation_anchor_id",
    "authority_rotation_path_digest",
]
