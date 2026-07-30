"""Crash-safe, content-free state for the disabled control foundation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Iterator, Mapping, Protocol
import uuid

from .david_control_kernel import (
    MAX_ACTION_COUNT,
    MAX_SAFE_INTEGER,
    AuthorityRejected,
    ControlEnvelope,
    ControlPolicy,
    ControlRoute,
    ControlSigner,
    ControlVerifier,
    Operation,
    SnapshotRef,
    TargetKind,
    canonical_json_bytes,
    content_digest,
    decode_json_payload,
    verify_envelope_authority,
)


JOURNAL_SCHEMA_VERSION = 2
JOURNAL_APPLICATION_ID = 0x414C474F
JOURNAL_BUSY_TIMEOUT_MS = 2_000
RECEIPT_SCHEMA_VERSION = 2
MAX_RECEIPT_ANCHOR_BYTES = 4 * 1024
MAX_RECEIPT_ANCHOR_RETRIES = 8
# Stable sqlite3_limit category numbers from sqlite3.h. Python 3.10's typing
# stubs do not expose the symbolic module constants on every supported build.
_SQLITE_LIMIT_LENGTH = 0
_SQLITE_LIMIT_SQL_LENGTH = 1
_SQLITE_LIMIT_COLUMN = 2
_SQLITE_LIMIT_EXPR_DEPTH = 3
_SQLITE_LIMIT_VARIABLE_NUMBER = 9
EMPTY_EVIDENCE_DIGEST = "sha256:" + hashlib.sha256(b"algo-control-empty-evidence-v1").hexdigest()
EMPTY_RECEIPT_HEAD_DIGEST = "sha256:" + hashlib.sha256(
    b"algo-control-receipt-genesis-v1"
).hexdigest()
_RECEIPT_NAMESPACE = uuid.UUID("adac0de0-7dc7-4a1a-8ada-000000000001")

_SAFE_ID_RE = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPAQUE_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class ControlJournalError(RuntimeError):
    """A content-free durable-journal failure."""


class ControlJournalCorrupt(ControlJournalError):
    """The on-disk schema or a stored row failed closed validation."""


class ControlJournalRejected(ControlJournalError):
    """A durable policy invariant rejected a claim or transition."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = _safe_id(reason_code, "journal_rejected")
        super().__init__(self.reason_code)


class ReceiptHeadAnchorStore(Protocol):
    """Minimal CAS boundary for an external, content-free signed receipt head."""

    def load(self, journal_id: str) -> bytes | None: ...

    def compare_and_set(
        self,
        journal_id: str,
        *,
        expected_digest: str | None,
        value: bytes,
    ) -> bool: ...


class ControlEffectState(str, Enum):
    PREPARED = "prepared"
    STARTED = "started"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RevocationKind(str, Enum):
    GRANT = "grant"
    PERMIT = "permit"


_ALLOWED_TRANSITIONS: Mapping[ControlEffectState, frozenset[ControlEffectState]] = {
    ControlEffectState.PREPARED: frozenset({ControlEffectState.STARTED, ControlEffectState.FAILED}),
    ControlEffectState.STARTED: frozenset(
        {
            ControlEffectState.APPLIED,
            ControlEffectState.FAILED,
            ControlEffectState.UNKNOWN,
        }
    ),
    ControlEffectState.APPLIED: frozenset(
        {
            ControlEffectState.VERIFIED,
            ControlEffectState.FAILED,
            ControlEffectState.UNKNOWN,
        }
    ),
    ControlEffectState.UNKNOWN: frozenset({ControlEffectState.VERIFIED, ControlEffectState.FAILED}),
    ControlEffectState.VERIFIED: frozenset(),
    ControlEffectState.FAILED: frozenset(),
}
_RECEIPT_STATES = frozenset(
    {
        ControlEffectState.VERIFIED,
        ControlEffectState.FAILED,
        ControlEffectState.UNKNOWN,
    }
)


def _safe_id(value: Any, fallback: str = "invalid_value") -> str:
    if type(value) is str and _SAFE_ID_RE.fullmatch(value):
        return value
    if type(fallback) is str and _SAFE_ID_RE.fullmatch(fallback):
        return fallback
    return "invalid_value"


def _require_safe_id(value: Any, label: str) -> str:
    if type(value) is not str or not _SAFE_ID_RE.fullmatch(value):
        raise ControlJournalCorrupt(label)
    return value


def _require_uuid(value: Any, label: str) -> str:
    if type(value) is not str:
        raise ControlJournalCorrupt(label)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ControlJournalCorrupt(label) from None
    if str(parsed) != value or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        raise ControlJournalCorrupt(label)
    return value


def _require_int(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SAFE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ControlJournalCorrupt(label)
    return value


def _require_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise ControlJournalCorrupt(label)
    return value


def _require_digest(value: Any, label: str) -> str:
    return _require_pattern(value, _DIGEST_RE, label)


def _require_opaque(value: Any, label: str) -> str:
    return _require_pattern(value, _OPAQUE_ID_RE, label)


def _require_revision(value: Any, label: str) -> str:
    return _require_pattern(value, _REVISION_RE, label)


def _require_key_id(value: Any, label: str) -> str:
    return _require_pattern(value, _KEY_ID_RE, label)


def _require_signature(value: Any, label: str) -> str:
    return _require_pattern(value, _SIGNATURE_RE, label)


def _require_enum(enum_type: type[Enum], value: Any, label: str) -> Any:
    if type(value) is not str:
        raise ControlJournalCorrupt(label)
    try:
        return enum_type(value)
    except ValueError:
        raise ControlJournalCorrupt(label) from None


@dataclass(frozen=True, slots=True)
class ControlEffectRecord:
    effect_id: str
    permit_id: str
    grant_id: str
    session_id: str
    request_id: str
    request_digest: str
    target_kind: TargetKind
    target_id: str
    target_epoch: int
    target_revision: str
    fencing_token: int
    snapshot_id: str
    sequence: int
    operation: Operation
    route: ControlRoute
    authority_key_id: str
    state: ControlEffectState
    reason_code: str
    evidence_digest: str
    transition_version: int
    prepared_at_ms: int
    updated_at_ms: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ControlEffectRecord":
        try:
            return cls(
                effect_id=_require_uuid(row["effect_id"], "effect_id"),
                permit_id=_require_uuid(row["permit_id"], "permit_id"),
                grant_id=_require_uuid(row["grant_id"], "grant_id"),
                session_id=_require_uuid(row["session_id"], "session_id"),
                request_id=_require_uuid(row["request_id"], "request_id"),
                request_digest=_require_digest(row["request_digest"], "request_digest"),
                target_kind=_require_enum(TargetKind, row["target_kind"], "target_kind"),
                target_id=_require_opaque(row["target_id"], "target_id"),
                target_epoch=_require_int(row["target_epoch"], "target_epoch", minimum=1),
                target_revision=_require_revision(row["target_revision"], "target_revision"),
                fencing_token=_require_int(row["fencing_token"], "fencing_token", minimum=1),
                snapshot_id=_require_uuid(row["snapshot_id"], "snapshot_id"),
                sequence=_require_int(row["sequence"], "sequence", minimum=1),
                operation=_require_enum(Operation, row["operation"], "operation"),
                route=_require_enum(ControlRoute, row["route"], "route"),
                authority_key_id=_require_key_id(row["authority_key_id"], "authority_key_id"),
                state=_require_enum(ControlEffectState, row["state"], "effect_state"),
                reason_code=_require_safe_id(row["reason_code"], "reason_code"),
                evidence_digest=_require_digest(row["evidence_digest"], "evidence_digest"),
                transition_version=_require_int(
                    row["transition_version"],
                    "transition_version",
                    minimum=1,
                ),
                prepared_at_ms=_require_int(row["prepared_at_ms"], "prepared_at_ms"),
                updated_at_ms=_require_int(row["updated_at_ms"], "updated_at_ms"),
            )
        except (KeyError, TypeError):
            raise ControlJournalCorrupt("effect_row") from None

    @property
    def is_receiptable(self) -> bool:
        return self.state in _RECEIPT_STATES


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    receipt_id: str
    journal_id: str
    receipt_sequence: int
    previous_receipt_digest: str
    effect_id: str
    grant_id: str
    permit_id: str
    request_digest: str
    target_id: str
    target_epoch: int
    target_revision: str
    fencing_token: int
    sequence: int
    operation: Operation
    route: ControlRoute
    state: ControlEffectState
    reason_code: str
    evidence_digest: str
    transition_version: int
    completed_at_ms: int
    authority_key_id: str
    signature: str
    schema_version: int = RECEIPT_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Any) -> "ControlReceipt":
        fields = {
            "authority_key_id",
            "completed_at_ms",
            "effect_id",
            "evidence_digest",
            "fencing_token",
            "grant_id",
            "journal_id",
            "operation",
            "permit_id",
            "previous_receipt_digest",
            "reason_code",
            "receipt_id",
            "receipt_sequence",
            "request_digest",
            "route",
            "schema_version",
            "sequence",
            "signature",
            "state",
            "target_epoch",
            "target_id",
            "target_revision",
            "transition_version",
        }
        if type(value) is not dict or set(value) != fields:
            raise ControlJournalCorrupt("receipt_schema")
        if type(value["schema_version"]) is not int or value["schema_version"] != RECEIPT_SCHEMA_VERSION:
            raise ControlJournalCorrupt("receipt_version")
        state = _require_enum(ControlEffectState, value["state"], "receipt_state")
        if state not in _RECEIPT_STATES:
            raise ControlJournalCorrupt("receipt_state")
        return cls(
            receipt_id=_require_uuid(value["receipt_id"], "receipt_id"),
            journal_id=_require_digest(value["journal_id"], "journal_id"),
            receipt_sequence=_require_int(
                value["receipt_sequence"],
                "receipt_sequence",
                minimum=1,
            ),
            previous_receipt_digest=_require_digest(
                value["previous_receipt_digest"],
                "previous_receipt_digest",
            ),
            effect_id=_require_uuid(value["effect_id"], "effect_id"),
            grant_id=_require_uuid(value["grant_id"], "grant_id"),
            permit_id=_require_uuid(value["permit_id"], "permit_id"),
            request_digest=_require_digest(value["request_digest"], "request_digest"),
            target_id=_require_opaque(value["target_id"], "target_id"),
            target_epoch=_require_int(value["target_epoch"], "target_epoch", minimum=1),
            target_revision=_require_revision(value["target_revision"], "target_revision"),
            fencing_token=_require_int(value["fencing_token"], "fencing_token", minimum=1),
            sequence=_require_int(value["sequence"], "sequence", minimum=1),
            operation=_require_enum(Operation, value["operation"], "operation"),
            route=_require_enum(ControlRoute, value["route"], "route"),
            state=state,
            reason_code=_require_safe_id(value["reason_code"], "reason_code"),
            evidence_digest=_require_digest(value["evidence_digest"], "evidence_digest"),
            transition_version=_require_int(
                value["transition_version"],
                "transition_version",
                minimum=1,
            ),
            completed_at_ms=_require_int(value["completed_at_ms"], "completed_at_ms"),
            authority_key_id=_require_key_id(value["authority_key_id"], "authority_key_id"),
            signature=_require_signature(value["signature"], "signature"),
        )

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "journal_id": self.journal_id,
            "receipt_sequence": self.receipt_sequence,
            "previous_receipt_digest": self.previous_receipt_digest,
            "effect_id": self.effect_id,
            "grant_id": self.grant_id,
            "permit_id": self.permit_id,
            "request_digest": self.request_digest,
            "target_id": self.target_id,
            "target_epoch": self.target_epoch,
            "target_revision": self.target_revision,
            "fencing_token": self.fencing_token,
            "sequence": self.sequence,
            "operation": self.operation.value,
            "route": self.route.value,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "evidence_digest": self.evidence_digest,
            "transition_version": self.transition_version,
            "completed_at_ms": self.completed_at_ms,
            "authority_key_id": self.authority_key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}


def verify_control_receipt(
    receipt: ControlReceipt,
    verifier: ControlVerifier,
) -> None:
    if receipt.authority_key_id != verifier.key_id:
        raise AuthorityRejected("receipt_key")
    verifier.verify("control_receipt", receipt.unsigned_dict(), receipt.signature)


def _closed_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(properties),
        "properties": properties,
    }


def _json_schema_pattern(pattern: str) -> str:
    """Convert Python full-line patterns to an absolute ECMA-262 end guard."""

    if not pattern.startswith("^") or not pattern.endswith("$"):
        raise RuntimeError("schema_pattern")
    return pattern[:-1] + r"(?![\s\S])"


CONTROL_RECEIPT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    **_closed_schema(
        {
            "schema_version": {"const": RECEIPT_SCHEMA_VERSION},
            "receipt_id": {"type": "string", "pattern": _json_schema_pattern(_UUID_PATTERN)},
            "journal_id": {"type": "string", "pattern": _json_schema_pattern(_DIGEST_RE.pattern)},
            "receipt_sequence": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "previous_receipt_digest": {
                "type": "string",
                "pattern": _json_schema_pattern(_DIGEST_RE.pattern),
            },
            "effect_id": {"type": "string", "pattern": _json_schema_pattern(_UUID_PATTERN)},
            "grant_id": {"type": "string", "pattern": _json_schema_pattern(_UUID_PATTERN)},
            "permit_id": {"type": "string", "pattern": _json_schema_pattern(_UUID_PATTERN)},
            "request_digest": {"type": "string", "pattern": _json_schema_pattern(_DIGEST_RE.pattern)},
            "target_id": {"type": "string", "pattern": _json_schema_pattern(_OPAQUE_ID_RE.pattern)},
            "target_epoch": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "target_revision": {"type": "string", "pattern": _json_schema_pattern(_REVISION_RE.pattern)},
            "fencing_token": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "sequence": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "operation": {"enum": [item.value for item in Operation]},
            "route": {"enum": [item.value for item in ControlRoute]},
            "state": {"enum": [item.value for item in sorted(_RECEIPT_STATES, key=lambda item: item.value)]},
            "reason_code": {"type": "string", "pattern": _json_schema_pattern(_SAFE_ID_RE.pattern)},
            "evidence_digest": {"type": "string", "pattern": _json_schema_pattern(_DIGEST_RE.pattern)},
            "transition_version": {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER},
            "completed_at_ms": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER},
            "authority_key_id": {"type": "string", "pattern": _json_schema_pattern(_KEY_ID_RE.pattern)},
            "signature": {"type": "string", "pattern": _json_schema_pattern(_SIGNATURE_RE.pattern)},
        }
    ),
}


_SCHEMA_SQL = f"""
BEGIN IMMEDIATE;
CREATE TABLE ada_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    journal_id TEXT NOT NULL UNIQUE
) WITHOUT ROWID;
INSERT INTO ada_identity (singleton, journal_id)
VALUES (1, '__ADA_JOURNAL_ID__');
CREATE TABLE ada_grants (
    grant_id TEXT PRIMARY KEY,
    binding_digest TEXT NOT NULL,
    maximum_action_count INTEGER NOT NULL CHECK (
        maximum_action_count BETWEEN 1 AND {MAX_ACTION_COUNT}
    ),
    used_count INTEGER NOT NULL CHECK (
        used_count >= 0 AND used_count <= maximum_action_count
    ),
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms > 0)
) WITHOUT ROWID;
CREATE TABLE ada_permits (
    permit_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES ada_grants(grant_id),
    binding_digest TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    consumed_at_ms INTEGER NOT NULL CHECK (consumed_at_ms >= 0),
    expires_at_ms INTEGER NOT NULL CHECK (expires_at_ms > consumed_at_ms)
) WITHOUT ROWID;
CREATE TABLE ada_revocations (
    kind TEXT NOT NULL CHECK (kind IN ('grant', 'permit')),
    object_id TEXT NOT NULL,
    revoked_at_ms INTEGER NOT NULL CHECK (revoked_at_ms >= 0),
    reason_code TEXT NOT NULL,
    PRIMARY KEY (kind, object_id)
) WITHOUT ROWID;
CREATE TABLE ada_sessions (
    session_id TEXT PRIMARY KEY,
    last_sequence INTEGER NOT NULL CHECK (last_sequence > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
) WITHOUT ROWID;
CREATE TABLE ada_targets (
    target_id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK (
        target_kind IN ('browser_document', 'desktop_surface', 'external_resource')
    ),
    target_epoch INTEGER NOT NULL CHECK (target_epoch > 0),
    target_revision TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0)
) WITHOUT ROWID;
CREATE TABLE ada_effects (
    effect_id TEXT PRIMARY KEY,
    permit_id TEXT NOT NULL UNIQUE REFERENCES ada_permits(permit_id),
    grant_id TEXT NOT NULL REFERENCES ada_grants(grant_id),
    session_id TEXT NOT NULL REFERENCES ada_sessions(session_id),
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (
        target_kind IN ('browser_document', 'desktop_surface', 'external_resource')
    ),
    target_id TEXT NOT NULL REFERENCES ada_targets(target_id),
    target_epoch INTEGER NOT NULL CHECK (target_epoch > 0),
    target_revision TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    snapshot_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    operation TEXT NOT NULL CHECK (
        operation IN (
            'observe', 'activate', 'input_text', 'select_option',
            'scroll', 'upload', 'coordinate_activate', 'handoff'
        )
    ),
    route TEXT NOT NULL CHECK (
        route IN (
            'connector', 'shortcut', 'apple_event', 'dom', 'ax',
            'screenshot', 'coordinate', 'handoff'
        )
    ),
    authority_key_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('prepared', 'started', 'applied', 'verified', 'failed', 'unknown')
    ),
    reason_code TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    transition_version INTEGER NOT NULL CHECK (transition_version > 0),
    prepared_at_ms INTEGER NOT NULL CHECK (prepared_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= prepared_at_ms),
    UNIQUE (session_id, sequence)
) WITHOUT ROWID;
CREATE INDEX ada_effect_state_index ON ada_effects(state, updated_at_ms);
CREATE TABLE ada_receipts (
    effect_id TEXT NOT NULL REFERENCES ada_effects(effect_id),
    transition_version INTEGER NOT NULL CHECK (transition_version > 0),
    receipt_sequence INTEGER NOT NULL UNIQUE CHECK (receipt_sequence > 0),
    previous_receipt_digest TEXT NOT NULL,
    receipt_blob BLOB NOT NULL,
    receipt_digest TEXT NOT NULL,
    PRIMARY KEY (effect_id, transition_version)
) WITHOUT ROWID;
PRAGMA application_id = {JOURNAL_APPLICATION_ID};
PRAGMA user_version = {JOURNAL_SCHEMA_VERSION};
COMMIT;
"""

_EXPECTED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "ada_identity": ("singleton", "journal_id"),
    "ada_grants": (
        "grant_id",
        "binding_digest",
        "maximum_action_count",
        "used_count",
        "expires_at_ms",
    ),
    "ada_permits": (
        "permit_id",
        "grant_id",
        "binding_digest",
        "request_digest",
        "consumed_at_ms",
        "expires_at_ms",
    ),
    "ada_revocations": ("kind", "object_id", "revoked_at_ms", "reason_code"),
    "ada_sessions": ("session_id", "last_sequence", "updated_at_ms"),
    "ada_targets": (
        "target_id",
        "target_kind",
        "target_epoch",
        "target_revision",
        "fencing_token",
        "updated_at_ms",
    ),
    "ada_effects": (
        "effect_id",
        "permit_id",
        "grant_id",
        "session_id",
        "request_id",
        "request_digest",
        "target_kind",
        "target_id",
        "target_epoch",
        "target_revision",
        "fencing_token",
        "snapshot_id",
        "sequence",
        "operation",
        "route",
        "authority_key_id",
        "state",
        "reason_code",
        "evidence_digest",
        "transition_version",
        "prepared_at_ms",
        "updated_at_ms",
    ),
    "ada_receipts": (
        "effect_id",
        "transition_version",
        "receipt_sequence",
        "previous_receipt_digest",
        "receipt_blob",
        "receipt_digest",
    ),
}


class ControlJournal:
    """SQLite state machine for one-use control permits.

    The journal contains only opaque identifiers, finite enums, counters,
    timestamps, reason codes, and digests. It intentionally has no argument,
    URL, selector, path, screenshot, or model-content column.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        receipt_anchor_store: ReceiptHeadAnchorStore | None = None,
    ) -> None:
        if not isinstance(path, (str, Path)):
            raise ControlJournalError("journal_path")
        if receipt_anchor_store is not None and any(
            not callable(getattr(receipt_anchor_store, method, None))
            for method in ("load", "compare_and_set")
        ):
            raise ControlJournalError("receipt_anchor_store")
        raw_path = Path(path).expanduser()
        if not raw_path.is_absolute() or raw_path.name in {"", ".", ".."}:
            raise ControlJournalError("journal_path")
        if raw_path.is_symlink():
            raise ControlJournalError("journal_symlink")
        self.path = raw_path.resolve(strict=False)
        self._receipt_anchor_store = receipt_anchor_store
        self._receipt_anchor_bootstrap_allowed = False
        self._prepare_private_file()
        self._initialize()

    @classmethod
    def at_path(cls, path: str | Path) -> "ControlJournal":
        return cls(path)

    def _prepare_private_file(self) -> None:
        parent = self.path.parent
        try:
            if not parent.exists():
                parent.mkdir(mode=0o700)
            parent_stat = parent.lstat()
        except OSError:
            raise ControlJournalError("journal_directory") from None
        if not stat.S_ISDIR(parent_stat.st_mode) or parent.is_symlink():
            raise ControlJournalError("journal_directory")
        self._validate_private_stat(parent_stat, directory=True)

        try:
            file_stat = self.path.lstat()
        except FileNotFoundError:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
                os.close(descriptor)
                file_stat = self.path.lstat()
            except OSError:
                raise ControlJournalError("journal_create") from None
        except OSError:
            raise ControlJournalError("journal_file") from None
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            raise ControlJournalError("journal_file")
        if file_stat.st_nlink != 1:
            raise ControlJournalError("journal_hardlink")
        self._validate_private_stat(file_stat, directory=False)

    @staticmethod
    def _validate_private_stat(value: os.stat_result, *, directory: bool) -> None:
        if os.name != "posix":
            return
        if hasattr(os, "getuid") and value.st_uid != os.getuid():
            raise ControlJournalError("journal_directory_owner" if directory else "journal_file_owner")
        if value.st_mode & 0o077:
            raise ControlJournalError("journal_directory_mode" if directory else "journal_file_mode")

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=JOURNAL_BUSY_TIMEOUT_MS / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            # Some hardened Python builds omit extension loading entirely. In
            # that safer configuration the disabling API is absent as well.
            if hasattr(connection, "enable_load_extension"):
                connection.enable_load_extension(False)
            if hasattr(connection, "setlimit"):
                connection.setlimit(_SQLITE_LIMIT_LENGTH, 1_048_576)
                connection.setlimit(_SQLITE_LIMIT_SQL_LENGTH, 100_000)
                connection.setlimit(_SQLITE_LIMIT_COLUMN, 128)
                connection.setlimit(_SQLITE_LIMIT_VARIABLE_NUMBER, 128)
                connection.setlimit(_SQLITE_LIMIT_EXPR_DEPTH, 128)
            connection.execute(f"PRAGMA busy_timeout = {JOURNAL_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA temp_store = MEMORY")
            mode_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            connection.execute("PRAGMA synchronous = FULL")
            if mode_row is None or str(mode_row[0]).lower() != "wal":
                raise ControlJournalError("journal_wal")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise ControlJournalError("journal_foreign_keys")
            if int(connection.execute("PRAGMA trusted_schema").fetchone()[0]) != 0:
                raise ControlJournalError("journal_trusted_schema")
            if int(connection.execute("PRAGMA synchronous").fetchone()[0]) != 2:
                raise ControlJournalError("journal_synchronous")
            return connection
        except ControlJournalError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error, ValueError):
            if connection is not None:
                connection.close()
            raise ControlJournalError("journal_unavailable") from None

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                journal_id = content_digest({"journal_nonce": uuid.uuid4().hex})
                connection.executescript(_SCHEMA_SQL.replace("__ADA_JOURNAL_ID__", journal_id))
            elif version != JOURNAL_SCHEMA_VERSION:
                raise ControlJournalCorrupt("journal_version")
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id != JOURNAL_APPLICATION_ID:
                raise ControlJournalCorrupt("journal_application")
            self._validate_schema(connection)
            self._journal_id(connection)
            quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise ControlJournalCorrupt("journal_integrity")
            receipt_count = connection.execute(
                "SELECT COUNT(*) FROM ada_receipts"
            ).fetchone()
            if receipt_count is None:
                raise ControlJournalCorrupt("receipt_sequence")
            self._receipt_anchor_bootstrap_allowed = int(receipt_count[0]) == 0
        except (ControlJournalError, ControlJournalCorrupt):
            raise
        except sqlite3.Error:
            raise ControlJournalError("journal_unavailable") from None
        finally:
            connection.close()
            self._validate_sidecars()

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        objects = connection.execute(
            "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = {str(row[1]) for row in objects if row[0] == "table"}
        if tables != set(_EXPECTED_COLUMNS):
            raise ControlJournalCorrupt("journal_schema")
        if any(row[0] in {"trigger", "view"} for row in objects):
            raise ControlJournalCorrupt("journal_schema_object")
        for table, expected in _EXPECTED_COLUMNS.items():
            columns = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall())
            if columns != expected:
                raise ControlJournalCorrupt("journal_schema_columns")

    def _validate_sidecars(self) -> None:
        for candidate in (
            self.path,
            Path(str(self.path) + "-wal"),
            Path(str(self.path) + "-shm"),
        ):
            try:
                value = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise ControlJournalError("journal_sidecar") from None
            if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
                raise ControlJournalError("journal_sidecar")
            # SQLite unlinks WAL/SHM sidecars as the final connection closes.
            # On POSIX, a racing lstat can observe that regular inode with zero
            # remaining directory links. It is no longer addressable by this
            # path and is not a hard-link escape. The durable database itself
            # must always retain exactly one link; live sidecars may have zero
            # or one, and any count above one still fails closed.
            allowed_links = {1} if candidate == self.path else {0, 1}
            if value.st_nlink not in allowed_links:
                raise ControlJournalError("journal_sidecar_hardlink")
            self._validate_private_stat(value, directory=False)

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except sqlite3.Error:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise ControlJournalError("journal_unavailable") from None
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
            self._validate_sidecars()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        except sqlite3.Error:
            raise ControlJournalError("journal_unavailable") from None
        finally:
            connection.close()
            self._validate_sidecars()

    @staticmethod
    def _input_time(value: Any) -> int:
        if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
            raise ControlJournalRejected("invalid_time")
        return value

    @staticmethod
    def _input_uuid(value: Any, label: str) -> str:
        try:
            return _require_uuid(value, label)
        except ControlJournalCorrupt:
            raise ControlJournalRejected(label) from None

    @staticmethod
    def _input_reason(value: Any) -> str:
        if type(value) is not str or not _SAFE_ID_RE.fullmatch(value):
            raise ControlJournalRejected("invalid_reason")
        return value

    @staticmethod
    def _input_evidence(value: Any) -> str:
        if type(value) is not str or not _DIGEST_RE.fullmatch(value):
            raise ControlJournalRejected("invalid_evidence")
        return value

    @staticmethod
    def _is_revoked(
        connection: sqlite3.Connection,
        kind: RevocationKind,
        object_id: str,
    ) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM ada_revocations WHERE kind = ? AND object_id = ?",
                (kind.value, object_id),
            ).fetchone()
            is not None
        )

    def revoke(
        self,
        kind: RevocationKind,
        object_id: str,
        *,
        revoked_at_ms: int,
        reason_code: str = "operator_revoked",
    ) -> None:
        if type(kind) is not RevocationKind:
            raise ControlJournalRejected("revocation_kind")
        identifier = self._input_uuid(object_id, "revocation_id")
        timestamp = self._input_time(revoked_at_ms)
        reason = self._input_reason(reason_code)
        with self._immediate() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ada_revocations "
                "(kind, object_id, revoked_at_ms, reason_code) VALUES (?, ?, ?, ?)",
                (kind.value, identifier, timestamp, reason),
            )

    def is_revoked(self, kind: RevocationKind, object_id: str) -> bool:
        if type(kind) is not RevocationKind:
            raise ControlJournalRejected("revocation_kind")
        identifier = self._input_uuid(object_id, "revocation_id")
        with self._reader() as connection:
            return self._is_revoked(connection, kind, identifier)

    @staticmethod
    def _effect_from_connection(
        connection: sqlite3.Connection,
        effect_id: str,
    ) -> ControlEffectRecord:
        row = connection.execute(
            "SELECT * FROM ada_effects WHERE effect_id = ?",
            (effect_id,),
        ).fetchone()
        if row is None:
            raise ControlJournalRejected("effect_missing")
        return ControlEffectRecord.from_row(row)

    @staticmethod
    def _journal_id(connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT singleton, journal_id FROM ada_identity"
        ).fetchall()
        if len(rows) != 1 or rows[0]["singleton"] != 1:
            raise ControlJournalCorrupt("journal_identity")
        return _require_digest(rows[0]["journal_id"], "journal_identity")

    @staticmethod
    def _receipt_from_row(
        row: Mapping[str, Any],
        verifier: ControlVerifier,
        *,
        journal_id: str,
    ) -> ControlReceipt:
        blob = row["receipt_blob"]
        if type(blob) is not bytes:
            raise ControlJournalCorrupt("receipt_blob")
        value = decode_json_payload(blob)
        stored_digest = _require_digest(row["receipt_digest"], "receipt_digest")
        if content_digest(value) != stored_digest:
            raise ControlJournalCorrupt("receipt_digest")
        receipt = ControlReceipt.from_dict(value)
        verify_control_receipt(receipt, verifier)
        if (
            receipt.journal_id != journal_id
            or receipt.effect_id != row["effect_id"]
            or receipt.transition_version != row["transition_version"]
            or receipt.receipt_sequence != row["receipt_sequence"]
            or receipt.previous_receipt_digest != row["previous_receipt_digest"]
        ):
            raise ControlJournalCorrupt("receipt_binding")
        return receipt

    @classmethod
    def _receipt_sequence_from_connection(
        cls,
        connection: sqlite3.Connection,
        verifier: ControlVerifier,
    ) -> tuple[ControlReceipt, ...]:
        journal_id = cls._journal_id(connection)
        rows = connection.execute(
            "SELECT effect_id, transition_version, receipt_sequence, "
            "previous_receipt_digest, receipt_blob, receipt_digest "
            "FROM ada_receipts ORDER BY receipt_sequence"
        ).fetchall()
        expected_sequence = 1
        previous_digest = EMPTY_RECEIPT_HEAD_DIGEST
        results: list[ControlReceipt] = []
        for row in rows:
            receipt = cls._receipt_from_row(
                row,
                verifier,
                journal_id=journal_id,
            )
            if (
                receipt.receipt_sequence != expected_sequence
                or receipt.previous_receipt_digest != previous_digest
            ):
                raise ControlJournalCorrupt("receipt_sequence")
            results.append(receipt)
            previous_digest = content_digest(receipt.to_dict())
            expected_sequence += 1
        return tuple(results)

    @property
    def receipt_anchor_configured(self) -> bool:
        return self._receipt_anchor_store is not None

    @staticmethod
    def _receipt_anchor_digest(value: bytes) -> str:
        return "sha256:" + hashlib.sha256(value).hexdigest()

    def _load_receipt_anchor(
        self,
        journal_id: str,
        verifier: ControlVerifier,
    ) -> tuple[ControlReceipt, bytes] | None:
        store = self._receipt_anchor_store
        if store is None:
            return None
        try:
            blob = store.load(journal_id)
        except Exception:
            raise ControlJournalError("receipt_anchor_unavailable") from None
        if blob is None:
            return None
        if type(blob) is not bytes or not 1 <= len(blob) <= MAX_RECEIPT_ANCHOR_BYTES:
            raise ControlJournalCorrupt("receipt_anchor_value")
        try:
            value = decode_json_payload(blob)
            receipt = ControlReceipt.from_dict(value)
            verify_control_receipt(receipt, verifier)
            if canonical_json_bytes(receipt.to_dict()) != blob:
                raise ControlJournalCorrupt("receipt_anchor_encoding")
        except ControlJournalCorrupt:
            raise
        except Exception:
            raise ControlJournalCorrupt("receipt_anchor_value") from None
        if receipt.journal_id != journal_id:
            raise ControlJournalCorrupt("receipt_anchor_journal")
        return receipt, blob

    def _read_receipt_sequence(
        self,
        verifier: ControlVerifier,
    ) -> tuple[str, tuple[ControlReceipt, ...]]:
        with self._reader() as connection:
            journal_id = self._journal_id(connection)
            sequence = self._receipt_sequence_from_connection(connection, verifier)
        return journal_id, sequence

    def _synchronize_receipt_anchor_sequence(
        self,
        sequence: tuple[ControlReceipt, ...],
        *,
        journal_id: str,
        verifier: ControlVerifier,
    ) -> None:
        store = self._receipt_anchor_store
        if store is None:
            return
        for _attempt in range(MAX_RECEIPT_ANCHOR_RETRIES):
            loaded = self._load_receipt_anchor(journal_id, verifier)
            if loaded is None:
                if not sequence:
                    return
                if not self._receipt_anchor_bootstrap_allowed:
                    raise ControlJournalCorrupt("receipt_anchor_missing")
                candidate = sequence[0]
                expected_digest = None
            else:
                anchored, blob = loaded
                self._receipt_anchor_bootstrap_allowed = False
                index = anchored.receipt_sequence - 1
                if index >= len(sequence):
                    refreshed_journal_id, refreshed = self._read_receipt_sequence(verifier)
                    if refreshed_journal_id != journal_id:
                        raise ControlJournalCorrupt("receipt_anchor_journal")
                    if refreshed == sequence:
                        raise ControlJournalCorrupt("receipt_head_rollback")
                    sequence = refreshed
                    continue
                if index < 0 or sequence[index] != anchored:
                    raise ControlJournalCorrupt("receipt_head_rollback")
                if anchored == sequence[-1]:
                    return
                candidate = sequence[-1]
                expected_digest = self._receipt_anchor_digest(blob)
            value = canonical_json_bytes(candidate.to_dict())
            if len(value) > MAX_RECEIPT_ANCHOR_BYTES:
                raise ControlJournalCorrupt("receipt_anchor_value")
            try:
                changed = store.compare_and_set(
                    journal_id,
                    expected_digest=expected_digest,
                    value=value,
                )
            except Exception:
                raise ControlJournalError("receipt_anchor_unavailable") from None
            if type(changed) is not bool:
                raise ControlJournalError("receipt_anchor_unavailable")
            if changed:
                self._receipt_anchor_bootstrap_allowed = False
            else:
                refreshed_journal_id, sequence = self._read_receipt_sequence(verifier)
                if refreshed_journal_id != journal_id:
                    raise ControlJournalCorrupt("receipt_anchor_journal")
        raise ControlJournalError("receipt_anchor_race")

    def synchronize_receipt_anchor(self, verifier: ControlVerifier) -> ControlReceipt | None:
        if type(verifier) is not ControlVerifier:
            raise ControlJournalRejected("receipt_verifier")
        journal_id, sequence = self._read_receipt_sequence(verifier)
        self._synchronize_receipt_anchor_sequence(
            sequence,
            journal_id=journal_id,
            verifier=verifier,
        )
        return None if not sequence else sequence[-1]

    def claim(
        self,
        envelope: ControlEnvelope,
        route: ControlRoute,
        *,
        verifier: ControlVerifier,
        policy: ControlPolicy,
        live_snapshot: SnapshotRef,
        now_ms: int,
    ) -> ControlEffectRecord:
        if type(envelope) is not ControlEnvelope:
            raise ControlJournalRejected("envelope_type")
        if type(route) is not ControlRoute:
            raise ControlJournalRejected("route_type")
        if type(verifier) is not ControlVerifier or type(policy) is not ControlPolicy:
            raise ControlJournalRejected("authority_type")
        if type(live_snapshot) is not SnapshotRef:
            raise ControlJournalRejected("snapshot_type")
        envelope = ControlEnvelope.from_dict(envelope.to_dict())
        timestamp = self._input_time(now_ms)
        request = envelope.request
        grant = envelope.grant
        permit = envelope.permit
        selected_route = verify_envelope_authority(
            envelope,
            verifier,
            policy,
            now_ms=timestamp,
            live_routes=(route,),
            live_snapshot=live_snapshot,
        )
        if selected_route is not route:
            raise ControlJournalRejected("route_selection")
        if route not in request.requested_routes or route not in grant.routes or route not in permit.routes:
            raise ControlJournalRejected("route_scope")
        if timestamp >= grant.expires_at_ms:
            raise ControlJournalRejected("grant_expired")
        if timestamp < permit.issued_at_ms or timestamp >= permit.expires_at_ms:
            raise ControlJournalRejected("permit_expired")
        grant_binding = content_digest(grant.to_dict())
        permit_binding = content_digest(permit.to_dict())
        effect_id = str(uuid.uuid4())

        with self._immediate() as connection:
            if self._is_revoked(connection, RevocationKind.GRANT, grant.grant_id):
                raise ControlJournalRejected("grant_revoked")
            if self._is_revoked(connection, RevocationKind.PERMIT, permit.permit_id):
                raise ControlJournalRejected("permit_revoked")

            grant_row = connection.execute(
                "SELECT binding_digest, maximum_action_count, used_count, expires_at_ms "
                "FROM ada_grants WHERE grant_id = ?",
                (grant.grant_id,),
            ).fetchone()
            if grant_row is None:
                connection.execute(
                    "INSERT INTO ada_grants "
                    "(grant_id, binding_digest, maximum_action_count, used_count, expires_at_ms) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (
                        grant.grant_id,
                        grant_binding,
                        grant.maximum_action_count,
                        grant.expires_at_ms,
                    ),
                )
                used_count = 0
            else:
                stored_binding = _require_digest(grant_row["binding_digest"], "grant_binding")
                stored_maximum = _require_int(
                    grant_row["maximum_action_count"],
                    "grant_action_count",
                    minimum=1,
                    maximum=MAX_ACTION_COUNT,
                )
                used_count = _require_int(
                    grant_row["used_count"],
                    "grant_used_count",
                    maximum=stored_maximum,
                )
                stored_expiry = _require_int(
                    grant_row["expires_at_ms"],
                    "grant_expiry",
                    minimum=1,
                )
                if (
                    stored_binding != grant_binding
                    or stored_maximum != grant.maximum_action_count
                    or stored_expiry != grant.expires_at_ms
                ):
                    raise ControlJournalRejected("grant_binding")
            if used_count >= grant.maximum_action_count:
                raise ControlJournalRejected("grant_exhausted")

            if (
                connection.execute(
                    "SELECT 1 FROM ada_permits WHERE permit_id = ?",
                    (permit.permit_id,),
                ).fetchone()
                is not None
            ):
                raise ControlJournalRejected("permit_replayed")

            session_row = connection.execute(
                "SELECT last_sequence, updated_at_ms FROM ada_sessions WHERE session_id = ?",
                (request.session_id,),
            ).fetchone()
            if session_row is None:
                if request.sequence != 1:
                    raise ControlJournalRejected("session_sequence_start")
                connection.execute(
                    "INSERT INTO ada_sessions (session_id, last_sequence, updated_at_ms) VALUES (?, ?, ?)",
                    (request.session_id, request.sequence, timestamp),
                )
            else:
                last_sequence = _require_int(session_row["last_sequence"], "session_sequence", minimum=1)
                last_updated = _require_int(session_row["updated_at_ms"], "session_updated")
                if request.sequence != last_sequence + 1:
                    raise ControlJournalRejected("session_sequence")
                if timestamp < last_updated:
                    raise ControlJournalRejected("clock_regression")
                connection.execute(
                    "UPDATE ada_sessions SET last_sequence = ?, updated_at_ms = ? "
                    "WHERE session_id = ? AND last_sequence = ?",
                    (
                        request.sequence,
                        timestamp,
                        request.session_id,
                        last_sequence,
                    ),
                )

            target_row = connection.execute(
                "SELECT target_kind, target_epoch, target_revision, fencing_token, updated_at_ms "
                "FROM ada_targets WHERE target_id = ?",
                (request.target.target_id,),
            ).fetchone()
            if target_row is None:
                connection.execute(
                    "INSERT INTO ada_targets "
                    "(target_id, target_kind, target_epoch, target_revision, fencing_token, updated_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        request.target.target_id,
                        request.target.kind.value,
                        request.target.epoch,
                        request.target.revision,
                        request.target.fencing_token,
                        timestamp,
                    ),
                )
            else:
                stored_kind = _require_enum(TargetKind, target_row["target_kind"], "target_kind")
                stored_epoch = _require_int(target_row["target_epoch"], "target_epoch", minimum=1)
                stored_revision = _require_revision(target_row["target_revision"], "target_revision")
                stored_fence = _require_int(target_row["fencing_token"], "target_fence", minimum=1)
                target_updated = _require_int(target_row["updated_at_ms"], "target_updated")
                if stored_kind is not request.target.kind:
                    raise ControlJournalRejected("target_kind_changed")
                if request.target.epoch < stored_epoch:
                    raise ControlJournalRejected("target_epoch_stale")
                if request.target.fencing_token < stored_fence:
                    raise ControlJournalRejected("target_fence_stale")
                if request.target.epoch == stored_epoch:
                    if request.target.revision != stored_revision:
                        raise ControlJournalRejected("target_revision_changed")
                elif request.target.fencing_token <= stored_fence:
                    raise ControlJournalRejected("target_fence_not_advanced")
                if timestamp < target_updated:
                    raise ControlJournalRejected("clock_regression")
                connection.execute(
                    "UPDATE ada_targets SET target_epoch = ?, target_revision = ?, "
                    "fencing_token = ?, updated_at_ms = ? WHERE target_id = ?",
                    (
                        request.target.epoch,
                        request.target.revision,
                        request.target.fencing_token,
                        timestamp,
                        request.target.target_id,
                    ),
                )

            connection.execute(
                "INSERT INTO ada_permits "
                "(permit_id, grant_id, binding_digest, request_digest, consumed_at_ms, expires_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    permit.permit_id,
                    grant.grant_id,
                    permit_binding,
                    request.digest,
                    timestamp,
                    permit.expires_at_ms,
                ),
            )
            updated = connection.execute(
                "UPDATE ada_grants SET used_count = used_count + 1 "
                "WHERE grant_id = ? AND used_count < maximum_action_count",
                (grant.grant_id,),
            )
            if updated.rowcount != 1:
                raise ControlJournalRejected("grant_exhausted")
            connection.execute(
                "INSERT INTO ada_effects "
                "(effect_id, permit_id, grant_id, session_id, request_id, request_digest, "
                "target_kind, target_id, target_epoch, target_revision, fencing_token, "
                "snapshot_id, sequence, operation, route, authority_key_id, state, "
                "reason_code, evidence_digest, transition_version, prepared_at_ms, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    effect_id,
                    permit.permit_id,
                    grant.grant_id,
                    request.session_id,
                    request.request_id,
                    request.digest,
                    request.target.kind.value,
                    request.target.target_id,
                    request.target.epoch,
                    request.target.revision,
                    request.target.fencing_token,
                    request.snapshot.snapshot_id,
                    request.sequence,
                    request.operation.value,
                    route.value,
                    permit.authority_key_id,
                    ControlEffectState.PREPARED.value,
                    "none",
                    EMPTY_EVIDENCE_DIGEST,
                    timestamp,
                    timestamp,
                ),
            )
            return self._effect_from_connection(connection, effect_id)

    def transition(
        self,
        effect_id: str,
        new_state: ControlEffectState,
        *,
        now_ms: int,
        reason_code: str,
        evidence_digest: str = EMPTY_EVIDENCE_DIGEST,
    ) -> ControlEffectRecord:
        identifier = self._input_uuid(effect_id, "effect_id")
        if type(new_state) is not ControlEffectState:
            raise ControlJournalRejected("effect_state")
        timestamp = self._input_time(now_ms)
        reason = self._input_reason(reason_code)
        evidence = self._input_evidence(evidence_digest)
        if (
            new_state
            in {
                ControlEffectState.STARTED,
                ControlEffectState.APPLIED,
            }
            and reason != "none"
        ):
            raise ControlJournalRejected("transition_reason")
        if (
            new_state
            in {
                ControlEffectState.VERIFIED,
                ControlEffectState.FAILED,
                ControlEffectState.UNKNOWN,
            }
            and reason == "none"
        ):
            raise ControlJournalRejected("transition_reason")

        with self._immediate() as connection:
            current = self._effect_from_connection(connection, identifier)
            if new_state not in _ALLOWED_TRANSITIONS[current.state]:
                raise ControlJournalRejected("effect_transition")
            if timestamp < current.updated_at_ms:
                raise ControlJournalRejected("clock_regression")
            if new_state is ControlEffectState.STARTED:
                if self._is_revoked(connection, RevocationKind.GRANT, current.grant_id):
                    raise ControlJournalRejected("grant_revoked")
                if self._is_revoked(connection, RevocationKind.PERMIT, current.permit_id):
                    raise ControlJournalRejected("permit_revoked")
                permit_row = connection.execute(
                    "SELECT expires_at_ms FROM ada_permits WHERE permit_id = ?",
                    (current.permit_id,),
                ).fetchone()
                if permit_row is None:
                    raise ControlJournalCorrupt("permit_missing")
                permit_expiry = _require_int(permit_row["expires_at_ms"], "permit_expiry", minimum=1)
                if timestamp >= permit_expiry:
                    raise ControlJournalRejected("permit_expired")
                target_row = connection.execute(
                    "SELECT target_kind, target_epoch, target_revision, fencing_token "
                    "FROM ada_targets WHERE target_id = ?",
                    (current.target_id,),
                ).fetchone()
                if target_row is None:
                    raise ControlJournalCorrupt("target_missing")
                if (
                    target_row["target_kind"] != current.target_kind.value
                    or target_row["target_epoch"] != current.target_epoch
                    or target_row["target_revision"] != current.target_revision
                    or target_row["fencing_token"] != current.fencing_token
                ):
                    raise ControlJournalRejected("target_fence_stale")
            changed = connection.execute(
                "UPDATE ada_effects SET state = ?, reason_code = ?, evidence_digest = ?, "
                "transition_version = transition_version + 1, updated_at_ms = ? "
                "WHERE effect_id = ? AND state = ? AND transition_version = ?",
                (
                    new_state.value,
                    reason,
                    evidence,
                    timestamp,
                    identifier,
                    current.state.value,
                    current.transition_version,
                ),
            )
            if changed.rowcount != 1:
                raise ControlJournalRejected("effect_race")
            return self._effect_from_connection(connection, identifier)

    def get(self, effect_id: str) -> ControlEffectRecord:
        identifier = self._input_uuid(effect_id, "effect_id")
        with self._reader() as connection:
            return self._effect_from_connection(connection, identifier)

    def by_permit(self, permit_id: str) -> ControlEffectRecord | None:
        identifier = self._input_uuid(permit_id, "permit_id")
        with self._reader() as connection:
            row = connection.execute("SELECT * FROM ada_effects WHERE permit_id = ?", (identifier,)).fetchone()
            return None if row is None else ControlEffectRecord.from_row(row)

    def recovery_candidates(self) -> tuple[ControlEffectRecord, ...]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM ada_effects WHERE state IN "
                "('prepared', 'started', 'applied', 'unknown') "
                "ORDER BY prepared_at_ms, effect_id"
            ).fetchall()
            return tuple(ControlEffectRecord.from_row(row) for row in rows)

    def grant_usage(self, grant_id: str) -> tuple[int, int]:
        identifier = self._input_uuid(grant_id, "grant_id")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT used_count, maximum_action_count FROM ada_grants WHERE grant_id = ?",
                (identifier,),
            ).fetchone()
            if row is None:
                return (0, 0)
            maximum = _require_int(
                row["maximum_action_count"],
                "grant_action_count",
                minimum=1,
                maximum=MAX_ACTION_COUNT,
            )
            used = _require_int(row["used_count"], "grant_used_count", maximum=maximum)
            return (used, maximum)

    def finalize_receipt(
        self,
        effect_id: str,
        signer: ControlSigner,
        *,
        completed_at_ms: int,
    ) -> ControlReceipt:
        receipt = self._finalize_receipt_unanchored(
            effect_id,
            signer,
            completed_at_ms=completed_at_ms,
        )
        if self._receipt_anchor_store is not None:
            self.synchronize_receipt_anchor(signer.verifier)
        return receipt

    def _finalize_receipt_unanchored(
        self,
        effect_id: str,
        signer: ControlSigner,
        *,
        completed_at_ms: int,
    ) -> ControlReceipt:
        identifier = self._input_uuid(effect_id, "effect_id")
        timestamp = self._input_time(completed_at_ms)
        if type(signer) is not ControlSigner:
            raise ControlJournalRejected("receipt_signer")
        with self._immediate() as connection:
            effect = self._effect_from_connection(connection, identifier)
            if not effect.is_receiptable:
                raise ControlJournalRejected("effect_not_receiptable")
            if signer.key_id != effect.authority_key_id:
                raise AuthorityRejected("receipt_key")
            journal_id = self._journal_id(connection)
            existing = connection.execute(
                "SELECT effect_id, transition_version, receipt_sequence, "
                "previous_receipt_digest, receipt_blob, receipt_digest "
                "FROM ada_receipts WHERE effect_id = ? AND transition_version = ?",
                (identifier, effect.transition_version),
            ).fetchone()
            if existing is not None:
                receipt = self._receipt_from_row(
                    existing,
                    signer.verifier,
                    journal_id=journal_id,
                )
                if (
                    receipt.effect_id != effect.effect_id
                    or receipt.transition_version != effect.transition_version
                    or receipt.state is not effect.state
                ):
                    raise ControlJournalCorrupt("receipt_binding")
                return receipt

            sequence_row = connection.execute(
                "SELECT COUNT(*) AS receipt_count, "
                "MIN(receipt_sequence) AS minimum_sequence, "
                "MAX(receipt_sequence) AS maximum_sequence FROM ada_receipts"
            ).fetchone()
            if sequence_row is None:
                raise ControlJournalCorrupt("receipt_sequence")
            receipt_count = _require_int(
                sequence_row["receipt_count"],
                "receipt_count",
            )
            if receipt_count == 0:
                receipt_sequence = 1
                previous_receipt_digest = EMPTY_RECEIPT_HEAD_DIGEST
            else:
                minimum_sequence = _require_int(
                    sequence_row["minimum_sequence"],
                    "receipt_sequence",
                    minimum=1,
                )
                maximum_sequence = _require_int(
                    sequence_row["maximum_sequence"],
                    "receipt_sequence",
                    minimum=1,
                )
                if minimum_sequence != 1 or maximum_sequence != receipt_count:
                    raise ControlJournalCorrupt("receipt_sequence")
                latest_row = connection.execute(
                    "SELECT effect_id, transition_version, receipt_sequence, "
                    "previous_receipt_digest, receipt_blob, receipt_digest "
                    "FROM ada_receipts WHERE receipt_sequence = ?",
                    (maximum_sequence,),
                ).fetchone()
                if latest_row is None:
                    raise ControlJournalCorrupt("receipt_sequence")
                latest = self._receipt_from_row(
                    latest_row,
                    signer.verifier,
                    journal_id=journal_id,
                )
                receipt_sequence = latest.receipt_sequence + 1
                if receipt_sequence > MAX_SAFE_INTEGER:
                    raise ControlJournalRejected("receipt_sequence_exhausted")
                previous_receipt_digest = content_digest(latest.to_dict())

            unsigned = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "receipt_id": str(
                    uuid.uuid5(
                        _RECEIPT_NAMESPACE,
                        f"{effect.effect_id}:{effect.transition_version}",
                    )
                ),
                "journal_id": journal_id,
                "receipt_sequence": receipt_sequence,
                "previous_receipt_digest": previous_receipt_digest,
                "effect_id": effect.effect_id,
                "grant_id": effect.grant_id,
                "permit_id": effect.permit_id,
                "request_digest": effect.request_digest,
                "target_id": effect.target_id,
                "target_epoch": effect.target_epoch,
                "target_revision": effect.target_revision,
                "fencing_token": effect.fencing_token,
                "sequence": effect.sequence,
                "operation": effect.operation.value,
                "route": effect.route.value,
                "state": effect.state.value,
                "reason_code": effect.reason_code,
                "evidence_digest": effect.evidence_digest,
                "transition_version": effect.transition_version,
                "completed_at_ms": max(timestamp, effect.updated_at_ms),
                "authority_key_id": signer.key_id,
            }
            receipt = ControlReceipt.from_dict(
                {
                    **unsigned,
                    "signature": signer.sign("control_receipt", unsigned),
                }
            )
            verify_control_receipt(receipt, signer.verifier)
            blob = canonical_json_bytes(receipt.to_dict())
            digest = content_digest(receipt.to_dict())
            connection.execute(
                "INSERT INTO ada_receipts "
                "(effect_id, transition_version, receipt_sequence, "
                "previous_receipt_digest, receipt_blob, receipt_digest) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    effect.transition_version,
                    receipt_sequence,
                    previous_receipt_digest,
                    blob,
                    digest,
                ),
            )
            return receipt

    def receipts(
        self,
        effect_id: str,
        verifier: ControlVerifier,
    ) -> tuple[ControlReceipt, ...]:
        identifier = self._input_uuid(effect_id, "effect_id")
        if type(verifier) is not ControlVerifier:
            raise ControlJournalRejected("receipt_verifier")
        with self._reader() as connection:
            return tuple(
                receipt
                for receipt in self._receipt_sequence_from_connection(connection, verifier)
                if receipt.effect_id == identifier
            )

    def receipt_sequence(
        self,
        verifier: ControlVerifier,
        *,
        expected_head: ControlReceipt | None = None,
    ) -> tuple[ControlReceipt, ...]:
        """Verify the complete signed chain and optionally detect tail rollback.

        The caller must retain ``expected_head`` outside this SQLite file to
        detect replacement or deletion of the newest local rows. The journal is
        tamper-evident; it is not an immutable or independently anchored log.
        """

        if type(verifier) is not ControlVerifier:
            raise ControlJournalRejected("receipt_verifier")
        if expected_head is not None:
            if type(expected_head) is not ControlReceipt:
                raise ControlJournalRejected("receipt_head")
            expected_head = ControlReceipt.from_dict(expected_head.to_dict())
            verify_control_receipt(expected_head, verifier)
        with self._reader() as connection:
            results = self._receipt_sequence_from_connection(connection, verifier)
            journal_id = self._journal_id(connection)
        self._synchronize_receipt_anchor_sequence(
            results,
            journal_id=journal_id,
            verifier=verifier,
        )
        if expected_head is not None:
            if not results or results[-1] != expected_head:
                raise ControlJournalCorrupt("receipt_head_rollback")
        return results

    def receipt_head(self, verifier: ControlVerifier) -> ControlReceipt | None:
        sequence = self.receipt_sequence(verifier)
        return None if not sequence else sequence[-1]

    def checkpoint(self) -> None:
        connection = self._connect()
        try:
            result = connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            if result is None or int(result[0]) != 0:
                raise ControlJournalError("journal_checkpoint")
        except sqlite3.Error:
            raise ControlJournalError("journal_unavailable") from None
        finally:
            connection.close()
            self._validate_sidecars()


__all__ = [
    "CONTROL_RECEIPT_SCHEMA",
    "EMPTY_EVIDENCE_DIGEST",
    "EMPTY_RECEIPT_HEAD_DIGEST",
    "JOURNAL_SCHEMA_VERSION",
    "MAX_RECEIPT_ANCHOR_BYTES",
    "ControlEffectRecord",
    "ControlEffectState",
    "ControlJournal",
    "ControlJournalCorrupt",
    "ControlJournalError",
    "ControlJournalRejected",
    "ControlReceipt",
    "ReceiptHeadAnchorStore",
    "RevocationKind",
    "verify_control_receipt",
]
