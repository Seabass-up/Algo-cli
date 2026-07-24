"""Fail-closed install-evidence publication for native control surfaces.

This module does not copy, activate, sign, or notarize an application. The
explicit current-user Austin finalizer calls it only after the exact production
surfaces are present and a release-identity probe has verified them.
Publication is single-writer and crash-safe: the per-user inventory authority
key is immutable once a valid inventory exists, and the signed inventory
advances with one atomic rename.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import time
from typing import Callable, Iterable, NoReturn, Protocol
import uuid

from .arthur_control_doctor import has_hardened_runtime
from .david_control_kernel import ControlSigner
from .oliver_control_installation import (
    AUSTIN_APP_BUNDLE_ID,
    AUSTIN_APP_EXECUTABLE,
    AUSTIN_CREDENTIAL_MIGRATOR_EXECUTABLE,
    MAX_INVENTORY_BYTES,
    NEON_NATIVE_HOST_EXECUTABLE,
    OliverCredentialStore,
    OliverInstallInventory,
    OliverInstallRoots,
    OliverUninstallRejected,
    capture_oliver_install_inventory,
)


ADA_INSTALL_STATE_DIRECTORY = "Algo CLI Control"
ADA_INSTALL_INVENTORY_FILENAME = "AdaInstallInventory.json"
ADA_INSTALL_AUTHORITY_FILENAME = "AdaInstallAuthority.bin"
ADA_INSTALL_LOCK_FILENAME = "AdaInstallEvidence.lock"

_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
_MAX_COMMAND_OUTPUT_BYTES = 128 * 1024
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.01


def _reject(reason_code: str) -> NoReturn:
    raise OliverUninstallRejected(reason_code)


def _installed_version_key(
    inventory: OliverInstallInventory,
) -> tuple[tuple[int, int, int], int]:
    version = tuple(int(part) for part in inventory.app_version.split("."))
    if len(version) != 3:
        _reject("install_evidence_version")
    return (version[0], version[1], version[2]), int(inventory.app_build_number)


@dataclass(frozen=True, slots=True)
class OliverInstallEvidencePaths:
    directory: Path
    inventory: Path
    authority: Path
    lock: Path
    uid: int
    production: bool

    @classmethod
    def for_current_user(cls) -> "OliverInstallEvidencePaths":
        uid = os.getuid() if hasattr(os, "getuid") else -1
        directory = (
            Path.home()
            / "Library"
            / "Application Support"
            / ADA_INSTALL_STATE_DIRECTORY
        )
        return cls(
            directory=directory,
            inventory=directory / ADA_INSTALL_INVENTORY_FILENAME,
            authority=directory / ADA_INSTALL_AUTHORITY_FILENAME,
            lock=directory / ADA_INSTALL_LOCK_FILENAME,
            uid=uid,
            production=True,
        )

    @classmethod
    def _for_test(cls, directory: Path, *, uid: int) -> "OliverInstallEvidencePaths":
        selected = directory.absolute()
        return cls(
            directory=selected,
            inventory=selected / ADA_INSTALL_INVENTORY_FILENAME,
            authority=selected / ADA_INSTALL_AUTHORITY_FILENAME,
            lock=selected / ADA_INSTALL_LOCK_FILENAME,
            uid=uid,
            production=False,
        )


@dataclass(frozen=True, slots=True)
class OliverInstallEvidencePublication:
    status: str
    inventory_digest: str
    authority_key_id: str
    replaced_previous_inventory: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "authority_key_id": self.authority_key_id,
            "inventory_digest": self.inventory_digest,
            "replaced_previous_inventory": self.replaced_previous_inventory,
            "status": self.status,
        }


class OliverInstallIdentityProbe(Protocol):
    def verify(self, *, roots: OliverInstallRoots, team_id: str) -> None: ...


class OliverMacOSReleaseIdentityProbe:
    """Verify exact Developer ID, runtime, entitlements, Gatekeeper, and staple."""

    _IDENTITIES = {
        "bundle": ("com.algo-cli.austin.control", "sandboxed"),
        "app": ("com.algo-cli.austin.control", "sandboxed"),
        "relay": ("com.algo-cli.austin.relay", "sandboxed"),
        "adapter": ("com.algo-cli.austin.tcc-adapter", "adapter"),
        "credential_migrator": (
            "com.algo-cli.austin.credential-migrator",
            "empty",
        ),
        "neon": ("com.algo-cli.neon.host", "empty"),
    }

    def __init__(self, *, timeout_seconds: float = 20.0) -> None:
        if not 0 < float(timeout_seconds) <= 60:
            raise ValueError("identity probe timeout must be between zero and 60 seconds")
        self._timeout_seconds = float(timeout_seconds)

    def _run(self, command: tuple[str, ...]) -> str:
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self._timeout_seconds,
                env={
                    "HOME": str(Path.home()),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            _reject("install_identity_command")
        output = completed.stdout[:_MAX_COMMAND_OUTPUT_BYTES].decode(
            "utf-8", errors="replace"
        )
        if completed.returncode != 0 or len(completed.stdout) > _MAX_COMMAND_OUTPUT_BYTES:
            _reject("install_identity_rejected")
        return output

    def _entitlements(self, path: Path) -> dict[str, object]:
        output = self._run(
            ("/usr/bin/codesign", "-d", "--entitlements", ":-", str(path))
        )
        start = output.find("<?xml")
        if start < 0:
            start = output.find("<plist")
        if start < 0:
            return {}
        try:
            value = plistlib.loads(output[start:].encode("utf-8"))
        except plistlib.InvalidFileException:
            _reject("install_entitlements")
        if type(value) is not dict:
            _reject("install_entitlements")
        return value

    @staticmethod
    def _verify_production_path(label: str, path: Path) -> None:
        try:
            value = path.lstat()
        except OSError:
            _reject("install_identity_path")
        expected_type = stat.S_ISDIR if label == "bundle" else stat.S_ISREG
        if (
            stat.S_ISLNK(value.st_mode)
            or not expected_type(value.st_mode)
            or value.st_uid != 0
            or value.st_mode & 0o022
            or (label != "bundle" and value.st_nlink != 1)
        ):
            _reject("install_identity_path")

    def verify(self, *, roots: OliverInstallRoots, team_id: str) -> None:
        if sys.platform != "darwin" or not roots.production:
            _reject("install_identity_platform")
        if type(team_id) is not str or not _TEAM_ID_RE.fullmatch(team_id):
            _reject("install_team_id")
        paths = {
            "bundle": roots.app_bundle,
            "app": roots.app_bundle / "Contents" / "MacOS" / AUSTIN_APP_EXECUTABLE,
            "relay": roots.app_bundle / "Contents" / "Helpers" / "austin-relay",
            "adapter": roots.app_bundle / "Contents" / "Helpers" / "austin-tcc-adapter",
            "credential_migrator": roots.app_bundle
            / "Contents"
            / "Helpers"
            / AUSTIN_CREDENTIAL_MIGRATOR_EXECUTABLE,
            "neon": roots.app_bundle
            / "Contents"
            / "Helpers"
            / NEON_NATIVE_HOST_EXECUTABLE,
        }
        self._run(
            (
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                "--verbose=4",
                str(roots.app_bundle),
            )
        )
        expected_sandboxed: dict[str, object] = {
            "com.apple.security.app-sandbox": True,
            "com.apple.security.application-groups": ["group.com.algo-cli.control"],
        }
        expected_entitlements: dict[str, dict[str, object]] = {
            "sandboxed": expected_sandboxed,
            "adapter": {"com.apple.security.automation.apple-events": True},
            "empty": {},
        }
        for label, path in paths.items():
            identifier, entitlement_kind = self._IDENTITIES[label]
            self._verify_production_path(label, path)
            self._run(
                (
                    "/usr/bin/codesign",
                    "--verify",
                    "--strict",
                    "--verbose=4",
                    str(path),
                )
            )
            details = self._run(
                ("/usr/bin/codesign", "-d", "--verbose=4", str(path))
            )
            if (
                f"Identifier={identifier}" not in details
                or f"TeamIdentifier={team_id}" not in details
                or "Authority=Developer ID Application:" not in details
                or "Authority=Developer ID Certification Authority" not in details
                or "Authority=Apple Root CA" not in details
                or not has_hardened_runtime(details)
            ):
                _reject("install_identity_mismatch")
            requirement = (
                "designated => anchor apple generic and "
                "certificate leaf[field.1.2.840.113635.100.6.1.13] exists and "
                f'certificate leaf[subject.OU] = "{team_id}" and identifier "{identifier}"'
            )
            self._run(
                (
                    "/usr/bin/codesign",
                    "--verify",
                    "--strict",
                    f"-R={requirement}",
                    str(path),
                )
            )
            if self._entitlements(path) != expected_entitlements[entitlement_kind]:
                _reject("install_entitlements")
        self._run(
            (
                "/usr/sbin/spctl",
                "--assess",
                "--type",
                "execute",
                str(roots.app_bundle),
            )
        )
        self._run(
            (
                "/usr/bin/xcrun",
                "stapler",
                "validate",
                str(roots.app_bundle),
            )
        )


def _validate_paths(
    paths: OliverInstallEvidencePaths, *, allow_test_paths: bool
) -> None:
    if type(paths) is not OliverInstallEvidencePaths or os.name != "posix":
        _reject("install_evidence_paths")
    if paths.production:
        if paths != OliverInstallEvidencePaths.for_current_user():
            _reject("install_evidence_paths")
    elif not allow_test_paths:
        _reject("install_evidence_test_paths")
    expected = {
        paths.directory / ADA_INSTALL_INVENTORY_FILENAME,
        paths.directory / ADA_INSTALL_AUTHORITY_FILENAME,
        paths.directory / ADA_INSTALL_LOCK_FILENAME,
    }
    if (
        paths.uid < 0
        or not paths.directory.is_absolute()
        or ".." in paths.directory.parts
        or {paths.inventory, paths.authority, paths.lock} != expected
    ):
        _reject("install_evidence_paths")


def _assert_safe_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current = current / component
        try:
            value = current.lstat()
        except FileNotFoundError:
            _reject("install_evidence_parent")
        except OSError:
            _reject("install_evidence_parent")
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            _reject("install_evidence_parent")


def _open_evidence_directory(paths: OliverInstallEvidencePaths) -> int:
    _assert_safe_ancestors(paths.directory)
    try:
        paths.directory.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        pass
    except OSError:
        _reject("install_evidence_directory")
    try:
        before = paths.directory.lstat()
    except OSError:
        _reject("install_evidence_directory")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or before.st_uid != paths.uid
        or stat.S_IMODE(before.st_mode) != 0o700
    ):
        _reject("install_evidence_directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(paths.directory, flags)
    except OSError:
        _reject("install_evidence_directory")
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(descriptor)
        _reject("install_evidence_directory")
    return descriptor


def _read_at(
    directory_fd: int,
    name: str,
    *,
    uid: int,
    maximum: int,
    expected_mode: int,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError:
        _reject("install_evidence_file")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or not 1 <= before.st_size <= maximum
        ):
            _reject("install_evidence_file")
        remaining = before.st_size
        chunks = bytearray()
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                _reject("install_evidence_file")
            chunks.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("install_evidence_file")
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
            _reject("install_evidence_file")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _lock(directory_fd: int, *, uid: int) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            ADA_INSTALL_LOCK_FILENAME,
            flags,
            0o600,
            dir_fd=directory_fd,
        )
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != uid
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            _reject("install_evidence_lock")
        import fcntl

        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    _reject("install_evidence_busy")
                time.sleep(_LOCK_RETRY_SECONDS)
        return descriptor
    except OliverUninstallRejected:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError:
        _reject("install_evidence_lock")


def _unlock(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _atomic_replace_at(
    directory_fd: int,
    *,
    final_name: str,
    payload: bytes,
    mode: int,
) -> None:
    temporary_name = f".AdaInstallEvidence.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                _reject("install_evidence_write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except OliverUninstallRejected:
        raise
    except OSError:
        _reject("install_evidence_write")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            _reject("install_evidence_cleanup")


def publish_oliver_install_evidence(
    *,
    inventory: OliverInstallInventory,
    signer: ControlSigner,
    paths: OliverInstallEvidencePaths,
    allow_test_paths: bool = False,
    fault_hook: Callable[[str], None] | None = None,
) -> OliverInstallEvidencePublication:
    """Publish a verified inventory and immutable authority under one file lock."""

    _validate_paths(paths, allow_test_paths=allow_test_paths)
    if type(inventory) is not OliverInstallInventory or type(signer) is not ControlSigner:
        _reject("install_evidence_input")
    inventory.verify(signer.verifier)
    if inventory.user_uid != paths.uid:
        _reject("install_evidence_user")
    if fault_hook is not None and not callable(fault_hook):
        _reject("install_evidence_hook")
    payload = inventory.to_bytes()
    authority = signer.verifier.public_bytes
    directory_fd = _open_evidence_directory(paths)
    lock_fd = -1
    try:
        lock_fd = _lock(directory_fd, uid=paths.uid)
        existing_inventory_payload = _read_at(
            directory_fd,
            ADA_INSTALL_INVENTORY_FILENAME,
            uid=paths.uid,
            maximum=MAX_INVENTORY_BYTES,
            expected_mode=0o600,
        )
        existing_authority = _read_at(
            directory_fd,
            ADA_INSTALL_AUTHORITY_FILENAME,
            uid=paths.uid,
            maximum=32,
            expected_mode=0o444,
        )
        if existing_authority is None or not hmac.compare_digest(
            existing_authority, authority
        ):
            if existing_inventory_payload is not None:
                _reject("install_evidence_authority_changed")
            _atomic_replace_at(
                directory_fd,
                final_name=ADA_INSTALL_AUTHORITY_FILENAME,
                payload=authority,
                mode=0o444,
            )
        if fault_hook is not None:
            fault_hook("authority_committed")

        replaced = False
        if existing_inventory_payload is not None:
            try:
                existing = OliverInstallInventory.from_bytes(existing_inventory_payload)
                existing.verify(signer.verifier)
            except OliverUninstallRejected:
                _reject("install_evidence_inventory_invalid")
            if hmac.compare_digest(existing_inventory_payload, payload):
                return OliverInstallEvidencePublication(
                    status="unchanged",
                    inventory_digest=inventory.digest,
                    authority_key_id=inventory.authority_key_id,
                    replaced_previous_inventory=False,
                )
            if (
                existing.app_bundle_id != AUSTIN_APP_BUNDLE_ID
                or existing.authority_key_id != inventory.authority_key_id
                or existing.team_id != inventory.team_id
                or existing.extension_origin != inventory.extension_origin
                or existing.user_uid != inventory.user_uid
            ):
                _reject("install_evidence_identity_changed")
            incoming_version, incoming_build = _installed_version_key(inventory)
            existing_version, existing_build = _installed_version_key(existing)
            if incoming_version < existing_version or incoming_build < existing_build:
                _reject("install_evidence_version_rollback")
            if (
                inventory.install_id == existing.install_id
                or inventory.installed_at_ms <= existing.installed_at_ms
            ):
                _reject("install_evidence_stale")
            replaced = True

        _atomic_replace_at(
            directory_fd,
            final_name=ADA_INSTALL_INVENTORY_FILENAME,
            payload=payload,
            mode=0o600,
        )
        if fault_hook is not None:
            fault_hook("inventory_committed")
        confirmed = _read_at(
            directory_fd,
            ADA_INSTALL_INVENTORY_FILENAME,
            uid=paths.uid,
            maximum=MAX_INVENTORY_BYTES,
            expected_mode=0o600,
        )
        if confirmed is None or not hmac.compare_digest(confirmed, payload):
            _reject("install_evidence_confirmation")
        return OliverInstallEvidencePublication(
            status="published",
            inventory_digest=inventory.digest,
            authority_key_id=inventory.authority_key_id,
            replaced_previous_inventory=replaced,
        )
    finally:
        if lock_fd >= 0:
            _unlock(lock_fd)
        os.close(directory_fd)


def capture_and_publish_oliver_install_evidence(
    *,
    roots: OliverInstallRoots,
    paths: OliverInstallEvidencePaths,
    signer: ControlSigner,
    team_id: str,
    extension_origin: str,
    installed_at_ms: int,
    install_id: str,
    credential_store: OliverCredentialStore,
    credential_labels: Iterable[str],
    credential_inventory_complete: bool,
    identity_probe: OliverInstallIdentityProbe | None = None,
    allow_test_roots: bool = False,
    allow_test_paths: bool = False,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[OliverInstallInventory, OliverInstallEvidencePublication]:
    """Verify identity, capture the live tree, and publish its signed evidence."""

    probe = identity_probe or OliverMacOSReleaseIdentityProbe()
    probe.verify(roots=roots, team_id=team_id)
    inventory = capture_oliver_install_inventory(
        roots=roots,
        signer=signer,
        team_id=team_id,
        extension_origin=extension_origin,
        installed_at_ms=installed_at_ms,
        install_id=install_id,
        credential_store=credential_store,
        credential_labels=credential_labels,
        credential_inventory_complete=credential_inventory_complete,
        allow_test_roots=allow_test_roots,
    )
    publication = publish_oliver_install_evidence(
        inventory=inventory,
        signer=signer,
        paths=paths,
        allow_test_paths=allow_test_paths,
        fault_hook=fault_hook,
    )
    return inventory, publication


__all__ = [
    "ADA_INSTALL_AUTHORITY_FILENAME",
    "ADA_INSTALL_INVENTORY_FILENAME",
    "ADA_INSTALL_LOCK_FILENAME",
    "ADA_INSTALL_STATE_DIRECTORY",
    "OliverInstallEvidencePaths",
    "OliverInstallEvidencePublication",
    "OliverInstallIdentityProbe",
    "OliverMacOSReleaseIdentityProbe",
    "capture_and_publish_oliver_install_evidence",
    "publish_oliver_install_evidence",
]
