#!/usr/bin/env python3
"""Fail closed on Echo Veil dependency, install-origin, and RECORD drift."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import importlib.util
from importlib import metadata
import json
from pathlib import Path
import stat
import sys
from typing import Any, NoReturn

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algo_cli.ada_echo_veil_identity import (  # noqa: E402
    QUALIFIED_ECHO_SOURCE_TREE_SHA256,
    QualifiedEchoSnapshotFinder,
    QualifiedEchoSourceError,
    capture_qualified_echo_source_tree,
)

PROJECT_PATH = ROOT / "pyproject.toml"
LOCK_PATH = ROOT / "uv.lock"
EXPECTED_NAME = "echo-veil"
EXPECTED_VERSION = "0.8.0"
EXPECTED_COMMIT = "879200fa2e16a1d59f6af011f26e5c7538c482a7"
EXPECTED_REPOSITORY = "https://github.com/Seabass-up/echo-veil.git"
EXPECTED_REQUIREMENT = f"echo-veil @ git+{EXPECTED_REPOSITORY}@{EXPECTED_COMMIT}"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_DIRECT_URL_BYTES = 16 * 1024
MAX_RECORD_FILES = 4_096
MAX_INSTALLED_FILE_BYTES = 16 * 1024 * 1024
EXPECTED_MODULE_MEMBERS = {
    "echo_veil": "echo_veil/__init__.py",
    "echo_veil.agent_memory": "echo_veil/agent_memory.py",
}


class EchoDependencyAuditError(RuntimeError):
    """An Echo dependency identity or installed-source invariant failed."""


def _reject(reason: str) -> NoReturn:
    raise EchoDependencyAuditError(reason)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError:
        _reject("metadata_missing")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink() or info.st_size > MAX_METADATA_BYTES:
        _reject("metadata_identity")
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        _reject("metadata_invalid")
    if not isinstance(payload, dict):
        _reject("metadata_invalid")
    return payload


def _validate_dependency_files() -> None:
    project = _read_toml(PROJECT_PATH)
    lock = _read_toml(LOCK_PATH)
    try:
        requirements = project["project"]["optional-dependencies"]["echo-veil"]
    except (KeyError, TypeError):
        _reject("project_pin_missing")
    if requirements != [EXPECTED_REQUIREMENT]:
        _reject("project_pin_mismatch")

    packages = lock.get("package")
    if not isinstance(packages, list):
        _reject("lock_invalid")
    matches = [row for row in packages if isinstance(row, dict) and row.get("name") == EXPECTED_NAME]
    if len(matches) != 1:
        _reject("lock_package_ambiguity")
    package = matches[0]
    source = package.get("source")
    if package.get("version") != EXPECTED_VERSION or not isinstance(source, dict):
        _reject("lock_identity_mismatch")
    git_source = source.get("git")
    expected_git = f"{EXPECTED_REPOSITORY}?rev={EXPECTED_COMMIT}#{EXPECTED_COMMIT}"
    if git_source != expected_git:
        _reject("lock_pin_mismatch")


def _validate_direct_url(document: str | None) -> dict[str, Any]:
    if not isinstance(document, str) or not 0 < len(document.encode("utf-8")) <= MAX_DIRECT_URL_BYTES:
        _reject("direct_url_invalid")
    try:
        payload = json.loads(document)
    except (json.JSONDecodeError, UnicodeError):
        _reject("direct_url_invalid")
    if not isinstance(payload, dict) or set(payload) != {"url", "vcs_info"}:
        _reject("direct_url_schema")
    vcs = payload.get("vcs_info")
    if not isinstance(vcs, dict) or set(vcs) != {"vcs", "commit_id", "requested_revision"}:
        _reject("direct_url_schema")
    if payload.get("url") != EXPECTED_REPOSITORY or vcs.get("vcs") != "git":
        _reject("direct_url_origin")
    if vcs.get("commit_id") != EXPECTED_COMMIT or vcs.get("requested_revision") != EXPECTED_COMMIT:
        _reject("direct_url_commit")
    return payload


def _record_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def _verify_import_origin(
    distribution: Any,
    *,
    install_prefix: Path,
    module_name: str,
    origin: object,
) -> None:
    expected_member = EXPECTED_MODULE_MEMBERS.get(module_name)
    if expected_member is None or not isinstance(origin, str) or not origin:
        _reject("module_origin_missing")
    try:
        prefix = install_prefix.resolve(strict=True)
        root = Path(distribution.locate_file("")).resolve(strict=True)
        candidate_path = Path(origin)
        candidate = candidate_path.resolve(strict=True)
        relative = candidate.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        _reject("module_origin_scope")
    if not root.is_relative_to(prefix) or relative != expected_member:
        _reject("module_origin_scope")
    current = root
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            _reject("module_origin_missing")
        if stat.S_ISLNK(info.st_mode):
            _reject("module_origin_symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            _reject("module_origin_scope")
    members = list(getattr(distribution, "files", None) or ())
    if not 1 <= len(members) <= MAX_RECORD_FILES:
        _reject("record_bounds")
    matches = [member for member in members if str(member).replace("\\", "/") == expected_member]
    if len(matches) != 1:
        _reject("module_record_missing")
    member = matches[0]
    try:
        recorded_path = Path(distribution.locate_file(member)).resolve(strict=True)
        info = candidate.lstat()
    except OSError:
        _reject("module_origin_missing")
    if recorded_path != candidate or candidate_path.is_symlink():
        _reject("module_origin_mismatch")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_INSTALLED_FILE_BYTES:
        _reject("module_origin_identity")
    recorded_size = getattr(member, "size", None)
    file_hash = getattr(member, "hash", None)
    if recorded_size != info.st_size:
        _reject("record_size_mismatch")
    if file_hash is None or getattr(file_hash, "mode", "") != "sha256":
        _reject("record_hash_missing")
    if not hmac.compare_digest(_record_digest(candidate), str(getattr(file_hash, "value", ""))):
        _reject("record_hash_mismatch")


def _verify_distribution(
    distribution: Any,
    *,
    install_prefix: Path,
    module_version: str,
    module_origins: dict[str, object],
) -> dict[str, Any]:
    if str(getattr(distribution, "version", "")) != EXPECTED_VERSION:
        _reject("distribution_version")
    if module_version != EXPECTED_VERSION:
        _reject("source_version")
    _validate_direct_url(distribution.read_text("direct_url.json"))

    if set(module_origins) != set(EXPECTED_MODULE_MEMBERS):
        _reject("module_origin_set")
    for module_name, origin in module_origins.items():
        _verify_import_origin(
            distribution,
            install_prefix=install_prefix,
            module_name=module_name,
            origin=origin,
        )

    verified, verified_python, member_count = _verify_installed_record(
        distribution,
        install_prefix=install_prefix,
    )
    return {
        "commit": EXPECTED_COMMIT,
        "distribution": EXPECTED_NAME,
        "record_files": member_count,
        "source_tree_sha256": QUALIFIED_ECHO_SOURCE_TREE_SHA256,
        "verified_files": verified,
        "verified_python_files": verified_python,
        "verified_module_origins": len(module_origins),
        "version": EXPECTED_VERSION,
    }


def _verify_installed_record(
    distribution: Any,
    *,
    install_prefix: Path,
) -> tuple[int, int, int]:
    """Authenticate every RECORD member before any Echo module import."""

    members = list(getattr(distribution, "files", None) or ())
    if not 1 <= len(members) <= MAX_RECORD_FILES:
        _reject("record_bounds")
    prefix = install_prefix.resolve(strict=True)
    verified = 0
    verified_python = 0
    for member in members:
        located = Path(distribution.locate_file(member))
        if located.is_symlink():
            _reject("installed_symlink")
        try:
            resolved = located.resolve(strict=True)
            info = resolved.stat()
        except OSError:
            _reject("installed_file_missing")
        if not resolved.is_relative_to(prefix) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _reject("installed_file_scope")
        if info.st_size > MAX_INSTALLED_FILE_BYTES:
            _reject("installed_file_bounds")
        recorded_size = getattr(member, "size", None)
        if recorded_size is not None and recorded_size != info.st_size:
            _reject("record_size_mismatch")
        file_hash = getattr(member, "hash", None)
        if file_hash is None:
            if Path(str(member)).name != "RECORD":
                _reject("record_hash_missing")
            continue
        if getattr(file_hash, "mode", "") != "sha256":
            _reject("record_hash_algorithm")
        if not hmac.compare_digest(_record_digest(resolved), str(getattr(file_hash, "value", ""))):
            _reject("record_hash_mismatch")
        verified += 1
        if resolved.suffix == ".py":
            verified_python += 1
    if verified_python == 0:
        _reject("python_sources_unverified")
    return verified, verified_python, len(members)


def audit() -> dict[str, Any]:
    _validate_dependency_files()
    snapshot_finder: QualifiedEchoSnapshotFinder | None = None
    try:
        distribution = metadata.distribution(EXPECTED_NAME)
        if any(
            name in {"echo_veil", "echo_veil_origin"} or name.startswith(("echo_veil.", "echo_veil_origin."))
            for name in sys.modules
        ):
            _reject("module_namespace_preloaded")
        try:
            snapshot = capture_qualified_echo_source_tree(
                Path(distribution.locate_file("")),  # type: ignore[arg-type]
            )
        except QualifiedEchoSourceError as exc:
            _reject(str(exc))
        _verify_installed_record(
            distribution,
            install_prefix=Path(sys.prefix),
        )
        snapshot_finder = QualifiedEchoSnapshotFinder(snapshot)
        sys.meta_path.insert(0, snapshot_finder)
        echo_spec = importlib.util.find_spec("echo_veil")
    except (ImportError, ValueError, metadata.PackageNotFoundError):
        _reject("distribution_missing")
    _verify_import_origin(
        distribution,
        install_prefix=Path(sys.prefix),
        module_name="echo_veil",
        origin=getattr(echo_spec, "origin", None),
    )
    try:
        echo_veil = importlib.import_module("echo_veil")
        agent_spec = importlib.util.find_spec("echo_veil.agent_memory")
    except (ImportError, ValueError):
        _reject("module_import")
    _verify_import_origin(
        distribution,
        install_prefix=Path(sys.prefix),
        module_name="echo_veil",
        origin=getattr(echo_veil, "__file__", None),
    )
    _verify_import_origin(
        distribution,
        install_prefix=Path(sys.prefix),
        module_name="echo_veil.agent_memory",
        origin=getattr(agent_spec, "origin", None),
    )
    try:
        agent_memory = importlib.import_module("echo_veil.agent_memory")
    except ImportError:
        _reject("module_import")
    if snapshot_finder is None or not all(
        snapshot_finder.owns_module(module)
        for name, module in tuple(sys.modules.items())
        if (name in {"echo_veil", "echo_veil_origin"} or name.startswith(("echo_veil.", "echo_veil_origin.")))
        and isinstance(module, type(sys))
    ):
        _reject("module_loader_identity")
    _verify_import_origin(
        distribution,
        install_prefix=Path(sys.prefix),
        module_name="echo_veil.agent_memory",
        origin=getattr(agent_memory, "__file__", None),
    )
    receipt = _verify_distribution(
        distribution,
        install_prefix=Path(sys.prefix),
        module_version=str(getattr(echo_veil, "__version__", "")),
        module_origins={
            "echo_veil": getattr(echo_veil, "__file__", None),
            "echo_veil.agent_memory": getattr(agent_memory, "__file__", None),
        },
    )
    return {"audit": "henry-echo-veil-dependency-v1", "passed": True, **receipt}


def main() -> int:
    try:
        report = audit()
    except EchoDependencyAuditError as exc:
        print(json.dumps({"audit": "henry-echo-veil-dependency-v1", "passed": False, "reason": str(exc)}))
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
