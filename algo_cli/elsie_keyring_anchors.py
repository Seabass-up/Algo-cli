"""Memory-only receipt heads in one fixed, authenticated OS credential.

Unlike native-control journals, these four closed memory domains need no
dynamic Keychain labels or signed native census. Existing dynamic heads stay
on the native registry path; provisioning never migrates or discards them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from .grace_key_store import (
    ELSIE_MEMORY_ANCHORS_LABEL,
    ContentFreeReceiptHead,
    KeyringKeyStore,
    PasswordBackend,
    ReceiptAnchorStoreError,
    _anchor_digest,
    _anchor_label,
    _decode_anchor,
    _decode_key,
    _encode_anchor,
)


_NAMESPACES = frozenset(
    {
        "elsie-goal-store-v1",
        "elsie-agent-thread-store-v1",
        "elsie-memory-candidate-store-v1",
        "elsie-skill-run-history-store-v1",
    }
)
_PRIVACY_LABEL = "irene-privacy-hmac-v1"
_DOMAIN = b"algo-cli/elsie-keyring-anchors/v1\x00"
_MAX_BYTES = 60 * 1024
_MAX_HEADS = 64


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _head(value: bytes, journal_id: str) -> ContentFreeReceiptHead:
    head = ContentFreeReceiptHead.from_bytes(value)
    if head.namespace not in _NAMESPACES or head.journal_id != journal_id:
        raise ReceiptAnchorStoreError("memory_anchor_scope")
    return head


class ElsieKeyringAnchorStore:
    """A bounded memory-only CAS store; never a native inventory authority."""

    def __init__(self, key_store: KeyringKeyStore) -> None:
        if type(key_store) is not KeyringKeyStore:
            raise ReceiptAnchorStoreError("anchor_key_store")
        self._key_store = key_store

    def _key(self, backend: PasswordBackend) -> bytes:
        encoded = backend.get_password(self._key_store.service, _PRIVACY_LABEL)
        if encoded is None:
            raise ReceiptAnchorStoreError("memory_anchor_key_unavailable")
        return _decode_key(encoded, length=32)

    def _encode(self, heads: dict[str, str], key: bytes) -> str:
        unsigned = {"schema_version": 1, "service": self._key_store.service, "heads": heads}
        mac = hmac.new(key, _DOMAIN + _canonical(unsigned), hashlib.sha256).hexdigest()
        encoded = _canonical({**unsigned, "authentication": mac})
        if len(heads) > _MAX_HEADS or len(encoded) > _MAX_BYTES:
            raise ReceiptAnchorStoreError("memory_anchor_capacity")
        return encoded.decode("ascii")

    def _read(self, backend: PasswordBackend) -> dict[str, str] | None:
        encoded = backend.get_password(self._key_store.service, ELSIE_MEMORY_ANCHORS_LABEL)
        if encoded is None:
            return None
        try:
            if type(encoded) is not str or not encoded.isascii() or len(encoded) > _MAX_BYTES:
                raise ValueError("encoding")
            value = json.loads(encoded)
            if (
                type(value) is not dict
                or set(value) != {"schema_version", "service", "heads", "authentication"}
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or value["service"] != self._key_store.service
                or type(value["heads"]) is not dict
                or len(value["heads"]) > _MAX_HEADS
            ):
                raise ValueError("schema")
            for journal_id, item in value["heads"].items():
                _anchor_label(journal_id)
                _head(_decode_anchor(item), journal_id)
            expected = self._encode(value["heads"], self._key(backend))
            if not hmac.compare_digest(encoded, expected):
                raise ValueError("authentication")
            return dict(value["heads"])
        except Exception as exc:
            raise ReceiptAnchorStoreError("memory_anchor_invalid") from exc

    def _write(self, backend: PasswordBackend, heads: dict[str, str]) -> None:
        encoded = self._encode(heads, self._key(backend))
        backend.set_password(self._key_store.service, ELSIE_MEMORY_ANCHORS_LABEL, encoded)
        confirmed = backend.get_password(self._key_store.service, ELSIE_MEMORY_ANCHORS_LABEL)
        if type(confirmed) is not str or not hmac.compare_digest(encoded, confirmed):
            raise ReceiptAnchorStoreError("anchor_write_lost")
        if self._read(backend) != heads:
            raise ReceiptAnchorStoreError("anchor_write_lost")

    def provision(self) -> bool:
        """Create only an absent fixed credential. Never replace an invalid one."""

        backend = self._key_store._password_backend()
        with self._key_store._inventory_lease():
            if self._read(backend) is not None:
                return False
            self._write(backend, {})
            return True

    def status(self) -> dict[str, bool | int]:
        backend = self._key_store._password_backend()
        with self._key_store._inventory_lease():
            heads = self._read(backend)
            return {"provisioned": heads is not None, "memory_heads": 0 if heads is None else len(heads)}

    def _current(self, backend: PasswordBackend, journal_id: str) -> tuple[dict[str, str] | None, bytes | None, bool]:
        heads = self._read(backend)
        legacy = backend.get_password(self._key_store.service, _anchor_label(journal_id))
        bundled = None if heads is None else heads.get(journal_id)
        if legacy is not None and bundled is not None:
            raise ReceiptAnchorStoreError("memory_anchor_conflicting_routes")
        encoded = legacy if legacy is not None else bundled
        current = None if encoded is None else _decode_anchor(encoded)
        if current is not None:
            _head(current, journal_id)
        return heads, current, legacy is not None

    def load(self, journal_id: str) -> bytes | None:
        _anchor_label(journal_id)
        backend = self._key_store._password_backend()
        with self._key_store._inventory_lease():
            _heads, current, _legacy = self._current(backend, journal_id)
            return current

    def compare_and_set(self, journal_id: str, *, expected_digest: str | None, value: bytes) -> bool:
        label = _anchor_label(journal_id)
        incoming = _head(value, journal_id)
        if expected_digest is not None:
            _anchor_label(expected_digest)
        backend = self._key_store._password_backend()
        with self._key_store._inventory_lease():
            heads, current, legacy = self._current(backend, journal_id)
            if (None if current is None else _anchor_digest(current)) != expected_digest:
                return False
            if current is not None:
                previous = _head(current, journal_id)
                if (
                    incoming.namespace != previous.namespace
                    or incoming.subject_digest != previous.subject_digest
                    or incoming.sequence != previous.sequence + 1
                ):
                    raise ReceiptAnchorStoreError("memory_anchor_sequence")
            elif incoming.sequence != 1:
                raise ReceiptAnchorStoreError("memory_anchor_sequence")
            # Existing native users retain the exact native registry gate. A
            # missing CLI bundle is not an invitation to migrate their heads.
            if legacy or (heads is None and self._key_store._load_registry_locked(backend) is not None):
                self._key_store._register_inventory_label_locked(backend, label)
                with self._key_store._leases.acquire(f"keyring:{self._key_store.service}:{label}"):
                    backend.set_password(self._key_store.service, label, _encode_anchor(value))
                    confirmed = backend.get_password(self._key_store.service, label)
                    if confirmed is None or not hmac.compare_digest(_decode_anchor(confirmed), value):
                        raise ReceiptAnchorStoreError("anchor_write_lost")
                return True
            if heads is None:
                raise ReceiptAnchorStoreError("memory_anchor_provisioning_required")
            heads[journal_id] = _encode_anchor(value)
            self._write(backend, heads)
            return True
