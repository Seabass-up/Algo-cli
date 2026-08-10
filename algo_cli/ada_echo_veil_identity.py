"""Algo-owned source identity for the qualified Echo Veil runtime.

This module is deliberately stdlib-only so both the runtime bridge and Henry's
dependency audit can authenticate Echo source bytes before importing Echo.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import stat
from types import ModuleType
from typing import Sequence


QUALIFIED_ECHO_SOURCE_PATHS = (
    "echo_veil/__init__.py",
    "echo_veil/_bounded_process.py",
    "echo_veil/_json.py",
    "echo_veil/agent_cli.py",
    "echo_veil/agent_memory.py",
    "echo_veil/agent_preflight.py",
    "echo_veil/agent_security.py",
    "echo_veil/ann.py",
    "echo_veil/archive.py",
    "echo_veil/capability.py",
    "echo_veil/caretaker.py",
    "echo_veil/cloudflare_provider.py",
    "echo_veil/confidence.py",
    "echo_veil/conflict.py",
    "echo_veil/crypto_shield.py",
    "echo_veil/deployment.py",
    "echo_veil/drift.py",
    "echo_veil/guarded_runner.py",
    "echo_veil/memory_layers.py",
    "echo_veil/migration.py",
    "echo_veil/oracle.py",
    "echo_veil/persistence.py",
    "echo_veil/proximity.py",
    "echo_veil/vectors.py",
    "echo_veil/vine.py",
    "echo_veil/workspace.py",
    "echo_veil/zkp.py",
    "echo_veil_origin/__init__.py",
    "echo_veil_origin/_json.py",
    "echo_veil_origin/app.py",
    "echo_veil_origin/core.py",
    "echo_veil_origin/openfhe_engine.py",
    "echo_veil_origin/proof_verifier.py",
)
QUALIFIED_ECHO_SOURCE_TREE_SHA256 = "85c851878e117dfb6087806e57e9a676ef1b2eff9cb70acf44b99930121f5faa"
MAX_QUALIFIED_SOURCE_BYTES = 16 * 1024 * 1024


class QualifiedEchoSourceError(RuntimeError):
    """The installed Echo source tree differs from Algo's qualified commit."""


def _reject(reason: str) -> None:
    raise QualifiedEchoSourceError(reason)


@dataclass(frozen=True)
class QualifiedEchoSourceSnapshot:
    """Immutable source bytes captured from the qualified package tree."""

    distribution_root: Path
    tree_sha256: str
    files: tuple[tuple[str, bytes], ...]

    def payload_for_module(self, fullname: str) -> tuple[str, bytes, bool] | None:
        selected_package = ""
        for package_name in ("echo_veil", "echo_veil_origin"):
            if fullname == package_name:
                relative = f"{package_name}/__init__.py"
                package = True
                selected_package = package_name
                break
            if fullname.startswith(f"{package_name}.") and fullname.count(".") == 1:
                relative = f"{package_name}/{fullname.rsplit('.', 1)[1]}.py"
                package = False
                selected_package = package_name
                break
        if not selected_package:
            return None
        for path, payload in self.files:
            if path == relative:
                return path, payload, package
        return None


class _QualifiedEchoSnapshotLoader(importlib.abc.Loader):
    def __init__(
        self,
        snapshot: QualifiedEchoSourceSnapshot,
        fullname: str,
        relative: str,
        payload: bytes,
        package: bool,
    ) -> None:
        self.snapshot = snapshot
        self.fullname = fullname
        self.relative = relative
        self.payload = payload
        self.package = package

    @property
    def origin(self) -> str:
        return str(self.snapshot.distribution_root / self.relative)

    def create_module(self, _spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        try:
            code = compile(
                self.payload,
                self.origin,
                "exec",
                dont_inherit=True,
            )
        except (SyntaxError, ValueError) as exc:
            raise ImportError("qualified Echo source compilation failed") from exc
        module.__file__ = self.origin
        if self.package:
            module.__path__ = [str(Path(self.origin).parent)]  # type: ignore[attr-defined]
        exec(code, module.__dict__)


class QualifiedEchoSnapshotFinder(importlib.abc.MetaPathFinder):
    """Load every Echo module from the authenticated in-memory snapshot."""

    def __init__(self, snapshot: QualifiedEchoSourceSnapshot) -> None:
        self.snapshot = snapshot

    def owns_module(self, module: ModuleType) -> bool:
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        return isinstance(loader, _QualifiedEchoSnapshotLoader) and loader.snapshot is self.snapshot

    def find_spec(
        self,
        fullname: str,
        _path: Sequence[str] | None = None,
        _target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        managed = fullname in {"echo_veil", "echo_veil_origin"} or fullname.startswith(
            ("echo_veil.", "echo_veil_origin.")
        )
        if not managed:
            return None
        selected = self.snapshot.payload_for_module(fullname)
        if selected is None:
            raise ModuleNotFoundError(
                "Echo module is outside the qualified source snapshot",
                name=fullname,
            )
        relative, payload, package = selected
        loader = _QualifiedEchoSnapshotLoader(
            self.snapshot,
            fullname,
            relative,
            payload,
            package,
        )
        return importlib.util.spec_from_loader(
            fullname,
            loader,
            origin=loader.origin,
            is_package=package,
        )


def capture_qualified_echo_source_tree(distribution_root: Path) -> QualifiedEchoSourceSnapshot:
    """Capture exact source bytes through a pinned no-follow dirfd."""

    root = Path(distribution_root)
    try:
        root_info = root.lstat()
    except OSError:
        _reject("qualified_source_missing")
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        _reject("qualified_source_root_identity")
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= int(getattr(os, name, 0))
    try:
        root_fd = os.open(root, flags)
    except OSError:
        _reject("qualified_source_root_open")
    try:
        pinned = os.fstat(root_fd)
        if not stat.S_ISDIR(pinned.st_mode) or (pinned.st_dev, pinned.st_ino) != (root_info.st_dev, root_info.st_ino):
            _reject("qualified_source_root_changed")
        digest = hashlib.sha256()
        total = 0
        captured: list[tuple[str, bytes]] = []
        file_flags = os.O_RDONLY
        for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
            file_flags |= int(getattr(os, name, 0))
        directory_flags = flags
        for package_name in ("echo_veil", "echo_veil_origin"):
            try:
                package_fd = os.open(package_name, directory_flags, dir_fd=root_fd)
            except OSError:
                _reject("qualified_source_package_open")
            try:
                package_info = os.fstat(package_fd)
                if not stat.S_ISDIR(package_info.st_mode):
                    _reject("qualified_source_package_identity")
                expected_names = {
                    Path(path).name for path in QUALIFIED_ECHO_SOURCE_PATHS if path.startswith(f"{package_name}/")
                }
                try:
                    entries = list(os.scandir(package_fd))
                except OSError:
                    _reject("qualified_source_scan")
                actual_sources = {entry.name for entry in entries if entry.name.endswith(".py")}
                if actual_sources != expected_names:
                    _reject("qualified_source_set")
                for relative in (path for path in QUALIFIED_ECHO_SOURCE_PATHS if path.startswith(f"{package_name}/")):
                    name = Path(relative).name
                    try:
                        descriptor = os.open(name, file_flags, dir_fd=package_fd)
                    except OSError:
                        _reject("qualified_source_open")
                    try:
                        before = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(before.st_mode)
                            or before.st_nlink != 1
                            or not 0 <= before.st_size <= MAX_QUALIFIED_SOURCE_BYTES
                        ):
                            _reject("qualified_source_identity")
                        payload = bytearray()
                        while chunk := os.read(
                            descriptor,
                            min(
                                128 * 1024,
                                MAX_QUALIFIED_SOURCE_BYTES + 1 - len(payload),
                            ),
                        ):
                            payload.extend(chunk)
                            if len(payload) > MAX_QUALIFIED_SOURCE_BYTES:
                                _reject("qualified_source_bounds")
                        after = os.fstat(descriptor)
                        stable = (
                            "st_dev",
                            "st_ino",
                            "st_mode",
                            "st_nlink",
                            "st_size",
                            "st_mtime_ns",
                            "st_ctime_ns",
                        )
                        if (
                            any(getattr(before, field) != getattr(after, field) for field in stable)
                            or len(payload) != before.st_size
                        ):
                            _reject("qualified_source_changed")
                    finally:
                        os.close(descriptor)
                    total += len(payload)
                    if total > MAX_QUALIFIED_SOURCE_BYTES:
                        _reject("qualified_source_bounds")
                    path_bytes = relative.encode("utf-8")
                    digest.update(path_bytes)
                    digest.update(b"\0")
                    digest.update(len(payload).to_bytes(8, "big"))
                    digest.update(payload)
                    captured.append((relative, bytes(payload)))
                current_package = os.fstat(package_fd)
                if (current_package.st_dev, current_package.st_ino) != (
                    package_info.st_dev,
                    package_info.st_ino,
                ):
                    _reject("qualified_source_package_changed")
            finally:
                os.close(package_fd)
        current = os.fstat(root_fd)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            _reject("qualified_source_root_changed")
        value = digest.hexdigest()
        if value != QUALIFIED_ECHO_SOURCE_TREE_SHA256:
            _reject("qualified_source_digest")
        return QualifiedEchoSourceSnapshot(
            distribution_root=root,
            tree_sha256=value,
            files=tuple(captured),
        )
    finally:
        os.close(root_fd)


def verify_qualified_echo_source_tree(distribution_root: Path) -> str:
    """Verify the source tree and return its Algo-owned canonical digest."""

    return capture_qualified_echo_source_tree(distribution_root).tree_sha256


__all__ = [
    "QUALIFIED_ECHO_SOURCE_PATHS",
    "QUALIFIED_ECHO_SOURCE_TREE_SHA256",
    "QualifiedEchoSnapshotFinder",
    "QualifiedEchoSourceError",
    "QualifiedEchoSourceSnapshot",
    "capture_qualified_echo_source_tree",
    "verify_qualified_echo_source_tree",
]
