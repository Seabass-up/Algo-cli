"""Encrypted, capability-scoped, short-lived private artifact storage.

Alice stores ciphertext only.  Each run receives an unpersisted 256-bit bearer
capability, while an OS-backed master key derives independent run, manifest,
capability, identity, and per-artifact encryption keys.  Signed manifests make
quota and revocation state tamper-evident; AES-256-GCM binds every ciphertext to
its run and artifact identifiers.

The store deliberately does not promise secure erasure.  Unlinking encrypted
files revokes ordinary access but cannot erase filesystem snapshots, backups,
or previously copied ciphertext.  Expiry and revocation are therefore enforced
cryptographically and in the signed manifest before ciphertext cleanup.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import tempfile
import time
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import CONFIG_DIR
from .grace_key_store import KeyStoreError, get_key_material
from .henry_effect_control import TargetLeaseManager


ARTIFACT_SCHEMA_VERSION = 1
MASTER_KEY_LABEL = "alice-artifact-master-v1"
DEFAULT_TTL_SECONDS = 15 * 60.0
MAX_TTL_SECONDS = 24 * 60 * 60.0
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_ID_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}$"
)
_MANIFEST_FILE = "manifest.alice.json"
_ARTIFACT_SUFFIX = ".alice"
_MAX_MANIFEST_BYTES = 512 * 1024
_INNER_HEADER_BYTES = 4
_RUN_KEY_BYTES = 128


class ArtifactStoreError(RuntimeError):
    """Base failure for private artifact persistence."""


class ArtifactAccessDenied(ArtifactStoreError):
    """A capability is absent, wrong, expired, or revoked."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Ciphertext, metadata, or signed state failed validation."""


class ArtifactQuotaExceeded(ArtifactStoreError):
    """A bounded store or run quota would be exceeded."""


class UnsafeArtifactPath(ArtifactStoreError):
    """A private path was replaced with a link or special file."""


@dataclass(frozen=True)
class ArtifactPolicy:
    """Mandatory limits for one encrypted artifact store."""

    max_artifact_bytes: int = 8 * 1024 * 1024
    max_run_bytes: int = 32 * 1024 * 1024
    max_run_disk_bytes: int = 48 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_total_disk_bytes: int = 384 * 1024 * 1024
    max_artifacts_per_run: int = 128
    max_runs: int = 256
    default_ttl_seconds: float = DEFAULT_TTL_SECONDS
    max_ttl_seconds: float = MAX_TTL_SECONDS

    def __post_init__(self) -> None:
        integer_fields = (
            "max_artifact_bytes",
            "max_run_bytes",
            "max_run_disk_bytes",
            "max_total_bytes",
            "max_total_disk_bytes",
            "max_artifacts_per_run",
            "max_runs",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_artifact_bytes > self.max_run_bytes:
            raise ValueError("max_artifact_bytes cannot exceed max_run_bytes")
        if self.max_run_bytes > self.max_total_bytes:
            raise ValueError("max_run_bytes cannot exceed max_total_bytes")
        if self.max_run_disk_bytes > self.max_total_disk_bytes:
            raise ValueError("max_run_disk_bytes cannot exceed max_total_disk_bytes")
        for name in ("default_ttl_seconds", "max_ttl_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{name} must be positive and finite")
        if float(self.default_ttl_seconds) > float(self.max_ttl_seconds):
            raise ValueError("default_ttl_seconds cannot exceed max_ttl_seconds")


@dataclass(frozen=True)
class RunCapability:
    """Unpersisted bearer authority for exactly one short-lived run."""

    run_id: str
    token: bytes = field(repr=False)
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("run capability has an invalid run id")
        if not isinstance(self.token, bytes) or len(self.token) != 32:
            raise ValueError("run capability token must be 32 bytes")
        if (
            not math.isfinite(float(self.issued_at))
            or not math.isfinite(float(self.expires_at))
            or float(self.issued_at) < 0
            or float(self.expires_at) <= float(self.issued_at)
        ):
            raise ValueError("run capability timestamps are invalid")

    def public_view(self) -> dict[str, Any]:
        """Return structural state without the bearer token."""

        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class EncryptedArtifactRef:
    """Opaque artifact reference; access still requires its run capability."""

    uri: str
    run_id: str
    artifact_id: str
    content_id: str
    byte_count: int
    media_type: str
    expires_at: float

    def __post_init__(self) -> None:
        if not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("artifact reference run id is invalid")
        if not _ARTIFACT_ID_RE.fullmatch(self.artifact_id):
            raise ValueError("artifact reference id is invalid")
        if self.uri != f"artifact://private/v1/{self.artifact_id}":
            raise ValueError("artifact reference URI is invalid")
        if not _CONTENT_ID_RE.fullmatch(self.content_id):
            raise ValueError("artifact reference content identity is invalid")
        if (
            isinstance(self.byte_count, bool)
            or not isinstance(self.byte_count, int)
            or self.byte_count < 0
        ):
            raise ValueError("artifact reference byte count is invalid")
        _validate_media_type(self.media_type)
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(float(self.expires_at))
            or float(self.expires_at) <= 0
        ):
            raise ValueError("artifact reference expiry is invalid")

    def public_view(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "content_id": self.content_id,
            "bytes": self.byte_count,
            "media_type": self.media_type,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class RevocationResult:
    already_revoked: bool
    ciphertext_files_deleted: int
    ciphertext_files_pending: int


@dataclass(frozen=True)
class CleanupResult:
    scanned_runs: int
    active_runs: int
    deleted_runs: int
    deleted_orphans: int
    deleted_temporary_paths: int
    repaired_manifests: int
    corrupt_runs: int
    active_artifacts: int
    active_plaintext_bytes: int
    active_disk_bytes: int
    quota_satisfied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_runs": self.scanned_runs,
            "active_runs": self.active_runs,
            "deleted_runs": self.deleted_runs,
            "deleted_orphans": self.deleted_orphans,
            "deleted_temporary_paths": self.deleted_temporary_paths,
            "repaired_manifests": self.repaired_manifests,
            "corrupt_runs": self.corrupt_runs,
            "active_artifacts": self.active_artifacts,
            "active_plaintext_bytes": self.active_plaintext_bytes,
            "active_disk_bytes": self.active_disk_bytes,
            "quota_satisfied": self.quota_satisfied,
        }


@dataclass(frozen=True)
class _RunKeys:
    encryption: bytes = field(repr=False)
    manifest: bytes = field(repr=False)
    capability: bytes = field(repr=False)
    identity: bytes = field(repr=False)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _strict_json_loads(payload: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"invalid JSON numeric constant: {value}")

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactIntegrityError("private artifact state is not strict JSON") from exc


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64_decode(value: Any, *, expected_bytes: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise ArtifactIntegrityError("private artifact base64 field is invalid")
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (TypeError, ValueError) as exc:
        raise ArtifactIntegrityError("private artifact base64 field is invalid") from exc
    if _b64_encode(decoded) != value or (
        expected_bytes is not None and len(decoded) != expected_bytes
    ):
        raise ArtifactIntegrityError("private artifact base64 field is non-canonical")
    return decoded


def _validate_media_type(media_type: str) -> str:
    normalized = str(media_type or "").strip()
    if not _MEDIA_TYPE_RE.fullmatch(normalized):
        raise ValueError("artifact media_type must be a bounded MIME type without parameters")
    return normalized.lower()


def _validate_ttl(ttl_seconds: float, policy: ArtifactPolicy) -> float:
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(float(ttl_seconds))
        or float(ttl_seconds) <= 0
        or float(ttl_seconds) > float(policy.max_ttl_seconds)
    ):
        raise ValueError("artifact TTL must be positive, finite, and within policy")
    return float(ttl_seconds)


def _safe_now(clock: Callable[[], float]) -> float:
    try:
        now = float(clock())
    except Exception as exc:
        raise ArtifactStoreError("artifact clock failed") from exc
    if not math.isfinite(now) or now < 0:
        raise ArtifactStoreError("artifact clock must return a non-negative finite value")
    return now


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise UnsafeArtifactPath("private artifact directory is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeArtifactPath("private artifact directories must be real directories")
    if os.name == "posix":
        try:
            os.chmod(path, _DIRECTORY_MODE)
        except OSError as exc:
            raise UnsafeArtifactPath("private artifact directory permissions are unsafe") from exc


def _ensure_regular_or_missing(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UnsafeArtifactPath("private artifact path cannot be inspected") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeArtifactPath("private artifact files must be regular non-symlink files")


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, payload: bytes) -> None:
    _ensure_private_directory(path.parent)
    _ensure_regular_or_missing(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, _FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, _FILE_MODE)
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)
        raise


def _publish_new(path: Path, payload: bytes) -> None:
    _ensure_private_directory(path.parent)
    _ensure_regular_or_missing(path)
    if path.exists():
        raise ArtifactIntegrityError("artifact identifier collision")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, _FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        if os.name == "posix":
            os.chmod(path, _FILE_MODE)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise ArtifactIntegrityError("artifact identifier collision") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)


def _read_regular(path: Path, *, max_bytes: int) -> bytes:
    _ensure_regular_or_missing(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactIntegrityError("private artifact file is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeArtifactPath("private artifact file is not regular")
        if os.name == "posix" and stat.S_IMODE(info.st_mode) != _FILE_MODE:
            raise UnsafeArtifactPath("private artifact file permissions are unsafe")
        if info.st_size < 1 or info.st_size > max_bytes:
            raise ArtifactIntegrityError("private artifact file has an invalid size")
        chunks: list[bytes] = []
        remaining = int(info.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ArtifactIntegrityError("private artifact file was truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ArtifactIntegrityError("private artifact file changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _remove_tree_no_follow(path: Path) -> int:
    """Remove one private tree without following links; return removed paths."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        path.unlink()
        return 1
    removed = 0
    for child in list(path.iterdir()):
        removed += _remove_tree_no_follow(child)
    path.rmdir()
    return removed + 1


def _derive_run_keys(master_key: bytes, salt: bytes, run_id: str) -> _RunKeys:
    if len(master_key) != 32 or len(salt) != 32 or not _RUN_ID_RE.fullmatch(run_id):
        raise ArtifactIntegrityError("artifact key derivation inputs are invalid")
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=_RUN_KEY_BYTES,
        salt=salt,
        info=b"algo-cli/alice/run-keys/v1/" + run_id.encode("ascii"),
    ).derive(master_key)
    return _RunKeys(material[:32], material[32:64], material[64:96], material[96:128])


def _derive_artifact_key(run_key: bytes, artifact_id: str) -> bytes:
    if len(run_key) != 32 or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise ArtifactIntegrityError("artifact key derivation inputs are invalid")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes.fromhex(artifact_id),
        info=b"algo-cli/alice/artifact-key/v1",
    ).derive(run_key)


def _manifest_signature(manifest: Mapping[str, Any], key: bytes) -> str:
    unsigned = {name: value for name, value in manifest.items() if name != "signature"}
    return hmac.new(key, _canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()


def _signed_manifest(manifest: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    result = dict(manifest)
    result["signature"] = _manifest_signature(result, key)
    return result


class EncryptedArtifactStore:
    """Bounded encrypted store with per-run capability authority."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        policy: ArtifactPolicy = ArtifactPolicy(),
        key_store: Any | None = None,
        clock: Callable[[], float] = time.time,
        lease_manager: TargetLeaseManager | None = None,
    ) -> None:
        configured = Path(root) if root is not None else CONFIG_DIR / "private" / "alice-artifacts-v1"
        self.root = Path(os.path.abspath(str(configured.expanduser())))
        self.policy = policy
        self._key_store = key_store
        self._clock = clock
        self._ensure_layout()
        self._leases = lease_manager or TargetLeaseManager(self.root / "leases")

    @property
    def _runs(self) -> Path:
        return self.root / "runs"

    def _ensure_layout(self) -> None:
        _ensure_private_directory(self.root)
        _ensure_private_directory(self.root / "runs")
        _ensure_private_directory(self.root / "leases")

    def _master_key(self) -> bytes:
        try:
            material = get_key_material(
                MASTER_KEY_LABEL,
                length=32,
                require_persistent=True,
                store=self._key_store,
            )
        except KeyStoreError as exc:
            raise ArtifactStoreError("persistent artifact key material is unavailable") from exc
        if len(material.key) != 32 or not material.persistent:
            raise ArtifactStoreError("persistent artifact key material is invalid")
        return material.key

    def _run_dir(self, run_id: str) -> Path:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ArtifactIntegrityError("artifact run id is invalid")
        return self._runs / run_id

    def _manifest_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / _MANIFEST_FILE

    def _artifact_path(self, run_id: str, artifact_id: str) -> Path:
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise ArtifactIntegrityError("artifact id is invalid")
        return self._run_dir(run_id) / "artifacts" / f"{artifact_id}{_ARTIFACT_SUFFIX}"

    def _lease(self):
        self._ensure_layout()
        return self._leases.acquire("alice-artifact-store-v1", lease_seconds=120.0)

    def _decode_manifest(
        self,
        run_id: str,
        master_key: bytes,
        *,
        allow_counter_repair: bool,
    ) -> tuple[dict[str, Any], _RunKeys, bool]:
        payload = _read_regular(self._manifest_path(run_id), max_bytes=_MAX_MANIFEST_BYTES)
        loaded = _strict_json_loads(payload)
        if not isinstance(loaded, dict):
            raise ArtifactIntegrityError("artifact manifest is not an object")
        required = {
            "schema_version",
            "run_id",
            "status",
            "created_at",
            "updated_at",
            "expires_at",
            "salt",
            "capability_verifier",
            "revision",
            "artifact_count",
            "plaintext_bytes",
            "disk_bytes",
            "artifacts",
            "signature",
        }
        if set(loaded) != required:
            raise ArtifactIntegrityError("artifact manifest fields are invalid")
        salt = _b64_decode(loaded.get("salt"), expected_bytes=32)
        keys = _derive_run_keys(master_key, salt, run_id)
        signature = loaded.get("signature")
        if (
            not isinstance(signature, str)
            or not _HEX_DIGEST_RE.fullmatch(signature)
            or not hmac.compare_digest(signature, _manifest_signature(loaded, keys.manifest))
        ):
            raise ArtifactIntegrityError("artifact manifest signature is invalid")
        created_at = loaded.get("created_at")
        updated_at = loaded.get("updated_at")
        expires_at = loaded.get("expires_at")
        revision = loaded.get("revision")
        verifier = loaded.get("capability_verifier")
        artifacts = loaded.get("artifacts")
        if (
            loaded.get("schema_version") != ARTIFACT_SCHEMA_VERSION
            or loaded.get("run_id") != run_id
            or loaded.get("status") not in {"active", "revoked"}
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or float(created_at) < 0
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))
            or float(updated_at) < float(created_at)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= float(created_at)
            or float(expires_at) - float(created_at) > float(self.policy.max_ttl_seconds) + 0.001
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(verifier, str)
            or not _HEX_DIGEST_RE.fullmatch(verifier)
            or not isinstance(artifacts, dict)
            or len(artifacts) > self.policy.max_artifacts_per_run
        ):
            raise ArtifactIntegrityError("artifact manifest state is invalid")
        total_plaintext = 0
        total_disk = 0
        for artifact_id, record in artifacts.items():
            if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
                raise ArtifactIntegrityError("artifact manifest id is invalid")
            if not isinstance(record, dict) or set(record) != {
                "byte_count",
                "disk_bytes",
                "expires_at",
                "content_id",
            }:
                raise ArtifactIntegrityError("artifact manifest record is invalid")
            byte_count = record.get("byte_count")
            disk_bytes = record.get("disk_bytes")
            artifact_expires = record.get("expires_at")
            content_id = record.get("content_id")
            if (
                isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or byte_count < 0
                or byte_count > self.policy.max_artifact_bytes
                or isinstance(disk_bytes, bool)
                or not isinstance(disk_bytes, int)
                or disk_bytes < 1
                or disk_bytes > self._max_envelope_bytes()
                or isinstance(artifact_expires, bool)
                or not isinstance(artifact_expires, (int, float))
                or not math.isfinite(float(artifact_expires))
                or float(artifact_expires) < float(created_at)
                or float(artifact_expires) > float(expires_at)
                or not isinstance(content_id, str)
                or not _CONTENT_ID_RE.fullmatch(content_id)
            ):
                raise ArtifactIntegrityError("artifact manifest record values are invalid")
            total_plaintext += byte_count
            total_disk += disk_bytes
        declared_count = loaded.get("artifact_count")
        declared_plaintext = loaded.get("plaintext_bytes")
        declared_disk = loaded.get("disk_bytes")
        counters_match = (
            isinstance(declared_count, int)
            and not isinstance(declared_count, bool)
            and declared_count == len(artifacts)
            and isinstance(declared_plaintext, int)
            and not isinstance(declared_plaintext, bool)
            and declared_plaintext == total_plaintext
            and isinstance(declared_disk, int)
            and not isinstance(declared_disk, bool)
            and declared_disk == total_disk
        )
        if not counters_match and not allow_counter_repair:
            raise ArtifactIntegrityError("artifact manifest counters are invalid")
        return loaded, keys, counters_match

    def _write_manifest(self, manifest: Mapping[str, Any], keys: _RunKeys) -> None:
        payload = _canonical_json_bytes(_signed_manifest(manifest, keys.manifest))
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise ArtifactQuotaExceeded("artifact manifest exceeds its size ceiling")
        _atomic_replace(self._manifest_path(str(manifest["run_id"])), payload)

    @staticmethod
    def _verify_capability(
        capability: RunCapability,
        manifest: Mapping[str, Any],
        keys: _RunKeys,
    ) -> None:
        run_id = manifest.get("run_id")
        if capability.run_id != run_id:
            raise ArtifactAccessDenied("artifact capability is for a different run")
        verifier = hmac.new(keys.capability, capability.token, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(verifier, str(manifest.get("capability_verifier") or "")):
            raise ArtifactAccessDenied("artifact capability is invalid")
        if (
            float(capability.issued_at) != float(manifest["created_at"])
            or float(capability.expires_at) != float(manifest["expires_at"])
        ):
            raise ArtifactAccessDenied("artifact capability timestamps are invalid")

    @staticmethod
    def _require_active(manifest: Mapping[str, Any], now: float) -> None:
        if manifest.get("status") != "active":
            raise ArtifactAccessDenied("artifact run is revoked")
        if now < float(manifest["updated_at"]):
            raise ArtifactAccessDenied("artifact clock moved backwards")
        if now >= float(manifest["expires_at"]):
            raise ArtifactAccessDenied("artifact run is expired")

    def _max_envelope_bytes(self) -> int:
        raw = self.policy.max_artifact_bytes + 16 * 1024 + 16
        return (raw * 4 + 2) // 3 + 16 * 1024

    def _check_run_files(
        self,
        run_id: str,
        manifest: Mapping[str, Any],
        *,
        clean_orphans: bool,
    ) -> tuple[int, int]:
        artifact_dir = self._run_dir(run_id) / "artifacts"
        _ensure_private_directory(artifact_dir)
        known = set(manifest["artifacts"])
        deleted_orphans = 0
        deleted_temporary = 0
        for child in list(artifact_dir.iterdir()):
            try:
                info = child.lstat()
            except OSError as exc:
                raise UnsafeArtifactPath("artifact directory cannot be inspected") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise UnsafeArtifactPath("artifact directory contains an unsafe path")
            if child.name.startswith(".") and child.name.endswith(".tmp"):
                if not clean_orphans:
                    raise ArtifactIntegrityError("artifact run has an incomplete temporary file")
                child.unlink()
                deleted_temporary += 1
                continue
            if not child.name.endswith(_ARTIFACT_SUFFIX):
                raise UnsafeArtifactPath("artifact directory contains an unknown file")
            artifact_id = child.name[: -len(_ARTIFACT_SUFFIX)]
            if artifact_id not in known:
                if not clean_orphans:
                    raise ArtifactIntegrityError("artifact run has an uncommitted orphan")
                child.unlink()
                deleted_orphans += 1
        for artifact_id, record in manifest["artifacts"].items():
            path = self._artifact_path(run_id, artifact_id)
            _ensure_regular_or_missing(path)
            if not path.exists() or path.stat().st_size != record["disk_bytes"]:
                raise ArtifactIntegrityError("manifest references a missing or resized artifact")
        if deleted_orphans or deleted_temporary:
            _fsync_directory(artifact_dir)
        return deleted_orphans, deleted_temporary

    @staticmethod
    def _repair_counters(manifest: dict[str, Any]) -> None:
        records = manifest["artifacts"].values()
        manifest["artifact_count"] = len(manifest["artifacts"])
        manifest["plaintext_bytes"] = sum(record["byte_count"] for record in records)
        manifest["disk_bytes"] = sum(
            record["disk_bytes"] for record in manifest["artifacts"].values()
        )

    def _active_totals(
        self,
        master_key: bytes,
        now: float,
        *,
        prune_inactive: bool,
    ) -> tuple[int, int, int]:
        runs = 0
        plaintext = 0
        disk = 0
        for child in list(self._runs.iterdir()):
            if child.name.startswith(".create-"):
                if prune_inactive:
                    _remove_tree_no_follow(child)
                    continue
                raise ArtifactIntegrityError("artifact store has an incomplete run creation")
            try:
                info = child.lstat()
            except OSError as exc:
                raise UnsafeArtifactPath("artifact run directory cannot be inspected") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise UnsafeArtifactPath("artifact runs must be real directories")
            if not _RUN_ID_RE.fullmatch(child.name):
                raise UnsafeArtifactPath("artifact run directory name is invalid")
            manifest, _keys, _matches = self._decode_manifest(
                child.name,
                master_key,
                allow_counter_repair=False,
            )
            if now < float(manifest["updated_at"]):
                raise ArtifactIntegrityError("artifact clock moved backwards")
            if manifest["status"] != "active" or now >= float(manifest["expires_at"]):
                if prune_inactive:
                    _remove_tree_no_follow(child)
                continue
            self._check_run_files(
                child.name,
                manifest,
                clean_orphans=prune_inactive,
            )
            runs += 1
            plaintext += int(manifest["plaintext_bytes"])
            disk += int(manifest["disk_bytes"])
        return runs, plaintext, disk

    def create_run(
        self,
        *,
        ttl_seconds: float | None = None,
        run_id: str | None = None,
    ) -> RunCapability:
        ttl = _validate_ttl(
            self.policy.default_ttl_seconds if ttl_seconds is None else ttl_seconds,
            self.policy,
        )
        master_key = self._master_key()
        requested_id = run_id
        if requested_id is not None and not _RUN_ID_RE.fullmatch(requested_id):
            raise ValueError("run_id must be 32 lowercase hexadecimal characters")
        with self._lease():
            now = _safe_now(self._clock)
            active_runs, _plaintext, _disk = self._active_totals(
                master_key,
                now,
                prune_inactive=True,
            )
            if active_runs >= self.policy.max_runs:
                raise ArtifactQuotaExceeded("active run quota is exhausted")
            selected_id = requested_id or secrets.token_hex(16)
            while self._run_dir(selected_id).exists():
                if requested_id is not None:
                    raise ArtifactIntegrityError("requested artifact run already exists")
                selected_id = secrets.token_hex(16)
            token = secrets.token_bytes(32)
            salt = secrets.token_bytes(32)
            keys = _derive_run_keys(master_key, salt, selected_id)
            expires_at = now + ttl
            manifest = {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "run_id": selected_id,
                "status": "active",
                "created_at": now,
                "updated_at": now,
                "expires_at": expires_at,
                "salt": _b64_encode(salt),
                "capability_verifier": hmac.new(
                    keys.capability, token, hashlib.sha256
                ).hexdigest(),
                "revision": 1,
                "artifact_count": 0,
                "plaintext_bytes": 0,
                "disk_bytes": 0,
                "artifacts": {},
            }
            staging = Path(tempfile.mkdtemp(prefix=".create-", dir=str(self._runs)))
            try:
                if os.name == "posix":
                    os.chmod(staging, _DIRECTORY_MODE)
                _ensure_private_directory(staging / "artifacts")
                payload = _canonical_json_bytes(_signed_manifest(manifest, keys.manifest))
                _atomic_replace(staging / _MANIFEST_FILE, payload)
                os.rename(staging, self._run_dir(selected_id))
                _fsync_directory(self._runs)
            except BaseException:
                _remove_tree_no_follow(staging)
                raise
            return RunCapability(selected_id, token, now, expires_at)

    def put(
        self,
        capability: RunCapability,
        content: bytes,
        *,
        media_type: str,
        ttl_seconds: float | None = None,
    ) -> EncryptedArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        if len(content) > self.policy.max_artifact_bytes:
            raise ArtifactQuotaExceeded("artifact exceeds its per-item byte limit")
        safe_media_type = _validate_media_type(media_type)
        requested_ttl = _validate_ttl(
            self.policy.default_ttl_seconds if ttl_seconds is None else ttl_seconds,
            self.policy,
        )
        master_key = self._master_key()
        with self._lease():
            now = _safe_now(self._clock)
            manifest, keys, _matches = self._decode_manifest(
                capability.run_id,
                master_key,
                allow_counter_repair=False,
            )
            self._verify_capability(capability, manifest, keys)
            self._require_active(manifest, now)
            self._check_run_files(
                capability.run_id,
                manifest,
                clean_orphans=True,
            )
            active_runs, total_plaintext, total_disk = self._active_totals(
                master_key,
                now,
                prune_inactive=True,
            )
            del active_runs
            if int(manifest["artifact_count"]) >= self.policy.max_artifacts_per_run:
                raise ArtifactQuotaExceeded("artifact count quota is exhausted for this run")
            if int(manifest["plaintext_bytes"]) + len(content) > self.policy.max_run_bytes:
                raise ArtifactQuotaExceeded("artifact byte quota is exhausted for this run")
            if total_plaintext + len(content) > self.policy.max_total_bytes:
                raise ArtifactQuotaExceeded("artifact store plaintext quota is exhausted")

            artifact_id = secrets.token_hex(16)
            while self._artifact_path(capability.run_id, artifact_id).exists():
                artifact_id = secrets.token_hex(16)
            expires_at = min(float(manifest["expires_at"]), now + requested_ttl)
            content_id = "hmac-sha256:" + hmac.new(
                keys.identity, content, hashlib.sha256
            ).hexdigest()
            header = {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "run_id": capability.run_id,
                "artifact_id": artifact_id,
                "byte_count": len(content),
                "media_type": safe_media_type,
                "created_at": now,
                "expires_at": expires_at,
                "content_id": content_id,
            }
            header_bytes = _canonical_json_bytes(header)
            if len(header_bytes) > 16 * 1024:
                raise ArtifactIntegrityError("artifact header exceeds its size ceiling")
            plaintext = struct.pack(">I", len(header_bytes)) + header_bytes + content
            aad = _canonical_json_bytes(
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "run_id": capability.run_id,
                    "artifact_id": artifact_id,
                }
            )
            nonce = secrets.token_bytes(12)
            ciphertext = AESGCM(_derive_artifact_key(keys.encryption, artifact_id)).encrypt(
                nonce,
                plaintext,
                aad,
            )
            envelope = _canonical_json_bytes(
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "run_id": capability.run_id,
                    "artifact_id": artifact_id,
                    "nonce": _b64_encode(nonce),
                    "ciphertext": _b64_encode(ciphertext),
                }
            )
            if len(envelope) > self._max_envelope_bytes():
                raise ArtifactQuotaExceeded("encrypted artifact exceeds its disk ceiling")
            if int(manifest["disk_bytes"]) + len(envelope) > self.policy.max_run_disk_bytes:
                raise ArtifactQuotaExceeded("artifact disk quota is exhausted for this run")
            if total_disk + len(envelope) > self.policy.max_total_disk_bytes:
                raise ArtifactQuotaExceeded("artifact store disk quota is exhausted")

            path = self._artifact_path(capability.run_id, artifact_id)
            _publish_new(path, envelope)
            record = {
                "byte_count": len(content),
                "disk_bytes": len(envelope),
                "expires_at": expires_at,
                "content_id": content_id,
            }
            updated = dict(manifest)
            updated.pop("signature", None)
            updated_artifacts = dict(manifest["artifacts"])
            updated_artifacts[artifact_id] = record
            updated["artifacts"] = updated_artifacts
            updated["revision"] = int(manifest["revision"]) + 1
            updated["updated_at"] = now
            self._repair_counters(updated)
            try:
                self._write_manifest(updated, keys)
            except BaseException:
                try:
                    path.unlink()
                    _fsync_directory(path.parent)
                except OSError:
                    pass
                raise
            return EncryptedArtifactRef(
                uri=f"artifact://private/v1/{artifact_id}",
                run_id=capability.run_id,
                artifact_id=artifact_id,
                content_id=content_id,
                byte_count=len(content),
                media_type=safe_media_type,
                expires_at=expires_at,
            )

    def _decrypt_record(
        self,
        run_id: str,
        artifact_id: str,
        record: Mapping[str, Any],
        keys: _RunKeys,
    ) -> tuple[dict[str, Any], bytes]:
        envelope_payload = _read_regular(
            self._artifact_path(run_id, artifact_id),
            max_bytes=self._max_envelope_bytes(),
        )
        if len(envelope_payload) != int(record["disk_bytes"]):
            raise ArtifactIntegrityError("encrypted artifact size changed")
        envelope = _strict_json_loads(envelope_payload)
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema_version",
            "run_id",
            "artifact_id",
            "nonce",
            "ciphertext",
        }:
            raise ArtifactIntegrityError("encrypted artifact envelope is invalid")
        if (
            envelope.get("schema_version") != ARTIFACT_SCHEMA_VERSION
            or envelope.get("run_id") != run_id
            or envelope.get("artifact_id") != artifact_id
        ):
            raise ArtifactIntegrityError("encrypted artifact envelope is misbound")
        nonce = _b64_decode(envelope.get("nonce"), expected_bytes=12)
        ciphertext = _b64_decode(envelope.get("ciphertext"))
        aad = _canonical_json_bytes(
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "run_id": run_id,
                "artifact_id": artifact_id,
            }
        )
        try:
            plaintext = AESGCM(
                _derive_artifact_key(keys.encryption, artifact_id)
            ).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise ArtifactIntegrityError("encrypted artifact authentication failed") from exc
        if len(plaintext) < _INNER_HEADER_BYTES:
            raise ArtifactIntegrityError("encrypted artifact plaintext is truncated")
        header_length = struct.unpack(">I", plaintext[:_INNER_HEADER_BYTES])[0]
        if header_length < 2 or header_length > 16 * 1024:
            raise ArtifactIntegrityError("encrypted artifact header size is invalid")
        content_offset = _INNER_HEADER_BYTES + header_length
        if content_offset > len(plaintext):
            raise ArtifactIntegrityError("encrypted artifact header is truncated")
        header = _strict_json_loads(plaintext[_INNER_HEADER_BYTES:content_offset])
        content = plaintext[content_offset:]
        if not isinstance(header, dict) or set(header) != {
            "schema_version",
            "run_id",
            "artifact_id",
            "byte_count",
            "media_type",
            "created_at",
            "expires_at",
            "content_id",
        }:
            raise ArtifactIntegrityError("encrypted artifact header is invalid")
        byte_count = header.get("byte_count")
        media_type = header.get("media_type")
        created_at = header.get("created_at")
        expires_at = header.get("expires_at")
        content_id = header.get("content_id")
        if (
            header.get("schema_version") != ARTIFACT_SCHEMA_VERSION
            or header.get("run_id") != run_id
            or header.get("artifact_id") != artifact_id
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or len(content) != byte_count
            or record.get("byte_count") != byte_count
            or not isinstance(media_type, str)
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or float(created_at) < 0
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= float(created_at)
            or record.get("expires_at") != expires_at
            or not isinstance(content_id, str)
            or not _CONTENT_ID_RE.fullmatch(content_id)
            or record.get("content_id") != content_id
        ):
            raise ArtifactIntegrityError("encrypted artifact metadata is inconsistent")
        try:
            _validate_media_type(media_type)
        except ValueError as exc:
            raise ArtifactIntegrityError("encrypted artifact media type is invalid") from exc
        calculated = "hmac-sha256:" + hmac.new(
            keys.identity, content, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated, content_id):
            raise ArtifactIntegrityError("encrypted artifact content identity failed")
        return header, content

    def _decrypt(
        self,
        ref: EncryptedArtifactRef,
        record: Mapping[str, Any],
        keys: _RunKeys,
    ) -> bytes:
        header, content = self._decrypt_record(
            ref.run_id,
            ref.artifact_id,
            record,
            keys,
        )
        expected = {
            "byte_count": ref.byte_count,
            "media_type": ref.media_type,
            "expires_at": ref.expires_at,
            "content_id": ref.content_id,
        }
        for name, value in expected.items():
            if header.get(name) != value:
                raise ArtifactIntegrityError("encrypted artifact reference is misbound")
        return content

    def read(self, capability: RunCapability, ref: EncryptedArtifactRef) -> bytes:
        if not isinstance(ref, EncryptedArtifactRef):
            raise TypeError("artifact reference has the wrong type")
        if capability.run_id != ref.run_id:
            raise ArtifactAccessDenied("artifact reference belongs to a different run")
        master_key = self._master_key()
        with self._lease():
            now = _safe_now(self._clock)
            manifest, keys, _matches = self._decode_manifest(
                capability.run_id,
                master_key,
                allow_counter_repair=False,
            )
            self._verify_capability(capability, manifest, keys)
            self._require_active(manifest, now)
            if now >= float(ref.expires_at):
                raise ArtifactAccessDenied("artifact is expired")
            record = manifest["artifacts"].get(ref.artifact_id)
            if not isinstance(record, dict):
                raise ArtifactAccessDenied("artifact is not granted to this run")
            return self._decrypt(ref, record, keys)

    def revoke_run(self, capability: RunCapability) -> RevocationResult:
        master_key = self._master_key()
        with self._lease():
            now = _safe_now(self._clock)
            manifest, keys, _matches = self._decode_manifest(
                capability.run_id,
                master_key,
                allow_counter_repair=False,
            )
            self._verify_capability(capability, manifest, keys)
            if now < float(manifest["updated_at"]):
                raise ArtifactAccessDenied("artifact clock moved backwards")
            already_revoked = manifest["status"] == "revoked"
            if not already_revoked:
                updated = dict(manifest)
                updated.pop("signature", None)
                updated["status"] = "revoked"
                updated["revision"] = int(manifest["revision"]) + 1
                updated["updated_at"] = now
                self._write_manifest(updated, keys)
                manifest = updated
            deleted = 0
            pending = 0
            for artifact_id in manifest["artifacts"]:
                path = self._artifact_path(capability.run_id, artifact_id)
                try:
                    _ensure_regular_or_missing(path)
                    if path.exists():
                        path.unlink()
                        deleted += 1
                except (OSError, ArtifactStoreError):
                    pending += 1
            _fsync_directory(self._run_dir(capability.run_id) / "artifacts")
            return RevocationResult(already_revoked, deleted, pending)

    def cleanup(self) -> CleanupResult:
        master_key = self._master_key()
        with self._lease():
            now = _safe_now(self._clock)
            scanned = 0
            active_runs = 0
            deleted_runs = 0
            deleted_orphans = 0
            deleted_temporary = 0
            repaired = 0
            corrupt = 0
            active_artifacts = 0
            active_plaintext = 0
            active_disk = 0
            for child in list(self._runs.iterdir()):
                if child.name.startswith(".create-"):
                    deleted_temporary += _remove_tree_no_follow(child)
                    continue
                scanned += 1
                try:
                    info = child.lstat()
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        raise UnsafeArtifactPath("artifact run path is unsafe")
                    if not _RUN_ID_RE.fullmatch(child.name):
                        raise UnsafeArtifactPath("artifact run directory name is invalid")
                    manifest, keys, counters_match = self._decode_manifest(
                        child.name,
                        master_key,
                        allow_counter_repair=True,
                    )
                    if now < float(manifest["updated_at"]):
                        raise ArtifactIntegrityError("artifact clock moved backwards")
                    if manifest["status"] == "revoked" or now >= float(manifest["expires_at"]):
                        _remove_tree_no_follow(child)
                        deleted_runs += 1
                        continue
                    orphan_count, temporary_count = self._check_run_files(
                        child.name,
                        manifest,
                        clean_orphans=True,
                    )
                    deleted_orphans += orphan_count
                    deleted_temporary += temporary_count
                    for artifact_id, record in manifest["artifacts"].items():
                        self._decrypt_record(
                            child.name,
                            artifact_id,
                            record,
                            keys,
                        )
                    if not counters_match:
                        repaired_manifest = dict(manifest)
                        repaired_manifest.pop("signature", None)
                        self._repair_counters(repaired_manifest)
                        repaired_manifest["revision"] = int(manifest["revision"]) + 1
                        repaired_manifest["updated_at"] = now
                        self._write_manifest(repaired_manifest, keys)
                        manifest = repaired_manifest
                        repaired += 1
                    active_runs += 1
                    active_artifacts += int(manifest["artifact_count"])
                    active_plaintext += int(manifest["plaintext_bytes"])
                    active_disk += int(manifest["disk_bytes"])
                except (OSError, ArtifactStoreError, ValueError, TypeError):
                    corrupt += 1
            quota_satisfied = (
                active_runs <= self.policy.max_runs
                and active_plaintext <= self.policy.max_total_bytes
                and active_disk <= self.policy.max_total_disk_bytes
            )
            return CleanupResult(
                scanned,
                active_runs,
                deleted_runs,
                deleted_orphans,
                deleted_temporary,
                repaired,
                corrupt,
                active_artifacts,
                active_plaintext,
                active_disk,
                quota_satisfied,
            )

    def readiness(self) -> dict[str, Any]:
        """Return content-free structural readiness without mutating stored data."""

        try:
            master_key = self._master_key()
            with self._lease():
                now = _safe_now(self._clock)
                active_runs, plaintext, disk = self._active_totals(
                    master_key,
                    now,
                    prune_inactive=False,
                )
            return {
                "status": "ready",
                "key_source": "persistent",
                "active_runs": active_runs,
                "active_plaintext_bytes": plaintext,
                "active_disk_bytes": disk,
                "secure_erasure": False,
            }
        except Exception as exc:
            return {
                "status": "not_ready",
                "error_class": type(exc).__name__,
                "secure_erasure": False,
            }


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "MASTER_KEY_LABEL",
    "ArtifactAccessDenied",
    "ArtifactIntegrityError",
    "ArtifactPolicy",
    "ArtifactQuotaExceeded",
    "ArtifactStoreError",
    "CleanupResult",
    "EncryptedArtifactRef",
    "EncryptedArtifactStore",
    "RevocationResult",
    "RunCapability",
    "UnsafeArtifactPath",
]
