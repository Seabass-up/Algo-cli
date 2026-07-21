"""Validate and normalize release SBOMs and checksum manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterable, NoReturn
import uuid

from . import __version__

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_TREE_DEPTH = 32
MAX_TREE_ITEMS = 200_000
MAX_STRING_BYTES = 65_536
_SBOM_NAMESPACE = uuid.UUID("ada00000-7dc7-4a1a-8ada-000000000007")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9._+-]{0,63}$")
_HOME_PATH_RE = re.compile(r"(?:^|[\s\"'])/(?:Users|home)/[^/\s\"']+")
_WINDOWS_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+", re.IGNORECASE)


class SupplyChainManifestError(RuntimeError):
    """A release manifest could not be proven bounded and public-safe."""


def _fail(reason_code: str) -> NoReturn:
    raise SupplyChainManifestError(reason_code)


def _pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _fail("json_duplicate_key")
        result[key] = value
    return result


def _constant(_value: str) -> NoReturn:
    _fail("json_constant")


def _regular_file(path: Path) -> os.stat_result:
    if not path.is_absolute():
        _fail("path_absolute")
    try:
        value = path.lstat()
    except OSError:
        _fail("file_unavailable")
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
        _fail("file_type")
    if value.st_nlink != 1:
        _fail("file_hardlink")
    if value.st_size < 1 or value.st_size > MAX_MANIFEST_BYTES:
        _fail("file_size")
    return value


def _read_regular(path: Path) -> bytes:
    before = _regular_file(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino, opened.st_size)
                != (before.st_dev, before.st_ino, before.st_size)
            ):
                _fail("file_changed")
            buffer = bytearray()
            while len(buffer) <= MAX_MANIFEST_BYTES:
                chunk = os.read(descriptor, min(1_048_576, MAX_MANIFEST_BYTES + 1 - len(buffer)))
                if not chunk:
                    break
                buffer.extend(chunk)
        finally:
            os.close(descriptor)
    except OSError:
        _fail("file_read")
    if len(buffer) != before.st_size:
        _fail("file_changed")
    return bytes(buffer)


def _bound_tree(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if count is None:
        count = [0]
    if depth > MAX_TREE_DEPTH:
        _fail("json_depth")
    count[0] += 1
    if count[0] > MAX_TREE_ITEMS:
        _fail("json_items")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        _fail("json_float")
    if type(value) is str:
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _fail("json_string")
        if len(encoded) > MAX_STRING_BYTES or any(ord(character) < 0x20 for character in value):
            _fail("json_string")
        if _HOME_PATH_RE.search(value) or _WINDOWS_HOME_RE.search(value) or "file://" in value.casefold():
            _fail("private_path")
        return
    if type(value) is list:
        for item in value:
            _bound_tree(item, depth=depth + 1, count=count)
        return
    if type(value) is dict:
        for key, item in value.items():
            _bound_tree(key, depth=depth + 1, count=count)
            _bound_tree(item, depth=depth + 1, count=count)
        return
    _fail("json_type")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _project_version() -> str:
    if not _VERSION_RE.fullmatch(__version__):
        _fail("project_version")
    return __version__


def _atomic_public_write(path: Path, data: bytes) -> None:
    if not path.is_absolute() or not data or len(data) > MAX_MANIFEST_BYTES:
        _fail("output")
    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except OSError:
        _fail("output_directory")
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        _fail("output_directory")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError:
        _fail("output")
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode)):
        _fail("output_type")

    temporary = parent / f".ada-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o644)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("output_write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        _fail("output_write")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def normalize_sbom(source: Path, output: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            _read_regular(source).decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except UnicodeDecodeError:
        _fail("sbom_utf8")
    except json.JSONDecodeError:
        _fail("sbom_json")
    _bound_tree(value)
    if type(value) is not dict:
        _fail("sbom_schema")
    if value.get("bomFormat") != "CycloneDX" or value.get("specVersion") != "1.5":
        _fail("sbom_format")
    if type(value.get("version")) is not int or value["version"] != 1:
        _fail("sbom_version")
    metadata = value.get("metadata")
    components = value.get("components")
    dependencies = value.get("dependencies")
    if type(metadata) is not dict or type(components) is not list or type(dependencies) is not list:
        _fail("sbom_schema")
    root = metadata.get("component")
    if type(root) is not dict or root.get("name") != "algo-cli-runtime":
        _fail("sbom_root")
    root_ref = root.get("bom-ref")
    if type(root_ref) is not str or not root_ref:
        _fail("sbom_root")

    references = {root_ref}
    for component in components:
        if type(component) is not dict:
            _fail("sbom_component")
        reference = component.get("bom-ref")
        name = component.get("name")
        version = component.get("version")
        if (
            type(reference) is not str
            or not reference
            or reference in references
            or type(name) is not str
            or not name
            or type(version) is not str
            or not version
        ):
            _fail("sbom_component")
        references.add(reference)

    dependency_refs: set[str] = set()
    for dependency in dependencies:
        if type(dependency) is not dict or set(dependency) - {"ref", "dependsOn"}:
            _fail("sbom_dependency")
        reference = dependency.get("ref")
        depends_on = dependency.get("dependsOn", [])
        if type(reference) is not str or reference not in references or reference in dependency_refs:
            _fail("sbom_dependency")
        if type(depends_on) is not list or any(type(item) is not str or item not in references for item in depends_on):
            _fail("sbom_dependency")
        dependency_refs.add(reference)
    if root_ref not in dependency_refs:
        _fail("sbom_dependency")

    normalized = dict(value)
    normalized_metadata = dict(metadata)
    normalized_metadata.pop("timestamp", None)
    normalized_root = dict(root)
    normalized_root["type"] = "application"
    normalized_root["version"] = _project_version()
    normalized_metadata["component"] = normalized_root
    metadata_properties = normalized_metadata.get("properties", [])
    if type(metadata_properties) is not list or any(
        type(item) is not dict
        or type(item.get("name")) is not str
        or item["name"].startswith("algo-cli:")
        for item in metadata_properties
    ):
        _fail("sbom_metadata")
    normalized_metadata["properties"] = [
        *metadata_properties,
        {
            "name": "algo-cli:sbom-scope",
            "value": "locked-runtime-resolution",
        },
        {
            "name": "algo-cli:runtime-dependencies-embedded",
            "value": "false",
        },
    ]
    normalized["metadata"] = normalized_metadata
    normalized.pop("serialNumber", None)
    identity_digest = hashlib.sha256(_canonical(normalized)).hexdigest()
    normalized["serialNumber"] = "urn:uuid:" + str(uuid.uuid5(_SBOM_NAMESPACE, identity_digest))
    _bound_tree(normalized)
    payload = _canonical(normalized) + b"\n"
    _atomic_public_write(output, payload)
    return {
        "component_count": len(components),
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "spec_version": "1.5",
        "status": "passed",
    }


def write_checksums(artifacts: Iterable[Path], output: Path) -> dict[str, Any]:
    paths = tuple(artifacts)
    if not paths:
        _fail("artifact_missing")
    by_name: dict[str, Path] = {}
    for path in paths:
        _regular_file(path)
        if path.parent != output.parent or path == output or path.name in by_name:
            _fail("artifact_path")
        by_name[path.name] = path
    lines = []
    for name in sorted(by_name):
        digest = hashlib.sha256(_read_regular(by_name[name])).hexdigest()
        if not _DIGEST_RE.fullmatch(digest):
            _fail("artifact_digest")
        lines.append(f"{digest}  {name}\n")
    payload = "".join(lines).encode("ascii")
    _atomic_public_write(output, payload)
    return {
        "artifact_count": len(paths),
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "status": "passed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sbom = subparsers.add_parser("sbom")
    sbom.add_argument("--source", type=Path, required=True)
    sbom.add_argument("--output", type=Path, required=True)
    checksums = subparsers.add_parser("checksums")
    checksums.add_argument("--output", type=Path, required=True)
    checksums.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "sbom":
            result = normalize_sbom(args.source.resolve(strict=False), args.output.resolve(strict=False))
        else:
            result = write_checksums(
                (path.resolve(strict=False) for path in args.artifacts),
                args.output.resolve(strict=False),
            )
    except SupplyChainManifestError as exc:
        print(json.dumps({"reason_code": str(exc), "status": "blocked"}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
