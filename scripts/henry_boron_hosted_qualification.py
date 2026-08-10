#!/usr/bin/env python3
"""Run repeated source-bound Boron sessions on a native hosted amd64 runner."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, NoReturn


ROOT = Path(__file__).resolve().parents[1]
MIN_REPETITIONS = 5
MAX_REPETITIONS = 20
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_DURATION_MS = 10 * 60 * 1000
MAX_GIT_TREE_BYTES = 128 * 1024
GIT_OBJECT_TIMEOUT_SECONDS = 15
GIT_EXECUTABLE = Path("/usr/bin/git")
HOSTED_REPOSITORY = "Seabass-up/Algo-cli"
HOSTED_REPOSITORY_ID = "1297752684"
CHROME_VERSION = "151.0.7922.108"
BORON_MAX_SECURITY_LAG_MS = 72 * 60 * 60 * 1000
HOSTED_LIMITATION = (
    "Repeated isolated public GET evidence only; it does not prove broad-site "
    "compatibility, selected-Chrome behavior, supported-task completion, model "
    "quality, token or screenshot reduction, interactive macOS permissions, or "
    "product readiness."
)
HOSTED_BUILD_CONTEXT_PATHS = (
    ".github/workflows/oliver-ci.yml",
    "pyproject.toml",
    "uv.lock",
    "algo_cli/__init__.py",
    "algo_cli/boron_browser_entry.py",
    "algo_cli/boron_browser_isolation.py",
    "algo_cli/boron_browser_wrapper.py",
    "algo_cli/xenon_browser_broker.py",
    "algo_cli/xenon_browser_egress.py",
    "algo_cli/xenon_browser_entry.py",
    "algo_cli/resources/boron_browser/boron_browser_wrapper.sh",
    "algo_cli/resources/boron_browser/boron_managed_policy.json",
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile",
    "algo_cli/resources/boron_browser/boron_public_browser.Dockerfile.dockerignore",
    "algo_cli/resources/boron_browser/boron_seccomp_profile.json",
    "algo_cli/resources/boron_browser/xenon_egress_broker.sh",
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile",
    "algo_cli/resources/boron_browser/xenon_egress_broker.Dockerfile.dockerignore",
    "scripts/boron_browser_build_images.py",
    "scripts/boron_browser_live_session.py",
    "scripts/henry_boron_hosted_qualification.py",
    "tests/test_boron_browser_entry.py",
    "tests/test_boron_browser_images.py",
    "tests/test_boron_browser_isolation.py",
    "tests/test_boron_browser_wrapper.py",
    "tests/test_henry_boron_hosted_qualification.py",
    "tests/test_xenon_browser_broker.py",
    "tests/test_xenon_browser_egress.py",
    "tests/test_xenon_browser_entry.py",
)
SOURCE_PATHS = HOSTED_BUILD_CONTEXT_PATHS

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_INTEGER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LIVE_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "platform",
        "qualification_source_digest",
        "browser_index_digest",
        "browser_platform_manifest_digest",
        "browser_config_digest",
        "browser_build_metadata_digest",
        "browser_provenance_digest",
        "browser_sbom_digest",
        "broker_index_digest",
        "broker_platform_manifest_digest",
        "broker_config_digest",
        "broker_build_metadata_digest",
        "broker_provenance_digest",
        "broker_sbom_digest",
        "broker_code_digest",
        "topology_evidence_digest",
        "internal_participant_count",
        "browser_state",
        "browser_major",
        "browser_security_update_lag_ms",
        "browser_security_source_digest",
        "browser_command_count",
        "browser_event_count",
        "broker_disposition",
        "broker_connection_count",
        "broker_request_count",
        "broker_redirect_count",
        "broker_bytes_to_browser",
        "target_decision_digest",
        "ca_certificate_digest",
        "browser_stderr",
        "broker_stderr",
    }
)


class HostedQualificationRejected(RuntimeError):
    """A content-free hosted qualification invariant failed closed."""

    def __init__(self, reason_code: str) -> None:
        selected = str(reason_code or "")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", selected) is None:
            selected = "hosted_qualification_invalid"
        self.reason_code = selected
        super().__init__(selected)


def _reject(reason_code: str) -> NoReturn:
    raise HostedQualificationRejected(reason_code)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        _reject("hosted_evidence_json")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class _RuntimeDependencies:
    build_module: ModuleType
    live_module: ModuleType
    rejection_types: tuple[type[BaseException], ...]


_RUNTIME_MODULE_PATHS = {
    "algo_cli": "algo_cli/__init__.py",
    "algo_cli.boron_browser_entry": "algo_cli/boron_browser_entry.py",
    "algo_cli.boron_browser_isolation": "algo_cli/boron_browser_isolation.py",
    "algo_cli.boron_browser_wrapper": "algo_cli/boron_browser_wrapper.py",
    "algo_cli.xenon_browser_broker": "algo_cli/xenon_browser_broker.py",
    "algo_cli.xenon_browser_egress": "algo_cli/xenon_browser_egress.py",
    "algo_cli.xenon_browser_entry": "algo_cli/xenon_browser_entry.py",
    "boron_browser_build_images": "scripts/boron_browser_build_images.py",
    "boron_browser_live_session": "scripts/boron_browser_live_session.py",
}
_RUNTIME_EXCEPTION_BINDINGS = {
    "BuildRejected": ("build_module", "BuildRejected"),
    "LiveSessionRejected": ("live_module", "LiveSessionRejected"),
    "BoronIsolationRejected": ("live_module", "BoronIsolationRejected"),
    "BoronEntryRejected": ("live_module", "BoronEntryRejected"),
    "BoronPipeRejected": ("live_module", "BoronPipeRejected"),
    "XenonBrokerRejected": ("live_module", "XenonBrokerRejected"),
    "XenonEntryRejected": ("live_module", "XenonEntryRejected"),
}
_ACTIVE_RUNTIME: _RuntimeDependencies | None = None


class _CapturedSourceFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import the qualification runtime directly from captured source bytes."""

    def __init__(self, payloads: Mapping[str, bytes]) -> None:
        self._payloads = payloads

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        relative = _RUNTIME_MODULE_PATHS.get(fullname)
        if relative is None:
            return None
        spec = importlib.machinery.ModuleSpec(
            fullname,
            self,
            origin=str(ROOT / relative),
            is_package=fullname == "algo_cli",
        )
        spec.has_location = True
        return spec

    def create_module(self, _spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        relative = _RUNTIME_MODULE_PATHS.get(module.__name__)
        payload = None if relative is None else self._payloads.get(relative)
        if relative is None or type(payload) is not bytes:
            _reject("hosted_runtime_source")
        try:
            code = compile(
                payload,
                str(ROOT / relative),
                "exec",
                dont_inherit=True,
            )
            exec(code, module.__dict__)
        except HostedQualificationRejected:
            raise
        except BaseException:
            _reject("hosted_runtime_import")


class _RuntimeBinding:
    """Keep verified modules installed for exactly one qualification boundary."""

    def __init__(
        self,
        *,
        runtime: _RuntimeDependencies,
        loaded: Mapping[str, ModuleType],
        previous_modules: Mapping[str, ModuleType],
        inserted_site_packages: Path | None,
    ) -> None:
        self.runtime = runtime
        self._loaded = dict(loaded)
        self._previous_modules = dict(previous_modules)
        self._inserted_site_packages = inserted_site_packages
        self._previous_runtime: _RuntimeDependencies | None = None
        self._active = False

    def __enter__(self) -> _RuntimeDependencies:
        global _ACTIVE_RUNTIME
        if self._active:
            _reject("hosted_runtime_binding")
        self._active = True
        self._previous_runtime = _ACTIVE_RUNTIME
        _ACTIVE_RUNTIME = self.runtime
        _bind_runtime_exceptions(self.runtime)
        return self.runtime

    def __exit__(self, *_exc: object) -> None:
        global _ACTIVE_RUNTIME
        if not self._active:
            return
        self._active = False
        for name in _RUNTIME_MODULE_PATHS:
            sys.modules.pop(name, None)
        sys.modules.update(self._previous_modules)
        if self._inserted_site_packages is not None:
            site_value = str(self._inserted_site_packages)
            try:
                sys.path.remove(site_value)
            except ValueError:
                pass
        _ACTIVE_RUNTIME = self._previous_runtime
        _bind_runtime_exceptions(self._previous_runtime)


def _bind_runtime_exceptions(runtime: _RuntimeDependencies | None) -> None:
    for exported, (module_field, attribute) in _RUNTIME_EXCEPTION_BINDINGS.items():
        if runtime is None:
            globals().pop(exported, None)
            continue
        module = getattr(runtime, module_field)
        value = getattr(module, attribute, None)
        if not isinstance(value, type) or not issubclass(value, BaseException):
            _reject("hosted_runtime_contract")
        globals()[exported] = value


def _activate_locked_site_packages() -> Path | None:
    """Expose the uv-synced venv after `-S` without executing .pth files."""

    if not sys.flags.no_site:
        return None
    raw_environment = os.environ.get("VIRTUAL_ENV")
    expected_environment = ROOT / ".venv"
    if type(raw_environment) is not str or Path(raw_environment) != expected_environment:
        _reject("hosted_runtime_environment")
    site_packages = (
        expected_environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    )
    try:
        environment_info = expected_environment.lstat()
        site_info = site_packages.lstat()
        if (
            not stat.S_ISDIR(environment_info.st_mode)
            or not stat.S_ISDIR(site_info.st_mode)
            or environment_info.st_uid != os.getuid()
            or site_info.st_uid != os.getuid()
            or environment_info.st_mode & 0o022
            or site_info.st_mode & 0o022
            or expected_environment.resolve(strict=True) != expected_environment
            or site_packages.resolve(strict=True) != site_packages
        ):
            _reject("hosted_runtime_environment")
    except OSError:
        _reject("hosted_runtime_environment")
    site_value = str(site_packages)
    if site_value in sys.path:
        return None
    sys.path.append(site_value)
    return site_packages


def _runtime_from_payloads(payloads: Iterable[tuple[str, bytes]]) -> _RuntimeBinding:
    rows: dict[str, bytes] = {}
    try:
        for relative, payload in payloads:
            if type(relative) is not str or type(payload) is not bytes or relative in rows:
                _reject("hosted_runtime_source")
            rows[relative] = payload
    except (TypeError, ValueError):
        _reject("hosted_runtime_source")
    required_paths = frozenset(_RUNTIME_MODULE_PATHS.values())
    if not required_paths <= rows.keys():
        _reject("hosted_runtime_source")

    inserted_site_packages = _activate_locked_site_packages()
    previous_modules = {
        name: module for name in _RUNTIME_MODULE_PATHS if isinstance((module := sys.modules.get(name)), ModuleType)
    }
    for name in sorted(_RUNTIME_MODULE_PATHS, key=lambda value: value.count("."), reverse=True):
        sys.modules.pop(name, None)
    finder = _CapturedSourceFinder(rows)
    loaded: dict[str, ModuleType] = {}
    sys.meta_path.insert(0, finder)
    try:
        build_module = importlib.import_module("boron_browser_build_images")
        live_module = importlib.import_module("boron_browser_live_session")
        loaded = {
            name: module for name in _RUNTIME_MODULE_PATHS if isinstance((module := sys.modules.get(name)), ModuleType)
        }
        if set(loaded) != set(_RUNTIME_MODULE_PATHS):
            _reject("hosted_runtime_import")
        if (
            tuple(getattr(build_module, "HOSTED_BUILD_CONTEXT_PATHS", ())) != HOSTED_BUILD_CONTEXT_PATHS
            or getattr(build_module, "HOSTED_REPOSITORY", None) != HOSTED_REPOSITORY
            or str(getattr(build_module, "HOSTED_REPOSITORY_ID", "")) != HOSTED_REPOSITORY_ID
            or getattr(build_module, "CHROME_VERSION", None) != CHROME_VERSION
            or getattr(build_module, "BORON_MAX_SECURITY_LAG_MS", None) != BORON_MAX_SECURITY_LAG_MS
        ):
            _reject("hosted_runtime_contract")
        rejection_types = tuple(
            getattr(
                build_module if module_field == "build_module" else live_module,
                attribute,
            )
            for module_field, attribute in _RUNTIME_EXCEPTION_BINDINGS.values()
        )
        if any(not isinstance(value, type) or not issubclass(value, BaseException) for value in rejection_types):
            _reject("hosted_runtime_contract")
        runtime = _RuntimeDependencies(
            build_module=build_module,
            live_module=live_module,
            rejection_types=rejection_types,
        )
        return _RuntimeBinding(
            runtime=runtime,
            loaded=loaded,
            previous_modules=previous_modules,
            inserted_site_packages=inserted_site_packages,
        )
    except BaseException:
        for name, module in loaded.items():
            if sys.modules.get(name) is module:
                sys.modules.pop(name, None)
        for name in _RUNTIME_MODULE_PATHS:
            if name not in previous_modules:
                sys.modules.pop(name, None)
        sys.modules.update(previous_modules)
        if inserted_site_packages is not None:
            try:
                sys.path.remove(str(inserted_site_packages))
            except ValueError:
                pass
        raise
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)


def _runtime() -> _RuntimeDependencies:
    if _ACTIVE_RUNTIME is None:
        _reject("hosted_runtime_unverified")
    return _ACTIVE_RUNTIME


def _validated_build_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    return _runtime().live_module._validated_build_evidence(value)


def hosted_registry_tags(environment: Mapping[str, str]) -> tuple[str, str]:
    return _runtime().build_module.hosted_registry_tags(environment)


def canonical_build_context_archive(payloads: Iterable[tuple[str, bytes]]) -> bytes:
    return _runtime().build_module.canonical_build_context_archive(payloads)


def build_registry_images(
    *,
    environment: Mapping[str, str],
    qualification_source_digest: str,
    context_archive: bytes,
) -> dict[str, Any]:
    return _runtime().build_module.build_registry_images(
        environment=environment,
        qualification_source_digest=qualification_source_digest,
        context_archive=context_archive,
    )


def run_live_session(
    *,
    build_evidence: Mapping[str, Any],
    environment: Mapping[str, str],
    seccomp_profile: bytes,
) -> dict[str, Any]:
    return _runtime().live_module.run_live_session(
        build_evidence=build_evidence,
        environment=environment,
        seccomp_profile=seccomp_profile,
    )


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_mode, info.st_dev, info.st_ino)


def _read_exact_descriptor(
    descriptor: int,
    size: int,
    *,
    reason_code: str,
) -> bytes:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = bytearray()
        while len(payload) < size:
            chunk = os.read(descriptor, min(64 * 1024, size - len(payload)))
            if not chunk:
                _reject(reason_code)
            payload.extend(chunk)
        if os.read(descriptor, 1):
            _reject(reason_code)
    except OSError:
        _reject(reason_code)
    return bytes(payload)


def _strict_report(path: Path) -> tuple[dict[str, Any], str]:
    """Read one bounded regular report and return its exact subject digest."""

    try:
        info = path.lstat()
    except OSError:
        _reject("hosted_report_file")
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or not 1 <= info.st_size <= MAX_REPORT_BYTES
    ):
        _reject("hosted_report_file")

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size)
        ):
            _reject("hosted_report_changed")
        payload = bytearray()
        while len(payload) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(payload)))
            if not chunk:
                _reject("hosted_report_changed")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            _reject("hosted_report_changed")
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _reject("hosted_report_changed")
    except OSError:
        _reject("hosted_report_file")
    finally:
        if descriptor is not None:
            os.close(descriptor)

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for key, value in pairs:
            if key in row:
                _reject("hosted_report_duplicate_key")
            row[key] = value
        return row

    try:
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: _reject("hosted_report_number"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject("hosted_report_json")
    if type(document) is not dict:
        _reject("hosted_report_shape")
    return document, "sha256:" + hashlib.sha256(payload).hexdigest()


def _strict_report_document(path: Path) -> dict[str, Any]:
    """Compatibility wrapper for callers that only need the parsed document."""

    return _strict_report(path)[0]


def _write_passed_report(
    path: Path,
    report: Mapping[str, Any],
    *,
    guard: Callable[[], None] | None = None,
) -> None:
    """Publish exact passed bytes exclusively, with rollback on every late failure."""

    if report.get("status") != "passed":
        _reject("hosted_report_status")
    parent = path.parent
    name = path.name
    if not name or name in {".", ".."} or Path(name).name != name:
        _reject("hosted_report_output")
    try:
        parent_info = parent.lstat()
    except OSError:
        _reject("hosted_report_output")
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        _reject("hosted_report_output")
    payload = _canonical(report) + b"\n"
    if not 1 <= len(payload) <= MAX_REPORT_BYTES:
        _reject("hosted_report_output")

    parent_descriptor: int | None = None
    temporary_descriptor: int | None = None
    output_descriptor: int | None = None
    temporary = ""
    temporary_identity: tuple[int, int] | None = None
    published_identity: tuple[int, int] | None = None
    completed = False
    try:
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_parent = os.fstat(parent_descriptor)
        if not stat.S_ISDIR(opened_parent.st_mode) or _directory_identity(opened_parent) != _directory_identity(
            parent_info
        ):
            _reject("hosted_report_output")
        try:
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _reject("hosted_report_output")

        for _attempt in range(16):
            temporary = ".henry-boron-" + secrets.token_hex(16) + ".json.tmp"
            try:
                temporary_descriptor = os.open(
                    temporary,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                temporary = ""
                continue
            break
        if temporary_descriptor is None or not temporary:
            _reject("hosted_report_output")

        written = 0
        while written < len(payload):
            count = os.write(temporary_descriptor, payload[written:])
            if count < 1:
                _reject("hosted_report_output")
            written += count
        os.fchmod(temporary_descriptor, 0o600)
        os.fsync(temporary_descriptor)
        temporary_info = os.fstat(temporary_descriptor)
        temporary_identity = (temporary_info.st_dev, temporary_info.st_ino)
        if (
            not stat.S_ISREG(temporary_info.st_mode)
            or temporary_info.st_nlink != 1
            or temporary_info.st_size != len(payload)
            or _read_exact_descriptor(
                temporary_descriptor,
                temporary_info.st_size,
                reason_code="hosted_report_output",
            )
            != payload
        ):
            _reject("hosted_report_output")
        named_temporary = os.stat(
            temporary,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (named_temporary.st_dev, named_temporary.st_ino) != temporary_identity:
            _reject("hosted_report_output")

        if guard is not None:
            guard()
        os.link(
            temporary,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked_info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        published_identity = (linked_info.st_dev, linked_info.st_ino)
        output_descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened_output = os.fstat(output_descriptor)
        if (
            published_identity != temporary_identity
            or (opened_output.st_dev, opened_output.st_ino) != temporary_identity
            or not stat.S_ISREG(opened_output.st_mode)
            or opened_output.st_nlink != 2
            or opened_output.st_size != len(payload)
            or _read_exact_descriptor(
                output_descriptor,
                opened_output.st_size,
                reason_code="hosted_report_output",
            )
            != payload
        ):
            _reject("hosted_report_output")

        named_temporary = os.stat(
            temporary,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (named_temporary.st_dev, named_temporary.st_ino) != temporary_identity:
            _reject("hosted_report_output")
        os.unlink(temporary, dir_fd=parent_descriptor)
        temporary = ""
        final_output = os.fstat(output_descriptor)
        final_named_output = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        final_parent = parent.lstat()
        if (
            (final_output.st_dev, final_output.st_ino) != temporary_identity
            or final_output.st_nlink != 1
            or final_output.st_size != len(payload)
            or (final_named_output.st_dev, final_named_output.st_ino) != temporary_identity
            or _directory_identity(final_parent) != _directory_identity(opened_parent)
            or _read_exact_descriptor(
                output_descriptor,
                final_output.st_size,
                reason_code="hosted_report_output",
            )
            != payload
        ):
            _reject("hosted_report_output")
        if guard is not None:
            guard()
        os.fsync(parent_descriptor)
        if guard is not None:
            guard()
        completed = True
    except OSError:
        _reject("hosted_report_output")
    finally:
        if not completed and parent_descriptor is not None and published_identity is not None:
            try:
                current = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == published_identity:
                    os.unlink(name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        if temporary and parent_descriptor is not None and temporary_identity is not None:
            try:
                current = os.stat(
                    temporary,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == temporary_identity:
                    os.unlink(temporary, dir_fd=parent_descriptor)
            except OSError:
                pass
        for descriptor in (output_descriptor, temporary_descriptor, parent_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _percentile(values: list[int], percentile: float) -> int:
    if not values or not 0.0 <= percentile <= 1.0:
        _reject("hosted_duration")
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)),
    )
    return ordered[index]


def _wilson_interval(successes: int, trials: int) -> list[float]:
    if type(successes) is not int or type(trials) is not int or not 0 <= successes <= trials or trials < 1:
        _reject("hosted_denominator")
    z = 1.959963984540054
    proportion = successes / trials
    denominator = 1.0 + (z * z / trials)
    center = (proportion + (z * z / (2.0 * trials))) / denominator
    margin = z * ((proportion * (1.0 - proportion) / trials) + (z * z / (4.0 * trials * trials))) ** 0.5 / denominator
    return [
        round(max(0.0, center - margin), 6),
        round(min(1.0, center + margin), 6),
    ]


@dataclass(frozen=True, slots=True)
class _SourceDirectory:
    descriptor: int
    parent_descriptor: int | None
    name: str | None
    identity: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _SourceFile:
    relative: str
    descriptor: int
    parent_descriptor: int
    name: str
    identity: tuple[int, int, int, int, int, int, int]


def _source_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        info.st_mode,
        info.st_dev,
        info.st_ino,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _valid_source_info(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_nlink == 1
        and 1 <= info.st_size <= MAX_SOURCE_BYTES
    )


def _valid_directory_info(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_nlink >= 1


def _open_source_directory(
    *,
    parent_descriptor: int,
    name: str,
    reason_code: str,
) -> _SourceDirectory:
    descriptor: int | None = None
    try:
        named = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _valid_directory_info(named):
            _reject(reason_code)
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if not _valid_directory_info(opened) or _directory_identity(opened) != _directory_identity(named):
            _reject(reason_code)
        result = _SourceDirectory(
            descriptor=descriptor,
            parent_descriptor=parent_descriptor,
            name=name,
            identity=_directory_identity(opened),
        )
        descriptor = None
        return result
    except OSError:
        _reject(reason_code)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_source_directories(
    directories: tuple[_SourceDirectory, ...],
    *,
    reason_code: str,
) -> None:
    for directory in directories:
        try:
            opened = os.fstat(directory.descriptor)
            named = (
                os.stat("/", follow_symlinks=False)
                if directory.parent_descriptor is None
                else os.stat(
                    str(directory.name),
                    dir_fd=directory.parent_descriptor,
                    follow_symlinks=False,
                )
            )
        except OSError:
            _reject(reason_code)
        if (
            not _valid_directory_info(opened)
            or not _valid_directory_info(named)
            or _directory_identity(opened) != directory.identity
            or _directory_identity(named) != directory.identity
        ):
            _reject(reason_code)


def _read_source_file(entry: _SourceFile, *, reason_code: str) -> bytes:
    """Read one held descriptor and prove its directory entry stayed fixed."""

    try:
        before = os.fstat(entry.descriptor)
        path_before = os.stat(
            entry.name,
            dir_fd=entry.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not _valid_source_info(before)
            or not _valid_source_info(path_before)
            or _source_identity(before) != entry.identity
            or _source_identity(path_before) != entry.identity
        ):
            _reject(reason_code)
        payload = _read_exact_descriptor(
            entry.descriptor,
            before.st_size,
            reason_code=reason_code,
        )
        after = os.fstat(entry.descriptor)
        path_after = os.stat(
            entry.name,
            dir_fd=entry.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not _valid_source_info(after)
            or not _valid_source_info(path_after)
            or _source_identity(after) != entry.identity
            or _source_identity(path_after) != entry.identity
        ):
            _reject(reason_code)
    except OSError:
        _reject(reason_code)
    return payload


def _digest_source_files(
    entries: tuple[_SourceFile, ...],
    directories: tuple[_SourceDirectory, ...],
    *,
    reason_code: str,
) -> str:
    _verify_source_directories(directories, reason_code=reason_code)
    digest = hashlib.sha256()
    for entry in entries:
        payload = _read_source_file(entry, reason_code=reason_code)
        encoded = entry.relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    # Recheck every identity after the complete multi-file read. This closes the
    # window where an earlier path could be replaced while a later path is read.
    _verify_source_directories(directories, reason_code=reason_code)
    for entry in entries:
        try:
            descriptor_info = os.fstat(entry.descriptor)
            path_info = os.stat(
                entry.name,
                dir_fd=entry.parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            _reject(reason_code)
        if (
            not _valid_source_info(descriptor_info)
            or not _valid_source_info(path_info)
            or _source_identity(descriptor_info) != entry.identity
            or _source_identity(path_info) != entry.identity
        ):
            _reject(reason_code)
    return "sha256:" + digest.hexdigest()


class _SourceSnapshot:
    """A no-follow, ancestry-pinned source snapshot for one qualification."""

    __slots__ = ("_closed", "_directories", "_entries", "digest", "root", "source_paths")

    def __init__(
        self,
        *,
        root: Path,
        source_paths: tuple[str, ...],
        directories: tuple[_SourceDirectory, ...],
        entries: tuple[_SourceFile, ...],
    ) -> None:
        self.root = root
        self.source_paths = source_paths
        self._directories = directories
        self._entries = entries
        self._closed = False
        self.digest = _digest_source_files(
            entries,
            directories,
            reason_code="hosted_source_changed",
        )

    @classmethod
    def capture(
        cls,
        *,
        root: Path | None = None,
        source_paths: tuple[str, ...] | None = None,
    ) -> "_SourceSnapshot":
        selected_root = ROOT if root is None else root
        selected_paths = SOURCE_PATHS if source_paths is None else source_paths
        if not isinstance(selected_root, Path) or not selected_root.is_absolute():
            _reject("hosted_source_identity")
        normalized_root = Path(os.path.abspath(os.fspath(selected_root)))
        if normalized_root != selected_root:
            _reject("hosted_source_identity")
        relative_paths = sorted(selected_paths)
        if (
            not relative_paths
            or len(relative_paths) != len(set(relative_paths))
            or any(
                type(relative) is not str
                or not relative
                or Path(relative).is_absolute()
                or any(part in {"", ".", ".."} for part in Path(relative).parts)
                or Path(*Path(relative).parts).as_posix() != relative
                or _CONTROL_RE.search(relative) is not None
                for relative in relative_paths
            )
        ):
            _reject("hosted_source_identity")
        directories: list[_SourceDirectory] = []
        entries: list[_SourceFile] = []
        try:
            root_descriptor = os.open(
                "/",
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_info = os.fstat(root_descriptor)
            named_root = os.stat("/", follow_symlinks=False)
            if not _valid_directory_info(root_info) or _directory_identity(root_info) != _directory_identity(
                named_root
            ):
                os.close(root_descriptor)
                _reject("hosted_source_identity")
            directories.append(
                _SourceDirectory(
                    descriptor=root_descriptor,
                    parent_descriptor=None,
                    name=None,
                    identity=_directory_identity(root_info),
                )
            )
            current_directory = directories[0]
            for component in selected_root.parts[1:]:
                current_directory = _open_source_directory(
                    parent_descriptor=current_directory.descriptor,
                    name=component,
                    reason_code="hosted_source_identity",
                )
                directories.append(current_directory)
            source_root = current_directory
            relative_directories: dict[tuple[str, ...], _SourceDirectory] = {(): source_root}
            for relative in relative_paths:
                parts = Path(relative).parts
                parent_parts: tuple[str, ...] = ()
                parent = source_root
                for component in parts[:-1]:
                    directory_key = parent_parts + (component,)
                    existing = relative_directories.get(directory_key)
                    if existing is None:
                        existing = _open_source_directory(
                            parent_descriptor=parent.descriptor,
                            name=component,
                            reason_code="hosted_source_identity",
                        )
                        relative_directories[directory_key] = existing
                        directories.append(existing)
                    parent = existing
                    parent_parts = directory_key
                name = parts[-1]
                try:
                    info = os.stat(
                        name,
                        dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    _reject("hosted_source_identity")
                if not _valid_source_info(info):
                    _reject("hosted_source_identity")
                descriptor: int | None = None
                try:
                    descriptor = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_NONBLOCK", 0),
                        dir_fd=parent.descriptor,
                    )
                    opened = os.fstat(descriptor)
                    if not _valid_source_info(opened) or _source_identity(opened) != _source_identity(info):
                        _reject("hosted_source_changed")
                    entries.append(
                        _SourceFile(
                            relative=relative,
                            descriptor=descriptor,
                            parent_descriptor=parent.descriptor,
                            name=name,
                            identity=_source_identity(opened),
                        )
                    )
                    descriptor = None
                except OSError:
                    _reject("hosted_source_identity")
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            return cls(
                root=selected_root,
                source_paths=tuple(relative_paths),
                directories=tuple(directories),
                entries=tuple(entries),
            )
        except BaseException:
            for entry in entries:
                try:
                    os.close(entry.descriptor)
                except OSError:
                    pass
            for directory in reversed(directories):
                try:
                    os.close(directory.descriptor)
                except OSError:
                    pass
            raise

    def verify(self) -> None:
        if self._closed:
            _reject("hosted_source_changed")
        if (
            _digest_source_files(
                self._entries,
                self._directories,
                reason_code="hosted_source_changed",
            )
            != self.digest
        ):
            _reject("hosted_source_changed")

    def payloads(self) -> tuple[tuple[str, bytes], ...]:
        self.verify()
        payloads = tuple(
            (
                entry.relative,
                _read_source_file(entry, reason_code="hosted_source_changed"),
            )
            for entry in self._entries
        )
        self.verify()
        return payloads

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for entry in self._entries:
            try:
                os.close(entry.descriptor)
            except OSError:
                pass
        for directory in reversed(self._directories):
            try:
                os.close(directory.descriptor)
            except OSError:
                pass

    def __enter__(self) -> "_SourceSnapshot":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _run_git_object(
    arguments: tuple[str, ...],
    *,
    maximum_bytes: int,
    root: Path | None = None,
) -> bytes:
    """Run one fixed Git object query with bounded, content-free failure."""

    selected_root = ROOT if root is None else root
    if (
        type(arguments) is not tuple
        or not arguments
        or any(type(value) is not str or not value or _CONTROL_RE.search(value) is not None for value in arguments)
        or type(maximum_bytes) is not int
        or not 1 <= maximum_bytes <= MAX_SOURCE_BYTES + 1
        or not isinstance(selected_root, Path)
        or not selected_root.is_absolute()
    ):
        _reject("hosted_git_object")
    try:
        executable = GIT_EXECUTABLE.lstat()
        if (
            not stat.S_ISREG(executable.st_mode)
            or executable.st_uid != 0
            or executable.st_mode & 0o022
            or GIT_EXECUTABLE.resolve(strict=True) != GIT_EXECUTABLE
        ):
            _reject("hosted_git_executable")
    except OSError:
        _reject("hosted_git_executable")

    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            [
                str(GIT_EXECUTABLE),
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(selected_root),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
        if process.stdout is None:
            _reject("hosted_git_object")
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + GIT_OBJECT_TIMEOUT_SECONDS
        payload = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _reject("hosted_git_object")
            if not selector.select(remaining):
                _reject("hosted_git_object")
            try:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, maximum_bytes + 1 - len(payload)),
                )
            except BlockingIOError:
                continue
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                _reject("hosted_git_object")
        remaining = max(0.001, deadline - time.monotonic())
        if process.wait(timeout=remaining) != 0 or not payload:
            _reject("hosted_git_object")
        return bytes(payload)
    except HostedQualificationRejected:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        _reject("hosted_git_object")
    finally:
        if selector is not None:
            try:
                selector.close()
            except OSError:
                pass
        if process is not None:
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass


def _verified_revision_payloads(
    snapshot: _SourceSnapshot,
    revision: str,
    *,
    git_query: Callable[..., bytes] = _run_git_object,
) -> tuple[tuple[str, bytes], ...]:
    """Bind every captured source byte to its exact Git-tree blob at revision."""

    if type(snapshot) is not _SourceSnapshot or type(revision) is not str or _REVISION_RE.fullmatch(revision) is None:
        _reject("hosted_revision_source")
    snapshot.verify()
    payloads = dict(snapshot.payloads())
    object_type = git_query(
        ("cat-file", "-t", revision),
        maximum_bytes=16,
        root=snapshot.root,
    )
    if object_type != b"commit\n":
        _reject("hosted_revision_source")
    tree = git_query(
        (
            "ls-tree",
            "-rz",
            "--full-tree",
            revision,
            "--",
            *snapshot.source_paths,
        ),
        maximum_bytes=MAX_GIT_TREE_BYTES,
        root=snapshot.root,
    )
    entries: dict[str, str] = {}
    for raw in tree.split(b"\x00"):
        if not raw:
            continue
        try:
            metadata, raw_path = raw.split(b"\t", 1)
            mode, kind, raw_object = metadata.split(b" ", 2)
            relative = raw_path.decode("ascii", errors="strict")
            object_id = raw_object.decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError):
            _reject("hosted_revision_tree")
        if (
            mode not in {b"100644", b"100755"}
            or kind != b"blob"
            or _REVISION_RE.fullmatch(object_id) is None
            or relative not in payloads
            or relative in entries
        ):
            _reject("hosted_revision_tree")
        entries[relative] = object_id
    if set(entries) != set(payloads):
        _reject("hosted_revision_tree")
    for relative in snapshot.source_paths:
        expected = git_query(
            ("cat-file", "blob", entries[relative]),
            maximum_bytes=MAX_SOURCE_BYTES + 1,
            root=snapshot.root,
        )
        if expected != payloads[relative]:
            _reject("hosted_revision_mismatch")
    snapshot.verify()
    return tuple((relative, payloads[relative]) for relative in snapshot.source_paths)


def _source_digest() -> str:
    with _SourceSnapshot.capture() as snapshot:
        return snapshot.digest


def _bounded_text(value: Any, reason_code: str, *, maximum: int = 512) -> str:
    if type(value) is not str or _CONTROL_RE.search(value) is not None:
        _reject(reason_code)
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        _reject(reason_code)
    if not 1 <= size <= maximum:
        _reject(reason_code)
    return value


def _positive_integer(value: Any, reason_code: str) -> int:
    if type(value) is not str or _INTEGER_RE.fullmatch(value) is None:
        _reject(reason_code)
    parsed = int(value)
    if not 1 <= parsed <= (1 << 53) - 1:
        _reject(reason_code)
    return parsed


@dataclass(frozen=True, slots=True)
class HostedRunnerContext:
    source_revision: str
    source_ref: str
    repository: str
    repository_id: int
    run_id: int
    run_attempt: int
    event_name: str
    ref_protected: bool
    workflow_revision: str
    workflow_ref_digest: str
    runner_environment: str
    runner_os: str
    runner_arch: str
    native_platform: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "HostedRunnerContext":
        if type(environment) is not dict or environment.get("GITHUB_ACTIONS") != "true":
            _reject("hosted_environment")
        revision = environment.get("GITHUB_SHA")
        if type(revision) is not str or _REVISION_RE.fullmatch(revision) is None:
            _reject("hosted_revision")
        runner_os = environment.get("RUNNER_OS")
        runner_arch = environment.get("RUNNER_ARCH")
        if runner_os != "Linux" or runner_arch != "X64":
            _reject("hosted_native_amd64_required")
        event_name = environment.get("GITHUB_EVENT_NAME")
        if event_name != "push":
            _reject("hosted_event")
        source_ref = environment.get("GITHUB_REF")
        if source_ref != "refs/heads/main" or environment.get("GITHUB_REF_PROTECTED") != "true":
            _reject("hosted_protected_ref")
        workflow_revision = environment.get("GITHUB_WORKFLOW_SHA")
        if workflow_revision != revision:
            _reject("hosted_workflow_revision")
        runner_environment = environment.get("RUNNER_ENVIRONMENT")
        if runner_environment != "github-hosted":
            _reject("hosted_runner_environment")
        repository = environment.get("GITHUB_REPOSITORY")
        repository_id = _positive_integer(
            environment.get("GITHUB_REPOSITORY_ID"),
            "hosted_repository_id",
        )
        if repository != HOSTED_REPOSITORY or repository_id != int(HOSTED_REPOSITORY_ID):
            _reject("hosted_repository")
        workflow_ref = _bounded_text(
            environment.get("GITHUB_WORKFLOW_REF"),
            "hosted_workflow_ref",
        )
        if workflow_ref != (HOSTED_REPOSITORY + "/.github/workflows/oliver-ci.yml@refs/heads/main"):
            _reject("hosted_workflow_ref")
        return cls(
            source_revision=revision,
            source_ref=source_ref,
            repository=repository,
            repository_id=repository_id,
            run_id=_positive_integer(environment.get("GITHUB_RUN_ID"), "hosted_run_id"),
            run_attempt=_positive_integer(
                environment.get("GITHUB_RUN_ATTEMPT"),
                "hosted_run_attempt",
            ),
            event_name=event_name,
            ref_protected=True,
            workflow_revision=workflow_revision,
            workflow_ref_digest="sha256:" + hashlib.sha256(workflow_ref.encode("utf-8")).hexdigest(),
            runner_environment=runner_environment,
            runner_os=runner_os,
            runner_arch=runner_arch,
            native_platform="linux/amd64",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "native_platform": self.native_platform,
            "ref_protected": self.ref_protected,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "run_attempt": self.run_attempt,
            "run_id": self.run_id,
            "runner_arch": self.runner_arch,
            "runner_environment": self.runner_environment,
            "runner_os": self.runner_os,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "workflow_revision": self.workflow_revision,
            "workflow_ref_digest": self.workflow_ref_digest,
        }


def _stderr_evidence(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"byte_count", "digest"}:
        _reject("hosted_stderr_evidence")
    byte_count = value["byte_count"]
    digest = value["digest"]
    if (
        type(byte_count) is not int
        or not 0 <= byte_count <= 1_048_576
        or type(digest) is not str
        or _DIGEST_RE.fullmatch(digest) is None
    ):
        _reject("hosted_stderr_evidence")
    return {"byte_count": byte_count, "digest": digest}


def _validated_live_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _LIVE_EVIDENCE_KEYS:
        _reject("hosted_live_evidence_shape")
    evidence = dict(value)
    if (
        type(evidence["schema_version"]) is not int
        or evidence["schema_version"] != 2
        or evidence["platform"] != "linux/amd64"
        or evidence["internal_participant_count"] != 2
        or evidence["browser_state"] != "verified"
        or evidence["broker_disposition"] != "verified"
    ):
        _reject("hosted_live_evidence_identity")
    for field in (
        "qualification_source_digest",
        "browser_index_digest",
        "browser_platform_manifest_digest",
        "browser_config_digest",
        "browser_build_metadata_digest",
        "browser_provenance_digest",
        "browser_sbom_digest",
        "broker_index_digest",
        "broker_platform_manifest_digest",
        "broker_config_digest",
        "broker_build_metadata_digest",
        "broker_provenance_digest",
        "broker_sbom_digest",
        "broker_code_digest",
        "topology_evidence_digest",
        "browser_security_source_digest",
        "target_decision_digest",
        "ca_certificate_digest",
    ):
        if type(evidence[field]) is not str or _DIGEST_RE.fullmatch(evidence[field]) is None:
            _reject("hosted_live_evidence_digest")
    exact_positive = (
        "browser_major",
        "browser_command_count",
        "browser_event_count",
        "broker_connection_count",
        "broker_request_count",
        "broker_bytes_to_browser",
    )
    if any(type(evidence[field]) is not int or not 1 <= evidence[field] <= (1 << 53) - 1 for field in exact_positive):
        _reject("hosted_live_evidence_count")
    if evidence["browser_major"] != int(CHROME_VERSION.split(".", 1)[0]):
        _reject("hosted_browser_major")
    lag = evidence["browser_security_update_lag_ms"]
    redirects = evidence["broker_redirect_count"]
    if (
        type(lag) is not int
        or not 0 <= lag <= BORON_MAX_SECURITY_LAG_MS
        or type(redirects) is not int
        or not 0 <= redirects <= 2
    ):
        _reject("hosted_live_evidence_count")
    evidence["browser_stderr"] = _stderr_evidence(evidence["browser_stderr"])
    evidence["broker_stderr"] = _stderr_evidence(evidence["broker_stderr"])
    return evidence


def _assert_build_live_binding(
    build: Mapping[str, Any],
    live: Mapping[str, Any],
) -> None:
    """Cross-bind each live result to the one source-attested image build."""

    if type(build) is not dict or type(live) is not dict:
        _reject("hosted_build_live_binding")
    bindings = (
        (
            "qualification_source_digest",
            "qualification_source_digest",
            "hosted_qualification_source_binding",
        ),
        (
            "browser_index_digest",
            "browser_index_digest",
            "hosted_browser_build_binding",
        ),
        (
            "browser_platform_manifest_digest",
            "browser_platform_manifest_digest",
            "hosted_browser_platform_binding",
        ),
        (
            "browser_config_digest",
            "browser_config_digest",
            "hosted_browser_config_binding",
        ),
        (
            "browser_build_metadata_digest",
            "browser_build_metadata_digest",
            "hosted_browser_metadata_binding",
        ),
        (
            "browser_provenance_digest",
            "browser_provenance_digest",
            "hosted_browser_provenance_binding",
        ),
        (
            "browser_sbom_digest",
            "browser_sbom_digest",
            "hosted_browser_sbom_binding",
        ),
        (
            "broker_index_digest",
            "broker_index_digest",
            "hosted_broker_build_binding",
        ),
        (
            "broker_platform_manifest_digest",
            "broker_platform_manifest_digest",
            "hosted_broker_platform_binding",
        ),
        (
            "broker_config_digest",
            "broker_config_digest",
            "hosted_broker_config_binding",
        ),
        (
            "broker_build_metadata_digest",
            "broker_build_metadata_digest",
            "hosted_broker_metadata_binding",
        ),
        (
            "broker_provenance_digest",
            "broker_provenance_digest",
            "hosted_broker_provenance_binding",
        ),
        (
            "broker_sbom_digest",
            "broker_sbom_digest",
            "hosted_broker_sbom_binding",
        ),
        ("broker_code_digest", "broker_code_digest", "hosted_broker_code_binding"),
        (
            "browser_security_source_digest",
            "browser_security_source_digest",
            "hosted_release_evidence_binding",
        ),
    )
    for build_field, live_field, reason_code in bindings:
        if build.get(build_field) != live.get(live_field):
            _reject(reason_code)


def build_hosted_report(
    *,
    context: HostedRunnerContext,
    build_evidence: Mapping[str, Any],
    repetitions: list[tuple[Mapping[str, Any], int]],
    generated_at: str,
    source_digest: str,
) -> dict[str, Any]:
    if type(context) is not HostedRunnerContext:
        _reject("hosted_context")
    build = _validated_build_evidence(build_evidence)
    expected_browser_tag, expected_broker_tag = hosted_registry_tags(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": context.event_name,
            "GITHUB_REPOSITORY": context.repository,
            "GITHUB_REPOSITORY_ID": str(context.repository_id),
            "GITHUB_REF": context.source_ref,
            "GITHUB_REF_PROTECTED": "true" if context.ref_protected else "false",
            "GITHUB_RUN_ATTEMPT": str(context.run_attempt),
            "GITHUB_RUN_ID": str(context.run_id),
            "GITHUB_SHA": context.source_revision,
            "GITHUB_WORKFLOW_SHA": context.workflow_revision,
            "GITHUB_WORKFLOW_REF": (HOSTED_REPOSITORY + "/.github/workflows/oliver-ci.yml@refs/heads/main"),
            "RUNNER_ARCH": context.runner_arch,
            "RUNNER_ENVIRONMENT": context.runner_environment,
            "RUNNER_OS": context.runner_os,
        }
    )
    if (
        build["browser_tag"] != expected_browser_tag
        or build["broker_tag"] != expected_broker_tag
        or build["browser_repository"] != expected_browser_tag.rsplit(":", 1)[0]
        or build["broker_repository"] != expected_broker_tag.rsplit(":", 1)[0]
    ):
        _reject("hosted_registry_build_binding")
    if not MIN_REPETITIONS <= len(repetitions) <= MAX_REPETITIONS:
        _reject("hosted_repetitions")
    try:
        parsed_generated_at = datetime.strptime(
            generated_at,
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        _reject("hosted_report_identity")
    if (
        parsed_generated_at.isoformat().replace("+00:00", "Z") != generated_at
        or type(source_digest) is not str
        or _DIGEST_RE.fullmatch(source_digest) is None
        or build.get("qualification_source_digest") != source_digest
    ):
        _reject("hosted_report_identity")

    rows: list[dict[str, Any]] = []
    durations: list[int] = []
    topology_digests: set[str] = set()
    browser_index_digest = ""
    broker_index_digest = ""
    for index, (raw, duration_ms) in enumerate(repetitions, start=1):
        if type(duration_ms) is not int or not 1 <= duration_ms <= MAX_DURATION_MS:
            _reject("hosted_duration")
        evidence = _validated_live_evidence(raw)
        _assert_build_live_binding(build, evidence)
        if index == 1:
            browser_index_digest = evidence["browser_index_digest"]
            broker_index_digest = evidence["broker_index_digest"]
        elif (
            evidence["browser_index_digest"] != browser_index_digest
            or evidence["broker_index_digest"] != broker_index_digest
        ):
            _reject("hosted_image_changed")
        topology = evidence["topology_evidence_digest"]
        if topology in topology_digests:
            _reject("hosted_topology_reused")
        topology_digests.add(topology)
        durations.append(duration_ms)
        rows.append(
            {
                "duration_ms": duration_ms,
                "evidence": evidence,
                "evidence_digest": _digest(evidence),
                "repetition": index,
                "session_state": "fresh_ephemeral",
            }
        )

    summary = {
        "completed": len(rows),
        "denominator": len(rows),
        "duration_p50_ms": _percentile(durations, 0.50),
        "duration_p95_ms": _percentile(durations, 0.95),
        "maximum_security_update_lag_ms": max(row["evidence"]["browser_security_update_lag_ms"] for row in rows),
        "native_amd64": True,
        "rate": 1.0,
        "unique_ephemeral_topologies": len(topology_digests),
        "wilson_95": _wilson_interval(len(rows), len(rows)),
    }
    report: dict[str, Any] = {
        "schema_version": 2,
        "status": "passed",
        "public_claim_eligible": False,
        "generated_at": generated_at,
        "source_digest": source_digest,
        "runner": context.to_dict(),
        "build_evidence": build,
        "build_evidence_digest": _digest(build),
        "repetitions": rows,
        "summary": summary,
        "supports": ["HARD-050"],
        "limitation": HOSTED_LIMITATION,
    }
    report["evidence_digest"] = _digest(report)
    return report


def verify_hosted_report(
    value: Mapping[str, Any],
    *,
    environment: Mapping[str, str],
    verified_source_digest: str | None = None,
) -> dict[str, Any]:
    """Reconstruct a passed report and bind it to this exact hosted run/source."""

    context = HostedRunnerContext.from_environment(environment)
    if verified_source_digest is None:
        with _SourceSnapshot.capture() as snapshot:
            payloads = _verified_revision_payloads(snapshot, context.source_revision)
            if _ACTIVE_RUNTIME is not None:
                return verify_hosted_report(
                    value,
                    environment=environment,
                    verified_source_digest=snapshot.digest,
                )
            with _runtime_from_payloads(payloads):
                return verify_hosted_report(
                    value,
                    environment=environment,
                    verified_source_digest=snapshot.digest,
                )

    expected_keys = {
        "schema_version",
        "status",
        "public_claim_eligible",
        "generated_at",
        "source_digest",
        "runner",
        "build_evidence",
        "build_evidence_digest",
        "repetitions",
        "summary",
        "supports",
        "limitation",
        "evidence_digest",
    }
    if type(value) is not dict or set(value) != expected_keys:
        _reject("hosted_report_shape")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
        or value.get("status") != "passed"
        or value.get("public_claim_eligible") is not False
        or value.get("supports") != ["HARD-050"]
    ):
        _reject("hosted_report_status")

    source_digest = verified_source_digest
    if type(source_digest) is not str or _DIGEST_RE.fullmatch(source_digest) is None:
        _reject("hosted_report_source")
    if value.get("source_digest") != source_digest:
        _reject("hosted_report_source")
    if value.get("runner") != context.to_dict():
        _reject("hosted_report_runner")

    raw_rows = value.get("repetitions")
    if type(raw_rows) is not list or not MIN_REPETITIONS <= len(raw_rows) <= MAX_REPETITIONS:
        _reject("hosted_report_repetitions")
    repetitions: list[tuple[Mapping[str, Any], int]] = []
    for index, raw in enumerate(raw_rows, start=1):
        if type(raw) is not dict or set(raw) != {
            "duration_ms",
            "evidence",
            "evidence_digest",
            "repetition",
            "session_state",
        }:
            _reject("hosted_report_repetition_shape")
        evidence = raw["evidence"]
        if (
            type(evidence) is not dict
            or raw["repetition"] != index
            or raw["session_state"] != "fresh_ephemeral"
            or raw["evidence_digest"] != _digest(evidence)
        ):
            _reject("hosted_report_repetition_identity")
        repetitions.append((evidence, raw["duration_ms"]))

    reconstructed = build_hosted_report(
        context=context,
        build_evidence=value["build_evidence"],
        repetitions=repetitions,
        generated_at=value["generated_at"],
        source_digest=source_digest,
    )
    if _canonical(reconstructed) != _canonical(value):
        _reject("hosted_report_mismatch")
    return reconstructed


def run_hosted_qualification(
    *,
    environment: Mapping[str, str],
    repetitions: int,
    build: Callable[[], Mapping[str, Any]] | None = None,
    session: Callable[..., Mapping[str, Any]] | None = None,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    source_snapshot: _SourceSnapshot | None = None,
    revision_verifier: Callable[..., tuple[tuple[str, bytes], ...]] = _verified_revision_payloads,
) -> dict[str, Any]:
    if type(repetitions) is not int or not MIN_REPETITIONS <= repetitions <= MAX_REPETITIONS:
        _reject("hosted_repetitions")
    context = HostedRunnerContext.from_environment(environment)
    owned_snapshot = source_snapshot is None
    snapshot = _SourceSnapshot.capture() if source_snapshot is None else source_snapshot
    try:
        if type(snapshot) is not _SourceSnapshot:
            _reject("hosted_source_identity")
        snapshot.verify()
        verified_payloads = revision_verifier(snapshot, context.source_revision)
        if (
            type(verified_payloads) is not tuple
            or tuple(relative for relative, _payload in verified_payloads) != snapshot.source_paths
        ):
            _reject("hosted_revision_source")
        snapshot.verify()
        source_digest = snapshot.digest
        payload_rows = dict(verified_payloads)

        def execute() -> dict[str, Any]:
            if build is None:
                if snapshot.source_paths != tuple(sorted(HOSTED_BUILD_CONTEXT_PATHS)):
                    _reject("hosted_build_context_paths")
                context_archive = canonical_build_context_archive(verified_payloads)
                snapshot.verify()

                def build_operation() -> Mapping[str, Any]:
                    return build_registry_images(
                        environment=environment,
                        qualification_source_digest=source_digest,
                        context_archive=context_archive,
                    )

            else:
                build_operation = build
            raw_build_evidence = build_operation()
            # This is the first boundary after the two-image registry build. Never
            # validate or execute evidence produced from a source tree that changed.
            snapshot.verify()
            build_evidence = _validated_build_evidence(raw_build_evidence)
            seccomp_profile = payload_rows.get("algo_cli/resources/boron_browser/boron_seccomp_profile.json")
            if session is None and type(seccomp_profile) is not bytes:
                _reject("hosted_seccomp_source")
            results: list[tuple[Mapping[str, Any], int]] = []
            for _index in range(repetitions):
                snapshot.verify()
                started = monotonic_ns()
                raw_evidence: Mapping[str, Any]
                if session is None:
                    if type(seccomp_profile) is not bytes:
                        _reject("hosted_seccomp_source")
                    raw_evidence = run_live_session(
                        build_evidence=build_evidence,
                        environment=environment,
                        seccomp_profile=seccomp_profile,
                    )
                else:
                    raw_evidence = session(
                        build_evidence=build_evidence,
                        environment=environment,
                    )
                snapshot.verify()
                evidence = _validated_live_evidence(raw_evidence)
                finished = monotonic_ns()
                if type(started) is not int or type(finished) is not int or finished <= started:
                    _reject("hosted_clock")
                duration_ms = max(1, (finished - started + 999_999) // 1_000_000)
                results.append((evidence, duration_ms))
            observed = now()
            if type(observed) is not datetime or observed.tzinfo is None:
                _reject("hosted_clock")
            generated_at = observed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            snapshot.verify()
            report = build_hosted_report(
                context=context,
                build_evidence=build_evidence,
                repetitions=results,
                generated_at=generated_at,
                source_digest=source_digest,
            )
            snapshot.verify()
            return report

        if _ACTIVE_RUNTIME is not None:
            try:
                return execute()
            except _ACTIVE_RUNTIME.rejection_types as error:
                _reject(_normalized_rejection_reason(error))
        binding = _runtime_from_payloads(verified_payloads)
        with binding as runtime:
            try:
                return execute()
            except runtime.rejection_types as error:
                _reject(_normalized_rejection_reason(error))
    finally:
        if owned_snapshot:
            snapshot.close()


def _normalized_rejection_reason(error: BaseException) -> str:
    if isinstance(error, HostedQualificationRejected):
        return error.reason_code
    runtime = _ACTIVE_RUNTIME
    if runtime is not None and isinstance(error, runtime.build_module.BuildRejected):
        return "hosted_build_failed"
    if runtime is not None and isinstance(error, runtime.live_module.LiveSessionRejected):
        return "hosted_live_failed"
    return "hosted_dependency_failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--repetitions", type=int)
    mode.add_argument("--verify-report", type=Path)
    parser.add_argument("--output-report", type=Path)
    parser.add_argument("--subject-digest-only", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.verify_report is not None and arguments.output_report is not None:
        parser.error("--output-report cannot be combined with --verify-report")
    if arguments.subject_digest_only and arguments.verify_report is None:
        parser.error("--subject-digest-only requires --verify-report")
    source_snapshot: _SourceSnapshot | None = None
    try:
        environment = dict(os.environ)
        context = HostedRunnerContext.from_environment(environment)
        source_snapshot = _SourceSnapshot.capture()
        verified_payloads = _verified_revision_payloads(
            source_snapshot,
            context.source_revision,
        )
        subject_digest: str | None = None

        def reuse_verified_payloads(
            candidate: _SourceSnapshot,
            revision: str,
        ) -> tuple[tuple[str, bytes], ...]:
            if candidate is not source_snapshot or revision != context.source_revision:
                _reject("hosted_revision_source")
            candidate.verify()
            return verified_payloads

        def execute() -> int:
            nonlocal subject_digest
            if arguments.verify_report is not None:
                document, subject_digest = _strict_report(arguments.verify_report)
                report = verify_hosted_report(
                    document,
                    environment=environment,
                    verified_source_digest=source_snapshot.digest,
                )
            else:
                report = run_hosted_qualification(
                    environment=environment,
                    repetitions=(MIN_REPETITIONS if arguments.repetitions is None else arguments.repetitions),
                    source_snapshot=source_snapshot,
                    revision_verifier=reuse_verified_payloads,
                )
                source_snapshot.verify()
                if arguments.output_report is not None:
                    source_snapshot.verify()
                    _write_passed_report(
                        arguments.output_report,
                        report,
                        guard=source_snapshot.verify,
                    )
            if arguments.subject_digest_only:
                if subject_digest is None:
                    return 1
                print(subject_digest)
            else:
                source_snapshot.verify()
                print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        if _ACTIVE_RUNTIME is not None:
            try:
                return execute()
            except _ACTIVE_RUNTIME.rejection_types as error:
                _reject(_normalized_rejection_reason(error))
        with _runtime_from_payloads(verified_payloads) as runtime:
            try:
                return execute()
            except runtime.rejection_types as error:
                _reject(_normalized_rejection_reason(error))
    except HostedQualificationRejected as error:
        reason_code = _normalized_rejection_reason(error)
        print(
            json.dumps(
                {"reason_code": reason_code, "status": "blocked"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {"reason_code": "hosted_internal_error", "status": "failed"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    finally:
        if source_snapshot is not None:
            source_snapshot.close()


if __name__ == "__main__":
    raise SystemExit(main())
