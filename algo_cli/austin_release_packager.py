"""Fail-closed Developer ID packaging and notarization for Austin.

The packager performs two notarization rounds: first the signed application,
then the signed flat installer that contains the already-stapled application.
It never accepts raw notarization credentials, never invokes a shell, never
overwrites an output directory, and emits only structural release evidence.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, NoReturn, Protocol
import uuid

from . import __version__
from .arthur_control_doctor import has_hardened_runtime
from .david_control_kernel import MAX_SAFE_INTEGER, canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
AUSTIN_STAGE_APP = ROOT / "native" / "austin" / ".build" / "austin-stage" / "Algo CLI Control.app"
AUSTIN_BUILD_SCRIPT = ROOT / "script" / "austin_build_and_run.sh"
AUSTIN_AUDIT_SCRIPT = ROOT / "scripts" / "austin_native_package_audit.py"
AUSTIN_BUNDLE_ID = "com.algo-cli.austin.control"
AUSTIN_PACKAGE_ID = "com.algo-cli.austin.control.pkg"
ADA_RELEASE_EVIDENCE_FILENAME = "AdaAustinReleaseEvidence.json"

_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_BUILD_RE = re.compile(r"^[1-9][0-9]{0,8}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,127}$")
_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUBMISSION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
_MAX_NOTARY_LOG_BYTES = 4 * 1024 * 1024
_MAX_RELEASE_ARTIFACT_BYTES = 1024 * 1024 * 1024
_CLEAN_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class AustinReleaseRejected(RuntimeError):
    """A content-free packaging or notarization rejection."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "austin_release_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise AustinReleaseRejected(reason_code)


def _duplicate_rejecting_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _reject("austin_release_json")
        result[key] = value
    return result


def _json_object(payload: bytes, *, maximum: int) -> dict[str, Any]:
    if type(payload) is not bytes or not 1 <= len(payload) <= maximum:
        _reject("austin_release_json")
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_pairs,
            parse_float=lambda _value: _reject("austin_release_json"),
            parse_constant=lambda _value: _reject("austin_release_json"),
        )
    except AustinReleaseRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _reject("austin_release_json")
    if type(value) is not dict:
        _reject("austin_release_json")
    return value


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current = current / component
        try:
            value = current.lstat()
        except OSError:
            _reject("austin_release_input_missing")
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
            _reject("austin_release_input_unsafe")


@contextmanager
def _safe_regular_reader(
    path: Path,
    *,
    exact_size: int | None = None,
    maximum_size: int = _MAX_RELEASE_ARTIFACT_BYTES,
) -> Iterator[tuple[int, int]]:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or type(maximum_size) is not int
        or not 1 <= maximum_size <= _MAX_RELEASE_ARTIFACT_BYTES
    ):
        _reject("austin_release_path")
    _assert_no_symlink_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _reject("austin_release_input_missing")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or (hasattr(os, "getuid") and before.st_uid not in {0, os.getuid()})
            or not 1 <= before.st_size <= maximum_size
            or (exact_size is not None and before.st_size != exact_size)
        ):
            _reject("austin_release_input_unsafe")
        yield descriptor, before.st_size
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
            _reject("austin_release_input_changed")
    finally:
        os.close(descriptor)


def _read_safe_regular(
    path: Path,
    *,
    exact_size: int | None = None,
    maximum_size: int = _MAX_NOTARY_LOG_BYTES,
) -> bytes:
    with _safe_regular_reader(
        path,
        exact_size=exact_size,
        maximum_size=maximum_size,
    ) as (descriptor, size):
        remaining = size
        payload = bytearray()
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                _reject("austin_release_input_unsafe")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_release_input_unsafe")
        return bytes(payload)


def _safe_regular(
    path: Path,
    *,
    exact_size: int | None = None,
    maximum_size: int = _MAX_RELEASE_ARTIFACT_BYTES,
) -> None:
    with _safe_regular_reader(
        path,
        exact_size=exact_size,
        maximum_size=maximum_size,
    ) as (descriptor, size):
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                _reject("austin_release_input_unsafe")
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_release_input_unsafe")


def _sha256_safe_regular(path: Path) -> str:
    digest = hashlib.sha256()
    with _safe_regular_reader(path) as (descriptor, size):
        remaining = size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                _reject("austin_release_input_unsafe")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("austin_release_input_unsafe")
    return "sha256:" + digest.hexdigest()


def _safe_output_parent(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or path.exists() or path.is_symlink():
        _reject("austin_release_output_exists")
    try:
        parent = path.parent
        resolved_parent = parent.resolve(strict=True)
        value = parent.lstat()
    except OSError:
        _reject("austin_release_output_parent")
    if (
        resolved_parent != parent
        or stat.S_ISLNK(value.st_mode)
        or not stat.S_ISDIR(value.st_mode)
        or value.st_mode & 0o022
        or (hasattr(os, "getuid") and value.st_uid != os.getuid())
    ):
        _reject("austin_release_output_parent")
    _assert_no_symlink_ancestors(path)


@dataclass(frozen=True, slots=True)
class AustinReleaseConfig:
    application_identity: str
    installer_identity: str
    team_id: str
    notary_profile: str
    extension_origin: str
    disabled_native_authority_public_key: Path
    disabled_native_authority_public_key_digest: str
    output_directory: Path
    version: str
    build_number: str

    def validate(self) -> "AustinReleaseConfig":
        if sys.platform != "darwin":
            _reject("austin_release_platform")
        if type(self.team_id) is not str or _TEAM_ID_RE.fullmatch(self.team_id) is None:
            _reject("austin_release_team")
        expected_application = re.compile(
            rf"^Developer ID Application: [^\r\n]{{1,160}} \({re.escape(self.team_id)}\)$"
        )
        expected_installer = re.compile(
            rf"^Developer ID Installer: [^\r\n]{{1,160}} \({re.escape(self.team_id)}\)$"
        )
        if (
            type(self.application_identity) is not str
            or expected_application.fullmatch(self.application_identity) is None
        ):
            _reject("austin_release_application_identity")
        if (
            type(self.installer_identity) is not str
            or expected_installer.fullmatch(self.installer_identity) is None
        ):
            _reject("austin_release_installer_identity")
        if type(self.notary_profile) is not str or _PROFILE_RE.fullmatch(self.notary_profile) is None:
            _reject("austin_release_notary_profile")
        if type(self.extension_origin) is not str or _ORIGIN_RE.fullmatch(self.extension_origin) is None:
            _reject("austin_release_extension_origin")
        if type(self.version) is not str or _VERSION_RE.fullmatch(self.version) is None:
            _reject("austin_release_version")
        if self.version != __version__:
            _reject("austin_release_version_mismatch")
        if type(self.build_number) is not str or _BUILD_RE.fullmatch(self.build_number) is None:
            _reject("austin_release_build")
        if (
            type(self.disabled_native_authority_public_key_digest) is not str
            or _DIGEST_RE.fullmatch(self.disabled_native_authority_public_key_digest)
            is None
        ):
            _reject("austin_release_authority_digest")
        _safe_regular(self.disabled_native_authority_public_key, exact_size=32)
        if (
            _sha256_safe_regular(self.disabled_native_authority_public_key)
            != self.disabled_native_authority_public_key_digest
        ):
            _reject("austin_release_authority_digest")
        _safe_output_parent(self.output_directory)
        return self


@dataclass(frozen=True, slots=True)
class AustinCommandResult:
    stdout: bytes


class AustinCommandRunner(Protocol):
    def run(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> AustinCommandResult: ...


class AustinSubprocessRunner:
    """Bounded no-shell command runner with a minimal inherited environment."""

    def run(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> AustinCommandResult:
        if (
            type(command) is not tuple
            or not command
            or any(type(part) is not str or "\x00" in part for part in command)
            or not 0 < float(timeout_seconds) <= 3_600
        ):
            _reject("austin_release_command")
        env = {
            "HOME": str(Path.home()),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": _CLEAN_PATH,
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        if environment is not None:
            for key, value in environment.items():
                if (
                    type(key) is not str
                    or type(value) is not str
                    or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) is None
                    or "\x00" in value
                    or len(value.encode("utf-8")) > 4_096
                ):
                    _reject("austin_release_environment")
                env[key] = value
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if process.stdout is None:
                _reject("austin_release_command_failed")
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + float(timeout_seconds)
            output = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _reject("austin_release_command_failed")
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
                if len(output) + len(chunk) > _MAX_COMMAND_OUTPUT_BYTES:
                    _reject("austin_release_command_failed")
                output.extend(chunk)
            remaining = max(0.01, deadline - time.monotonic())
            return_code = process.wait(timeout=remaining)
            if return_code != 0:
                _reject("austin_release_command_failed")
            return AustinCommandResult(stdout=bytes(output))
        except AustinReleaseRejected:
            raise
        except (OSError, subprocess.SubprocessError):
            _reject("austin_release_command_failed")
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
                if process.stdout is not None:
                    process.stdout.close()


@dataclass(frozen=True, slots=True)
class AustinReleaseResult:
    package_path: Path
    evidence_path: Path
    package_digest: str
    app_submission_id: str
    package_submission_id: str


class AustinReleasePackager:
    def __init__(
        self,
        *,
        runner: AustinCommandRunner | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._runner = runner or AustinSubprocessRunner()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def _run(
        self,
        *command: str,
        timeout_seconds: float = 300,
        environment: Mapping[str, str] | None = None,
    ) -> bytes:
        return self._runner.run(
            tuple(command),
            timeout_seconds=timeout_seconds,
            environment=environment,
        ).stdout

    @staticmethod
    def _identity_names(output: bytes) -> set[str]:
        text = output.decode("utf-8", errors="replace")
        return set(re.findall(r'^\s*\d+\)\s+[0-9A-Fa-f]+\s+"([^"\r\n]+)"', text, re.MULTILINE))

    def _preflight_identities(self, config: AustinReleaseConfig) -> None:
        code_signing = self._identity_names(
            self._run(
                "/usr/bin/security", "find-identity", "-v", "-p", "codesigning"
            )
        )
        all_identities = self._identity_names(
            self._run("/usr/bin/security", "find-identity", "-v")
        )
        if config.application_identity not in code_signing:
            _reject("austin_release_application_identity_missing")
        if config.installer_identity not in all_identities:
            _reject("austin_release_installer_identity_missing")
        self._run(
            "/usr/bin/xcrun",
            "notarytool",
            "history",
            "--keychain-profile",
            config.notary_profile,
            "--output-format",
            "json",
            timeout_seconds=120,
        )

    def _entitlements(self, path: Path) -> dict[str, Any]:
        output = self._run(
            "/usr/bin/codesign",
            "-d",
            "--entitlements",
            ":-",
            str(path),
        )
        start = output.find(b"<?xml")
        if start < 0:
            start = output.find(b"<plist")
        if start < 0:
            _reject("austin_release_entitlements")
        try:
            value = plistlib.loads(output[start:])
        except plistlib.InvalidFileException:
            _reject("austin_release_entitlements")
        if type(value) is not dict:
            _reject("austin_release_entitlements")
        return value

    def _verify_signed_app(self, app: Path, config: AustinReleaseConfig) -> None:
        if not app.is_dir() or app.is_symlink():
            _reject("austin_release_bundle")
        info_path = app / "Contents" / "Info.plist"
        try:
            info = plistlib.loads(info_path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            _reject("austin_release_info")
        if type(info) is not dict or info.get("CFBundleIdentifier") != AUSTIN_BUNDLE_ID:
            _reject("austin_release_info")
        if (
            info.get("CFBundleShortVersionString") != config.version
            or info.get("CFBundleVersion") != config.build_number
        ):
            _reject("austin_release_info_version")
        authority_key = app / "Contents" / "Resources" / "AustinAuthorityPublicKey.bin"
        if (
            hashlib.sha256(
                _read_safe_regular(authority_key, exact_size=32, maximum_size=32)
            ).hexdigest()
            != config.disabled_native_authority_public_key_digest.removeprefix(
                "sha256:"
            )
        ):
            _reject("austin_release_authority_digest")
        self._run(
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            "--verbose=4",
            str(app),
        )
        expected_app = {
            "com.apple.security.app-sandbox": True,
            "com.apple.security.application-groups": ["group.com.algo-cli.control"],
        }
        paths = {
            "bundle": (app, AUSTIN_BUNDLE_ID, expected_app),
            "app": (
                app / "Contents" / "MacOS" / "austin-control",
                AUSTIN_BUNDLE_ID,
                expected_app,
            ),
            "relay": (
                app / "Contents" / "Helpers" / "austin-relay",
                "com.algo-cli.austin.relay",
                expected_app,
            ),
            "adapter": (
                app / "Contents" / "Helpers" / "austin-tcc-adapter",
                "com.algo-cli.austin.tcc-adapter",
                {"com.apple.security.automation.apple-events": True},
            ),
            "credential_migrator": (
                app / "Contents" / "Helpers" / "austin-credential-migrator",
                "com.algo-cli.austin.credential-migrator",
                {},
            ),
            "neon": (
                app / "Contents" / "Helpers" / "neon-native-host",
                "com.algo-cli.neon.host",
                {},
            ),
        }
        for path, identifier, entitlements in paths.values():
            self._run(
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                "--verbose=4",
                str(path),
            )
            details = self._run(
                "/usr/bin/codesign", "-d", "--verbose=4", str(path)
            ).decode("utf-8", errors="replace")
            if (
                f"Identifier={identifier}" not in details
                or f"TeamIdentifier={config.team_id}" not in details
                or f"Authority={config.application_identity}" not in details
                or "Authority=Developer ID Certification Authority" not in details
                or "Authority=Apple Root CA" not in details
                or not has_hardened_runtime(details)
            ):
                _reject("austin_release_identity_mismatch")
            requirement = (
                "designated => anchor apple generic and "
                "certificate leaf[field.1.2.840.113635.100.6.1.13] exists and "
                f'certificate leaf[subject.OU] = "{config.team_id}" and identifier "{identifier}"'
            )
            self._run(
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                f"-R={requirement}",
                str(path),
            )
            if self._entitlements(path) != entitlements:
                _reject("austin_release_entitlements")
        self._run(sys.executable, str(AUSTIN_AUDIT_SCRIPT), "--bundle", str(app))

    def _notarize(self, artifact: Path, config: AustinReleaseConfig, log_path: Path) -> tuple[str, str]:
        submission = self._run(
            "/usr/bin/xcrun",
            "notarytool",
            "submit",
            str(artifact),
            "--keychain-profile",
            config.notary_profile,
            "--wait",
            "--output-format",
            "json",
            timeout_seconds=3_600,
        )
        row = _json_object(submission, maximum=_MAX_COMMAND_OUTPUT_BYTES)
        submission_id = row.get("id")
        if row.get("status") != "Accepted" or type(submission_id) is not str or _SUBMISSION_RE.fullmatch(submission_id) is None:
            _reject("austin_release_notarization_rejected")
        self._run(
            "/usr/bin/xcrun",
            "notarytool",
            "log",
            submission_id,
            "--keychain-profile",
            config.notary_profile,
            str(log_path),
            timeout_seconds=300,
        )
        log_payload = _read_safe_regular(
            log_path,
            maximum_size=_MAX_NOTARY_LOG_BYTES,
        )
        log = _json_object(log_payload, maximum=_MAX_NOTARY_LOG_BYTES)
        if log.get("status") != "Accepted" or log.get("issues") != []:
            _reject("austin_release_notary_log")
        return submission_id.lower(), "sha256:" + hashlib.sha256(log_payload).hexdigest()

    def _verify_package_signature(self, package: Path, config: AustinReleaseConfig) -> None:
        output = self._run("/usr/sbin/pkgutil", "--check-signature", str(package)).decode(
            "utf-8", errors="replace"
        )
        if (
            re.search(
                rf"^\s*1\.\s+{re.escape(config.installer_identity)}\s*$",
                output,
                re.MULTILINE,
            )
            is None
            or "Status: signed by a developer certificate issued by Apple for distribution"
            not in output
            or "Signed with a trusted timestamp" not in output
        ):
            _reject("austin_release_package_signature")

    @staticmethod
    def _write_output(
        *,
        output_directory: Path,
        package_source: Path,
        package_name: str,
        evidence: bytes,
    ) -> tuple[Path, Path]:
        parent = output_directory.parent
        temporary = parent / f".AustinRelease.{uuid.uuid4().hex}.tmp"
        package_target = temporary / package_name
        evidence_target = temporary / ADA_RELEASE_EVIDENCE_FILENAME
        try:
            temporary.mkdir(mode=0o700)
            with package_source.open("rb") as source, package_target.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            package_target.chmod(0o644)
            with evidence_target.open("xb") as target:
                target.write(evidence)
                target.flush()
                os.fsync(target.fileno())
            evidence_target.chmod(0o600)
            directory_fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.replace(temporary, output_directory)
            parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError:
            try:
                shutil.rmtree(temporary)
            except OSError:
                pass
            _reject("austin_release_output_write")
        return output_directory / package_name, output_directory / ADA_RELEASE_EVIDENCE_FILENAME

    def build(self, config: AustinReleaseConfig) -> AustinReleaseResult:
        config.validate()
        self._preflight_identities(config)
        with tempfile.TemporaryDirectory(prefix="AustinRelease.") as raw_temporary:
            temporary = Path(raw_temporary).resolve(strict=True)
            sealed_native_key = temporary / "AustinDisabledNativeAuthority.bin"
            native_key_payload = _read_safe_regular(
                config.disabled_native_authority_public_key,
                exact_size=32,
                maximum_size=32,
            )
            native_key_digest = "sha256:" + hashlib.sha256(native_key_payload).hexdigest()
            if native_key_digest != config.disabled_native_authority_public_key_digest:
                _reject("austin_release_authority_digest")
            sealed_native_key.write_bytes(native_key_payload)
            sealed_native_key.chmod(0o444)
            app_archive = temporary / "AustinApp.zip"
            app_log = temporary / "AdaAppNotaryLog.json"
            package_log = temporary / "AdaPackageNotaryLog.json"
            package = temporary / "Algo-CLI-Control.pkg"
            self._run(
                str(AUSTIN_BUILD_SCRIPT),
                "build",
                timeout_seconds=900,
                environment={
                    "AUSTIN_AUTHORITY_PUBLIC_KEY_FILE": str(
                        sealed_native_key
                    ),
                    "AUSTIN_CONFIGURATION": "release",
                    "AUSTIN_DEVELOPER_ID_IDENTITY": config.application_identity,
                    "AUSTIN_RELEASE_BUILD": config.build_number,
                    "AUSTIN_RELEASE_VERSION": config.version,
                    "NEON_EXTENSION_ORIGIN": config.extension_origin,
                },
            )
            self._verify_signed_app(AUSTIN_STAGE_APP, config)
            self._run(
                "/usr/bin/ditto",
                "-c",
                "-k",
                "--keepParent",
                str(AUSTIN_STAGE_APP),
                str(app_archive),
            )
            _safe_regular(app_archive)
            app_submission_id, app_log_digest = self._notarize(
                app_archive, config, app_log
            )
            self._run(
                "/usr/bin/xcrun", "stapler", "staple", str(AUSTIN_STAGE_APP)
            )
            self._run(
                "/usr/bin/xcrun", "stapler", "validate", str(AUSTIN_STAGE_APP)
            )
            self._run(
                "/usr/sbin/spctl",
                "--assess",
                "--type",
                "execute",
                "--verbose=4",
                str(AUSTIN_STAGE_APP),
            )
            self._verify_signed_app(AUSTIN_STAGE_APP, config)
            self._run(
                "/usr/bin/pkgbuild",
                "--component",
                str(AUSTIN_STAGE_APP),
                "--install-location",
                "/Applications",
                "--identifier",
                AUSTIN_PACKAGE_ID,
                "--version",
                config.version,
                "--sign",
                config.installer_identity,
                str(package),
            )
            _safe_regular(package)
            self._verify_package_signature(package, config)
            package_submission_id, package_log_digest = self._notarize(
                package, config, package_log
            )
            self._run("/usr/bin/xcrun", "stapler", "staple", str(package))
            self._run("/usr/bin/xcrun", "stapler", "validate", str(package))
            self._run(
                "/usr/sbin/spctl",
                "--assess",
                "--type",
                "install",
                "--verbose=4",
                str(package),
            )
            self._verify_package_signature(package, config)
            package_digest = _sha256_safe_regular(package)
            generated_at_ms = self._clock_ms()
            if type(generated_at_ms) is not int or not 0 <= generated_at_ms <= MAX_SAFE_INTEGER:
                _reject("austin_release_clock")
            evidence = canonical_json_bytes(
                {
                    "app_bundle_id": AUSTIN_BUNDLE_ID,
                    "app_notary_log_digest": app_log_digest,
                    "app_submission_id": app_submission_id,
                    "build_number": config.build_number,
                    "checks": [
                        "developer_id_application",
                        "exact_entitlements",
                        "hardened_runtime",
                        "designated_requirements",
                        "app_notarized_and_stapled",
                        "gatekeeper_execute",
                        "developer_id_installer",
                        "trusted_installer_timestamp",
                        "package_notarized_and_stapled",
                        "gatekeeper_install",
                    ],
                    "generated_at_ms": generated_at_ms,
                    "package_digest": package_digest,
                    "package_id": AUSTIN_PACKAGE_ID,
                    "package_notary_log_digest": package_log_digest,
                    "package_submission_id": package_submission_id,
                    "schema_version": 1,
                    "team_id": config.team_id,
                    "native_control_protocol": "disabled_foundation",
                    "native_authority_public_key_digest": native_key_digest,
                    "version": config.version,
                }
            )
            package_name = f"Algo-CLI-Control-{config.version}.pkg"
            package_path, evidence_path = self._write_output(
                output_directory=config.output_directory,
                package_source=package,
                package_name=package_name,
                evidence=evidence,
            )
            return AustinReleaseResult(
                package_path=package_path,
                evidence_path=evidence_path,
                package_digest=package_digest,
                app_submission_id=app_submission_id,
                package_submission_id=package_submission_id,
            )


__all__ = [
    "ADA_RELEASE_EVIDENCE_FILENAME",
    "AUSTIN_BUNDLE_ID",
    "AUSTIN_PACKAGE_ID",
    "AustinCommandResult",
    "AustinCommandRunner",
    "AustinReleaseConfig",
    "AustinReleasePackager",
    "AustinReleaseRejected",
    "AustinReleaseResult",
    "AustinSubprocessRunner",
]
