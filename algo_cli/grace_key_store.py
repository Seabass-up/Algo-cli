"""OS-backed key material for privacy HMACs and encrypted private artifacts."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterator, Protocol

from .ada_credential_registry import (
    ADA_CREDENTIAL_REGISTRY_LABEL,
    AdaCredentialRegistry,
    AdaCredentialRegistryError,
    AdaNativeCredentialEnumeration,
)
from .config import CONFIG_DIR
from .david_control_kernel import MAX_SAFE_INTEGER, ControlSigner, content_digest
from .henry_effect_control import TargetLeaseManager


KEYRING_SERVICE = "algo-cli-runtime"
CONTROL_SIGNING_KEY_LABEL = "control-signing-ed25519-v1"
BROWSER_PAIRING_KEY_LABEL = "browser-pairing-hmac-v1"
RECEIPT_ANCHOR_LABEL_PREFIX = "receipt-head-v1-"
AUTHORITY_ROTATION_ANCHOR_LABEL_PREFIX = "authority-rotation-v1-"
ALGO_FIXED_CREDENTIAL_LABELS = frozenset(
    {
        "alice-artifact-master-v1",
        ADA_CREDENTIAL_REGISTRY_LABEL,
        BROWSER_PAIRING_KEY_LABEL,
        CONTROL_SIGNING_KEY_LABEL,
        "irene-privacy-hmac-v1",
    }
)
MAX_RECEIPT_ANCHOR_BYTES = 4 * 1024
MAX_AUTHORITY_ROTATION_ANCHOR_BYTES = 16 * 1024
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ANCHOR_LABEL_RE = re.compile(
    rf"^{re.escape(RECEIPT_ANCHOR_LABEL_PREFIX)}[0-9a-f]{{64}}$"
)
_ROTATION_ANCHOR_LABEL_RE = re.compile(
    rf"^{re.escape(AUTHORITY_ROTATION_ANCHOR_LABEL_PREFIX)}[0-9a-f]{{64}}$"
)
_KEY_LENGTHS = frozenset({16, 24, 32, 48, 64})
_SECURE_SYSTEM_BACKEND_MODULES = frozenset(
    {
        "keyring.backends.SecretService",
        "keyring.backends.Windows",
        "keyring.backends.kwallet",
        "keyring.backends.libsecret",
        "keyring.backends.macOS",
    }
)
_VOLATILE_KEYS: dict[tuple[str, int], bytes] = {}
_VOLATILE_LOCK = threading.Lock()


class KeyStoreError(RuntimeError):
    """Raised when key material cannot be loaded without weakening storage."""


class ReceiptAnchorStoreError(KeyStoreError):
    """A content-free external receipt-head storage failure."""


class AuthorityRotationAnchorStoreError(KeyStoreError):
    """A content-free external authority-rotation head failure."""


class PasswordBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


@dataclass(frozen=True)
class KeyMaterial:
    key: bytes = field(repr=False)
    persistent: bool
    backend: str


def _validate_label(label: str) -> str:
    normalized = str(label or "").strip()
    if not _SAFE_LABEL_RE.fullmatch(normalized):
        raise ValueError("key label must be a bounded non-sensitive identifier")
    return normalized


def _validate_length(length: int) -> int:
    if isinstance(length, bool) or not isinstance(length, int) or length not in _KEY_LENGTHS:
        raise ValueError("key length must be one of 16, 24, 32, 48, or 64 bytes")
    return length


def _encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii")


def _decode_key(encoded: str, *, length: int) -> bytes:
    try:
        key = base64.b64decode(str(encoded), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise KeyStoreError("OS keyring contains malformed Algo CLI key material") from exc
    if len(key) != length or not hmac.compare_digest(_encode_key(key), str(encoded)):
        raise KeyStoreError("OS keyring contains invalid Algo CLI key material")
    return key


class KeyringKeyStore:
    """Persist keys in the platform credential store; never beside ciphertext."""

    def __init__(
        self,
        backend: PasswordBackend | None = None,
        *,
        service: str = KEYRING_SERVICE,
        lease_manager: TargetLeaseManager | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._backend = backend
        self.service = _validate_label(service)
        self._leases = lease_manager or TargetLeaseManager(
            CONFIG_DIR / "private" / "grace-keyring-leases"
        )
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def _inventory_lease_key(self) -> str:
        return f"keyring:{self.service}:{ADA_CREDENTIAL_REGISTRY_LABEL}:inventory"

    @contextmanager
    def _inventory_lease(self) -> Iterator[None]:
        with self._leases.acquire(self._inventory_lease_key()):
            yield

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
            raise KeyStoreError("credential_registry_clock")
        return value

    @staticmethod
    def _fingerprint_encoded(encoded: str | None) -> str | None:
        if encoded is None:
            return None
        if type(encoded) is not str:
            raise KeyStoreError("OS keyring contains non-text Algo CLI material")
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _allowed_inventory_label(label: str) -> bool:
        return (
            label in ALGO_FIXED_CREDENTIAL_LABELS
            or _ANCHOR_LABEL_RE.fullmatch(label) is not None
            or _ROTATION_ANCHOR_LABEL_RE.fullmatch(label) is not None
        )

    def _registry_signer_locked(self, backend: PasswordBackend) -> ControlSigner:
        encoded = backend.get_password(self.service, CONTROL_SIGNING_KEY_LABEL)
        if encoded is None:
            raise KeyStoreError("credential_registry_authority_missing")
        return ControlSigner.from_private_bytes(_decode_key(encoded, length=32))

    def _load_registry_locked(
        self, backend: PasswordBackend
    ) -> AdaCredentialRegistry | None:
        encoded = backend.get_password(self.service, ADA_CREDENTIAL_REGISTRY_LABEL)
        if encoded is None:
            return None
        if type(encoded) is not str:
            raise KeyStoreError("credential_registry_encoding")
        try:
            registry = AdaCredentialRegistry.from_bytes(
                encoded.encode("utf-8", errors="strict")
            )
            signer = self._registry_signer_locked(backend)
            registry.verify(signer.verifier)
        except (AdaCredentialRegistryError, UnicodeEncodeError) as exc:
            reason = getattr(exc, "reason_code", "credential_registry_encoding")
            raise KeyStoreError(str(reason)) from exc
        if (
            registry.service != self.service
            or not ALGO_FIXED_CREDENTIAL_LABELS <= set(registry.labels)
            or any(not self._allowed_inventory_label(label) for label in registry.labels)
        ):
            raise KeyStoreError("credential_registry_scope")
        return registry

    def _write_registry_locked(
        self,
        backend: PasswordBackend,
        registry: AdaCredentialRegistry,
    ) -> None:
        try:
            encoded = registry.to_bytes().decode("utf-8", errors="strict")
        except (AdaCredentialRegistryError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "reason_code", "credential_registry_encoding")
            raise KeyStoreError(str(reason)) from exc
        backend.set_password(self.service, ADA_CREDENTIAL_REGISTRY_LABEL, encoded)
        confirmed = backend.get_password(self.service, ADA_CREDENTIAL_REGISTRY_LABEL)
        if type(confirmed) is not str or not hmac.compare_digest(confirmed, encoded):
            raise KeyStoreError("credential_registry_write_lost")
        loaded = self._load_registry_locked(backend)
        if loaded != registry:
            raise KeyStoreError("credential_registry_write_lost")

    def initialize_fresh_credential_registry(self) -> AdaCredentialRegistry:
        """Create the registry only when the entire known namespace is empty.

        This compatibility path is restricted to an explicitly injected test
        backend. A production OS keyring cannot prove that dynamic labels are
        absent and must use ``initialize_from_native_credential_enumeration``.
        """

        if self._backend is None:
            raise KeyStoreError("credential_registry_native_enumeration_required")
        backend = self._password_backend()
        try:
            with self._inventory_lease():
                existing = self._load_registry_locked(backend)
                if existing is not None:
                    return existing
                if any(
                    backend.get_password(self.service, label) is not None
                    for label in ALGO_FIXED_CREDENTIAL_LABELS
                    if label != ADA_CREDENTIAL_REGISTRY_LABEL
                ):
                    raise KeyStoreError("credential_registry_migration_required")
                generated = secrets.token_bytes(32)
                encoded = _encode_key(generated)
                backend.set_password(self.service, CONTROL_SIGNING_KEY_LABEL, encoded)
                confirmed = backend.get_password(self.service, CONTROL_SIGNING_KEY_LABEL)
                if confirmed is None or not hmac.compare_digest(
                    _decode_key(confirmed, length=32), generated
                ):
                    raise KeyStoreError("credential_registry_authority_write_lost")
                signer = ControlSigner.from_private_bytes(generated)
                now_ms = self._now_ms()
                registry = AdaCredentialRegistry.create(
                    revision=1,
                    service=self.service,
                    labels=tuple(sorted(ALGO_FIXED_CREDENTIAL_LABELS)),
                    migration_kind="fresh_namespace",
                    migration_evidence_digest=content_digest(
                        {
                            "kind": "fresh_namespace",
                            "prior_known_label_count": 0,
                            "service": self.service,
                        }
                    ),
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                    signer=signer,
                )
                self._write_registry_locked(backend, registry)
                return registry
        except KeyStoreError:
            raise
        except Exception as exc:
            raise KeyStoreError(
                f"credential registry initialization failed: {type(exc).__name__}"
            ) from exc

    def initialize_from_native_credential_enumeration(
        self,
        evidence: AdaNativeCredentialEnumeration,
        *,
        expected_nonce: str,
        expected_code_identifier: str,
        expected_team_id: str,
        expected_designated_requirement_digest: str,
    ) -> AdaCredentialRegistry:
        """Initialize a complete registry from one fresh signed-helper census.

        The caller must obtain ``evidence`` from the exact Developer ID Austin
        helper while this method's cooperating-process lease is available. The
        evidence is nonce/identity/freshness bound, every reported digest is
        compared with the live backend, and the resulting signed registry
        commits to the canonical evidence digest.
        """

        if type(evidence) is not AdaNativeCredentialEnumeration:
            raise KeyStoreError("credential_enumeration_type")
        backend = self._password_backend()
        try:
            with self._inventory_lease():
                if self._load_registry_locked(backend) is not None:
                    raise KeyStoreError("credential_registry_already_initialized")
                now_ms = self._now_ms()
                evidence.verify_context(
                    expected_service=self.service,
                    expected_nonce=expected_nonce,
                    expected_code_identifier=expected_code_identifier,
                    expected_team_id=expected_team_id,
                    expected_designated_requirement_digest=(
                        expected_designated_requirement_digest
                    ),
                    now_ms=now_ms,
                )
                observed = {record.label: record.value_digest for record in evidence.records}
                if len(observed) != len(evidence.records):
                    raise KeyStoreError("credential_enumeration_duplicate")
                if ADA_CREDENTIAL_REGISTRY_LABEL in observed:
                    raise KeyStoreError("credential_enumeration_registry_present")
                if any(not self._allowed_inventory_label(label) for label in observed):
                    raise KeyStoreError("credential_enumeration_scope")

                for label in ALGO_FIXED_CREDENTIAL_LABELS - {
                    ADA_CREDENTIAL_REGISTRY_LABEL
                }:
                    actual = self._fingerprint_encoded(
                        backend.get_password(self.service, label)
                    )
                    expected = observed.get(label)
                    if actual != expected:
                        raise KeyStoreError("credential_enumeration_changed")
                for label, expected in observed.items():
                    actual = self._fingerprint_encoded(
                        backend.get_password(self.service, label)
                    )
                    if actual is None or not hmac.compare_digest(actual, expected):
                        raise KeyStoreError("credential_enumeration_changed")
                if backend.get_password(self.service, ADA_CREDENTIAL_REGISTRY_LABEL) is not None:
                    raise KeyStoreError("credential_enumeration_registry_present")

                encoded_control = backend.get_password(
                    self.service, CONTROL_SIGNING_KEY_LABEL
                )
                if encoded_control is None:
                    generated = secrets.token_bytes(32)
                    encoded_control = _encode_key(generated)
                    backend.set_password(
                        self.service,
                        CONTROL_SIGNING_KEY_LABEL,
                        encoded_control,
                    )
                    confirmed = backend.get_password(
                        self.service, CONTROL_SIGNING_KEY_LABEL
                    )
                    if confirmed is None or not hmac.compare_digest(
                        _decode_key(confirmed, length=32), generated
                    ):
                        raise KeyStoreError(
                            "credential_registry_authority_write_lost"
                        )
                signer = ControlSigner.from_private_bytes(
                    _decode_key(encoded_control, length=32)
                )
                labels = tuple(
                    sorted(
                        {
                            *ALGO_FIXED_CREDENTIAL_LABELS,
                            *(
                                label
                                for label in observed
                                if _ANCHOR_LABEL_RE.fullmatch(label)
                            ),
                        }
                    )
                )
                evidence_payload = evidence.to_bytes()
                evidence_digest = "sha256:" + hashlib.sha256(
                    evidence_payload
                ).hexdigest()
                registry = AdaCredentialRegistry.create(
                    revision=1,
                    service=self.service,
                    labels=labels,
                    migration_kind="native_enumeration",
                    migration_evidence_digest=evidence_digest,
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                    signer=signer,
                )
                self._write_registry_locked(backend, registry)

                for label, expected in observed.items():
                    actual = self._fingerprint_encoded(
                        backend.get_password(self.service, label)
                    )
                    if actual is None or not hmac.compare_digest(actual, expected):
                        raise KeyStoreError("credential_enumeration_changed")
                return registry
        except KeyStoreError:
            raise
        except AdaCredentialRegistryError as exc:
            raise KeyStoreError(exc.reason_code) from exc
        except Exception as exc:
            raise KeyStoreError(
                f"credential registry migration failed: {type(exc).__name__}"
            ) from exc

    def _register_inventory_label_locked(
        self,
        backend: PasswordBackend,
        label: str,
    ) -> AdaCredentialRegistry:
        safe_label = _validate_label(label)
        if not self._allowed_inventory_label(safe_label):
            raise KeyStoreError("credential_registry_label_scope")
        registry = self._load_registry_locked(backend)
        if registry is None:
            raise KeyStoreError("credential_registry_unavailable")
        signer = self._registry_signer_locked(backend)
        try:
            advanced = registry.advance(
                label=safe_label,
                updated_at_ms=max(self._now_ms(), registry.updated_at_ms),
                signer=signer,
            )
        except AdaCredentialRegistryError as exc:
            raise KeyStoreError(exc.reason_code) from exc
        if advanced != registry:
            self._write_registry_locked(backend, advanced)
        return advanced

    def register_inventory_label(self, label: str) -> AdaCredentialRegistry:
        """Durably register one finite label before its credential is created."""

        backend = self._password_backend()
        try:
            with self._inventory_lease():
                return self._register_inventory_label_locked(backend, label)
        except KeyStoreError:
            raise
        except Exception as exc:
            raise KeyStoreError(
                f"credential registry update failed: {type(exc).__name__}"
            ) from exc

    def _password_backend(self) -> PasswordBackend:
        if self._backend is not None:
            return self._backend
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - package dependency gate
            raise KeyStoreError("the keyring package is unavailable") from exc
        try:
            backend = keyring.get_keyring()
        except Exception as exc:
            raise KeyStoreError("the OS credential backend cannot be selected") from exc
        return self._validate_system_backend(backend)

    @staticmethod
    def _validate_system_backend(backend: Any) -> PasswordBackend:
        """Reject null, fail, chained, and third-party plaintext keyrings."""

        backend_type = type(backend)
        module_name = str(getattr(backend_type, "__module__", ""))
        if module_name not in _SECURE_SYSTEM_BACKEND_MODULES:
            raise KeyStoreError("a recognized OS credential backend is required")
        for method_name in ("get_password", "set_password", "delete_password"):
            if not callable(getattr(backend, method_name, None)):
                raise KeyStoreError("the OS credential backend contract is incomplete")
        return backend

    def get_or_create(self, label: str, *, length: int = 32) -> KeyMaterial:
        safe_label = _validate_label(label)
        safe_length = _validate_length(length)
        backend = self._password_backend()
        try:
            with self._inventory_lease():
                with self._leases.acquire(f"keyring:{self.service}:{safe_label}"):
                    encoded = backend.get_password(self.service, safe_label)
                    if encoded is not None:
                        return KeyMaterial(
                            _decode_key(encoded, length=safe_length),
                            persistent=True,
                            backend="os_keyring",
                        )
                    generated = secrets.token_bytes(safe_length)
                    encoded = _encode_key(generated)
                    backend.set_password(self.service, safe_label, encoded)
                    confirmed = backend.get_password(self.service, safe_label)
                    if confirmed is None:
                        raise KeyStoreError("OS keyring did not retain Algo CLI key material")
                    persisted = _decode_key(confirmed, length=safe_length)
                    if not hmac.compare_digest(generated, persisted):
                        raise KeyStoreError("OS keyring key creation lost a concurrent race")
                    return KeyMaterial(persisted, persistent=True, backend="os_keyring")
        except KeyStoreError:
            raise
        except Exception as exc:
            raise KeyStoreError(f"OS keyring operation failed: {type(exc).__name__}") from exc

    def get_existing(self, label: str, *, length: int = 32) -> KeyMaterial:
        """Load one exact key without creating state when it is absent."""

        safe_label = _validate_label(label)
        safe_length = _validate_length(length)
        backend = self._password_backend()
        try:
            with self._inventory_lease():
                with self._leases.acquire(f"keyring:{self.service}:{safe_label}"):
                    encoded = backend.get_password(self.service, safe_label)
                    if encoded is None:
                        raise KeyStoreError("required Algo CLI key material is absent")
                    return KeyMaterial(
                        _decode_key(encoded, length=safe_length),
                        persistent=True,
                        backend="os_keyring",
                    )
        except KeyStoreError:
            raise
        except Exception as exc:
            raise KeyStoreError(f"OS keyring operation failed: {type(exc).__name__}") from exc

    def delete(self, label: str) -> None:
        safe_label = _validate_label(label)
        backend = self._password_backend()
        try:
            with self._inventory_lease():
                with self._leases.acquire(f"keyring:{self.service}:{safe_label}"):
                    if backend.get_password(self.service, safe_label) is not None:
                        backend.delete_password(self.service, safe_label)
        except Exception as exc:
            raise KeyStoreError(f"OS keyring deletion failed: {type(exc).__name__}") from exc

    def fingerprint(self, label: str) -> str | None:
        """Return a content-free digest for one exact credential item."""

        safe_label = _validate_label(label)
        backend = self._password_backend()
        try:
            with self._inventory_lease():
                with self._leases.acquire(f"keyring:{self.service}:{safe_label}"):
                    encoded = backend.get_password(self.service, safe_label)
                    return self._fingerprint_encoded(encoded)
        except KeyStoreError:
            raise
        except Exception as exc:
            raise KeyStoreError(
                f"OS keyring fingerprint failed: {type(exc).__name__}"
            ) from exc

    def compare_and_delete(self, label: str, *, expected_digest: str) -> bool:
        """Delete one exact item only while its content-free digest still matches."""

        safe_label = _validate_label(label)
        if type(expected_digest) is not str or not _DIGEST_RE.fullmatch(expected_digest):
            raise ValueError("expected credential digest must be sha256")
        backend = self._password_backend()
        try:
            with self._inventory_lease():
                with self._leases.acquire(f"keyring:{self.service}:{safe_label}"):
                    encoded = backend.get_password(self.service, safe_label)
                    if encoded is None:
                        return False
                    observed = self._fingerprint_encoded(encoded)
                    if observed is None or not hmac.compare_digest(observed, expected_digest):
                        return False
                    backend.delete_password(self.service, safe_label)
                    if backend.get_password(self.service, safe_label) is not None:
                        raise KeyStoreError("OS keyring retained an Algo CLI credential after deletion")
                    return True
        except KeyStoreError:
            raise
        except Exception as exc:
            raise KeyStoreError(
                f"OS keyring compare-delete failed: {type(exc).__name__}"
            ) from exc

    def complete_inventory_labels(self) -> tuple[str, ...] | None:
        """Return the authenticated finite registry, never backend inference."""

        snapshot = self.complete_inventory_snapshot()
        return None if snapshot is None else tuple(label for label, _digest in snapshot)

    def complete_inventory_snapshot(
        self,
    ) -> tuple[tuple[str, str | None], ...] | None:
        """Atomically fingerprint every signed registry label under one lease."""

        backend = self._password_backend()
        try:
            with self._inventory_lease():
                registry = self._load_registry_locked(backend)
                if registry is None:
                    return None
                return tuple(
                    (
                        label,
                        self._fingerprint_encoded(
                            backend.get_password(self.service, label)
                        ),
                    )
                    for label in registry.labels
                )
        except KeyStoreError:
            raise
        except Exception as exc:
            raise KeyStoreError(
                f"credential registry snapshot failed: {type(exc).__name__}"
            ) from exc


def _anchor_label(journal_id: str) -> str:
    if type(journal_id) is not str or not _DIGEST_RE.fullmatch(journal_id):
        raise ReceiptAnchorStoreError("anchor_journal_id")
    return _validate_label(RECEIPT_ANCHOR_LABEL_PREFIX + journal_id.removeprefix("sha256:"))


def _anchor_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _validate_anchor_payload(value: bytes) -> bytes:
    if type(value) is not bytes or not 1 <= len(value) <= MAX_RECEIPT_ANCHOR_BYTES:
        raise ReceiptAnchorStoreError("anchor_value")
    try:
        from .ada_control_journal import ControlReceipt
        from .david_control_kernel import canonical_json_bytes, decode_json_payload

        receipt = ControlReceipt.from_dict(decode_json_payload(value))
        if not hmac.compare_digest(canonical_json_bytes(receipt.to_dict()), value):
            raise ValueError("noncanonical")
    except Exception as exc:
        raise ReceiptAnchorStoreError("anchor_value") from exc
    return value


def _encode_anchor(value: bytes) -> str:
    validated = _validate_anchor_payload(value)
    return "anchor-v1:" + base64.urlsafe_b64encode(validated).decode("ascii")


def _decode_anchor(value: str) -> bytes:
    if type(value) is not str or not value.startswith("anchor-v1:"):
        raise ReceiptAnchorStoreError("anchor_encoding")
    encoded = value.removeprefix("anchor-v1:")
    try:
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ReceiptAnchorStoreError("anchor_encoding") from exc
    if not hmac.compare_digest(_encode_anchor(decoded), value):
        raise ReceiptAnchorStoreError("anchor_encoding")
    return decoded


class GraceReceiptAnchorStore:
    """CAS-protected receipt heads held outside the SQLite journal in the OS keyring."""

    def __init__(self, key_store: KeyringKeyStore | None = None) -> None:
        if key_store is not None and type(key_store) is not KeyringKeyStore:
            raise ReceiptAnchorStoreError("anchor_key_store")
        self._key_store = key_store or KeyringKeyStore()

    def load(self, journal_id: str) -> bytes | None:
        label = _anchor_label(journal_id)
        try:
            backend = self._key_store._password_backend()
            with self._key_store._inventory_lease():
                with self._key_store._leases.acquire(
                    f"keyring:{self._key_store.service}:{label}"
                ):
                    encoded = backend.get_password(self._key_store.service, label)
                    return None if encoded is None else _decode_anchor(encoded)
        except ReceiptAnchorStoreError:
            raise
        except KeyStoreError as exc:
            raise ReceiptAnchorStoreError(str(exc)) from exc
        except Exception as exc:
            raise ReceiptAnchorStoreError(
                f"anchor_load_{type(exc).__name__.lower()}"
            ) from exc

    def compare_and_set(
        self,
        journal_id: str,
        *,
        expected_digest: str | None,
        value: bytes,
    ) -> bool:
        label = _anchor_label(journal_id)
        if expected_digest is not None and (
            type(expected_digest) is not str or not _DIGEST_RE.fullmatch(expected_digest)
        ):
            raise ReceiptAnchorStoreError("anchor_expected_digest")
        encoded_value = _encode_anchor(value)
        try:
            backend = self._key_store._password_backend()
            with self._key_store._inventory_lease():
                self._key_store._register_inventory_label_locked(backend, label)
                with self._key_store._leases.acquire(
                    f"keyring:{self._key_store.service}:{label}"
                ):
                    current_encoded = backend.get_password(self._key_store.service, label)
                    current = None if current_encoded is None else _decode_anchor(current_encoded)
                    current_digest = None if current is None else _anchor_digest(current)
                    if current_digest != expected_digest:
                        return False
                    backend.set_password(self._key_store.service, label, encoded_value)
                    confirmed = backend.get_password(self._key_store.service, label)
                    if confirmed is None or not hmac.compare_digest(
                        _decode_anchor(confirmed), value
                    ):
                        raise ReceiptAnchorStoreError("anchor_write_lost")
                    return True
        except ReceiptAnchorStoreError:
            raise
        except KeyStoreError as exc:
            raise ReceiptAnchorStoreError(str(exc)) from exc
        except Exception as exc:
            raise ReceiptAnchorStoreError(
                f"anchor_write_{type(exc).__name__.lower()}"
            ) from exc


def _rotation_anchor_label(anchor_id: str) -> str:
    if type(anchor_id) is not str or not _DIGEST_RE.fullmatch(anchor_id):
        raise AuthorityRotationAnchorStoreError("rotation_anchor_id")
    return _validate_label(
        AUTHORITY_ROTATION_ANCHOR_LABEL_PREFIX
        + anchor_id.removeprefix("sha256:")
    )


def _validate_rotation_anchor_payload(value: bytes, *, anchor_id: str) -> bytes:
    if (
        type(value) is not bytes
        or not 1 <= len(value) <= MAX_AUTHORITY_ROTATION_ANCHOR_BYTES
    ):
        raise AuthorityRotationAnchorStoreError("rotation_anchor_value")
    try:
        from .oliver_authority_rotation import OliverAuthorityRotationRecord

        record = OliverAuthorityRotationRecord.from_bytes(value)
        if record.anchor_id != anchor_id:
            raise ValueError("context")
    except Exception as exc:
        raise AuthorityRotationAnchorStoreError("rotation_anchor_value") from exc
    return value


def _encode_rotation_anchor(value: bytes, *, anchor_id: str) -> str:
    validated = _validate_rotation_anchor_payload(value, anchor_id=anchor_id)
    return "rotation-anchor-v1:" + base64.urlsafe_b64encode(validated).decode("ascii")


def _decode_rotation_anchor(value: str, *, anchor_id: str) -> bytes:
    if type(value) is not str or not value.startswith("rotation-anchor-v1:"):
        raise AuthorityRotationAnchorStoreError("rotation_anchor_encoding")
    encoded = value.removeprefix("rotation-anchor-v1:")
    try:
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthorityRotationAnchorStoreError("rotation_anchor_encoding") from exc
    if not hmac.compare_digest(
        _encode_rotation_anchor(decoded, anchor_id=anchor_id),
        value,
    ):
        raise AuthorityRotationAnchorStoreError("rotation_anchor_encoding")
    return decoded


class GraceAuthorityRotationAnchorStore:
    """CAS-protected rotation heads held outside the owner-controlled cache."""

    def __init__(self, key_store: KeyringKeyStore | None = None) -> None:
        if key_store is not None and type(key_store) is not KeyringKeyStore:
            raise AuthorityRotationAnchorStoreError("rotation_anchor_key_store")
        self._key_store = key_store or KeyringKeyStore()

    def load(self, anchor_id: str) -> bytes | None:
        label = _rotation_anchor_label(anchor_id)
        try:
            backend = self._key_store._password_backend()
            with self._key_store._inventory_lease():
                with self._key_store._leases.acquire(
                    f"keyring:{self._key_store.service}:{label}"
                ):
                    encoded = backend.get_password(self._key_store.service, label)
                    return (
                        None
                        if encoded is None
                        else _decode_rotation_anchor(encoded, anchor_id=anchor_id)
                    )
        except AuthorityRotationAnchorStoreError:
            raise
        except KeyStoreError as exc:
            raise AuthorityRotationAnchorStoreError(str(exc)) from exc
        except Exception as exc:
            raise AuthorityRotationAnchorStoreError(
                f"rotation_anchor_load_{type(exc).__name__.lower()}"
            ) from exc

    def compare_and_set(
        self,
        anchor_id: str,
        *,
        expected_digest: str | None,
        value: bytes,
    ) -> bool:
        label = _rotation_anchor_label(anchor_id)
        if expected_digest is not None and (
            type(expected_digest) is not str
            or not _DIGEST_RE.fullmatch(expected_digest)
        ):
            raise AuthorityRotationAnchorStoreError(
                "rotation_anchor_expected_digest"
            )
        encoded_value = _encode_rotation_anchor(value, anchor_id=anchor_id)
        try:
            backend = self._key_store._password_backend()
            with self._key_store._inventory_lease():
                self._key_store._register_inventory_label_locked(backend, label)
                with self._key_store._leases.acquire(
                    f"keyring:{self._key_store.service}:{label}"
                ):
                    current_encoded = backend.get_password(
                        self._key_store.service, label
                    )
                    current = (
                        None
                        if current_encoded is None
                        else _decode_rotation_anchor(
                            current_encoded,
                            anchor_id=anchor_id,
                        )
                    )
                    current_digest = (
                        None if current is None else _anchor_digest(current)
                    )
                    if current_digest != expected_digest:
                        return False
                    backend.set_password(
                        self._key_store.service,
                        label,
                        encoded_value,
                    )
                    confirmed = backend.get_password(self._key_store.service, label)
                    if confirmed is None or not hmac.compare_digest(
                        _decode_rotation_anchor(confirmed, anchor_id=anchor_id),
                        value,
                    ):
                        raise AuthorityRotationAnchorStoreError(
                            "rotation_anchor_write_lost"
                        )
                    return True
        except AuthorityRotationAnchorStoreError:
            raise
        except KeyStoreError as exc:
            raise AuthorityRotationAnchorStoreError(str(exc)) from exc
        except Exception as exc:
            raise AuthorityRotationAnchorStoreError(
                f"rotation_anchor_write_{type(exc).__name__.lower()}"
            ) from exc


class StaticKeyStore:
    """Explicit test/integration key source; never selected by production defaults."""

    def __init__(self, keys: dict[str, bytes] | None = None) -> None:
        self._keys = dict(keys or {})

    def get_or_create(self, label: str, *, length: int = 32) -> KeyMaterial:
        safe_label = _validate_label(label)
        safe_length = _validate_length(length)
        key = self._keys.setdefault(safe_label, secrets.token_bytes(safe_length))
        if len(key) != safe_length:
            raise KeyStoreError("static key has the wrong length")
        return KeyMaterial(bytes(key), persistent=True, backend="static")

    def delete(self, label: str) -> None:
        self._keys.pop(_validate_label(label), None)


def get_key_material(
    label: str,
    *,
    length: int = 32,
    require_persistent: bool,
    store: Any | None = None,
) -> KeyMaterial:
    """Load an OS-backed key or an explicitly bounded volatile privacy key."""

    safe_label = _validate_label(label)
    safe_length = _validate_length(length)
    selected = store or KeyringKeyStore()
    try:
        material = selected.get_or_create(safe_label, length=safe_length)
    except Exception as exc:
        if require_persistent:
            if isinstance(exc, KeyStoreError):
                raise
            raise KeyStoreError(f"persistent key source failed: {type(exc).__name__}") from exc
        with _VOLATILE_LOCK:
            key = _VOLATILE_KEYS.setdefault(
                (safe_label, safe_length),
                secrets.token_bytes(safe_length),
            )
        return KeyMaterial(key, persistent=False, backend="volatile_process")
    if require_persistent and not material.persistent:
        raise KeyStoreError("persistent key material is required")
    return material


def get_control_signer(*, store: Any | None = None) -> ControlSigner:
    """Load the durable control authority from a recognized OS key store."""

    material = get_key_material(
        CONTROL_SIGNING_KEY_LABEL,
        length=32,
        require_persistent=True,
        store=store,
    )
    return ControlSigner.from_private_bytes(material.key)


def load_control_signer(*, store: KeyringKeyStore | None = None) -> ControlSigner:
    """Load the existing control authority without creating a replacement key."""

    selected = store or KeyringKeyStore()
    material = selected.get_existing(CONTROL_SIGNING_KEY_LABEL, length=32)
    return ControlSigner.from_private_bytes(material.key)


def get_browser_pairing_key(*, store: Any | None = None) -> KeyMaterial:
    """Load the durable HMAC key used for an explicitly paired browser host."""

    return get_key_material(
        BROWSER_PAIRING_KEY_LABEL,
        length=32,
        require_persistent=True,
        store=store,
    )


__all__ = [
    "ADA_CREDENTIAL_REGISTRY_LABEL",
    "ALGO_FIXED_CREDENTIAL_LABELS",
    "AUTHORITY_ROTATION_ANCHOR_LABEL_PREFIX",
    "AuthorityRotationAnchorStoreError",
    "BROWSER_PAIRING_KEY_LABEL",
    "CONTROL_SIGNING_KEY_LABEL",
    "KEYRING_SERVICE",
    "MAX_AUTHORITY_ROTATION_ANCHOR_BYTES",
    "MAX_RECEIPT_ANCHOR_BYTES",
    "RECEIPT_ANCHOR_LABEL_PREFIX",
    "GraceAuthorityRotationAnchorStore",
    "GraceReceiptAnchorStore",
    "KeyMaterial",
    "KeyStoreError",
    "KeyringKeyStore",
    "ReceiptAnchorStoreError",
    "StaticKeyStore",
    "get_browser_pairing_key",
    "get_control_signer",
    "load_control_signer",
    "get_key_material",
]
