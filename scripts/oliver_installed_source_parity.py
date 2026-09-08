#!/usr/bin/env python3
"""Fail closed when a non-editable Algo CLI install differs from its source."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import stat
from typing import Iterable, Mapping

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PACKAGE = ROOT / "algo_cli"
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_REPORTED_PATHS = 20


class InstalledSourceParityError(RuntimeError):
    """The installed distribution is stale, shadowed, malformed, or divergent."""


@dataclass(frozen=True, slots=True)
class InstalledSourceParityReport:
    passed: bool
    source_files: int
    installed_python_files: int
    source_digest: str
    installed_digest: str
    missing: tuple[str, ...]
    divergent: tuple[str, ...]
    unexpected_python: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "passed": self.passed,
            "source_files": self.source_files,
            "installed_python_files": self.installed_python_files,
            "source_digest": self.source_digest,
            "installed_digest": self.installed_digest,
            "missing": list(self.missing),
            "divergent": list(self.divergent),
            "unexpected_python": list(self.unexpected_python),
        }


def _included(path: Path) -> bool:
    return path.name != ".DS_Store" and path.suffix != ".pyc" and "__pycache__" not in path.parts


def _relative_files(root: Path) -> tuple[str, ...]:
    if not root.is_dir() or root.is_symlink():
        raise InstalledSourceParityError("package_root")
    values: list[str] = []
    for path in root.rglob("*"):
        if not _included(path):
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise InstalledSourceParityError("package_file")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise InstalledSourceParityError("package_file")
        values.append(path.relative_to(root).as_posix())
    return tuple(sorted(values))


def _wheel_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or any(char in value for char in ("\\", "\0", ":")):
        raise InstalledSourceParityError("wheel_configuration")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {".", ".."} for part in value.split("/")):
        raise InstalledSourceParityError("wheel_configuration")
    return path


def _source_file_map(source: Path) -> dict[str, Path]:
    files = {relative: source / relative for relative in _relative_files(source)}
    project_root = source.parent
    manifest = project_root / "pyproject.toml"
    try:
        info = manifest.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise InstalledSourceParityError("wheel_configuration")
        with manifest.open("rb") as handle:
            payload = handle.read(MAX_FILE_BYTES + 1)
        if len(payload) > MAX_FILE_BYTES:
            raise InstalledSourceParityError("wheel_configuration")
        wheel = tomllib.loads(payload.decode("utf-8"))["tool"]["hatch"]["build"]["targets"]["wheel"]
        packages, additions = wheel["packages"], wheel.get("force-include", {})
        if not isinstance(packages, list) or source.name not in packages or not isinstance(additions, dict):
            raise InstalledSourceParityError("wheel_configuration")
        for package in packages:
            _wheel_relative(package)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise InstalledSourceParityError("wheel_configuration") from error

    # Wheel resources can originate outside the Python package. Bind their
    # installed names to the declared source instead of ignoring them as extras.
    for raw_origin, raw_target in additions.items():
        origin, target = _wheel_relative(raw_origin), _wheel_relative(raw_target)
        if target.parts[0] != source.name:
            continue
        destination = target.relative_to(source.name)
        try:
            source_path = project_root
            for part in origin.parts:
                source_path /= part
                if stat.S_ISLNK(source_path.lstat().st_mode):
                    raise InstalledSourceParityError("wheel_source")
            info = source_path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_FILE_BYTES:
                incoming = {destination.as_posix(): source_path}
            elif stat.S_ISDIR(info.st_mode):
                incoming = {
                    (destination / relative).as_posix(): source_path / relative
                    for relative in _relative_files(source_path)
                }
            else:
                raise InstalledSourceParityError("wheel_source")
        except (OSError, InstalledSourceParityError) as error:
            raise InstalledSourceParityError("wheel_source") from error
        if not incoming or "." in incoming:
            raise InstalledSourceParityError("wheel_configuration")
        for relative, path in incoming.items():
            if relative in files and files[relative] != path:
                raise InstalledSourceParityError("wheel_configuration")
            files[relative] = path
    return files


def _tree_digest(
    root: Path, relative_paths: Iterable[str], *, sources: Mapping[str, Path] | None = None
) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    count = 0
    for relative in relative_paths:
        path = sources[relative] if sources is not None else root / relative
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > MAX_FILE_BYTES:
            raise InstalledSourceParityError("package_file")
        payload = path.read_bytes()
        total += len(payload)
        if total > MAX_TOTAL_BYTES:
            raise InstalledSourceParityError("package_total")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        count += 1
    return ("sha256:" + digest.hexdigest(), count)


def _installed_package() -> Path:
    spec = importlib.util.find_spec("algo_cli")
    if spec is None or spec.origin is None:
        raise InstalledSourceParityError("installed_package_missing")
    return Path(spec.origin).parent


def check_installed_source_parity(
    *,
    source_root: Path = SOURCE_PACKAGE,
    installed_root: Path | None = None,
) -> InstalledSourceParityReport:
    installed_input = installed_root or _installed_package()
    if source_root.is_symlink() or installed_input.is_symlink():
        raise InstalledSourceParityError("package_root")
    source = source_root.resolve()
    installed = installed_input.resolve()
    if source == installed:
        raise InstalledSourceParityError("source_shadowed")

    source_files = _source_file_map(source)
    source_paths = tuple(sorted(source_files))
    installed_paths = _relative_files(installed)
    source_set = set(source_paths)
    installed_set = set(installed_paths)
    missing = tuple(sorted(source_set - installed_set))
    divergent = tuple(
        relative
        for relative in source_paths
        if relative in installed_set and source_files[relative].read_bytes() != (installed / relative).read_bytes()
    )
    source_python = {relative for relative in source_paths if Path(relative).suffix in {".py", ".pyi"}}
    installed_python = {relative for relative in installed_paths if Path(relative).suffix in {".py", ".pyi"}}
    unexpected_python = tuple(sorted(installed_python - source_python))
    source_digest, source_count = _tree_digest(source, source_paths, sources=source_files)
    comparable = tuple(relative for relative in source_paths if relative in installed_set)
    installed_digest, _ = _tree_digest(installed, comparable)
    return InstalledSourceParityReport(
        not missing and not divergent and not unexpected_python and source_digest == installed_digest,
        source_count,
        len(installed_python),
        source_digest,
        installed_digest,
        missing[:MAX_REPORTED_PATHS],
        divergent[:MAX_REPORTED_PATHS],
        unexpected_python[:MAX_REPORTED_PATHS],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        report = check_installed_source_parity()
    except InstalledSourceParityError as error:
        print(json.dumps({"schema_version": 1, "passed": False, "reason_code": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
