"""Signed finite ownership registry for Algo CLI credential labels.

The OS keyring abstraction can retrieve one exact item but cannot enumerate a
service reliably across supported platforms.  Ada therefore records the finite
set of labels the runtime is allowed to own.  The record contains no credential
values, is authenticated by the control authority, and advances monotonically
before a dynamic credential is created.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import re
from typing import Any, Iterable, Mapping

from .david_control_kernel import (
    MAX_SAFE_INTEGER,
    AuthorityRejected,
    ControlSigner,
    ControlVerifier,
    FrameRejected,
    canonical_json_bytes,
    decode_json_payload,
)


ADA_CREDENTIAL_REGISTRY_SCHEMA_VERSION = 1
ADA_CREDENTIAL_REGISTRY_SIGNATURE_KIND = "credential_label_registry"
ADA_CREDENTIAL_REGISTRY_LABEL = "ada-credential-labels-v1"
ADA_NATIVE_CREDENTIAL_ENUMERATION_KIND = "native_credential_enumeration"
ADA_NATIVE_CREDENTIAL_ENUMERATION_SCHEMA_VERSION = 1
MAX_CREDENTIAL_REGISTRY_BYTES = 64 * 1024
MAX_CREDENTIAL_REGISTRY_LABELS = 256
MAX_NATIVE_CREDENTIAL_ENUMERATION_AGE_MS = 60_000

_LABEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_KEY_ID_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
_CODE_IDENTIFIER_RE = re.compile(r"^com\.algo-cli\.austin\.[a-z0-9.-]{1,64}$")
_MIGRATION_KINDS = frozenset({"fresh_namespace", "native_enumeration"})


class AdaCredentialRegistryError(RuntimeError):
    """A content-free registry validation or transition failure."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected):
            selected = "credential_registry_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> None:
    raise AdaCredentialRegistryError(reason_code)


def _exact_dict(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        _reject("credential_registry_schema")
    if not all(type(key) is str for key in value):
        _reject("credential_registry_schema")
    return value


def _bounded_int(value: Any, *, minimum: int) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        _reject("credential_registry_integer")
    return value


def _pattern(value: Any, pattern: re.Pattern[str], reason_code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _reject(reason_code)
    return value


def _normalize_labels(labels: Iterable[str]) -> tuple[str, ...]:
    try:
        selected = tuple(labels)
    except TypeError:
        _reject("credential_registry_labels")
    if not 1 <= len(selected) <= MAX_CREDENTIAL_REGISTRY_LABELS:
        _reject("credential_registry_labels")
    if any(type(label) is not str or _LABEL_RE.fullmatch(label) is None for label in selected):
        _reject("credential_registry_label")
    normalized = tuple(sorted(set(selected)))
    if normalized != selected:
        _reject("credential_registry_label_order")
    return normalized


@dataclass(frozen=True, slots=True)
class AdaCredentialFingerprint:
    """One content-free item observed by the native Keychain enumerator."""

    label: str
    value_digest: str

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "value_digest": self.value_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdaCredentialFingerprint":
        row = _exact_dict(value, frozenset({"label", "value_digest"}))
        return cls(
            label=_pattern(row["label"], _LABEL_RE, "credential_enumeration_label"),
            value_digest=_pattern(
                row["value_digest"],
                _DIGEST_RE,
                "credential_enumeration_digest",
            ),
        )


@dataclass(frozen=True, slots=True)
class AdaNativeCredentialEnumeration:
    """Nonce-bound output from the separately signed Austin Keychain helper.

    The payload contains labels and SHA-256 fingerprints only. Its trust comes
    from executing the exact Developer ID helper between two identity checks;
    the signed Ada registry then commits to this payload's digest.
    """

    service: str
    nonce: str
    generated_at_ms: int
    code_identifier: str
    team_id: str
    designated_requirement_digest: str
    registry_present: bool
    unexpected_label_count: int
    records: tuple[AdaCredentialFingerprint, ...]
    schema_version: int = ADA_NATIVE_CREDENTIAL_ENUMERATION_SCHEMA_VERSION
    kind: str = ADA_NATIVE_CREDENTIAL_ENUMERATION_KIND
    query_class: str = "generic_password"
    match_limit: str = "all"
    synchronizable: str = "any"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_identifier": self.code_identifier,
            "designated_requirement_digest": self.designated_requirement_digest,
            "generated_at_ms": self.generated_at_ms,
            "kind": self.kind,
            "match_limit": self.match_limit,
            "nonce": self.nonce,
            "query_class": self.query_class,
            "records": [record.to_dict() for record in self.records],
            "registry_present": self.registry_present,
            "schema_version": self.schema_version,
            "service": self.service,
            "synchronizable": self.synchronizable,
            "team_id": self.team_id,
            "unexpected_label_count": self.unexpected_label_count,
        }

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_dict())
        except FrameRejected:
            _reject("credential_enumeration_encoding")
        if len(payload) > MAX_CREDENTIAL_REGISTRY_BYTES:
            _reject("credential_enumeration_size")
        return payload

    def verify_context(
        self,
        *,
        expected_service: str,
        expected_nonce: str,
        expected_code_identifier: str,
        expected_team_id: str,
        expected_designated_requirement_digest: str,
        now_ms: int,
    ) -> None:
        selected_now = _bounded_int(now_ms, minimum=0)
        if self.service != expected_service:
            _reject("credential_enumeration_service")
        if not hmac.compare_digest(self.nonce, expected_nonce):
            _reject("credential_enumeration_nonce")
        if self.code_identifier != expected_code_identifier:
            _reject("credential_enumeration_identity")
        if self.team_id != expected_team_id:
            _reject("credential_enumeration_team")
        if not hmac.compare_digest(
            self.designated_requirement_digest,
            expected_designated_requirement_digest,
        ):
            _reject("credential_enumeration_requirement")
        if (
            self.generated_at_ms > selected_now + 2_000
            or selected_now - self.generated_at_ms
            > MAX_NATIVE_CREDENTIAL_ENUMERATION_AGE_MS
        ):
            _reject("credential_enumeration_freshness")
        if self.registry_present or self.unexpected_label_count != 0:
            _reject("credential_enumeration_scope")

    @classmethod
    def from_bytes(cls, payload: bytes) -> "AdaNativeCredentialEnumeration":
        if type(payload) is not bytes or not 1 <= len(payload) <= MAX_CREDENTIAL_REGISTRY_BYTES:
            _reject("credential_enumeration_size")
        try:
            value = decode_json_payload(payload)
        except FrameRejected:
            _reject("credential_enumeration_encoding")
        parsed = cls.from_dict(value)
        if not hmac.compare_digest(parsed.to_bytes(), payload):
            _reject("credential_enumeration_noncanonical")
        return parsed

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdaNativeCredentialEnumeration":
        row = _exact_dict(
            value,
            frozenset(
                {
                    "code_identifier",
                    "designated_requirement_digest",
                    "generated_at_ms",
                    "kind",
                    "match_limit",
                    "nonce",
                    "query_class",
                    "records",
                    "registry_present",
                    "schema_version",
                    "service",
                    "synchronizable",
                    "team_id",
                    "unexpected_label_count",
                }
            ),
        )
        if row["schema_version"] != ADA_NATIVE_CREDENTIAL_ENUMERATION_SCHEMA_VERSION:
            _reject("credential_enumeration_version")
        if row["kind"] != ADA_NATIVE_CREDENTIAL_ENUMERATION_KIND:
            _reject("credential_enumeration_kind")
        if (
            row["query_class"] != "generic_password"
            or row["match_limit"] != "all"
            or row["synchronizable"] != "any"
        ):
            _reject("credential_enumeration_query")
        if type(row["registry_present"]) is not bool:
            _reject("credential_enumeration_registry")
        if row["unexpected_label_count"] != 0:
            _reject("credential_enumeration_scope")
        raw_records = row["records"]
        if type(raw_records) is not list or len(raw_records) > MAX_CREDENTIAL_REGISTRY_LABELS:
            _reject("credential_enumeration_records")
        records = tuple(AdaCredentialFingerprint.from_dict(item) for item in raw_records)
        if tuple(sorted(records, key=lambda item: item.label)) != records or len(
            {record.label for record in records}
        ) != len(records):
            _reject("credential_enumeration_order")
        return cls(
            service=_pattern(row["service"], _LABEL_RE, "credential_enumeration_service"),
            nonce=_pattern(row["nonce"], _NONCE_RE, "credential_enumeration_nonce"),
            generated_at_ms=_bounded_int(row["generated_at_ms"], minimum=0),
            code_identifier=_pattern(
                row["code_identifier"],
                _CODE_IDENTIFIER_RE,
                "credential_enumeration_identity",
            ),
            team_id=_pattern(row["team_id"], _TEAM_ID_RE, "credential_enumeration_team"),
            designated_requirement_digest=_pattern(
                row["designated_requirement_digest"],
                _DIGEST_RE,
                "credential_enumeration_requirement",
            ),
            registry_present=row["registry_present"],
            unexpected_label_count=row["unexpected_label_count"],
            records=records,
        )


@dataclass(frozen=True, slots=True)
class AdaCredentialRegistry:
    revision: int
    service: str
    labels: tuple[str, ...]
    migration_kind: str
    migration_evidence_digest: str
    created_at_ms: int
    updated_at_ms: int
    authority_key_id: str
    signature: str
    schema_version: int = ADA_CREDENTIAL_REGISTRY_SCHEMA_VERSION

    @property
    def unsigned(self) -> dict[str, Any]:
        return {
            "authority_key_id": self.authority_key_id,
            "created_at_ms": self.created_at_ms,
            "labels": list(self.labels),
            "migration_evidence_digest": self.migration_evidence_digest,
            "migration_kind": self.migration_kind,
            "revision": self.revision,
            "schema_version": self.schema_version,
            "service": self.service,
            "updated_at_ms": self.updated_at_ms,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned, "signature": self.signature}

    def to_bytes(self) -> bytes:
        try:
            payload = canonical_json_bytes(self.to_dict())
        except FrameRejected:
            _reject("credential_registry_encoding")
        if len(payload) > MAX_CREDENTIAL_REGISTRY_BYTES:
            _reject("credential_registry_size")
        return payload

    def verify(self, verifier: ControlVerifier) -> None:
        if type(verifier) is not ControlVerifier or self.authority_key_id != verifier.key_id:
            _reject("credential_registry_authority")
        try:
            verifier.verify(
                ADA_CREDENTIAL_REGISTRY_SIGNATURE_KIND,
                self.unsigned,
                self.signature,
            )
        except AuthorityRejected:
            _reject("credential_registry_signature")

    def advance(
        self,
        *,
        label: str,
        updated_at_ms: int,
        signer: ControlSigner,
    ) -> "AdaCredentialRegistry":
        self.verify(signer.verifier)
        selected_label = _pattern(label, _LABEL_RE, "credential_registry_label")
        selected_time = _bounded_int(updated_at_ms, minimum=self.updated_at_ms)
        if selected_label in self.labels:
            return self
        labels = tuple(sorted((*self.labels, selected_label)))
        if len(labels) > MAX_CREDENTIAL_REGISTRY_LABELS:
            _reject("credential_registry_labels")
        return self.create(
            revision=self.revision + 1,
            service=self.service,
            labels=labels,
            migration_kind=self.migration_kind,
            migration_evidence_digest=self.migration_evidence_digest,
            created_at_ms=self.created_at_ms,
            updated_at_ms=selected_time,
            signer=signer,
        )

    @classmethod
    def create(
        cls,
        *,
        revision: int,
        service: str,
        labels: Iterable[str],
        migration_kind: str,
        migration_evidence_digest: str,
        created_at_ms: int,
        updated_at_ms: int,
        signer: ControlSigner,
    ) -> "AdaCredentialRegistry":
        if type(signer) is not ControlSigner:
            _reject("credential_registry_signer")
        selected_revision = _bounded_int(revision, minimum=1)
        selected_service = _pattern(service, _LABEL_RE, "credential_registry_service")
        selected_labels = _normalize_labels(labels)
        if type(migration_kind) is not str or migration_kind not in _MIGRATION_KINDS:
            _reject("credential_registry_migration")
        selected_evidence = _pattern(
            migration_evidence_digest,
            _DIGEST_RE,
            "credential_registry_evidence",
        )
        selected_created = _bounded_int(created_at_ms, minimum=0)
        selected_updated = _bounded_int(updated_at_ms, minimum=selected_created)
        unsigned = {
            "authority_key_id": signer.key_id,
            "created_at_ms": selected_created,
            "labels": list(selected_labels),
            "migration_evidence_digest": selected_evidence,
            "migration_kind": migration_kind,
            "revision": selected_revision,
            "schema_version": ADA_CREDENTIAL_REGISTRY_SCHEMA_VERSION,
            "service": selected_service,
            "updated_at_ms": selected_updated,
        }
        signature = signer.sign(ADA_CREDENTIAL_REGISTRY_SIGNATURE_KIND, unsigned)
        return cls.from_dict({**unsigned, "signature": signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> "AdaCredentialRegistry":
        if type(payload) is not bytes or not 1 <= len(payload) <= MAX_CREDENTIAL_REGISTRY_BYTES:
            _reject("credential_registry_size")
        try:
            value = decode_json_payload(payload)
        except FrameRejected:
            _reject("credential_registry_encoding")
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AdaCredentialRegistry":
        row = _exact_dict(
            value,
            frozenset(
                {
                    "authority_key_id",
                    "created_at_ms",
                    "labels",
                    "migration_evidence_digest",
                    "migration_kind",
                    "revision",
                    "schema_version",
                    "service",
                    "signature",
                    "updated_at_ms",
                }
            ),
        )
        if row["schema_version"] != ADA_CREDENTIAL_REGISTRY_SCHEMA_VERSION:
            _reject("credential_registry_version")
        raw_labels = row["labels"]
        if type(raw_labels) is not list:
            _reject("credential_registry_labels")
        created = _bounded_int(row["created_at_ms"], minimum=0)
        migration_kind = row["migration_kind"]
        if type(migration_kind) is not str or migration_kind not in _MIGRATION_KINDS:
            _reject("credential_registry_migration")
        return cls(
            revision=_bounded_int(row["revision"], minimum=1),
            service=_pattern(row["service"], _LABEL_RE, "credential_registry_service"),
            labels=_normalize_labels(raw_labels),
            migration_kind=migration_kind,
            migration_evidence_digest=_pattern(
                row["migration_evidence_digest"],
                _DIGEST_RE,
                "credential_registry_evidence",
            ),
            created_at_ms=created,
            updated_at_ms=_bounded_int(row["updated_at_ms"], minimum=created),
            authority_key_id=_pattern(
                row["authority_key_id"], _KEY_ID_RE, "credential_registry_authority"
            ),
            signature=_pattern(
                row["signature"], _SIGNATURE_RE, "credential_registry_signature"
            ),
        )


__all__ = [
    "ADA_CREDENTIAL_REGISTRY_LABEL",
    "ADA_CREDENTIAL_REGISTRY_SCHEMA_VERSION",
    "ADA_CREDENTIAL_REGISTRY_SIGNATURE_KIND",
    "ADA_NATIVE_CREDENTIAL_ENUMERATION_KIND",
    "ADA_NATIVE_CREDENTIAL_ENUMERATION_SCHEMA_VERSION",
    "MAX_CREDENTIAL_REGISTRY_BYTES",
    "MAX_CREDENTIAL_REGISTRY_LABELS",
    "MAX_NATIVE_CREDENTIAL_ENUMERATION_AGE_MS",
    "AdaCredentialFingerprint",
    "AdaCredentialRegistry",
    "AdaCredentialRegistryError",
    "AdaNativeCredentialEnumeration",
]
