"""User-scoped, fail-closed finalization for an installed Austin app.

The signed installer places only the notarized application in /Applications.
This explicit current-user step creates the inert LaunchAgent definition and
stable-Chrome native-host manifest, then publishes signed Ada install evidence.
It never loads the agent, grants TCC, pairs a browser, or enables control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import selectors
import stat
import subprocess
import time
from typing import Callable, NoReturn
import uuid

from .ada_credential_registry import (
    AdaCredentialRegistryError,
    AdaNativeCredentialEnumeration,
)
from .david_control_kernel import MAX_SAFE_INTEGER, ControlSigner, canonical_json_bytes
from .grace_key_store import KeyStoreError, KeyringKeyStore, load_control_signer
from .oliver_control_installation import (
    AUSTIN_CREDENTIAL_MIGRATOR_EXECUTABLE,
    NEON_ALLOWED_ORIGIN_RESOURCE,
    OliverCredentialStore,
    OliverInstallRoots,
    OliverUninstallRejected,
    expected_austin_launch_agent,
    expected_neon_native_host,
)
from .oliver_control_installer import (
    OliverInstallEvidencePaths,
    OliverInstallEvidencePublication,
    OliverInstallIdentityProbe,
    OliverMacOSReleaseIdentityProbe,
    capture_and_publish_oliver_install_evidence,
)


_EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")
_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
_MAX_ORIGIN_BYTES = 64
_MAX_CREDENTIAL_ENUMERATION_BYTES = 64 * 1024
_AUSTIN_CREDENTIAL_MIGRATOR_IDENTIFIER = (
    "com.algo-cli.austin.credential-migrator"
)


class AustinInstallRejected(RuntimeError):
    """A content-free pre-install or finalization rejection."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "austin_install_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise AustinInstallRejected(reason_code)


def _credential_migrator_requirement(team_id: str) -> tuple[str, str]:
    if type(team_id) is not str or _TEAM_ID_RE.fullmatch(team_id) is None:
        _reject("austin_install_team")
    requirement = (
        "designated => anchor apple generic and "
        "certificate leaf[field.1.2.840.113635.100.6.1.13] exists and "
        f'certificate leaf[subject.OU] = "{team_id}" and '
        f'identifier "{_AUSTIN_CREDENTIAL_MIGRATOR_IDENTIFIER}"'
    )
    digest = "sha256:" + hashlib.sha256(requirement.encode("utf-8")).hexdigest()
    return requirement, digest


class AustinCredentialEnumerationRunner:
    """Invoke the exact signed helper over one bounded nonce-bound pipe."""

    def __init__(
        self,
        *,
        identity_probe: OliverInstallIdentityProbe,
        clock_ms: Callable[[], int] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not callable(getattr(identity_probe, "verify", None)):
            _reject("austin_install_credential_enumerator")
        if not 0 < float(timeout_seconds) <= 60:
            _reject("austin_install_credential_enumerator")
        self._identity_probe = identity_probe
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _run_bounded(executable: Path, request: bytes, *, timeout_seconds: float) -> bytes:
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        try:
            process = subprocess.Popen(
                (str(executable),),
                cwd=Path.home(),
                env={
                    "HOME": str(Path.home()),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if process.stdin is None or process.stdout is None:
                _reject("austin_install_credential_enumerator")
            process.stdin.write(request)
            process.stdin.close()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout_seconds
            output = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _reject("austin_install_credential_enumerator")
                events = selector.select(min(remaining, 0.25))
                if not events:
                    if process.poll() is not None:
                        events = selector.select(0)
                        if not events:
                            break
                    else:
                        continue
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
                if not chunk:
                    break
                if len(output) + len(chunk) > _MAX_CREDENTIAL_ENUMERATION_BYTES:
                    _reject("austin_install_credential_enumerator")
                output.extend(chunk)
            return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if return_code != 0:
                _reject("austin_install_credential_enumerator")
            return bytes(output)
        except AustinInstallRejected:
            raise
        except (OSError, subprocess.SubprocessError):
            _reject("austin_install_credential_enumerator")
        finally:
            if selector is not None:
                selector.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.SubprocessError:
                    pass
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()

    def enumerate(
        self,
        *,
        roots: OliverInstallRoots,
        team_id: str,
        nonce: str,
    ) -> AdaNativeCredentialEnumeration:
        if (
            not roots.production
            or roots != OliverInstallRoots.for_current_user()
            or type(nonce) is not str
            or re.fullmatch(r"[0-9a-f]{64}", nonce) is None
        ):
            _reject("austin_install_credential_enumerator")
        executable = (
            roots.app_bundle
            / "Contents"
            / "Helpers"
            / AUSTIN_CREDENTIAL_MIGRATOR_EXECUTABLE
        )
        self._identity_probe.verify(roots=roots, team_id=team_id)
        request = canonical_json_bytes({"nonce": nonce, "protocol_version": 1})
        payload = self._run_bounded(
            executable,
            request,
            timeout_seconds=self._timeout_seconds,
        )
        self._identity_probe.verify(roots=roots, team_id=team_id)
        try:
            evidence = AdaNativeCredentialEnumeration.from_bytes(payload)
            _requirement, requirement_digest = _credential_migrator_requirement(team_id)
            evidence.verify_context(
                expected_service="algo-cli-runtime",
                expected_nonce=nonce,
                expected_code_identifier=_AUSTIN_CREDENTIAL_MIGRATOR_IDENTIFIER,
                expected_team_id=team_id,
                expected_designated_requirement_digest=requirement_digest,
                now_ms=self._clock_ms(),
            )
            return evidence
        except AdaCredentialRegistryError as exc:
            raise AustinInstallRejected(exc.reason_code) from exc


def _assert_user_path(path: Path, *, roots: OliverInstallRoots) -> None:
    try:
        path.relative_to(roots.home)
    except ValueError:
        _reject("austin_install_path")
    current = roots.home
    try:
        home_stat = current.lstat()
    except OSError:
        _reject("austin_install_home")
    if (
        stat.S_ISLNK(home_stat.st_mode)
        or not stat.S_ISDIR(home_stat.st_mode)
        or home_stat.st_uid != roots.uid
        or home_stat.st_mode & 0o022
    ):
        _reject("austin_install_home")
    for component in path.relative_to(roots.home).parts:
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            _reject("austin_install_directory")
        try:
            value = current.lstat()
        except OSError:
            _reject("austin_install_directory")
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != roots.uid
            or value.st_mode & 0o022
        ):
            _reject("austin_install_directory")


def _read_exact_at(
    directory_fd: int,
    name: str,
    *,
    uid: int,
    mode: int,
    maximum: int,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError:
        _reject("austin_install_file")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != uid
            or stat.S_IMODE(before.st_mode) != mode
            or not 1 <= before.st_size <= maximum
        ):
            _reject("austin_install_file")
        remaining = before.st_size
        payload = bytearray()
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                _reject("austin_install_file")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_install_file")
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
            _reject("austin_install_file")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _create_exact_file(
    path: Path,
    payload: bytes,
    *,
    roots: OliverInstallRoots,
    mode: int = 0o600,
) -> str:
    if type(payload) is not bytes or not 1 <= len(payload) <= 65_536:
        _reject("austin_install_payload")
    _assert_user_path(path.parent, roots=roots)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path.parent, flags)
    except OSError:
        _reject("austin_install_directory")
    temporary = f".AdaAustinInstall.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        existing = _read_exact_at(
            directory_fd,
            path.name,
            uid=roots.uid,
            mode=mode,
            maximum=65_536,
        )
        if existing is not None:
            if existing != payload:
                _reject("austin_install_existing_mismatch")
            return "unchanged"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                _reject("austin_install_write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_exact_at(
                directory_fd,
                path.name,
                uid=roots.uid,
                mode=mode,
                maximum=65_536,
            )
            if existing != payload:
                _reject("austin_install_existing_mismatch")
            return "unchanged"
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
        confirmed = _read_exact_at(
            directory_fd,
            path.name,
            uid=roots.uid,
            mode=mode,
            maximum=65_536,
        )
        if confirmed != payload:
            _reject("austin_install_confirmation")
        return "created"
    except AustinInstallRejected:
        raise
    except OSError:
        _reject("austin_install_write")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            _reject("austin_install_cleanup")
        os.close(directory_fd)


def _sealed_extension_origin(roots: OliverInstallRoots) -> str:
    path = (
        roots.app_bundle
        / "Contents"
        / "Resources"
        / NEON_ALLOWED_ORIGIN_RESOURCE
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _reject("austin_install_origin")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, roots.uid}
            or before.st_mode & 0o022
            or not 1 <= before.st_size <= _MAX_ORIGIN_BYTES
        ):
            _reject("austin_install_origin")
        remaining = before.st_size
        chunks = bytearray()
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                _reject("austin_install_origin")
            chunks.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_install_origin")
        payload = bytes(chunks)
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
            _reject("austin_install_origin")
    finally:
        os.close(descriptor)
    try:
        origin = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject("austin_install_origin")
    if _EXTENSION_ORIGIN_RE.fullmatch(origin) is None:
        _reject("austin_install_origin")
    return origin


def _install_user_surfaces(
    roots: OliverInstallRoots,
    *,
    extension_origin: str,
) -> tuple[str, str]:
    launch_payload = plistlib.dumps(
        expected_austin_launch_agent(roots),
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )
    host_payload = canonical_json_bytes(
        expected_neon_native_host(roots, extension_origin=extension_origin)
    )
    launch_status = _create_exact_file(
        roots.launch_agent,
        launch_payload,
        roots=roots,
    )
    host_status = _create_exact_file(
        roots.chrome_native_host,
        host_payload,
        roots=roots,
    )
    return launch_status, host_status


def finalize_austin_install(
    *,
    roots: OliverInstallRoots,
    evidence_paths: OliverInstallEvidencePaths,
    signer: ControlSigner,
    credential_store: OliverCredentialStore,
    team_id: str,
    identity_probe: OliverInstallIdentityProbe | None = None,
    clock_ms: Callable[[], int] | None = None,
    install_id: str | None = None,
    allow_test_paths: bool = False,
) -> OliverInstallEvidencePublication:
    """Finalize inert user surfaces and publish a signed install inventory."""

    if type(team_id) is not str or _TEAM_ID_RE.fullmatch(team_id) is None:
        _reject("austin_install_team")
    if not allow_test_paths and (
        not roots.production
        or not evidence_paths.production
        or roots != OliverInstallRoots.for_current_user()
        or evidence_paths != OliverInstallEvidencePaths.for_current_user()
        or (hasattr(os, "geteuid") and os.geteuid() == 0)
    ):
        _reject("austin_install_context")
    selected_clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
    now_ms = selected_clock()
    if type(now_ms) is not int or not 0 <= now_ms <= MAX_SAFE_INTEGER:
        _reject("austin_install_clock")
    selected_install_id = install_id or str(uuid.uuid4())
    probe = identity_probe or OliverMacOSReleaseIdentityProbe()
    try:
        probe.verify(roots=roots, team_id=team_id)
        origin = _sealed_extension_origin(roots)
        labels = credential_store.complete_inventory_labels()
        if labels is None:
            _reject("austin_install_credential_registry")
        _install_user_surfaces(roots, extension_origin=origin)
        _inventory, publication = capture_and_publish_oliver_install_evidence(
            roots=roots,
            paths=evidence_paths,
            signer=signer,
            team_id=team_id,
            extension_origin=origin,
            installed_at_ms=now_ms,
            install_id=selected_install_id,
            credential_store=credential_store,
            credential_labels=labels,
            credential_inventory_complete=True,
            identity_probe=probe,
            allow_test_roots=allow_test_paths,
            allow_test_paths=allow_test_paths,
        )
        return publication
    except AustinInstallRejected:
        raise
    except OliverUninstallRejected as exc:
        raise AustinInstallRejected(exc.reason_code) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-id", required=True)
    args = parser.parse_args(argv)
    try:
        roots = OliverInstallRoots.for_current_user()
        evidence_paths = OliverInstallEvidencePaths.for_current_user()
        probe = OliverMacOSReleaseIdentityProbe()
        probe.verify(roots=roots, team_id=args.team_id)
        store = KeyringKeyStore()
        if store.complete_inventory_snapshot() is None:
            nonce = secrets.token_hex(32)
            enumeration = AustinCredentialEnumerationRunner(
                identity_probe=probe
            ).enumerate(
                roots=roots,
                team_id=args.team_id,
                nonce=nonce,
            )
            _requirement, requirement_digest = _credential_migrator_requirement(
                args.team_id
            )
            store.initialize_from_native_credential_enumeration(
                enumeration,
                expected_nonce=nonce,
                expected_code_identifier=_AUSTIN_CREDENTIAL_MIGRATOR_IDENTIFIER,
                expected_team_id=args.team_id,
                expected_designated_requirement_digest=requirement_digest,
            )
        signer = load_control_signer(store=store)
        publication = finalize_austin_install(
            roots=roots,
            evidence_paths=evidence_paths,
            signer=signer,
            credential_store=store,
            team_id=args.team_id,
            identity_probe=probe,
        )
    except (AustinInstallRejected, OliverUninstallRejected) as exc:
        reason = getattr(exc, "reason_code", "austin_install_failed")
        print(
            json.dumps(
                {"reason_code": reason, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    except KeyStoreError as exc:
        candidate = str(exc)
        reason = (
            candidate
            if re.fullmatch(r"credential_[a-z0-9_]{1,86}", candidate)
            else "credential_registry_unavailable"
        )
        print(
            json.dumps(
                {"reason_code": reason, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "reason_code": f"austin_install_{type(exc).__name__.lower()}",
                    "status": "blocked",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(publication.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AustinCredentialEnumerationRunner",
    "AustinInstallRejected",
    "finalize_austin_install",
    "main",
]
