"""Fail-closed installation inventory and bounded native-control uninstall.

The production surface is deliberately finite: one Developer ID application,
one user LaunchAgent, and one stable-Chrome native-host manifest.  This module
never guesses paths, expands globs, recursively removes a tree, invokes a
shell, escalates privileges, or deletes unenumerated credentials.

It remains outside the normal action registry while the hardening freeze is
active.  An installer must first create a signed inventory; without that
receipt the uninstall planner has no authority to remove anything.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, NoReturn, Protocol, Sequence
import uuid

from .david_control_kernel import (
    AuthorityRejected,
    ControlSigner,
    ControlVerifier,
    MAX_SAFE_INTEGER,
)
from .ada_credential_registry import ADA_CREDENTIAL_REGISTRY_LABEL
from .ada_uninstall_recovery import (
    AdaUninstallRecoveryError,
    AdaUninstallRecoveryRecord,
    AdaUninstallRecoveryStore,
)
from .grace_key_store import (
    ALGO_FIXED_CREDENTIAL_LABELS,
    CONTROL_SIGNING_KEY_LABEL,
    KEYRING_SERVICE,
    RECEIPT_ANCHOR_LABEL_PREFIX,
)


OLIVER_INSTALL_SCHEMA_VERSION = 2
OLIVER_PLAN_SCHEMA_VERSION = 1
OLIVER_RECEIPT_SCHEMA_VERSION = 1
OLIVER_INVENTORY_SIGNATURE_KIND = "control_uninstall_inventory"
OLIVER_RECEIPT_SIGNATURE_KIND = "control_uninstall_receipt"

AUSTIN_APP_BUNDLE_NAME = "Algo CLI Control.app"
AUSTIN_APP_BUNDLE_ID = "com.algo-cli.austin.control"
AUSTIN_APP_EXECUTABLE = "austin-control"
AUSTIN_CREDENTIAL_MIGRATOR_EXECUTABLE = "austin-credential-migrator"
AUSTIN_SERVICE_LABEL = "group.com.algo-cli.control.austin.tcc-adapter"
NEON_NATIVE_HOST_NAME = "com.algo_cli.neon"
NEON_NATIVE_HOST_FILENAME = f"{NEON_NATIVE_HOST_NAME}.json"
NEON_NATIVE_HOST_EXECUTABLE = "neon-native-host"
NEON_ALLOWED_ORIGIN_RESOURCE = "NeonAllowedOrigin.txt"

ALICE_ARTIFACT_KEY_LABEL = "alice-artifact-master-v1"
IRENE_PRIVACY_KEY_LABEL = "irene-privacy-hmac-v1"
OLIVER_FIXED_CREDENTIAL_LABELS = ALGO_FIXED_CREDENTIAL_LABELS

MAX_INVENTORY_BYTES = 512 * 1024
MAX_INVENTORY_ENTRIES = 512
MAX_CREDENTIAL_ENTRIES = 256
MAX_RELATIVE_PATH_BYTES = 768
MAX_COMPONENT_BYTES = 160
MAX_SINGLE_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 512 * 1024 * 1024
MAX_JSON_DEPTH = 20
MAX_JSON_ITEMS = 24_000
_ZERO_DIGEST = "sha256:" + ("0" * 64)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^ed25519:[0-9a-f]{64}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
_TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
_APP_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$"
)
_APP_BUILD_RE = re.compile(r"^[1-9][0-9]{0,8}$")
_EXTENSION_ORIGIN_RE = re.compile(r"^chrome-extension://[a-p]{32}/$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_ANCHOR_LABEL_RE = re.compile(
    rf"^{re.escape(RECEIPT_ANCHOR_LABEL_PREFIX)}[0-9a-f]{{64}}$"
)
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ +@().-]{0,159}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class OliverUninstallRejected(RuntimeError):
    """A content-free planning or pre-mutation rejection."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if not _REASON_RE.fullmatch(selected):
            selected = "invalid_rejection"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise OliverUninstallRejected(reason_code)


def _closed(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        _reject(f"{label}_schema")
    if not all(type(key) is str for key in value):
        _reject(f"{label}_schema")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _reject(label)
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _reject(label)
    return value


def _bounded_text(value: Any, label: str, maximum_bytes: int) -> str:
    if type(value) is not str:
        _reject(label)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _reject(label)
    if not encoded or len(encoded) > maximum_bytes:
        _reject(label)
    return value


def _pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    selected = _bounded_text(value, label, 1024)
    if not pattern.fullmatch(selected):
        _reject(label)
    return selected


def _uuid(value: Any, label: str) -> str:
    selected = _bounded_text(value, label, 36)
    try:
        parsed = uuid.UUID(selected)
    except (AttributeError, ValueError):
        _reject(label)
    if str(parsed) != selected or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        _reject(label)
    return selected


def _relative_path(value: Any) -> str:
    selected = _bounded_text(value, "entry_relative_path", MAX_RELATIVE_PATH_BYTES)
    if selected == ".":
        return selected
    if selected.startswith("/") or "\\" in selected or "\x00" in selected:
        _reject("entry_relative_path")
    parsed = PurePosixPath(selected)
    if str(parsed) != selected or not parsed.parts:
        _reject("entry_relative_path")
    for component in parsed.parts:
        if component in {"", ".", ".."}:
            _reject("entry_relative_path")
        if len(component.encode("utf-8")) > MAX_COMPONENT_BYTES or not _COMPONENT_RE.fullmatch(
            component
        ):
            _reject("entry_relative_path")
    return selected


def _duplicate_rejecting_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _reject("json_duplicate_key")
        result[key] = value
    return result


def _reject_number(_raw: str) -> NoReturn:
    _reject("json_number")


def _parse_integer(raw: str) -> int:
    if len(raw) > 17:
        _reject("json_integer")
    return _integer(int(raw), "json_integer", -MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)


def _bound_json(value: Any, *, depth: int = 0, counter: list[int] | None = None) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_JSON_ITEMS or depth > MAX_JSON_DEPTH:
        _reject("json_bounds")
    if value is None or type(value) is float:
        _reject("json_type")
    if type(value) is bool:
        return
    if type(value) is int:
        _integer(value, "json_integer", -MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)
        return
    if type(value) is str:
        _bounded_text(value, "json_string", MAX_INVENTORY_BYTES)
        return
    if type(value) is list:
        for child in value:
            _bound_json(child, depth=depth + 1, counter=counter)
        return
    if type(value) is dict:
        for key, child in value.items():
            _bounded_text(key, "json_key", 96)
            _bound_json(child, depth=depth + 1, counter=counter)
        return
    _reject("json_type")


def _canonical_bytes(value: Any) -> bytes:
    _bound_json(value)
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        _reject("json_encoding")
    if not encoded or len(encoded) > MAX_INVENTORY_BYTES:
        _reject("json_size")
    return encoded


def _decode_bytes(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_INVENTORY_BYTES:
        _reject("inventory_size")
    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_duplicate_rejecting_pairs,
            parse_int=_parse_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except UnicodeDecodeError:
        _reject("inventory_utf8")
    except json.JSONDecodeError:
        _reject("inventory_json")
    if type(value) is not dict:
        _reject("inventory_schema")
    _bound_json(value)
    if not hmac.compare_digest(_canonical_bytes(value), payload):
        _reject("inventory_noncanonical")
    return value


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _content_digest(value: Any) -> str:
    return _digest_bytes(_canonical_bytes(value))


class OliverSurface(str, Enum):
    AUSTIN_APP = "austin_app"
    AUSTIN_LAUNCH_AGENT = "austin_launch_agent"
    NEON_CHROME_NATIVE_HOST = "neon_chrome_native_host"


class OliverEntryKind(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"


class OliverCredentialState(str, Enum):
    ABSENT = "absent"
    PRESENT = "present"


class OliverUninstallMode(str, Enum):
    RUNTIME_ONLY = "runtime_only"
    PURGE_PRIVATE_STATE = "purge_private_state"


class OliverReceiptOutcome(str, Enum):
    COMPLETED = "completed"
    UNKNOWN_OUTCOME = "unknown_outcome"


@dataclass(frozen=True, slots=True)
class OliverInstallEntry:
    surface: OliverSurface
    relative_path: str
    kind: OliverEntryKind
    uid: int
    mode: int
    size: int
    digest: str

    @property
    def entry_id(self) -> str:
        return _content_digest(
            {"relative_path": self.relative_path, "surface": self.surface.value}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "kind": self.kind.value,
            "mode": self.mode,
            "relative_path": self.relative_path,
            "size": self.size,
            "surface": self.surface.value,
            "uid": self.uid,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OliverInstallEntry":
        row = _closed(
            value,
            frozenset({"digest", "kind", "mode", "relative_path", "size", "surface", "uid"}),
            "entry",
        )
        try:
            surface = OliverSurface(row["surface"])
            kind = OliverEntryKind(row["kind"])
        except (TypeError, ValueError):
            _reject("entry_enum")
        size = _integer(row["size"], "entry_size", 0, MAX_SINGLE_FILE_BYTES)
        if kind is OliverEntryKind.DIRECTORY and size != 0:
            _reject("entry_directory_size")
        mode = _integer(row["mode"], "entry_mode", 0, 0o7777)
        if mode & 0o022:
            _reject("entry_writable")
        return cls(
            surface=surface,
            relative_path=_relative_path(row["relative_path"]),
            kind=kind,
            uid=_integer(row["uid"], "entry_uid", 0, (1 << 31) - 1),
            mode=mode,
            size=size,
            digest=_pattern(row["digest"], _DIGEST_RE, "entry_digest"),
        )


@dataclass(frozen=True, slots=True)
class OliverCredentialEntry:
    service: str
    label: str
    state: OliverCredentialState
    value_digest: str

    @property
    def credential_id(self) -> str:
        return _content_digest({"label": self.label, "service": self.service})

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "service": self.service,
            "state": self.state.value,
            "value_digest": self.value_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OliverCredentialEntry":
        row = _closed(
            value,
            frozenset({"label", "service", "state", "value_digest"}),
            "credential",
        )
        service = _pattern(row["service"], _LABEL_RE, "credential_service")
        label = _pattern(row["label"], _LABEL_RE, "credential_label")
        try:
            state = OliverCredentialState(row["state"])
        except (TypeError, ValueError):
            _reject("credential_state")
        digest = _pattern(row["value_digest"], _DIGEST_RE, "credential_digest")
        if (state is OliverCredentialState.ABSENT) != hmac.compare_digest(
            digest, _ZERO_DIGEST
        ):
            _reject("credential_state_digest")
        return cls(service=service, label=label, state=state, value_digest=digest)


def _inventory_signature_envelope(
    *, schema_version: int, inventory_digest: str, authority_key_id: str
) -> dict[str, Any]:
    return {
        "authority_key_id": authority_key_id,
        "inventory_digest": inventory_digest,
        "schema_version": schema_version,
    }


@dataclass(frozen=True, slots=True)
class OliverInstallInventory:
    install_id: str
    installed_at_ms: int
    user_uid: int
    app_bundle_id: str
    app_version: str
    app_build_number: str
    team_id: str
    extension_origin: str
    authority_key_id: str
    credential_inventory_complete: bool
    entries: tuple[OliverInstallEntry, ...]
    credentials: tuple[OliverCredentialEntry, ...]
    signature: str
    schema_version: int = OLIVER_INSTALL_SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "app_bundle_id": self.app_bundle_id,
            "app_build_number": self.app_build_number,
            "app_version": self.app_version,
            "authority_key_id": self.authority_key_id,
            "credential_inventory_complete": self.credential_inventory_complete,
            "credentials": [entry.to_dict() for entry in self.credentials],
            "entries": [entry.to_dict() for entry in self.entries],
            "extension_origin": self.extension_origin,
            "install_id": self.install_id,
            "installed_at_ms": self.installed_at_ms,
            "schema_version": self.schema_version,
            "team_id": self.team_id,
            "user_uid": self.user_uid,
        }

    @property
    def unsigned_digest(self) -> str:
        return _content_digest(self.unsigned_dict())

    @property
    def digest(self) -> str:
        return _content_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature}

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())

    def verify(self, verifier: ControlVerifier) -> None:
        validated = type(self).from_dict(self.to_dict())
        if validated.authority_key_id != verifier.key_id:
            _reject("inventory_authority")
        envelope = _inventory_signature_envelope(
            schema_version=validated.schema_version,
            inventory_digest=validated.unsigned_digest,
            authority_key_id=validated.authority_key_id,
        )
        try:
            verifier.verify(OLIVER_INVENTORY_SIGNATURE_KIND, envelope, validated.signature)
        except AuthorityRejected:
            _reject("inventory_signature")

    @classmethod
    def create(
        cls,
        *,
        install_id: str,
        installed_at_ms: int,
        user_uid: int,
        team_id: str,
        app_version: str,
        app_build_number: str,
        extension_origin: str,
        credential_inventory_complete: bool,
        entries: Sequence[OliverInstallEntry],
        credentials: Sequence[OliverCredentialEntry],
        signer: ControlSigner,
    ) -> "OliverInstallInventory":
        unsigned = {
            "app_bundle_id": AUSTIN_APP_BUNDLE_ID,
            "app_build_number": app_build_number,
            "app_version": app_version,
            "authority_key_id": signer.key_id,
            "credential_inventory_complete": credential_inventory_complete,
            "credentials": [entry.to_dict() for entry in credentials],
            "entries": [entry.to_dict() for entry in entries],
            "extension_origin": extension_origin,
            "install_id": install_id,
            "installed_at_ms": installed_at_ms,
            "schema_version": OLIVER_INSTALL_SCHEMA_VERSION,
            "team_id": team_id,
            "user_uid": user_uid,
        }
        unsigned_digest = _content_digest(unsigned)
        signature = signer.sign(
            OLIVER_INVENTORY_SIGNATURE_KIND,
            _inventory_signature_envelope(
                schema_version=OLIVER_INSTALL_SCHEMA_VERSION,
                inventory_digest=unsigned_digest,
                authority_key_id=signer.key_id,
            ),
        )
        return cls.from_dict({**unsigned, "signature": signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> "OliverInstallInventory":
        return cls.from_dict(_decode_bytes(payload))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OliverInstallInventory":
        row = _closed(
            value,
            frozenset(
                {
                    "app_bundle_id",
                    "app_build_number",
                    "app_version",
                    "authority_key_id",
                    "credential_inventory_complete",
                    "credentials",
                    "entries",
                    "extension_origin",
                    "install_id",
                    "installed_at_ms",
                    "schema_version",
                    "signature",
                    "team_id",
                    "user_uid",
                }
            ),
            "inventory",
        )
        if _integer(row["schema_version"], "inventory_version", 2, 2) != OLIVER_INSTALL_SCHEMA_VERSION:
            _reject("inventory_version")
        if row["app_bundle_id"] != AUSTIN_APP_BUNDLE_ID:
            _reject("inventory_bundle_id")
        raw_entries = row["entries"]
        if type(raw_entries) is not list or not 1 <= len(raw_entries) <= MAX_INVENTORY_ENTRIES:
            _reject("inventory_entries")
        entries = tuple(OliverInstallEntry.from_dict(entry) for entry in raw_entries)
        entry_order = tuple((entry.surface.value, entry.relative_path) for entry in entries)
        if len(set(entry_order)) != len(entry_order) or tuple(sorted(entry_order)) != entry_order:
            _reject("inventory_entry_order")
        raw_credentials = row["credentials"]
        if type(raw_credentials) is not list or len(raw_credentials) > MAX_CREDENTIAL_ENTRIES:
            _reject("inventory_credentials")
        credentials = tuple(
            OliverCredentialEntry.from_dict(entry) for entry in raw_credentials
        )
        credential_order = tuple((entry.service, entry.label) for entry in credentials)
        if len(set(credential_order)) != len(credential_order) or tuple(
            sorted(credential_order)
        ) != credential_order:
            _reject("inventory_credential_order")
        inventory = cls(
            install_id=_uuid(row["install_id"], "inventory_install_id"),
            installed_at_ms=_integer(
                row["installed_at_ms"], "inventory_installed_at", 0, MAX_SAFE_INTEGER
            ),
            user_uid=_integer(row["user_uid"], "inventory_user_uid", 0, (1 << 31) - 1),
            app_bundle_id=AUSTIN_APP_BUNDLE_ID,
            app_version=_pattern(
                row["app_version"], _APP_VERSION_RE, "inventory_app_version"
            ),
            app_build_number=_pattern(
                row["app_build_number"], _APP_BUILD_RE, "inventory_app_build"
            ),
            team_id=_pattern(row["team_id"], _TEAM_ID_RE, "inventory_team_id"),
            extension_origin=_pattern(
                row["extension_origin"], _EXTENSION_ORIGIN_RE, "inventory_extension_origin"
            ),
            authority_key_id=_pattern(
                row["authority_key_id"], _KEY_ID_RE, "inventory_authority_key"
            ),
            credential_inventory_complete=_boolean(
                row["credential_inventory_complete"], "inventory_credential_complete"
            ),
            entries=entries,
            credentials=credentials,
            signature=_pattern(row["signature"], _SIGNATURE_RE, "inventory_signature"),
        )
        _validate_inventory_tree(inventory)
        _validate_inventory_credentials(inventory)
        return inventory


@dataclass(frozen=True, slots=True)
class OliverInstallRoots:
    home: Path
    uid: int
    app_bundle: Path
    launch_agent: Path
    chrome_native_host: Path
    production: bool

    @classmethod
    def for_current_user(cls) -> "OliverInstallRoots":
        home = Path.home()
        uid = os.getuid() if hasattr(os, "getuid") else -1
        return cls(
            home=home,
            uid=uid,
            app_bundle=Path("/Applications") / AUSTIN_APP_BUNDLE_NAME,
            launch_agent=home
            / "Library"
            / "LaunchAgents"
            / f"{AUSTIN_SERVICE_LABEL}.plist",
            chrome_native_host=home
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "NativeMessagingHosts"
            / NEON_NATIVE_HOST_FILENAME,
            production=True,
        )

    @classmethod
    def _for_test(cls, root: Path, *, uid: int) -> "OliverInstallRoots":
        selected = root.absolute()
        home = selected / "home"
        return cls(
            home=home,
            uid=uid,
            app_bundle=selected / "Applications" / AUSTIN_APP_BUNDLE_NAME,
            launch_agent=home
            / "Library"
            / "LaunchAgents"
            / f"{AUSTIN_SERVICE_LABEL}.plist",
            chrome_native_host=home
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "NativeMessagingHosts"
            / NEON_NATIVE_HOST_FILENAME,
            production=False,
        )

    def surface_roots(self) -> dict[OliverSurface, Path]:
        return {
            OliverSurface.AUSTIN_APP: self.app_bundle,
            OliverSurface.AUSTIN_LAUNCH_AGENT: self.launch_agent,
            OliverSurface.NEON_CHROME_NATIVE_HOST: self.chrome_native_host,
        }


class OliverCredentialStore(Protocol):
    service: str

    def fingerprint(self, label: str) -> str | None: ...

    def compare_and_delete(self, label: str, *, expected_digest: str) -> bool: ...

    def complete_inventory_labels(self) -> tuple[str, ...] | None: ...

    def complete_inventory_snapshot(
        self,
    ) -> tuple[tuple[str, str | None], ...] | None: ...


class OliverLaunchController(Protocol):
    def state(self, *, uid: int, label: str) -> str: ...

    def bootout(self, *, uid: int, label: str) -> None: ...


class OliverProcessProbe(Protocol):
    def assert_stopped(self, executable_paths: Sequence[Path]) -> None: ...


class OliverLaunchctl:
    """Exact-label launchctl adapter with bounded, content-free failures."""

    def __init__(self, executable: Path = Path("/bin/launchctl")) -> None:
        self.executable = executable

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                (str(self.executable), *arguments),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            _reject("launchctl_unavailable")

    def state(self, *, uid: int, label: str) -> str:
        target = f"gui/{_integer(uid, 'launch_uid', 0, (1 << 31) - 1)}/{label}"
        completed = self._run("print", target)
        if completed.returncode == 0:
            return "loaded"
        if completed.returncode == 113:
            return "absent"
        _reject("launchctl_state_unknown")

    def bootout(self, *, uid: int, label: str) -> None:
        target = f"gui/{_integer(uid, 'launch_uid', 0, (1 << 31) - 1)}/{label}"
        completed = self._run("bootout", target)
        if completed.returncode != 0 and self.state(uid=uid, label=label) != "absent":
            _reject("launchctl_bootout_failed")
        if self.state(uid=uid, label=label) != "absent":
            _reject("launchctl_bootout_unconfirmed")


class OliverLsofProcessProbe:
    """Block removal while an exact installed executable remains open."""

    def __init__(self, executable: Path = Path("/usr/sbin/lsof")) -> None:
        self.executable = executable

    def assert_stopped(self, executable_paths: Sequence[Path]) -> None:
        for path in executable_paths:
            if not path.exists():
                continue
            try:
                completed = subprocess.run(
                    (str(self.executable), "-F", "p", "--", str(path)),
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=10.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                _reject("process_probe_unavailable")
            if completed.returncode == 0 and any(
                line.startswith(b"p") and line[1:].isdigit()
                for line in completed.stdout[:65_536].splitlines()
            ):
                _reject("installed_component_running")
            if completed.returncode not in {0, 1}:
                _reject("process_probe_unknown")


def _validate_roots(roots: OliverInstallRoots, *, allow_test_roots: bool) -> None:
    if type(roots) is not OliverInstallRoots:
        _reject("install_roots")
    if roots.production:
        expected = OliverInstallRoots.for_current_user()
        if roots != expected:
            _reject("production_roots")
        if sys.platform != "darwin":
            _reject("platform_unsupported")
    elif not allow_test_roots:
        _reject("test_roots_forbidden")
    if roots.uid < 0:
        _reject("install_uid")
    paths = tuple(roots.surface_roots().values())
    if len(set(paths)) != len(paths):
        _reject("install_roots_overlap")
    for path in (roots.home, *paths):
        if not path.is_absolute() or ".." in path.parts:
            _reject("install_root_path")


def expected_austin_launch_agent(roots: OliverInstallRoots) -> dict[str, Any]:
    adapter = roots.app_bundle / "Contents" / "Helpers" / "austin-tcc-adapter"
    return {
        "Label": AUSTIN_SERVICE_LABEL,
        "LimitLoadToSessionType": "Aqua",
        "MachServices": {AUSTIN_SERVICE_LABEL: True},
        "ProcessType": "Interactive",
        "ProgramArguments": [str(adapter)],
        "RunAtLoad": False,
        "StandardErrorPath": "/dev/null",
        "StandardOutPath": "/dev/null",
        "ThrottleInterval": 30,
    }


def expected_neon_native_host(
    roots: OliverInstallRoots, *, extension_origin: str
) -> dict[str, Any]:
    origin = _pattern(extension_origin, _EXTENSION_ORIGIN_RE, "extension_origin")
    host = roots.app_bundle / "Contents" / "Helpers" / NEON_NATIVE_HOST_EXECUTABLE
    return {
        "allowed_origins": [origin],
        "description": "Algo CLI selected-tab observe-only bridge",
        "name": NEON_NATIVE_HOST_NAME,
        "path": str(host),
        "type": "stdio",
    }


def _assert_no_symlink_ancestors(path: Path) -> None:
    if not path.is_absolute():
        _reject("path_absolute")
    current = Path(path.anchor)
    for component in path.parts[1:-1]:
        current = current / component
        try:
            value = current.lstat()
        except FileNotFoundError:
            return
        except OSError:
            _reject("path_ancestor_unreadable")
        if stat.S_ISLNK(value.st_mode):
            _reject("path_ancestor_symlink")
        if not stat.S_ISDIR(value.st_mode):
            _reject("path_ancestor_type")


def _children_digest(path: Path) -> str:
    children: list[dict[str, str]] = []
    try:
        with os.scandir(path) as iterator:
            for child in iterator:
                value = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(value.st_mode):
                    _reject("entry_symlink")
                if stat.S_ISREG(value.st_mode):
                    kind = OliverEntryKind.FILE.value
                elif stat.S_ISDIR(value.st_mode):
                    kind = OliverEntryKind.DIRECTORY.value
                else:
                    _reject("entry_type")
                children.append({"kind": kind, "name": child.name})
    except OliverUninstallRejected:
        raise
    except OSError:
        _reject("directory_read")
    children.sort(key=lambda row: row["name"])
    return _content_digest({"children": children})


def _hash_regular(path: Path, *, expected_stat: os.stat_result | None = None) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _reject("file_open")
    try:
        before = os.fstat(descriptor)
        if expected_stat is not None and (
            before.st_dev != expected_stat.st_dev or before.st_ino != expected_stat.st_ino
        ):
            _reject("file_race")
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _reject("file_type")
        if before.st_size > MAX_SINGLE_FILE_BYTES:
            _reject("file_size")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                _reject("file_short_read")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _reject("file_grew")
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
            _reject("file_race")
        return ("sha256:" + digest.hexdigest(), before.st_size)
    finally:
        os.close(descriptor)


def _snapshot_path(
    path: Path, *, surface: OliverSurface, relative_path: str
) -> OliverInstallEntry:
    _assert_no_symlink_ancestors(path)
    try:
        value = path.lstat()
    except OSError:
        _reject("entry_missing")
    mode = stat.S_IMODE(value.st_mode)
    if mode & 0o022:
        _reject("entry_writable")
    if stat.S_ISLNK(value.st_mode):
        _reject("entry_symlink")
    if stat.S_ISREG(value.st_mode):
        if value.st_nlink != 1:
            _reject("entry_hardlink")
        digest, size = _hash_regular(path, expected_stat=value)
        kind = OliverEntryKind.FILE
    elif stat.S_ISDIR(value.st_mode):
        digest = _children_digest(path)
        size = 0
        kind = OliverEntryKind.DIRECTORY
    else:
        _reject("entry_type")
    return OliverInstallEntry(
        surface=surface,
        relative_path=relative_path,
        kind=kind,
        uid=value.st_uid,
        mode=mode,
        size=size,
        digest=digest,
    )


def _walk_surface(root: Path, surface: OliverSurface) -> tuple[OliverInstallEntry, ...]:
    first = _snapshot_path(root, surface=surface, relative_path=".")
    entries = [first]
    if first.kind is OliverEntryKind.FILE:
        return tuple(entries)
    pending = [root]
    while pending:
        parent = pending.pop()
        try:
            children = sorted(parent.iterdir(), key=lambda path: path.name)
        except OSError:
            _reject("directory_read")
        for child in children:
            relative = child.relative_to(root).as_posix()
            entry = _snapshot_path(child, surface=surface, relative_path=relative)
            entries.append(entry)
            if entry.kind is OliverEntryKind.DIRECTORY:
                pending.append(child)
            if len(entries) > MAX_INVENTORY_ENTRIES:
                _reject("inventory_entries")
    return tuple(sorted(entries, key=lambda entry: entry.relative_path))


def _expected_children(
    entries: Sequence[OliverInstallEntry], surface: OliverSurface, parent: str
) -> list[dict[str, str]]:
    prefix = "" if parent == "." else parent + "/"
    children: list[dict[str, str]] = []
    for entry in entries:
        if entry.surface is not surface or entry.relative_path == parent:
            continue
        relative = entry.relative_path
        if not relative.startswith(prefix):
            continue
        remainder = relative[len(prefix) :]
        if "/" not in remainder:
            children.append({"kind": entry.kind.value, "name": remainder})
    return sorted(children, key=lambda row: row["name"])


def _validate_inventory_tree(inventory: OliverInstallInventory) -> None:
    by_key = {(entry.surface, entry.relative_path): entry for entry in inventory.entries}
    if {surface for surface, relative in by_key if relative == "."} != set(OliverSurface):
        _reject("inventory_surface_roots")
    total = 0
    for entry in inventory.entries:
        if entry.surface is not OliverSurface.AUSTIN_APP and entry.relative_path != ".":
            _reject("inventory_single_file_surface")
        if entry.surface is not OliverSurface.AUSTIN_APP and entry.kind is not OliverEntryKind.FILE:
            _reject("inventory_single_file_surface")
        if entry.surface is OliverSurface.AUSTIN_APP and entry.relative_path == "." and entry.kind is not OliverEntryKind.DIRECTORY:
            _reject("inventory_app_root")
        if entry.uid not in {0, inventory.user_uid}:
            _reject("inventory_entry_owner")
        if entry.surface is not OliverSurface.AUSTIN_APP and entry.uid != inventory.user_uid:
            _reject("inventory_user_surface_owner")
        total += entry.size
        if total > MAX_TOTAL_FILE_BYTES:
            _reject("inventory_total_size")
        if entry.relative_path != ".":
            parent = str(PurePosixPath(entry.relative_path).parent)
            parent_entry = by_key.get((entry.surface, parent))
            if parent_entry is None or parent_entry.kind is not OliverEntryKind.DIRECTORY:
                _reject("inventory_tree_parent")
        if entry.kind is OliverEntryKind.DIRECTORY:
            expected = _content_digest(
                {"children": _expected_children(inventory.entries, entry.surface, entry.relative_path)}
            )
            if not hmac.compare_digest(entry.digest, expected):
                _reject("inventory_directory_digest")


def _validate_inventory_credentials(inventory: OliverInstallInventory) -> None:
    labels = {entry.label for entry in inventory.credentials}
    if inventory.credential_inventory_complete and not OLIVER_FIXED_CREDENTIAL_LABELS <= labels:
        _reject("inventory_fixed_credentials")
    for entry in inventory.credentials:
        if entry.service != KEYRING_SERVICE:
            _reject("inventory_credential_service")
        if entry.label not in OLIVER_FIXED_CREDENTIAL_LABELS and not _ANCHOR_LABEL_RE.fullmatch(
            entry.label
        ):
            _reject("inventory_credential_label")


def _load_plist(path: Path) -> dict[str, Any]:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        _reject("installed_plist")
    if type(value) is not dict:
        _reject("installed_plist")
    return value


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError:
        _reject("installed_json")
    if not payload or len(payload) > 65_536:
        _reject("installed_json")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"), object_pairs_hook=_duplicate_rejecting_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject("installed_json")
    if type(value) is not dict:
        _reject("installed_json")
    return value


def _validate_semantic_regular(
    path: Path,
    *,
    roots: OliverInstallRoots,
    allow_missing: bool,
    executable: bool,
    allow_owner_write: bool = True,
) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        _reject("installed_runtime_file")
    except OSError:
        _reject("installed_runtime_file")
    mode = stat.S_IMODE(value.st_mode)
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or value.st_uid not in {0, roots.uid}
        or mode & 0o022
        or (not allow_owner_write and value.st_uid == roots.uid and mode & 0o200)
        or (executable and not mode & 0o111)
    ):
        _reject("installed_runtime_file")
    return True


def _validate_semantic_install(
    roots: OliverInstallRoots,
    *,
    extension_origin: str,
    allow_missing: bool,
) -> None:
    info = roots.app_bundle / "Contents" / "Info.plist"
    if info.exists():
        row = _load_plist(info)
        if (
            row.get("CFBundleIdentifier") != AUSTIN_APP_BUNDLE_ID
            or row.get("CFBundleExecutable") != AUSTIN_APP_EXECUTABLE
            or row.get("CFBundlePackageType") != "APPL"
        ):
            _reject("installed_bundle_identity")
    elif not allow_missing:
        _reject("installed_bundle_identity")
    required_executables = (
        roots.app_bundle / "Contents" / "MacOS" / AUSTIN_APP_EXECUTABLE,
        roots.app_bundle / "Contents" / "Helpers" / "austin-relay",
        roots.app_bundle / "Contents" / "Helpers" / "austin-tcc-adapter",
        roots.app_bundle / "Contents" / "Helpers" / NEON_NATIVE_HOST_EXECUTABLE,
    )
    for executable in required_executables:
        _validate_semantic_regular(
            executable,
            roots=roots,
            allow_missing=allow_missing,
            executable=True,
        )
    authority = (
        roots.app_bundle
        / "Contents"
        / "Resources"
        / "AustinAuthorityPublicKey.bin"
    )
    if _validate_semantic_regular(
        authority,
        roots=roots,
        allow_missing=allow_missing,
        executable=False,
        allow_owner_write=False,
    ):
        _digest, size = _hash_regular(authority)
        if size != 32:
            _reject("installed_authority_key")
    allowed_origin = (
        roots.app_bundle
        / "Contents"
        / "Resources"
        / NEON_ALLOWED_ORIGIN_RESOURCE
    )
    if _validate_semantic_regular(
        allowed_origin,
        roots=roots,
        allow_missing=allow_missing,
        executable=False,
        allow_owner_write=False,
    ):
        digest, size = _hash_regular(allowed_origin)
        origin_bytes = extension_origin.encode("utf-8", errors="strict")
        if size != len(origin_bytes) or not hmac.compare_digest(
            digest,
            _digest_bytes(origin_bytes),
        ):
            _reject("installed_native_host_origin")
    if roots.launch_agent.exists():
        if _load_plist(roots.launch_agent) != expected_austin_launch_agent(roots):
            _reject("installed_launch_agent")
    elif not allow_missing:
        _reject("installed_launch_agent")
    if roots.chrome_native_host.exists():
        if _load_json_file(roots.chrome_native_host) != expected_neon_native_host(
            roots, extension_origin=extension_origin
        ):
            _reject("installed_native_host")
    elif not allow_missing:
        _reject("installed_native_host")


def capture_oliver_install_inventory(
    *,
    roots: OliverInstallRoots,
    signer: ControlSigner,
    team_id: str,
    extension_origin: str,
    installed_at_ms: int,
    install_id: str,
    credential_store: OliverCredentialStore,
    credential_labels: Iterable[str],
    credential_inventory_complete: bool,
    allow_test_roots: bool = False,
) -> OliverInstallInventory:
    """Capture a finite receipt after explicit current-user install finalization."""

    _validate_roots(roots, allow_test_roots=allow_test_roots)
    _pattern(team_id, _TEAM_ID_RE, "inventory_team_id")
    origin = _pattern(extension_origin, _EXTENSION_ORIGIN_RE, "inventory_extension_origin")
    if credential_store.service != KEYRING_SERVICE:
        _reject("credential_store_service")
    labels = tuple(sorted(set(credential_labels)))
    if len(labels) > MAX_CREDENTIAL_ENTRIES:
        _reject("inventory_credentials")
    complete_snapshot: dict[str, str | None] | None = None
    if credential_inventory_complete:
        snapshotter = getattr(credential_store, "complete_inventory_snapshot", None)
        if not callable(snapshotter):
            _reject("credential_inventory_unprovable")
        authoritative_snapshot = snapshotter()
        if authoritative_snapshot is None or type(authoritative_snapshot) is not tuple:
            _reject("credential_inventory_unprovable")
        authoritative_labels: list[str] = []
        complete_snapshot = {}
        for row in authoritative_snapshot:
            if type(row) is not tuple or len(row) != 2:
                _reject("credential_inventory_unprovable")
            label, digest = row
            _pattern(label, _LABEL_RE, "credential_label")
            if digest is not None:
                _pattern(digest, _DIGEST_RE, "credential_digest")
            if label in complete_snapshot:
                _reject("credential_inventory_unprovable")
            authoritative_labels.append(label)
            complete_snapshot[label] = digest
        normalized_authoritative = tuple(sorted(authoritative_labels))
        if (
            normalized_authoritative != tuple(authoritative_labels)
            or labels != normalized_authoritative
            or not OLIVER_FIXED_CREDENTIAL_LABELS <= set(labels)
        ):
            _reject("credential_inventory_incomplete")
    for label in labels:
        _pattern(label, _LABEL_RE, "credential_label")
        if label not in OLIVER_FIXED_CREDENTIAL_LABELS and not _ANCHOR_LABEL_RE.fullmatch(label):
            _reject("inventory_credential_label")
    _validate_semantic_install(
        roots,
        extension_origin=origin,
        allow_missing=False,
    )
    app_info = _load_plist(roots.app_bundle / "Contents" / "Info.plist")
    app_version = _pattern(
        app_info.get("CFBundleShortVersionString"),
        _APP_VERSION_RE,
        "installed_app_version",
    )
    app_build_number = _pattern(
        app_info.get("CFBundleVersion"),
        _APP_BUILD_RE,
        "installed_app_build",
    )
    entries: list[OliverInstallEntry] = []
    for surface, root in roots.surface_roots().items():
        entries.extend(_walk_surface(root, surface))
    entries.sort(key=lambda entry: (entry.surface.value, entry.relative_path))
    if sum(entry.size for entry in entries) > MAX_TOTAL_FILE_BYTES:
        _reject("inventory_total_size")
    credentials: list[OliverCredentialEntry] = []
    for label in labels:
        fingerprint = (
            complete_snapshot[label]
            if complete_snapshot is not None
            else credential_store.fingerprint(label)
        )
        if fingerprint is None:
            state = OliverCredentialState.ABSENT
            digest = _ZERO_DIGEST
        else:
            state = OliverCredentialState.PRESENT
            digest = _pattern(fingerprint, _DIGEST_RE, "credential_digest")
        credentials.append(
            OliverCredentialEntry(
                service=KEYRING_SERVICE,
                label=label,
                state=state,
                value_digest=digest,
            )
        )
    return OliverInstallInventory.create(
        install_id=install_id,
        installed_at_ms=installed_at_ms,
        user_uid=roots.uid,
        team_id=team_id,
        app_version=app_version,
        app_build_number=app_build_number,
        extension_origin=origin,
        credential_inventory_complete=credential_inventory_complete,
        entries=entries,
        credentials=credentials,
        signer=signer,
    )


def _path_for_entry(roots: OliverInstallRoots, entry: OliverInstallEntry) -> Path:
    root = roots.surface_roots()[entry.surface]
    return root if entry.relative_path == "." else root.joinpath(*PurePosixPath(entry.relative_path).parts)


def _present_entries(
    inventory: OliverInstallInventory, roots: OliverInstallRoots
) -> tuple[OliverInstallEntry, ...]:
    expected = {(entry.surface, entry.relative_path): entry for entry in inventory.entries}
    observed: list[OliverInstallEntry] = []
    for surface, root in roots.surface_roots().items():
        _assert_no_symlink_ancestors(root)
        try:
            root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _reject("entry_unreadable")
        for current in _walk_surface(root, surface):
            expected_entry = expected.get((current.surface, current.relative_path))
            if expected_entry is None:
                _reject("unexpected_installed_entry")
            if (
                current.kind is not expected_entry.kind
                or current.uid != expected_entry.uid
                or current.mode != expected_entry.mode
                or current.size != expected_entry.size
                or (
                    current.kind is OliverEntryKind.FILE
                    and not hmac.compare_digest(current.digest, expected_entry.digest)
                )
            ):
                _reject("installed_entry_changed")
            observed.append(expected_entry)
    return tuple(sorted(observed, key=lambda entry: (entry.surface.value, entry.relative_path)))


def _installed_executables(roots: OliverInstallRoots) -> tuple[Path, ...]:
    helpers = roots.app_bundle / "Contents" / "Helpers"
    return (
        roots.app_bundle / "Contents" / "MacOS" / AUSTIN_APP_EXECUTABLE,
        helpers / "austin-relay",
        helpers / "austin-tcc-adapter",
        helpers / AUSTIN_CREDENTIAL_MIGRATOR_EXECUTABLE,
        helpers / NEON_NATIVE_HOST_EXECUTABLE,
    )


def _assert_removal_access(
    roots: OliverInstallRoots, entries: Sequence[OliverInstallEntry]
) -> None:
    """Reject before launchd or filesystem mutation when any parent is not removable."""

    for entry in entries:
        parent = _path_for_entry(roots, entry).parent
        _assert_no_symlink_ancestors(parent / "placeholder")
        try:
            allowed = os.access(parent, os.W_OK | os.X_OK, effective_ids=True)
        except TypeError:  # pragma: no cover - production path is macOS
            allowed = os.access(parent, os.W_OK | os.X_OK)
        if not allowed:
            _reject("privileged_removal_required")


def _credential_presence(
    inventory: OliverInstallInventory,
    store: OliverCredentialStore,
) -> tuple[OliverCredentialEntry, ...]:
    if store.service != KEYRING_SERVICE:
        _reject("credential_store_service")
    snapshotter = getattr(store, "complete_inventory_snapshot", None)
    if not callable(snapshotter):
        _reject("credential_inventory_unprovable")
    snapshot = snapshotter()
    if snapshot is None or type(snapshot) is not tuple:
        _reject("credential_inventory_unprovable")
    observed_by_label: dict[str, str | None] = {}
    observed_order: list[str] = []
    for row in snapshot:
        if type(row) is not tuple or len(row) != 2:
            _reject("credential_inventory_unprovable")
        label, digest = row
        _pattern(label, _LABEL_RE, "credential_label")
        if label not in OLIVER_FIXED_CREDENTIAL_LABELS and not _ANCHOR_LABEL_RE.fullmatch(
            label
        ):
            _reject("credential_inventory_unprovable")
        if digest is not None:
            _pattern(digest, _DIGEST_RE, "credential_digest")
        if label in observed_by_label:
            _reject("credential_inventory_unprovable")
        observed_order.append(label)
        observed_by_label[label] = digest
    inventory_labels = tuple(entry.label for entry in inventory.credentials)
    if tuple(sorted(observed_order)) != tuple(observed_order):
        _reject("credential_inventory_unprovable")
    if inventory_labels != tuple(observed_order):
        _reject("credential_inventory_changed")
    present: list[OliverCredentialEntry] = []
    for entry in inventory.credentials:
        observed = observed_by_label[entry.label]
        if entry.state is OliverCredentialState.ABSENT:
            if observed is not None:
                _reject("unexpected_credential")
            continue
        if observed is None:
            continue
        if not hmac.compare_digest(observed, entry.value_digest):
            _reject("credential_changed")
        present.append(entry)
    return tuple(present)


@dataclass(frozen=True, slots=True)
class OliverUninstallPlan:
    mode: OliverUninstallMode
    inventory_digest: str
    present_entry_ids: tuple[str, ...]
    present_credential_ids: tuple[str, ...]
    launch_agent_state: str
    schema_version: int = OLIVER_PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "inventory_digest": self.inventory_digest,
            "launch_agent_state": self.launch_agent_state,
            "mode": self.mode.value,
            "present_credential_ids": list(self.present_credential_ids),
            "present_entry_ids": list(self.present_entry_ids),
            "schema_version": self.schema_version,
        }

    @property
    def digest(self) -> str:
        return _content_digest(self.to_dict())

    @property
    def confirmation_phrase(self) -> str:
        return "UNINSTALL ALGO CLI CONTROL " + self.digest[-12:].upper()


def plan_oliver_uninstall(
    *,
    inventory: OliverInstallInventory,
    verifier: ControlVerifier,
    roots: OliverInstallRoots,
    mode: OliverUninstallMode,
    launch_controller: OliverLaunchController,
    process_probe: OliverProcessProbe,
    credential_store: OliverCredentialStore | None = None,
    allow_test_roots: bool = False,
) -> OliverUninstallPlan:
    """Produce a deterministic dry-run plan after validating every live object."""

    _validate_roots(roots, allow_test_roots=allow_test_roots)
    inventory.verify(verifier)
    if inventory.user_uid != roots.uid:
        _reject("inventory_user")
    if type(mode) is not OliverUninstallMode:
        _reject("uninstall_mode")
    process_probe.assert_stopped(_installed_executables(roots))
    present = _present_entries(inventory, roots)
    _assert_removal_access(roots, present)
    _validate_semantic_install(
        roots,
        extension_origin=inventory.extension_origin,
        allow_missing=True,
    )
    state = launch_controller.state(uid=roots.uid, label=AUSTIN_SERVICE_LABEL)
    if state not in {"absent", "loaded"}:
        _reject("launchctl_state_unknown")
    credential_entries: tuple[OliverCredentialEntry, ...] = ()
    if mode is OliverUninstallMode.PURGE_PRIVATE_STATE:
        if not inventory.credential_inventory_complete:
            _reject("credential_inventory_incomplete")
        if credential_store is None:
            _reject("credential_store_required")
        credential_entries = _credential_presence(inventory, credential_store)
    return OliverUninstallPlan(
        mode=mode,
        inventory_digest=inventory.digest,
        present_entry_ids=tuple(sorted(entry.entry_id for entry in present)),
        present_credential_ids=tuple(
            sorted(entry.credential_id for entry in credential_entries)
        ),
        launch_agent_state=state,
    )


def _open_parent(path: Path) -> tuple[int, str]:
    if not path.is_absolute() or not path.name:
        _reject("delete_path")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:-1]:
            next_descriptor = os.open(component, flags | nofollow, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, path.name
    except OSError:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        _reject("delete_parent")


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _reject("delete_stat")


def _unlink_verified(path: Path, expected: OliverInstallEntry) -> bool:
    parent_fd, name = _open_parent(path)
    try:
        value = _stat_at(parent_fd, name)
        if value is None:
            return False
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != expected.uid
            or stat.S_IMODE(value.st_mode) != expected.mode
            or value.st_size != expected.size
        ):
            _reject("delete_entry_changed")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError:
            _reject("delete_open")
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino):
                _reject("delete_race")
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    _reject("delete_short_read")
                digest.update(chunk)
                remaining -= len(chunk)
            if not hmac.compare_digest("sha256:" + digest.hexdigest(), expected.digest):
                _reject("delete_entry_changed")
            named = _stat_at(parent_fd, name)
            if named is None or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                _reject("delete_race")
            os.unlink(name, dir_fd=parent_fd)
            return True
        finally:
            os.close(descriptor)
    except OliverUninstallRejected:
        raise
    except OSError:
        _reject("delete_unlink")
    finally:
        os.close(parent_fd)


def _rmdir_verified(path: Path, expected: OliverInstallEntry) -> bool:
    parent_fd, name = _open_parent(path)
    try:
        value = _stat_at(parent_fd, name)
        if value is None:
            return False
        if (
            not stat.S_ISDIR(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_uid != expected.uid
            or stat.S_IMODE(value.st_mode) != expected.mode
        ):
            _reject("delete_entry_changed")
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            _reject("delete_directory_not_empty")
        return True
    finally:
        os.close(parent_fd)


def _receipt_unsigned(
    *,
    install_id: str,
    inventory_digest: str,
    plan_digest: str,
    mode: OliverUninstallMode,
    outcome: OliverReceiptOutcome,
    reason_code: str,
    started_at_ms: int,
    finished_at_ms: int,
    launch_agent_booted_out: bool,
    deleted_entry_count: int,
    already_absent_entry_count: int,
    deleted_credential_count: int,
    already_absent_credential_count: int,
    authority_key_id: str,
) -> dict[str, Any]:
    return {
        "already_absent_credential_count": already_absent_credential_count,
        "already_absent_entry_count": already_absent_entry_count,
        "authority_key_id": authority_key_id,
        "deleted_credential_count": deleted_credential_count,
        "deleted_entry_count": deleted_entry_count,
        "finished_at_ms": finished_at_ms,
        "install_id": install_id,
        "inventory_digest": inventory_digest,
        "launch_agent_booted_out": launch_agent_booted_out,
        "mode": mode.value,
        "outcome": outcome.value,
        "plan_digest": plan_digest,
        "reason_code": reason_code,
        "schema_version": OLIVER_RECEIPT_SCHEMA_VERSION,
        "started_at_ms": started_at_ms,
    }


@dataclass(frozen=True, slots=True)
class OliverUninstallReceipt:
    payload: dict[str, Any]
    signature: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload, "signature": self.signature}

    def verify(self, verifier: ControlVerifier) -> None:
        if self.payload.get("authority_key_id") != verifier.key_id:
            _reject("receipt_authority")
        try:
            verifier.verify(OLIVER_RECEIPT_SIGNATURE_KIND, self.payload, self.signature)
        except AuthorityRejected:
            _reject("receipt_signature")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OliverUninstallReceipt":
        row = _closed(
            value,
            frozenset(
                {
                    "already_absent_credential_count",
                    "already_absent_entry_count",
                    "authority_key_id",
                    "deleted_credential_count",
                    "deleted_entry_count",
                    "finished_at_ms",
                    "install_id",
                    "inventory_digest",
                    "launch_agent_booted_out",
                    "mode",
                    "outcome",
                    "plan_digest",
                    "reason_code",
                    "schema_version",
                    "signature",
                    "started_at_ms",
                }
            ),
            "receipt",
        )
        if (
            _integer(row["schema_version"], "receipt_version", 1, 1)
            != OLIVER_RECEIPT_SCHEMA_VERSION
        ):
            _reject("receipt_version")
        started_at_ms = _integer(
            row["started_at_ms"], "receipt_started_at", 0, MAX_SAFE_INTEGER
        )
        finished_at_ms = _integer(
            row["finished_at_ms"], "receipt_finished_at", 0, MAX_SAFE_INTEGER
        )
        if finished_at_ms < started_at_ms:
            _reject("receipt_clock")
        try:
            mode = OliverUninstallMode(row["mode"])
            outcome = OliverReceiptOutcome(row["outcome"])
        except (TypeError, ValueError):
            _reject("receipt_enum")
        payload = {
            "already_absent_credential_count": _integer(
                row["already_absent_credential_count"],
                "receipt_absent_credentials",
                0,
                MAX_CREDENTIAL_ENTRIES,
            ),
            "already_absent_entry_count": _integer(
                row["already_absent_entry_count"],
                "receipt_absent_entries",
                0,
                MAX_INVENTORY_ENTRIES,
            ),
            "authority_key_id": _pattern(
                row["authority_key_id"], _KEY_ID_RE, "receipt_authority"
            ),
            "deleted_credential_count": _integer(
                row["deleted_credential_count"],
                "receipt_deleted_credentials",
                0,
                MAX_CREDENTIAL_ENTRIES,
            ),
            "deleted_entry_count": _integer(
                row["deleted_entry_count"],
                "receipt_deleted_entries",
                0,
                MAX_INVENTORY_ENTRIES,
            ),
            "finished_at_ms": finished_at_ms,
            "install_id": _uuid(row["install_id"], "receipt_install"),
            "inventory_digest": _pattern(
                row["inventory_digest"], _DIGEST_RE, "receipt_inventory"
            ),
            "launch_agent_booted_out": _boolean(
                row["launch_agent_booted_out"], "receipt_launch_agent"
            ),
            "mode": mode.value,
            "outcome": outcome.value,
            "plan_digest": _pattern(
                row["plan_digest"], _DIGEST_RE, "receipt_plan"
            ),
            "reason_code": _pattern(
                row["reason_code"], _REASON_RE, "receipt_reason"
            ),
            "schema_version": OLIVER_RECEIPT_SCHEMA_VERSION,
            "started_at_ms": started_at_ms,
        }
        return cls(
            payload=payload,
            signature=_pattern(row["signature"], _SIGNATURE_RE, "receipt_signature"),
        )


def _recovery_reject(exc: AdaUninstallRecoveryError) -> NoReturn:
    raise OliverUninstallRejected(exc.reason_code) from exc


def _notify_uninstall_stage(
    fault_injector: Callable[[str], None] | None,
    stage: str,
) -> None:
    if fault_injector is not None:
        fault_injector(stage)


def _ordered_purge_credentials(
    entries: Iterable[OliverCredentialEntry],
) -> list[OliverCredentialEntry]:
    return sorted(
        entries,
        key=lambda entry: (
            3
            if entry.label == CONTROL_SIGNING_KEY_LABEL
            else 2
            if entry.label == ADA_CREDENTIAL_REGISTRY_LABEL
            else 0,
            entry.label,
        ),
    )


def _recovery_credential_presence(
    inventory: OliverInstallInventory,
    credential_store: OliverCredentialStore,
    allowed_ids: tuple[str, ...],
) -> tuple[OliverCredentialEntry, ...]:
    """Reread only the labels authorized before registry deletion began."""

    if credential_store.service != KEYRING_SERVICE:
        _reject("credential_store_service")
    expected_by_id = {entry.credential_id: entry for entry in inventory.credentials}
    if not set(allowed_ids) <= set(expected_by_id):
        _reject("uninstall_recovery_credentials")
    present: list[OliverCredentialEntry] = []
    for credential_id in allowed_ids:
        entry = expected_by_id[credential_id]
        observed = credential_store.fingerprint(entry.label)
        if observed is None:
            continue
        if not hmac.compare_digest(observed, entry.value_digest):
            _reject("credential_changed")
        present.append(entry)
    return tuple(present)


def _verify_recovery_receipt_context(
    receipt: OliverUninstallReceipt,
    record: AdaUninstallRecoveryRecord,
    verifier: ControlVerifier,
) -> None:
    receipt.verify(verifier)
    payload = receipt.payload
    if (
        payload["install_id"] != record.install_id
        or not hmac.compare_digest(payload["inventory_digest"], record.inventory_digest)
        or not hmac.compare_digest(payload["plan_digest"], record.plan_digest)
        or payload["mode"] != record.mode
        or payload["outcome"] != OliverReceiptOutcome.COMPLETED.value
        or payload["deleted_entry_count"] != len(record.present_entry_ids)
        or payload["deleted_credential_count"]
        != len(record.present_credential_ids)
    ):
        _reject("uninstall_recovery_receipt")


def execute_oliver_uninstall(
    *,
    inventory: OliverInstallInventory,
    signer: ControlSigner,
    roots: OliverInstallRoots,
    mode: OliverUninstallMode,
    expected_plan_digest: str,
    confirmation: str,
    launch_controller: OliverLaunchController,
    process_probe: OliverProcessProbe,
    credential_store: OliverCredentialStore | None = None,
    clock_ms: Callable[[], int],
    allow_test_roots: bool = False,
    recovery_store: AdaUninstallRecoveryStore | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> OliverUninstallReceipt:
    """Execute an exact plan and always return a signed terminal effect receipt."""

    plan = plan_oliver_uninstall(
        inventory=inventory,
        verifier=signer.verifier,
        roots=roots,
        mode=mode,
        launch_controller=launch_controller,
        process_probe=process_probe,
        credential_store=credential_store,
        allow_test_roots=allow_test_roots,
    )
    if not hmac.compare_digest(
        _pattern(expected_plan_digest, _DIGEST_RE, "plan_digest"), plan.digest
    ):
        _reject("plan_changed")
    if type(confirmation) is not str or not hmac.compare_digest(
        confirmation, plan.confirmation_phrase
    ):
        _reject("confirmation_required")
    if (
        mode is OliverUninstallMode.PURGE_PRIVATE_STATE
        and recovery_store is None
    ):
        _reject("uninstall_recovery_required")
    started_at_ms = _integer(clock_ms(), "receipt_started_at", 0, MAX_SAFE_INTEGER)
    recovery_record: AdaUninstallRecoveryRecord | None = None
    if recovery_store is not None:
        try:
            if recovery_store.load() is not None:
                _reject("uninstall_recovery_exists")
            recovery_record = AdaUninstallRecoveryRecord.authorize(
                install_id=inventory.install_id,
                inventory_digest=inventory.digest,
                plan_digest=plan.digest,
                mode=mode.value,
                present_entry_ids=plan.present_entry_ids,
                present_credential_ids=plan.present_credential_ids,
                launch_agent_state=plan.launch_agent_state,
                created_at_ms=started_at_ms,
                signer=signer,
            )
            recovery_store.publish(recovery_record, verifier=signer.verifier)
        except AdaUninstallRecoveryError as exc:
            _recovery_reject(exc)
        _notify_uninstall_stage(fault_injector, "recovery_authorized")
    deleted_entries = 0
    deleted_credentials = 0
    launch_agent_booted_out = False
    outcome = OliverReceiptOutcome.COMPLETED
    reason_code = "completed"
    mutation_started = False
    deferred_signer_entry: OliverCredentialEntry | None = None
    try:
        if plan.launch_agent_state == "loaded":
            mutation_started = True
            launch_controller.bootout(uid=roots.uid, label=AUSTIN_SERVICE_LABEL)
            launch_agent_booted_out = True
            _notify_uninstall_stage(fault_injector, "launch_agent_booted_out")
        if launch_controller.state(uid=roots.uid, label=AUSTIN_SERVICE_LABEL) != "absent":
            _reject("launchctl_bootout_unconfirmed")
        process_probe.assert_stopped(_installed_executables(roots))
        present = _present_entries(inventory, roots)
        _assert_removal_access(roots, present)
        if tuple(sorted(entry.entry_id for entry in present)) != plan.present_entry_ids:
            _reject("plan_changed")
        _validate_semantic_install(
            roots,
            extension_origin=inventory.extension_origin,
            allow_missing=True,
        )
        expected_by_id = {entry.entry_id: entry for entry in inventory.entries}
        files = sorted(
            (entry for entry in present if entry.kind is OliverEntryKind.FILE),
            key=lambda entry: (
                0
                if entry.relative_path == "."
                else len(PurePosixPath(entry.relative_path).parts),
                entry.surface.value,
            ),
            reverse=True,
        )
        directories = sorted(
            (entry for entry in present if entry.kind is OliverEntryKind.DIRECTORY),
            key=lambda entry: (
                0
                if entry.relative_path == "."
                else len(PurePosixPath(entry.relative_path).parts)
            ),
            reverse=True,
        )
        for installed_entry in (*files, *directories):
            mutation_started = True
            expected = expected_by_id[installed_entry.entry_id]
            path = _path_for_entry(roots, expected)
            removed = (
                _unlink_verified(path, expected)
                if expected.kind is OliverEntryKind.FILE
                else _rmdir_verified(path, expected)
            )
            deleted_entries += int(removed)
            if removed:
                _notify_uninstall_stage(fault_injector, "entry_deleted")
        if mode is OliverUninstallMode.PURGE_PRIVATE_STATE:
            if credential_store is None:
                _reject("credential_store_required")
            present_credentials = _credential_presence(inventory, credential_store)
            if tuple(
                sorted(entry.credential_id for entry in present_credentials)
            ) != plan.present_credential_ids:
                _reject("plan_changed")
            ordered_credentials = _ordered_purge_credentials(present_credentials)
            for credential_entry in ordered_credentials:
                if credential_entry.label == CONTROL_SIGNING_KEY_LABEL:
                    deferred_signer_entry = credential_entry
                    continue
                mutation_started = True
                if not credential_store.compare_and_delete(
                    credential_entry.label,
                    expected_digest=credential_entry.value_digest,
                ):
                    _reject("credential_delete_changed")
                deleted_credentials += 1
                _notify_uninstall_stage(fault_injector, "credential_deleted")
    except OliverUninstallRejected as exc:
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = exc.reason_code if mutation_started else "pre_mutation_race"
    except Exception:
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = "uninstall_internal_error"
    finished_at_ms = _integer(clock_ms(), "receipt_finished_at", 0, MAX_SAFE_INTEGER)
    if finished_at_ms < started_at_ms:
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = "clock_regression"
    if (
        outcome is OliverReceiptOutcome.COMPLETED
        and mode is OliverUninstallMode.PURGE_PRIVATE_STATE
        and deferred_signer_entry is None
    ):
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = "control_signer_not_authorized"
    receipt_credential_count = deleted_credentials
    if (
        outcome is OliverReceiptOutcome.COMPLETED
        and mode is OliverUninstallMode.PURGE_PRIVATE_STATE
    ):
        receipt_credential_count += 1
    unsigned = _receipt_unsigned(
        install_id=inventory.install_id,
        inventory_digest=inventory.digest,
        plan_digest=plan.digest,
        mode=mode,
        outcome=outcome,
        reason_code=reason_code,
        started_at_ms=started_at_ms,
        finished_at_ms=finished_at_ms,
        launch_agent_booted_out=launch_agent_booted_out,
        deleted_entry_count=deleted_entries,
        already_absent_entry_count=len(inventory.entries) - len(plan.present_entry_ids),
        deleted_credential_count=receipt_credential_count,
        already_absent_credential_count=(
            len(inventory.credentials) - len(plan.present_credential_ids)
            if mode is OliverUninstallMode.PURGE_PRIVATE_STATE
            else 0
        ),
        authority_key_id=signer.key_id,
    )
    signature = signer.sign(OLIVER_RECEIPT_SIGNATURE_KIND, unsigned)
    receipt = OliverUninstallReceipt(payload=unsigned, signature=signature)
    receipt.verify(signer.verifier)

    def unknown_receipt(selected_reason: str) -> OliverUninstallReceipt:
        unknown = {
            **unsigned,
            "deleted_credential_count": deleted_credentials,
            "outcome": OliverReceiptOutcome.UNKNOWN_OUTCOME.value,
            "reason_code": selected_reason,
        }
        selected_signature = signer.sign(OLIVER_RECEIPT_SIGNATURE_KIND, unknown)
        selected = OliverUninstallReceipt(
            payload=unknown,
            signature=selected_signature,
        )
        selected.verify(signer.verifier)
        return selected

    if (
        recovery_store is not None
        and recovery_record is not None
        and outcome is OliverReceiptOutcome.COMPLETED
    ):
        if mode is OliverUninstallMode.PURGE_PRIVATE_STATE:
            try:
                commit_ready = recovery_record.prepare_commit(
                    terminal_receipt=receipt.to_dict(),
                    updated_at_ms=finished_at_ms,
                    signer=signer,
                )
                recovery_store.publish(commit_ready, verifier=signer.verifier)
            except AdaUninstallRecoveryError as exc:
                return unknown_receipt(exc.reason_code)
            _notify_uninstall_stage(fault_injector, "commit_ready_published")
            try:
                if credential_store is None or deferred_signer_entry is None:
                    _reject("control_signer_not_authorized")
                removed = credential_store.compare_and_delete(
                    deferred_signer_entry.label,
                    expected_digest=deferred_signer_entry.value_digest,
                )
                if not removed:
                    observed = credential_store.fingerprint(
                        deferred_signer_entry.label
                    )
                    if observed is not None:
                        _reject("credential_delete_changed")
                deleted_credentials += 1
            except OliverUninstallRejected as exc:
                return unknown_receipt(exc.reason_code)
            except Exception:
                return unknown_receipt("uninstall_internal_error")
            _notify_uninstall_stage(fault_injector, "credential_deleted")
            _notify_uninstall_stage(fault_injector, "control_signer_deleted")
            return receipt
        _notify_uninstall_stage(fault_injector, "before_terminal_recovery")
        try:
            terminal = recovery_record.complete(
                terminal_receipt=receipt.to_dict(),
                updated_at_ms=finished_at_ms,
                signer=signer,
            )
            recovery_store.publish(terminal, verifier=signer.verifier)
        except AdaUninstallRecoveryError as exc:
            receipt = unknown_receipt(exc.reason_code)
        else:
            _notify_uninstall_stage(fault_injector, "terminal_recovery_published")
    return receipt


def resume_oliver_uninstall(
    *,
    inventory: OliverInstallInventory,
    verifier: ControlVerifier,
    signer: ControlSigner | None,
    roots: OliverInstallRoots,
    recovery_store: AdaUninstallRecoveryStore,
    launch_controller: OliverLaunchController,
    process_probe: OliverProcessProbe,
    credential_store: OliverCredentialStore | None,
    clock_ms: Callable[[], int],
    allow_test_roots: bool = False,
    fault_injector: Callable[[str], None] | None = None,
) -> OliverUninstallReceipt:
    """Resume only the exact plan durably authorized before its first mutation."""

    _validate_roots(roots, allow_test_roots=allow_test_roots)
    inventory.verify(verifier)
    if inventory.user_uid != roots.uid:
        _reject("inventory_user")
    try:
        record = recovery_store.load()
        if record is None:
            _reject("uninstall_recovery_absent")
        record.verify(verifier)
        record.verify_context(
            install_id=inventory.install_id,
            inventory_digest=inventory.digest,
            mode=record.mode,
        )
    except AdaUninstallRecoveryError as exc:
        _recovery_reject(exc)
    try:
        mode = OliverUninstallMode(record.mode)
    except ValueError:
        _reject("uninstall_recovery_mode")
    original_plan = OliverUninstallPlan(
        mode=mode,
        inventory_digest=record.inventory_digest,
        present_entry_ids=record.present_entry_ids,
        present_credential_ids=record.present_credential_ids,
        launch_agent_state=record.launch_agent_state,
    )
    if not hmac.compare_digest(original_plan.digest, record.plan_digest):
        _reject("uninstall_recovery_plan")
    if not set(record.present_entry_ids) <= {
        entry.entry_id for entry in inventory.entries
    }:
        _reject("uninstall_recovery_entries")
    if not set(record.present_credential_ids) <= {
        entry.credential_id for entry in inventory.credentials
    }:
        _reject("uninstall_recovery_credentials")
    if mode is OliverUninstallMode.RUNTIME_ONLY and record.present_credential_ids:
        _reject("uninstall_recovery_credentials")
    if record.phase == "terminal":
        receipt = OliverUninstallReceipt.from_dict(record.terminal_receipt)
        _verify_recovery_receipt_context(receipt, record, verifier)
        return receipt
    if record.phase == "commit_ready":
        if mode is not OliverUninstallMode.PURGE_PRIVATE_STATE:
            _reject("uninstall_recovery_commit_mode")
        if credential_store is None:
            _reject("credential_store_required")
        if credential_store.service != KEYRING_SERVICE:
            _reject("credential_store_service")
        receipt = OliverUninstallReceipt.from_dict(record.terminal_receipt)
        _verify_recovery_receipt_context(receipt, record, verifier)
        try:
            process_probe.assert_stopped(_installed_executables(roots))
            if launch_controller.state(
                uid=roots.uid,
                label=AUSTIN_SERVICE_LABEL,
            ) != "absent":
                _reject("uninstall_recovery_commit_state")
            if _present_entries(inventory, roots):
                _reject("uninstall_recovery_commit_state")
            signing_entries = tuple(
                entry
                for entry in inventory.credentials
                if entry.label == CONTROL_SIGNING_KEY_LABEL
                and entry.credential_id in record.present_credential_ids
            )
            if len(signing_entries) != 1:
                _reject("uninstall_recovery_commit_authority")
            remaining = _recovery_credential_presence(
                inventory,
                credential_store,
                record.present_credential_ids,
            )
            if any(
                entry.label != CONTROL_SIGNING_KEY_LABEL
                for entry in remaining
            ):
                _reject("uninstall_recovery_commit_state")
            signing_entry = signing_entries[0]
            if remaining:
                removed = credential_store.compare_and_delete(
                    signing_entry.label,
                    expected_digest=signing_entry.value_digest,
                )
                if not removed and credential_store.fingerprint(
                    signing_entry.label
                ) is not None:
                    _reject("credential_delete_changed")
                _notify_uninstall_stage(fault_injector, "credential_deleted")
                _notify_uninstall_stage(fault_injector, "control_signer_deleted")
            if credential_store.fingerprint(signing_entry.label) is not None:
                _reject("uninstall_recovery_commit_state")
        except OliverUninstallRejected:
            raise
        except Exception:
            _reject("uninstall_recovery_commit_state")
        return receipt
    if signer is None or signer.key_id != verifier.key_id:
        _reject("uninstall_recovery_authority_unavailable")
    if mode is OliverUninstallMode.PURGE_PRIVATE_STATE:
        if credential_store is None:
            _reject("credential_store_required")
        if credential_store.service != KEYRING_SERVICE:
            _reject("credential_store_service")

    deleted_entries = 0
    deleted_credentials = 0
    outcome = OliverReceiptOutcome.COMPLETED
    reason_code = "completed"
    launch_agent_booted_out = record.launch_agent_state == "loaded"
    deferred_signer_entry: OliverCredentialEntry | None = None
    try:
        process_probe.assert_stopped(_installed_executables(roots))
        state = launch_controller.state(uid=roots.uid, label=AUSTIN_SERVICE_LABEL)
        if state not in {"absent", "loaded"}:
            _reject("launchctl_state_unknown")
        if state == "loaded":
            launch_controller.bootout(uid=roots.uid, label=AUSTIN_SERVICE_LABEL)
            launch_agent_booted_out = True
            _notify_uninstall_stage(fault_injector, "launch_agent_booted_out")
        if launch_controller.state(uid=roots.uid, label=AUSTIN_SERVICE_LABEL) != "absent":
            _reject("launchctl_bootout_unconfirmed")
        process_probe.assert_stopped(_installed_executables(roots))
        present = _present_entries(inventory, roots)
        _assert_removal_access(roots, present)
        _validate_semantic_install(
            roots,
            extension_origin=inventory.extension_origin,
            allow_missing=True,
        )
        if not {entry.entry_id for entry in present} <= set(record.present_entry_ids):
            _reject("uninstall_recovery_entries")
        expected_by_id = {entry.entry_id: entry for entry in inventory.entries}
        files = sorted(
            (entry for entry in present if entry.kind is OliverEntryKind.FILE),
            key=lambda entry: (
                0
                if entry.relative_path == "."
                else len(PurePosixPath(entry.relative_path).parts),
                entry.surface.value,
            ),
            reverse=True,
        )
        directories = sorted(
            (entry for entry in present if entry.kind is OliverEntryKind.DIRECTORY),
            key=lambda entry: (
                0
                if entry.relative_path == "."
                else len(PurePosixPath(entry.relative_path).parts)
            ),
            reverse=True,
        )
        for installed_entry in (*files, *directories):
            expected = expected_by_id[installed_entry.entry_id]
            path = _path_for_entry(roots, expected)
            removed = (
                _unlink_verified(path, expected)
                if expected.kind is OliverEntryKind.FILE
                else _rmdir_verified(path, expected)
            )
            deleted_entries += int(removed)
            if removed:
                _notify_uninstall_stage(fault_injector, "entry_deleted")
        if mode is OliverUninstallMode.PURGE_PRIVATE_STATE:
            if credential_store is None:
                _reject("credential_store_required")
            remaining_credentials = _recovery_credential_presence(
                inventory,
                credential_store,
                record.present_credential_ids,
            )
            for credential_entry in _ordered_purge_credentials(remaining_credentials):
                if credential_entry.label == CONTROL_SIGNING_KEY_LABEL:
                    deferred_signer_entry = credential_entry
                    continue
                if not credential_store.compare_and_delete(
                    credential_entry.label,
                    expected_digest=credential_entry.value_digest,
                ):
                    _reject("credential_delete_changed")
                deleted_credentials += 1
                _notify_uninstall_stage(fault_injector, "credential_deleted")
    except OliverUninstallRejected as exc:
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = exc.reason_code
    except Exception:
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = "uninstall_internal_error"
    finished_at_ms = _integer(clock_ms(), "receipt_finished_at", 0, MAX_SAFE_INTEGER)
    if finished_at_ms < record.created_at_ms:
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = "clock_regression"
    if (
        outcome is OliverReceiptOutcome.COMPLETED
        and mode is OliverUninstallMode.PURGE_PRIVATE_STATE
        and deferred_signer_entry is None
    ):
        outcome = OliverReceiptOutcome.UNKNOWN_OUTCOME
        reason_code = "control_signer_not_authorized"
    completed = outcome is OliverReceiptOutcome.COMPLETED
    receipt_credential_count = deleted_credentials
    if completed and mode is OliverUninstallMode.PURGE_PRIVATE_STATE:
        receipt_credential_count += 1
    unsigned = _receipt_unsigned(
        install_id=inventory.install_id,
        inventory_digest=inventory.digest,
        plan_digest=record.plan_digest,
        mode=mode,
        outcome=outcome,
        reason_code=reason_code,
        started_at_ms=record.created_at_ms,
        finished_at_ms=finished_at_ms,
        launch_agent_booted_out=launch_agent_booted_out,
        deleted_entry_count=(
            len(record.present_entry_ids) if completed else deleted_entries
        ),
        already_absent_entry_count=(
            len(inventory.entries) - len(record.present_entry_ids)
        ),
        deleted_credential_count=(
            len(record.present_credential_ids)
            if completed
            else receipt_credential_count
        ),
        already_absent_credential_count=(
            len(inventory.credentials) - len(record.present_credential_ids)
            if mode is OliverUninstallMode.PURGE_PRIVATE_STATE
            else 0
        ),
        authority_key_id=signer.key_id,
    )
    signature = signer.sign(OLIVER_RECEIPT_SIGNATURE_KIND, unsigned)
    receipt = OliverUninstallReceipt(payload=unsigned, signature=signature)
    receipt.verify(verifier)

    def unknown_receipt(selected_reason: str) -> OliverUninstallReceipt:
        unknown = {
            **unsigned,
            "deleted_credential_count": deleted_credentials,
            "outcome": OliverReceiptOutcome.UNKNOWN_OUTCOME.value,
            "reason_code": selected_reason,
        }
        selected_signature = signer.sign(OLIVER_RECEIPT_SIGNATURE_KIND, unknown)
        selected = OliverUninstallReceipt(
            payload=unknown,
            signature=selected_signature,
        )
        selected.verify(verifier)
        return selected

    if completed:
        if mode is OliverUninstallMode.PURGE_PRIVATE_STATE:
            try:
                commit_ready = record.prepare_commit(
                    terminal_receipt=receipt.to_dict(),
                    updated_at_ms=finished_at_ms,
                    signer=signer,
                )
                recovery_store.publish(commit_ready, verifier=verifier)
            except AdaUninstallRecoveryError as exc:
                return unknown_receipt(exc.reason_code)
            _notify_uninstall_stage(fault_injector, "commit_ready_published")
            try:
                if credential_store is None or deferred_signer_entry is None:
                    _reject("control_signer_not_authorized")
                removed = credential_store.compare_and_delete(
                    deferred_signer_entry.label,
                    expected_digest=deferred_signer_entry.value_digest,
                )
                if not removed and credential_store.fingerprint(
                    deferred_signer_entry.label
                ) is not None:
                    _reject("credential_delete_changed")
                deleted_credentials += 1
            except OliverUninstallRejected as exc:
                return unknown_receipt(exc.reason_code)
            except Exception:
                return unknown_receipt("uninstall_internal_error")
            _notify_uninstall_stage(fault_injector, "credential_deleted")
            _notify_uninstall_stage(fault_injector, "control_signer_deleted")
            return receipt
        _notify_uninstall_stage(fault_injector, "before_terminal_recovery")
        try:
            terminal = record.complete(
                terminal_receipt=receipt.to_dict(),
                updated_at_ms=finished_at_ms,
                signer=signer,
            )
            recovery_store.publish(terminal, verifier=verifier)
        except AdaUninstallRecoveryError as exc:
            receipt = unknown_receipt(exc.reason_code)
        else:
            _notify_uninstall_stage(fault_injector, "terminal_recovery_published")
    return receipt


__all__ = [
    "AUSTIN_APP_BUNDLE_ID",
    "AUSTIN_APP_BUNDLE_NAME",
    "AUSTIN_CREDENTIAL_MIGRATOR_EXECUTABLE",
    "AUSTIN_SERVICE_LABEL",
    "NEON_NATIVE_HOST_FILENAME",
    "NEON_NATIVE_HOST_NAME",
    "OLIVER_FIXED_CREDENTIAL_LABELS",
    "OliverCredentialEntry",
    "OliverCredentialState",
    "OliverInstallEntry",
    "OliverInstallInventory",
    "OliverInstallRoots",
    "OliverLaunchctl",
    "OliverLsofProcessProbe",
    "OliverReceiptOutcome",
    "OliverSurface",
    "OliverUninstallMode",
    "OliverUninstallPlan",
    "OliverUninstallReceipt",
    "OliverUninstallRejected",
    "capture_oliver_install_inventory",
    "execute_oliver_uninstall",
    "expected_austin_launch_agent",
    "expected_neon_native_host",
    "plan_oliver_uninstall",
    "resume_oliver_uninstall",
]
