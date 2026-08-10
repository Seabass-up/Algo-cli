#!/usr/bin/env python3
"""Bind Python release artifacts to an exact immutable Git source archive."""

from __future__ import annotations

import argparse
import base64
import binascii
import configparser
import csv
from dataclasses import dataclass
import email.parser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Iterable, Mapping
import zipfile

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
ARCHIVE_PREFIX = "source/"
MAX_GIT_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SOURCE_FILES = 20_000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_COMMIT_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 60
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[a-zA-Z0-9._+-]*)?$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")


class SourceBindingRejected(RuntimeError):
    """A stable, content-free release source-binding rejection."""


class _CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    mode: str
    object_id: str


@dataclass(frozen=True, slots=True)
class _ArchiveEntry:
    path: str
    mode: str
    payload: bytes


def _reject_json_constant(_value: str) -> None:
    raise SourceBindingRejected("json_number")


def _reject_json_float(_value: str) -> None:
    raise SourceBindingRejected("json_number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise SourceBindingRejected("json_duplicate_key")
        output[key] = value
    return output


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        payload, _info = _regular_file(path, stage="json_file", maximum=MAX_JSON_BYTES, minimum=1)
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceBindingRejected("json_content") from error
    if type(document) is not dict:
        raise SourceBindingRejected("json_content")
    return document


def _canonical_json(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_parent_descriptor(path: Path, *, stage: str) -> tuple[int, os.stat_result]:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise SourceBindingRejected(stage)
    try:
        before = path.parent.lstat()
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
    except OSError as error:
        raise SourceBindingRejected(stage) from error
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        raise SourceBindingRejected(stage)
    return descriptor, opened


def _parent_matches(path: Path, expected: os.stat_result) -> bool:
    try:
        current = path.parent.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and not stat.S_ISLNK(current.st_mode)
        and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
    )


def _unlink_pinned(parent_descriptor: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
            os.unlink(name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
    except OSError:
        pass


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written < 1:
            raise SourceBindingRejected("write_failed")
        view = view[written:]


def _write_exclusive_blob(
    path: Path,
    payload: bytes,
    *,
    maximum: int,
    mode: int,
    size_reason: str,
    exists_reason: str,
    write_reason: str,
) -> None:
    if not 1 <= len(payload) <= maximum:
        raise SourceBindingRejected(size_reason)
    parent_descriptor, parent_identity = _open_parent_descriptor(path, stage=write_reason)
    descriptor: int | None = None
    created_identity: os.stat_result | None = None
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_descriptor,
        )
        created_identity = os.fstat(descriptor)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
            created_identity.st_dev,
            created_identity.st_ino,
        ) or not _parent_matches(path, parent_identity):
            raise SourceBindingRejected(write_reason)
        os.fsync(parent_descriptor)
    except FileExistsError as error:
        raise SourceBindingRejected(exists_reason) from error
    except (OSError, SourceBindingRejected) as error:
        if created_identity is not None:
            _unlink_pinned(parent_descriptor, path.name, created_identity)
        if isinstance(error, SourceBindingRejected):
            raise
        raise SourceBindingRejected(write_reason) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


def _write_exclusive(path: Path, payload: bytes) -> None:
    _write_exclusive_blob(
        path,
        payload,
        maximum=MAX_JSON_BYTES,
        mode=0o600,
        size_reason="output_size",
        exists_reason="output_exists",
        write_reason="output_write",
    )


def _git_binary() -> str:
    fixed = Path("/usr/bin/git")
    candidate_text = str(fixed) if fixed.exists() else shutil.which("git")
    if candidate_text is None or (os.environ.get("GITHUB_ACTIONS") == "true" and candidate_text != str(fixed)):
        raise SourceBindingRejected("git_unavailable")
    candidate = Path(candidate_text)
    try:
        info = candidate.lstat()
    except OSError as error:
        raise SourceBindingRejected("git_unavailable") from error
    is_fixed = candidate == fixed
    allowed_owners = {0} if is_fixed else {0, os.getuid()}
    if (
        not candidate.is_absolute()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or not 1 <= info.st_nlink <= 256
        or info.st_uid not in allowed_owners
        or info.st_mode & 0o022
        or (not is_fixed and info.st_uid == os.getuid() and info.st_mode & 0o200)
    ):
        raise SourceBindingRejected("git_untrusted")
    return str(candidate)


def _git_environment() -> dict[str, str]:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    return environment


def _git(
    repository: Path,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
) -> bytes:
    command = [
        _git_binary(),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "tar.umask=0022",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            cwd=repository,
            env=_git_environment(),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SourceBindingRejected("git_execution") from error
    if result.returncode != 0:
        raise SourceBindingRejected("git_rejected")
    if len(result.stdout) > max_output_bytes:
        raise SourceBindingRejected("git_output_size")
    return result.stdout


def _git_to_descriptor(repository: Path, arguments: list[str], descriptor: int) -> None:
    command = [
        _git_binary(),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "tar.umask=0022",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            cwd=repository,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SourceBindingRejected("git_execution") from error
    if result.returncode != 0:
        raise SourceBindingRejected("git_rejected")


def _descriptor_payload(descriptor: int, *, maximum: int) -> bytes:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= maximum:
        raise SourceBindingRejected("source_archive")
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = bytearray()
    while len(payload) < before.st_size:
        chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(payload)))
        if not chunk:
            raise SourceBindingRejected("source_archive_changed")
        payload.extend(chunk)
    if os.read(descriptor, 1):
        raise SourceBindingRejected("source_archive_changed")
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
        raise SourceBindingRejected("source_archive_changed")
    return bytes(payload)


def _safe_relative(value: str, *, stage: str) -> str:
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise SourceBindingRejected(stage) from error
    path = PurePosixPath(value)
    if (
        not value
        or len(encoded) > 1024
        or value.startswith("/")
        or "\\" in value
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SourceBindingRejected(stage)
    return value


def _source_revision(repository: Path, revision: str) -> tuple[str, str, int, dict[str, _TreeEntry]]:
    if _REVISION_RE.fullmatch(revision) is None:
        raise SourceBindingRejected("source_revision")
    resolved = _git(repository, ["rev-parse", "--verify", revision + "^{commit}"]).decode("ascii").strip()
    if resolved != revision:
        raise SourceBindingRejected("source_revision")
    tree = _git(repository, ["rev-parse", "--verify", revision + "^{tree}"]).decode("ascii").strip()
    if _REVISION_RE.fullmatch(tree) is None:
        raise SourceBindingRejected("source_tree")
    timestamp_text = _git(repository, ["show", "-s", "--format=%ct", revision]).decode("ascii").strip()
    if not timestamp_text.isascii() or not timestamp_text.isdigit():
        raise SourceBindingRejected("source_date_epoch")
    source_date_epoch = int(timestamp_text)
    if not 1 <= source_date_epoch <= 9_999_999_999:
        raise SourceBindingRejected("source_date_epoch")

    raw = _git(repository, ["ls-tree", "-rzl", "--full-tree", revision, "--"])
    entries: dict[str, _TreeEntry] = {}
    source_directories: set[str] = set()
    total_source_bytes = 0
    for row in raw.split(b"\0"):
        if not row:
            continue
        try:
            metadata, raw_path = row.split(b"\t", 1)
            mode, kind, object_id, size_text = metadata.decode("ascii").split()
            path = _safe_relative(raw_path.decode("utf-8", errors="strict"), stage="source_path")
        except (ValueError, UnicodeDecodeError) as error:
            raise SourceBindingRejected("source_tree") from error
        if mode not in {"100644", "100755"} or kind != "blob" or _REVISION_RE.fullmatch(object_id) is None:
            raise SourceBindingRejected("source_tree_type")
        if not size_text.isdigit() or not 0 <= int(size_text) <= MAX_FILE_BYTES:
            raise SourceBindingRejected("source_tree_size")
        total_source_bytes += int(size_text)
        parent = PurePosixPath(path).parent
        while parent.as_posix() != ".":
            source_directories.add(parent.as_posix())
            parent = parent.parent
        if (
            total_source_bytes > MAX_SOURCE_BYTES
            or total_source_bytes + (len(entries) + len(source_directories) + 2) * 4096 + 10_240 > MAX_ARCHIVE_BYTES
        ):
            raise SourceBindingRejected("source_tree_size")
        if path in entries:
            raise SourceBindingRejected("source_tree_duplicate")
        entries[path] = _TreeEntry(path, mode, object_id)
    if not entries or len(entries) > MAX_SOURCE_FILES:
        raise SourceBindingRejected("source_tree_size")
    return resolved, tree, source_date_epoch, entries


def _git_object_id(payload: bytes, expected: str, *, kind: str = "blob") -> str:
    if kind not in {"blob", "tree", "commit"}:
        raise SourceBindingRejected("source_object_type")
    framed = kind.encode("ascii") + b" " + str(len(payload)).encode("ascii") + b"\0" + payload
    if len(expected) == 40:
        return hashlib.sha1(framed, usedforsecurity=False).hexdigest()
    if len(expected) == 64:
        return hashlib.sha256(framed).hexdigest()
    raise SourceBindingRejected("source_object_id")


def _tree_object_id(entries: Mapping[str, _ArchiveEntry], expected: str) -> str:
    root: dict[str, Any] = {}
    for path, entry in entries.items():
        node = root
        parts = PurePosixPath(path).parts
        for part in parts[:-1]:
            existing = node.setdefault(part, {})
            if type(existing) is not dict:
                raise SourceBindingRejected("source_tree_shape")
            node = existing
        if parts[-1] in node:
            raise SourceBindingRejected("source_tree_shape")
        node[parts[-1]] = entry

    def encode_tree(node: Mapping[str, Any]) -> tuple[str, bool]:
        encoded_entries: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            encoded_name = name.encode("utf-8", errors="strict")
            if type(value) is dict:
                object_id, is_directory = encode_tree(value)
                if not is_directory:
                    raise SourceBindingRejected("source_tree_shape")
                mode = b"40000"
                sort_name = encoded_name + b"/"
            elif isinstance(value, _ArchiveEntry):
                object_id = _git_object_id(value.payload, "0" * len(expected))
                mode = value.mode.encode("ascii")
                sort_name = encoded_name
            else:
                raise SourceBindingRejected("source_tree_shape")
            encoded_entries.append((sort_name, mode + b" " + encoded_name + b"\0" + bytes.fromhex(object_id)))
        payload = b"".join(value for _key, value in sorted(encoded_entries, key=lambda item: item[0]))
        return _git_object_id(payload, expected, kind="tree"), True

    return encode_tree(root)[0]


def _validate_commit_object(payload: bytes, *, revision: str, tree: str, source_date_epoch: int) -> None:
    if (
        len(revision) != len(tree)
        or not 1 <= len(payload) <= MAX_COMMIT_BYTES
        or _git_object_id(payload, revision, kind="commit") != revision
    ):
        raise SourceBindingRejected("source_commit")
    first_line = payload.partition(b"\n")[0]
    if first_line != b"tree " + tree.encode("ascii"):
        raise SourceBindingRejected("source_commit_tree")
    headers, separator, _message = payload.partition(b"\n\n")
    committers = [line for line in headers.splitlines() if line.startswith(b"committer ")]
    if not separator or len(committers) != 1:
        raise SourceBindingRejected("source_commit_time")
    try:
        _identity, timestamp, timezone = committers[0].rsplit(b" ", 2)
        parsed_timestamp = int(timestamp.decode("ascii"))
        parsed_timezone = timezone.decode("ascii")
    except (ValueError, UnicodeDecodeError) as error:
        raise SourceBindingRejected("source_commit_time") from error
    if parsed_timestamp != source_date_epoch or re.fullmatch(r"[+-][0-9]{4}", parsed_timezone) is None:
        raise SourceBindingRejected("source_commit_time")


def _regular_file(path: Path, *, stage: str, maximum: int, minimum: int = 0) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as error:
        raise SourceBindingRejected(stage) from error
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or not minimum <= before.st_size <= maximum
    ):
        raise SourceBindingRejected(stage)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != before_identity:
            raise SourceBindingRejected(stage + "_changed")
        payload = bytearray()
        while len(payload) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(payload)))
            if not chunk:
                raise SourceBindingRejected(stage + "_changed")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            raise SourceBindingRejected(stage + "_changed")
        after = os.fstat(descriptor)
    except OSError as error:
        raise SourceBindingRejected(stage) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        after_path = path.lstat()
    except OSError as error:
        raise SourceBindingRejected(stage + "_changed") from error
    identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or identity != (
        after_path.st_dev,
        after_path.st_ino,
        after_path.st_size,
        after_path.st_mtime_ns,
        after_path.st_ctime_ns,
    ):
        raise SourceBindingRejected(stage + "_changed")
    return bytes(payload), before


def _archive_entries(
    archive_path: Path,
    *,
    source_date_epoch: int,
    tree_entries: Mapping[str, _TreeEntry] | None = None,
    expected_revision: str | None = None,
) -> tuple[dict[str, _ArchiveEntry], str, int]:
    archive_payload, info = _regular_file(archive_path, stage="source_archive", maximum=MAX_ARCHIVE_BYTES, minimum=1)
    return _archive_payload_entries(
        archive_payload,
        archive_size=info.st_size,
        source_date_epoch=source_date_epoch,
        tree_entries=tree_entries,
        expected_revision=expected_revision,
    )


def _archive_payload_entries(
    archive_payload: bytes,
    *,
    archive_size: int,
    source_date_epoch: int,
    tree_entries: Mapping[str, _TreeEntry] | None = None,
    expected_revision: str | None = None,
) -> tuple[dict[str, _ArchiveEntry], str, int]:
    archive_digest = "sha256:" + hashlib.sha256(archive_payload).hexdigest()
    entries: dict[str, _ArchiveEntry] = {}
    directories: set[str] = set()
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_payload), mode="r|") as archive:
            member_count = 0
            for member in archive:
                member_count += 1
                if member_count > MAX_SOURCE_FILES * 3:
                    raise SourceBindingRejected("source_archive_shape")
                name = member.name
                if name == ARCHIVE_PREFIX.rstrip("/"):
                    suffix = ""
                elif name.startswith(ARCHIVE_PREFIX):
                    suffix = name.removeprefix(ARCHIVE_PREFIX).rstrip("/")
                else:
                    raise SourceBindingRejected("source_archive_path")
                if suffix:
                    _safe_relative(suffix, stage="source_archive_path")
                if expected_revision is not None and member.pax_headers.get("comment") != expected_revision:
                    raise SourceBindingRejected("source_archive_revision")
                if member.uid != 0 or member.gid != 0 or member.mtime != source_date_epoch:
                    raise SourceBindingRejected("source_archive_metadata")
                if member.isdir():
                    canonical = ARCHIVE_PREFIX if not suffix else ARCHIVE_PREFIX + suffix + "/"
                    if name.rstrip("/") + "/" != canonical or member.mode != 0o755 or canonical in directories:
                        raise SourceBindingRejected("source_archive_directory")
                    directories.add(canonical)
                    continue
                if not member.isfile() or not suffix or member.linkname:
                    raise SourceBindingRejected("source_archive_type")
                if suffix in entries or not 0 <= member.size <= MAX_FILE_BYTES:
                    raise SourceBindingRejected("source_archive_file")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SourceBindingRejected("source_archive_file")
                payload = stream.read(MAX_FILE_BYTES + 1)
                if len(payload) != member.size or len(payload) > MAX_FILE_BYTES:
                    raise SourceBindingRejected("source_archive_file")
                total += len(payload)
                if total > MAX_SOURCE_BYTES:
                    raise SourceBindingRejected("source_archive_size")
                expected_mode = "100755" if member.mode == 0o755 else "100644" if member.mode == 0o644 else ""
                if not expected_mode:
                    raise SourceBindingRejected("source_archive_mode")
                entries[suffix] = _ArchiveEntry(suffix, expected_mode, payload)
            if member_count == 0:
                raise SourceBindingRejected("source_archive_shape")
    except (tarfile.TarError, OSError) as error:
        raise SourceBindingRejected("source_archive_content") from error

    expected_directories = {ARCHIVE_PREFIX}
    for relative in entries:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(ARCHIVE_PREFIX + parent.as_posix() + "/")
            parent = parent.parent
    if directories != expected_directories or not entries or len(entries) > MAX_SOURCE_FILES:
        raise SourceBindingRejected("source_archive_directories")
    if tree_entries is not None:
        if set(entries) != set(tree_entries):
            raise SourceBindingRejected("source_archive_source_set")
        for path, entry in entries.items():
            expected = tree_entries[path]
            if entry.mode != expected.mode or _git_object_id(entry.payload, expected.object_id) != expected.object_id:
                raise SourceBindingRejected("source_archive_source_payload")
    return entries, archive_digest, archive_size


def _source_digest(entries: Mapping[str, _ArchiveEntry]) -> str:
    digest = hashlib.sha256()
    for path, entry in sorted(entries.items()):
        encoded = path.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(entry.mode.encode("ascii"))
        digest.update(len(entry.payload).to_bytes(8, "big"))
        digest.update(entry.payload)
    return "sha256:" + digest.hexdigest()


def _remove_stage_by_path(path: Path) -> None:
    for root, directories, _files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        try:
            os.chmod(root_path, 0o700)
        except OSError:
            pass
        for name in directories:
            target = root_path / name
            try:
                if not target.is_symlink():
                    os.chmod(target, 0o700)
            except OSError:
                pass
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        for name in files:
            target = root_path / name
            try:
                if not target.is_symlink():
                    os.chmod(target, 0o600)
                target.unlink()
            except OSError:
                pass
        for name in directories:
            target = root_path / name
            try:
                if target.is_symlink():
                    target.unlink()
                else:
                    os.chmod(target, 0o700)
                    target.rmdir()
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
        path.rmdir()
    except OSError:
        pass


def _clear_directory_fd(descriptor: int) -> None:
    os.fchmod(descriptor, 0o700)
    for name in os.listdir(descriptor):
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child_descriptor: int | None = None
            try:
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                opened = os.fstat(child_descriptor)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    continue
                _clear_directory_fd(child_descriptor)
            except OSError:
                continue
            finally:
                if child_descriptor is not None:
                    os.close(child_descriptor)
            try:
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    os.rmdir(name, dir_fd=descriptor)
            except OSError:
                pass
        else:
            try:
                os.unlink(name, dir_fd=descriptor)
            except OSError:
                pass


def _remove_stage(path: Path) -> None:
    try:
        root_info = path.lstat()
    except OSError:
        return
    if stat.S_ISLNK(root_info.st_mode):
        try:
            path.unlink()
        except OSError:
            pass
        return
    if not stat.S_ISDIR(root_info.st_mode):
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_descriptor: int | None = None
    descriptor: int | None = None
    try:
        parent_descriptor = os.open(path.parent, flags)
        pinned = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (pinned.st_dev, pinned.st_ino) != (root_info.st_dev, root_info.st_ino):
            return
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (root_info.st_dev, root_info.st_ino):
            return
        _clear_directory_fd(descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (root_info.st_dev, root_info.st_ino):
            os.rmdir(path.name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        return
    except (OSError, NotImplementedError):
        # Windows lacks the complete dirfd surface. Its modern rmtree does not
        # traverse directory junctions; retain the identity checks above.
        try:
            current = path.lstat()
        except OSError:
            return
        if (
            stat.S_ISDIR(current.st_mode)
            and not stat.S_ISLNK(current.st_mode)
            and (current.st_dev, current.st_ino) == (root_info.st_dev, root_info.st_ino)
        ):
            _remove_stage_by_path(path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _extract_stage_by_path(stage_path: Path, entries: Mapping[str, _ArchiveEntry], source_date_epoch: int) -> None:
    try:
        stage_path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SourceBindingRejected("stage_exists") from error
    created_directories = {stage_path}
    try:
        for relative, entry in sorted(entries.items()):
            target = stage_path / relative
            missing: list[Path] = []
            parent = target.parent
            while parent != stage_path and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700)
                created_directories.add(directory)
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                _write_all(descriptor, entry.payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(target, 0o555 if entry.mode == "100755" else 0o444)
            os.utime(target, (source_date_epoch, source_date_epoch), follow_symlinks=False)
        for directory in sorted(created_directories, key=lambda value: len(value.parts), reverse=True):
            os.utime(directory, (source_date_epoch, source_date_epoch), follow_symlinks=False)
            os.chmod(directory, 0o555)
        _fsync_directory(stage_path.parent)
    except (OSError, SourceBindingRejected) as error:
        _remove_stage(stage_path)
        if isinstance(error, SourceBindingRejected):
            raise
        raise SourceBindingRejected("stage_extract") from error


def _entry_tree(entries: Mapping[str, _ArchiveEntry]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for relative, entry in entries.items():
        node = root
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if type(child) is not dict:
                raise SourceBindingRejected("stage_extract")
            node = child
        if parts[-1] in node:
            raise SourceBindingRejected("stage_extract")
        node[parts[-1]] = entry
    return root


def _extract_tree_fd(descriptor: int, node: Mapping[str, Any], source_date_epoch: int) -> None:
    for name, value in sorted(node.items()):
        if type(value) is dict:
            os.mkdir(name, mode=0o700, dir_fd=descriptor)
            child = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                _extract_tree_fd(child, value, source_date_epoch)
                os.utime(child, (source_date_epoch, source_date_epoch))
                os.fchmod(child, 0o555)
                os.fsync(child)
            finally:
                os.close(child)
        elif isinstance(value, _ArchiveEntry):
            child = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=descriptor,
            )
            try:
                _write_all(child, value.payload)
                os.utime(child, (source_date_epoch, source_date_epoch))
                os.fchmod(child, 0o555 if value.mode == "100755" else 0o444)
                os.fsync(child)
            finally:
                os.close(child)
        else:
            raise SourceBindingRejected("stage_extract")


def _extract_stage(stage_path: Path, entries: Mapping[str, _ArchiveEntry], source_date_epoch: int) -> None:
    if os.open not in os.supports_dir_fd or os.mkdir not in os.supports_dir_fd:
        _extract_stage_by_path(stage_path, entries, source_date_epoch)
        return
    parent_descriptor, parent_identity = _open_parent_descriptor(stage_path, stage="stage_path")
    root_descriptor: int | None = None
    root_identity: os.stat_result | None = None
    try:
        os.mkdir(stage_path.name, mode=0o700, dir_fd=parent_descriptor)
        root_descriptor = os.open(
            stage_path.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        root_identity = os.fstat(root_descriptor)
        _extract_tree_fd(root_descriptor, _entry_tree(entries), source_date_epoch)
        os.utime(root_descriptor, (source_date_epoch, source_date_epoch))
        os.fchmod(root_descriptor, 0o555)
        os.fsync(root_descriptor)
        current = os.stat(stage_path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (root_identity.st_dev, root_identity.st_ino) or not _parent_matches(
            stage_path, parent_identity
        ):
            raise SourceBindingRejected("stage_path_changed")
        os.fsync(parent_descriptor)
    except FileExistsError as error:
        raise SourceBindingRejected("stage_exists") from error
    except (OSError, SourceBindingRejected) as error:
        if root_descriptor is not None and root_identity is not None:
            _clear_directory_fd(root_descriptor)
            try:
                current = os.stat(stage_path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (root_identity.st_dev, root_identity.st_ino):
                    os.rmdir(stage_path.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        if isinstance(error, SourceBindingRejected):
            raise
        raise SourceBindingRejected("stage_extract") from error
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _verify_stage(stage_path: Path, entries: Mapping[str, _ArchiveEntry]) -> str:
    try:
        root_info = stage_path.lstat()
    except OSError as error:
        raise SourceBindingRejected("stage_root") from error
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) != 0o555
        or root_info.st_uid != os.getuid()
    ):
        raise SourceBindingRejected("stage_root")
    observed: dict[str, _ArchiveEntry] = {}
    observed_directories: set[str] = set()
    for root, directories, files in os.walk(stage_path, followlinks=False):
        root_path = Path(root)
        for name in directories:
            child = root_path / name
            info = child.lstat()
            relative = child.relative_to(stage_path).as_posix()
            _safe_relative(relative, stage="stage_path")
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o555
                or info.st_uid != os.getuid()
            ):
                raise SourceBindingRejected("stage_type")
            observed_directories.add(relative)
        for name in files:
            child = root_path / name
            relative = child.relative_to(stage_path).as_posix()
            _safe_relative(relative, stage="stage_path")
            payload, info = _regular_file(child, stage="stage_file", maximum=MAX_FILE_BYTES)
            expected = entries.get(relative)
            if expected is None:
                raise SourceBindingRejected("stage_source_mismatch")
            expected_permissions = 0o555 if expected.mode == "100755" else 0o444
            if stat.S_IMODE(info.st_mode) != expected_permissions or info.st_uid != os.getuid():
                raise SourceBindingRejected("stage_permissions")
            mode = expected.mode
            observed[relative] = _ArchiveEntry(relative, mode, payload)
    expected_directories: set[str] = set()
    for relative in entries:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if observed_directories != expected_directories or observed != dict(entries):
        raise SourceBindingRejected("stage_source_mismatch")
    return _source_digest(observed)


def _publish_archive_payload(payload: bytes, destination: Path) -> None:
    _write_exclusive_blob(
        destination,
        payload,
        maximum=MAX_ARCHIVE_BYTES,
        mode=0o400,
        size_reason="archive_size",
        exists_reason="archive_exists",
        write_reason="archive_publish",
    )


def _receipt_document(
    *,
    revision: str,
    tree: str,
    commit_payload: bytes,
    source_date_epoch: int,
    entries: Mapping[str, _ArchiveEntry],
    archive_digest: str,
    archive_size: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "algo-cli-release-source-capture",
        "source": {
            "revision": revision,
            "tree": tree,
            "commit_object_b64": base64.b64encode(commit_payload).decode("ascii"),
            "source_date_epoch": source_date_epoch,
            "file_count": len(entries),
            "payload_bytes": sum(len(entry.payload) for entry in entries.values()),
            "digest": _source_digest(entries),
        },
        "archive": {
            "format": "git-archive-tar-v1",
            "prefix": ARCHIVE_PREFIX,
            "sha256": archive_digest,
            "size": archive_size,
        },
    }


def _validate_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    if set(document) != {"schema_version", "kind", "source", "archive"}:
        raise SourceBindingRejected("receipt_shape")
    source = document.get("source")
    archive = document.get("archive")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != "algo-cli-release-source-capture"
        or type(source) is not dict
        or type(archive) is not dict
        or set(source)
        != {
            "revision",
            "tree",
            "commit_object_b64",
            "source_date_epoch",
            "file_count",
            "payload_bytes",
            "digest",
        }
        or set(archive) != {"format", "prefix", "sha256", "size"}
        or _REVISION_RE.fullmatch(source.get("revision", "")) is None
        or _REVISION_RE.fullmatch(source.get("tree", "")) is None
        or type(source.get("source_date_epoch")) is not int
        or not 1 <= source["source_date_epoch"] <= 9_999_999_999
        or type(source.get("file_count")) is not int
        or not 1 <= source["file_count"] <= MAX_SOURCE_FILES
        or type(source.get("payload_bytes")) is not int
        or not 1 <= source["payload_bytes"] <= MAX_SOURCE_BYTES
        or _DIGEST_RE.fullmatch(source.get("digest", "")) is None
        or archive.get("format") != "git-archive-tar-v1"
        or archive.get("prefix") != ARCHIVE_PREFIX
        or _DIGEST_RE.fullmatch(archive.get("sha256", "")) is None
        or type(archive.get("size")) is not int
        or not 1 <= archive["size"] <= MAX_ARCHIVE_BYTES
    ):
        raise SourceBindingRejected("receipt_content")
    commit_text = source.get("commit_object_b64")
    if type(commit_text) is not str or not 1 <= len(commit_text) <= (MAX_COMMIT_BYTES * 4 // 3) + 4:
        raise SourceBindingRejected("receipt_content")
    try:
        commit_payload = base64.b64decode(commit_text, validate=True)
    except (ValueError, binascii.Error) as error:
        raise SourceBindingRejected("receipt_content") from error
    _validate_commit_object(
        commit_payload,
        revision=source["revision"],
        tree=source["tree"],
        source_date_epoch=source["source_date_epoch"],
    )
    return dict(document)


def capture_revision(
    *,
    repository: Path,
    revision: str,
    archive_path: Path,
    stage_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    try:
        repository = repository.resolve(strict=True)
    except OSError as error:
        raise SourceBindingRejected("repository") from error
    if not repository.is_dir() or repository.is_symlink():
        raise SourceBindingRejected("repository")
    for output in (archive_path, stage_path, receipt_path):
        if not output.is_absolute() or not output.parent.is_dir() or output.exists() or output.is_symlink():
            raise SourceBindingRejected("output_path")
    resolved, tree, source_date_epoch, tree_entries = _source_revision(repository, revision)
    commit_payload = _git(repository, ["cat-file", "commit", resolved], max_output_bytes=MAX_COMMIT_BYTES)
    _validate_commit_object(commit_payload, revision=resolved, tree=tree, source_date_epoch=source_date_epoch)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".oliver-source-", suffix=".tar", dir=archive_path.parent)
    temporary = Path(temporary_name)
    temporary_identity = os.fstat(descriptor)
    try:
        temporary.unlink()
    except OSError:
        pass
    archive_published = False
    stage_published = False
    try:
        _git_to_descriptor(
            repository,
            [
                "archive",
                "--format=tar",
                "--prefix=" + ARCHIVE_PREFIX,
                resolved,
            ],
            descriptor,
        )
        os.fsync(descriptor)
        archive_payload = _descriptor_payload(descriptor, maximum=MAX_ARCHIVE_BYTES)
        archive_entries, archive_digest, archive_size = _archive_payload_entries(
            archive_payload,
            archive_size=len(archive_payload),
            source_date_epoch=source_date_epoch,
            tree_entries=tree_entries,
            expected_revision=resolved,
        )
        if _tree_object_id(archive_entries, tree) != tree:
            raise SourceBindingRejected("source_archive_tree")
        embedded = _git(repository, ["get-tar-commit-id"], input_bytes=archive_payload[:1024]).decode("ascii").strip()
        if embedded != resolved:
            raise SourceBindingRejected("source_archive_revision")
        receipt = _receipt_document(
            revision=resolved,
            tree=tree,
            commit_payload=commit_payload,
            source_date_epoch=source_date_epoch,
            entries=archive_entries,
            archive_digest=archive_digest,
            archive_size=archive_size,
        )
        _publish_archive_payload(archive_payload, archive_path)
        archive_published = True
        _extract_stage(stage_path, archive_entries, source_date_epoch)
        stage_published = True
        if _verify_stage(stage_path, archive_entries) != receipt["source"]["digest"]:
            raise SourceBindingRejected("stage_digest")
        _write_exclusive(receipt_path, _canonical_json(receipt))
        return receipt
    except Exception:
        if stage_published:
            _remove_stage(stage_path)
        if archive_published:
            try:
                archive_path.unlink()
            except OSError:
                pass
        raise
    finally:
        os.close(descriptor)
        try:
            current = temporary.lstat()
            if (current.st_dev, current.st_ino) == (temporary_identity.st_dev, temporary_identity.st_ino):
                temporary.unlink()
        except OSError:
            pass


def _captured_entries(
    *, archive_path: Path, receipt_path: Path, expected_revision: str
) -> tuple[dict[str, Any], dict[str, _ArchiveEntry]]:
    if _REVISION_RE.fullmatch(expected_revision) is None:
        raise SourceBindingRejected("expected_revision")
    receipt = _validate_receipt(_strict_json(receipt_path))
    source = receipt["source"]
    archive = receipt["archive"]
    if source["revision"] != expected_revision:
        raise SourceBindingRejected("expected_revision")
    entries, archive_digest, archive_size = _archive_entries(
        archive_path,
        source_date_epoch=source["source_date_epoch"],
        expected_revision=source["revision"],
    )
    if (
        archive_digest != archive["sha256"]
        or archive_size != archive["size"]
        or len(entries) != source["file_count"]
        or sum(len(entry.payload) for entry in entries.values()) != source["payload_bytes"]
        or _source_digest(entries) != source["digest"]
        or _tree_object_id(entries, source["tree"]) != source["tree"]
    ):
        raise SourceBindingRejected("source_binding_mismatch")
    return receipt, entries


def materialize_stage(
    *, archive_path: Path, receipt_path: Path, stage_path: Path, expected_revision: str
) -> dict[str, Any]:
    if not stage_path.is_absolute() or not stage_path.parent.is_dir() or stage_path.exists() or stage_path.is_symlink():
        raise SourceBindingRejected("stage_path")
    receipt, entries = _captured_entries(
        archive_path=archive_path, receipt_path=receipt_path, expected_revision=expected_revision
    )
    _extract_stage(stage_path, entries, receipt["source"]["source_date_epoch"])
    try:
        if _verify_stage(stage_path, entries) != receipt["source"]["digest"]:
            raise SourceBindingRejected("stage_digest")
    except Exception:
        _remove_stage(stage_path)
        raise
    return receipt


def verify_stage(*, archive_path: Path, stage_path: Path, receipt_path: Path, expected_revision: str) -> dict[str, Any]:
    receipt, entries = _captured_entries(
        archive_path=archive_path, receipt_path=receipt_path, expected_revision=expected_revision
    )
    if _verify_stage(stage_path, entries) != receipt["source"]["digest"]:
        raise SourceBindingRejected("source_binding_mismatch")
    return receipt


def _toml(payload: bytes, *, stage: str) -> dict[str, Any]:
    try:
        document = tomllib.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SourceBindingRejected(stage) from error
    if type(document) is not dict:
        raise SourceBindingRejected(stage)
    return document


def _package_configuration(entries: Mapping[str, _ArchiveEntry]) -> tuple[str, dict[str, Any], str]:
    try:
        project = _toml(entries["pyproject.toml"].payload, stage="pyproject")
        build_system = project["build-system"]
        metadata = project["project"]
        tool = project["tool"]["hatch"]
        name = metadata["name"]
        version_path = tool["version"]["path"]
        version_payload = entries[version_path].payload.decode("utf-8", errors="strict")
    except (KeyError, TypeError, UnicodeDecodeError) as error:
        raise SourceBindingRejected("package_configuration") from error
    if (
        name != "algo-cli-runtime"
        or type(version_path) is not str
        or type(build_system) is not dict
        or set(build_system) != {"requires", "build-backend"}
        or build_system.get("requires") != ["hatchling==1.31.0"]
        or build_system.get("build-backend") != "hatchling.build"
    ):
        raise SourceBindingRejected("package_configuration")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', version_payload, flags=re.MULTILINE)
    if match is None or _VERSION_RE.fullmatch(match.group(1)) is None:
        raise SourceBindingRejected("package_version")
    return match.group(1), project, version_path


def _included_source_paths(patterns: Iterable[str], entries: Mapping[str, _ArchiveEntry]) -> set[str]:
    selected: set[str] = set()
    for raw in patterns:
        if type(raw) is not str or not raw.startswith("/"):
            raise SourceBindingRejected("sdist_configuration")
        pattern = _safe_relative(raw.removeprefix("/"), stage="sdist_configuration")
        if any(character in pattern for character in "*?["):
            matches = {path for path in entries if PurePosixPath(path).match(pattern)}
        elif pattern in entries:
            matches = {pattern}
        else:
            prefix = pattern.rstrip("/") + "/"
            matches = {path for path in entries if path.startswith(prefix)}
        if not matches:
            raise SourceBindingRejected("sdist_configuration")
        selected.update(matches)
    return selected


def _safe_sdist(path: Path, *, version: str) -> tuple[dict[str, bytes], bytes]:
    payload, _info = _regular_file(path, stage="sdist_file", maximum=MAX_ARCHIVE_BYTES, minimum=1)
    prefix = "algo_cli_runtime-" + version + "/"
    observed: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r|gz") as archive:
            member_count = 0
            for member in archive:
                member_count += 1
                if member_count > MAX_SOURCE_FILES * 3:
                    raise SourceBindingRejected("sdist_size")
                if not member.name.startswith(prefix):
                    raise SourceBindingRejected("sdist_path")
                relative = member.name.removeprefix(prefix).rstrip("/")
                if relative:
                    _safe_relative(relative, stage="sdist_path")
                if member.isdir():
                    continue
                if not member.isfile() or not relative or member.linkname or relative in observed:
                    raise SourceBindingRejected("sdist_type")
                if not 0 <= member.size <= MAX_FILE_BYTES:
                    raise SourceBindingRejected("sdist_size")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SourceBindingRejected("sdist_content")
                content = stream.read(MAX_FILE_BYTES + 1)
                if len(content) != member.size or len(content) > MAX_FILE_BYTES:
                    raise SourceBindingRejected("sdist_content")
                total += len(content)
                if total > MAX_SOURCE_BYTES:
                    raise SourceBindingRejected("sdist_size")
                observed[relative] = content
            if member_count == 0:
                raise SourceBindingRejected("sdist_content")
    except (tarfile.TarError, OSError) as error:
        raise SourceBindingRejected("sdist_content") from error
    if not observed:
        raise SourceBindingRejected("sdist_content")
    return observed, payload


def _safe_wheel(path: Path) -> tuple[dict[str, bytes], bytes, dict[str, zipfile.ZipInfo]]:
    payload, _info = _regular_file(path, stage="wheel_file", maximum=MAX_ARCHIVE_BYTES, minimum=1)
    observed: dict[str, bytes] = {}
    infos: dict[str, zipfile.ZipInfo] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos_list = archive.infolist()
            if not infos_list or len(infos_list) > MAX_SOURCE_FILES * 3:
                raise SourceBindingRejected("wheel_size")
            for info in infos_list:
                name = info.filename.rstrip("/")
                if name:
                    _safe_relative(name, stage="wheel_path")
                if info.filename.endswith("/"):
                    unix_mode = info.external_attr >> 16
                    if unix_mode and not stat.S_ISDIR(unix_mode):
                        raise SourceBindingRejected("wheel_type")
                    continue
                unix_mode = info.external_attr >> 16
                if (unix_mode and not stat.S_ISREG(unix_mode)) or info.flag_bits & 0x1 or name in observed:
                    raise SourceBindingRejected("wheel_type")
                if not 0 <= info.file_size <= MAX_FILE_BYTES:
                    raise SourceBindingRejected("wheel_size")
                content = archive.read(info)
                if len(content) != info.file_size:
                    raise SourceBindingRejected("wheel_content")
                total += len(content)
                if total > MAX_SOURCE_BYTES:
                    raise SourceBindingRejected("wheel_size")
                observed[name] = content
                infos[name] = info
    except SourceBindingRejected:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise SourceBindingRejected("wheel_content") from error
    if not observed:
        raise SourceBindingRejected("wheel_content")
    return observed, payload, infos


def _wheel_source_mapping(project: Mapping[str, Any], entries: Mapping[str, _ArchiveEntry]) -> dict[str, str]:
    try:
        wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
        packages = wheel["packages"]
        force_include = wheel["force-include"]
    except (KeyError, TypeError) as error:
        raise SourceBindingRejected("wheel_configuration") from error
    if type(packages) is not list or type(force_include) is not dict:
        raise SourceBindingRejected("wheel_configuration")
    mapping: dict[str, str] = {}
    for package in packages:
        if type(package) is not str:
            raise SourceBindingRejected("wheel_configuration")
        package = _safe_relative(package, stage="wheel_configuration").rstrip("/")
        for source in entries:
            if source == package or source.startswith(package + "/"):
                mapping[source] = source
    for raw_source, raw_target in force_include.items():
        if type(raw_source) is not str or type(raw_target) is not str:
            raise SourceBindingRejected("wheel_configuration")
        source = _safe_relative(raw_source, stage="wheel_configuration").rstrip("/")
        target = _safe_relative(raw_target, stage="wheel_configuration").rstrip("/")
        if source in entries:
            additions = {target: source}
        else:
            prefix = source + "/"
            additions = {target + path.removeprefix(source): path for path in entries if path.startswith(prefix)}
        if not additions:
            raise SourceBindingRejected("wheel_configuration")
        for destination, origin in additions.items():
            if destination in mapping and mapping[destination] != origin:
                raise SourceBindingRejected("wheel_configuration")
            mapping[destination] = origin
    if not mapping:
        raise SourceBindingRejected("wheel_configuration")
    return mapping


def _record_digest(payload: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")


def _canonical_extra(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value).lower()
    if _SAFE_NAME_RE.fullmatch(normalized) is None:
        raise SourceBindingRejected("artifact_core_metadata")
    return normalized


def _canonical_requirement(value: str) -> str:
    requirement, separator, marker = value.partition(";")
    requirement = re.sub(r"\s+", "", requirement)
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$", requirement)
    if match is None:
        raise SourceBindingRejected("artifact_core_metadata")
    canonical_name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    normalized = canonical_name + match.group(2)
    if separator:
        normalized_marker = re.sub(r"\s+", " ", marker.strip().replace('"', "'"))
        if not normalized_marker:
            raise SourceBindingRejected("artifact_core_metadata")
        normalized += "; " + normalized_marker
    return normalized


def _expected_requirements(metadata: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    dependencies = metadata.get("dependencies", [])
    optional = metadata.get("optional-dependencies", {})
    if type(dependencies) is not list or type(optional) is not dict:
        raise SourceBindingRejected("package_configuration")
    expected: set[str] = set()
    for requirement in dependencies:
        if type(requirement) is not str:
            raise SourceBindingRejected("package_configuration")
        expected.add(_canonical_requirement(requirement))
    extras: set[str] = set()
    for raw_extra, requirements in optional.items():
        if type(raw_extra) is not str or type(requirements) is not list:
            raise SourceBindingRejected("package_configuration")
        extra = _canonical_extra(raw_extra)
        extras.add(extra)
        for requirement in requirements:
            if type(requirement) is not str:
                raise SourceBindingRejected("package_configuration")
            base, separator, marker = requirement.partition(";")
            if separator:
                combined = f"{base}; ({marker.strip()}) and extra == '{extra}'"
            else:
                combined = f"{base}; extra == '{extra}'"
            expected.add(_canonical_requirement(combined))
    return expected, extras


def _verify_core_metadata(
    payload: bytes,
    *,
    version: str,
    project: Mapping[str, Any],
    entries: Mapping[str, _ArchiveEntry],
) -> None:
    try:
        metadata = project["project"]
        if type(metadata) is not dict:
            raise TypeError
        parsed = email.parser.BytesParser().parsebytes(payload)
    except (KeyError, TypeError) as error:
        raise SourceBindingRejected("artifact_core_metadata") from error
    expected_requirements, expected_extras = _expected_requirements(metadata)
    try:
        observed_requirements = {_canonical_requirement(value) for value in (parsed.get_all("Requires-Dist") or [])}
    except (TypeError, AttributeError) as error:
        raise SourceBindingRejected("artifact_core_metadata") from error
    if (
        parsed.get("Metadata-Version") != "2.4"
        or parsed.get("Name") != "algo-cli-runtime"
        or parsed.get("Version") != version
        or parsed.get("Summary") != metadata.get("description")
        or parsed.get("Requires-Python") != metadata.get("requires-python")
        or observed_requirements != expected_requirements
        or set(parsed.get_all("Provides-Extra") or []) != expected_extras
    ):
        raise SourceBindingRejected("artifact_core_metadata")

    urls = metadata.get("urls", {})
    classifiers = metadata.get("classifiers", [])
    keywords = metadata.get("keywords", [])
    authors = metadata.get("authors", [])
    if (
        type(urls) is not dict
        or type(classifiers) is not list
        or type(keywords) is not list
        or type(authors) is not list
        or any(type(key) is not str or type(value) is not str for key, value in urls.items())
        or any(type(value) is not str for value in classifiers)
        or any(type(value) is not str for value in keywords)
        or any(type(value) is not dict or set(value) != {"name"} or type(value["name"]) is not str for value in authors)
    ):
        raise SourceBindingRejected("package_configuration")
    expected_author = ", ".join(value["name"] for value in authors) or None
    expected_keywords = {value for value in keywords}
    observed_keywords = set(filter(None, (parsed.get("Keywords") or "").split(",")))
    if (
        set(parsed.get_all("Project-URL") or []) != {f"{key}, {value}" for key, value in urls.items()}
        or set(parsed.get_all("Classifier") or []) != set(classifiers)
        or observed_keywords != expected_keywords
        or parsed.get("Author") != expected_author
    ):
        raise SourceBindingRejected("artifact_core_metadata")

    readme = metadata.get("readme")
    license_config = metadata.get("license")
    if type(readme) is not str or type(license_config) is not dict or set(license_config) != {"file"}:
        raise SourceBindingRejected("package_configuration")
    license_path = license_config.get("file")
    if type(license_path) is not str:
        raise SourceBindingRejected("package_configuration")
    readme = _safe_relative(readme, stage="package_configuration")
    license_path = _safe_relative(license_path, stage="package_configuration")
    try:
        expected_description = entries[readme].payload
    except KeyError as error:
        raise SourceBindingRejected("package_configuration") from error
    _headers, separator, description = payload.partition(b"\n\n")
    expected_content_type = "text/markdown" if readme.lower().endswith(".md") else None
    if (
        not separator
        or description != expected_description
        or parsed.get("Description-Content-Type") != expected_content_type
        or parsed.get_all("License-File") != [license_path]
    ):
        raise SourceBindingRejected("artifact_core_metadata")


def _verify_record(wheel: Mapping[str, bytes], record_path: str) -> None:
    try:
        rows = list(csv.reader(io.StringIO(wheel[record_path].decode("utf-8", errors="strict"), newline="")))
    except (UnicodeDecodeError, csv.Error, KeyError) as error:
        raise SourceBindingRejected("wheel_record") from error
    observed: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in observed:
            raise SourceBindingRejected("wheel_record")
        observed[row[0]] = (row[1], row[2])
    if set(observed) != set(wheel) or observed.get(record_path) != ("", ""):
        raise SourceBindingRejected("wheel_record")
    for name, payload in wheel.items():
        if name == record_path:
            continue
        if observed[name] != (_record_digest(payload), str(len(payload))):
            raise SourceBindingRejected("wheel_record")


def _tool_versions(lock_payload: bytes) -> tuple[str, str]:
    lock = _toml(lock_payload, stage="tool_lock")
    packages = lock.get("package")
    if type(packages) is not list:
        raise SourceBindingRejected("tool_lock")
    versions: dict[str, list[str]] = {}
    for package in packages:
        if type(package) is not dict or type(package.get("name")) is not str or type(package.get("version")) is not str:
            raise SourceBindingRejected("tool_lock")
        versions.setdefault(package["name"], []).append(package["version"])
    if versions.get("build") != ["1.5.0"] or versions.get("hatchling") != ["1.31.0"]:
        raise SourceBindingRejected("tool_lock_versions")
    return "build==1.5.0", "hatchling==1.31.0"


def _distribution_binding(
    *,
    entries: Mapping[str, _ArchiveEntry],
    dist_path: Path,
    tool_lock_path: Path | None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    version, project, _version_path = _package_configuration(entries)
    normalized = version.replace("-", "_")
    wheel_name = f"algo_cli_runtime-{normalized}-py3-none-any.whl"
    sdist_name = f"algo_cli_runtime-{version}.tar.gz"
    try:
        children = tuple(dist_path.iterdir())
    except OSError as error:
        raise SourceBindingRejected("distribution_directory") from error
    if {child.name for child in children} != {wheel_name, sdist_name}:
        raise SourceBindingRejected("distribution_set")
    wheel_path = dist_path / wheel_name
    sdist_path = dist_path / sdist_name
    sdist, sdist_blob = _safe_sdist(sdist_path, version=version)
    wheel, wheel_blob, _infos = _safe_wheel(wheel_path)

    try:
        sdist_patterns = project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    except (KeyError, TypeError) as error:
        raise SourceBindingRejected("sdist_configuration") from error
    if type(sdist_patterns) is not list:
        raise SourceBindingRejected("sdist_configuration")
    expected_sdist = _included_source_paths(sdist_patterns, entries)
    if set(sdist) != expected_sdist | {"PKG-INFO"}:
        raise SourceBindingRejected("sdist_source_set")
    if any(sdist[path] != entries[path].payload for path in expected_sdist):
        raise SourceBindingRejected("sdist_source_parity")

    wheel_mapping = _wheel_source_mapping(project, entries)
    dist_info = f"algo_cli_runtime-{normalized}.dist-info"
    generated = {
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/licenses/LICENSE",
    }
    if set(wheel) != set(wheel_mapping) | generated:
        raise SourceBindingRejected("wheel_source_set")
    for destination, source in wheel_mapping.items():
        if source not in sdist or wheel[destination] != sdist[source]:
            raise SourceBindingRejected("wheel_source_parity")
    if wheel[f"{dist_info}/METADATA"] != sdist["PKG-INFO"]:
        raise SourceBindingRejected("artifact_metadata_parity")
    if wheel[f"{dist_info}/licenses/LICENSE"] != sdist.get("LICENSE"):
        raise SourceBindingRejected("artifact_license_parity")

    _verify_core_metadata(sdist["PKG-INFO"], version=version, project=project, entries=entries)

    parser = _CaseSensitiveConfigParser(interpolation=None)
    try:
        parser.read_string(wheel[f"{dist_info}/entry_points.txt"].decode("utf-8", errors="strict"))
        scripts = project["project"]["scripts"]
    except (UnicodeDecodeError, configparser.Error, KeyError, TypeError) as error:
        raise SourceBindingRejected("artifact_entry_points") from error
    if parser.sections() != ["console_scripts"] or dict(parser.items("console_scripts")) != scripts:
        raise SourceBindingRejected("artifact_entry_points")

    try:
        wheel_metadata = email.parser.BytesParser().parsebytes(wheel[f"{dist_info}/WHEEL"])
    except (KeyError, TypeError) as error:
        raise SourceBindingRejected("wheel_metadata") from error
    if (
        wheel_metadata.get("Wheel-Version") != "1.0"
        or wheel_metadata.get("Generator") != "hatchling 1.31.0"
        or wheel_metadata.get("Root-Is-Purelib") != "true"
        or wheel_metadata.get_all("Tag") != ["py3-none-any"]
    ):
        raise SourceBindingRejected("wheel_metadata")
    _verify_record(wheel, f"{dist_info}/RECORD")

    try:
        captured_lock = entries["uv.lock"].payload
    except KeyError as error:
        raise SourceBindingRejected("tool_lock_source") from error
    lock_payload = captured_lock
    if tool_lock_path is not None:
        lock_payload, _lock_info = _regular_file(tool_lock_path, stage="tool_lock", maximum=MAX_FILE_BYTES, minimum=1)
        if lock_payload != captured_lock:
            raise SourceBindingRejected("tool_lock_source")
    frontend, backend = _tool_versions(lock_payload)
    binding = {
        "version": version,
        "build": {
            "frontend": frontend,
            "backend": backend,
            "strategy": "python-build-default-wheel-from-sdist",
            "trial_count": 2,
            "artifacts_byte_identical": True,
            "sdist_source_parity": True,
            "wheel_from_sdist_content_parity": True,
            "metadata_parity": True,
            "wheel_record_verified": True,
        },
        "tool_lock": {
            "path": "uv.lock",
            "sha256": "sha256:" + hashlib.sha256(lock_payload).hexdigest(),
            "size": len(lock_payload),
        },
        "artifacts": [
            {
                "kind": "sdist",
                "filename": sdist_name,
                "sha256": "sha256:" + hashlib.sha256(sdist_blob).hexdigest(),
                "size": len(sdist_blob),
            },
            {
                "kind": "wheel",
                "filename": wheel_name,
                "sha256": "sha256:" + hashlib.sha256(wheel_blob).hexdigest(),
                "size": len(wheel_blob),
            },
        ],
    }
    return binding, {sdist_name: sdist_blob, wheel_name: wheel_blob}


def _manifest_document(receipt: Mapping[str, Any], distribution: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "algo-cli-release-source-binding",
        "source": dict(receipt["source"]),
        "archive": dict(receipt["archive"]),
        **distribution,
    }


def _publish_bound_distribution(path: Path, payloads: Mapping[str, bytes], source_date_epoch: int) -> None:
    if not payloads or any(_SAFE_NAME_RE.fullmatch(name) is None for name in payloads):
        raise SourceBindingRejected("bound_distribution")
    parent_descriptor, parent_identity = _open_parent_descriptor(path, stage="bound_distribution")
    root_descriptor: int | None = None
    root_identity: os.stat_result | None = None
    try:
        os.mkdir(path.name, mode=0o700, dir_fd=parent_descriptor)
        root_descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        root_identity = os.fstat(root_descriptor)
        for name, payload in sorted(payloads.items()):
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o400,
                dir_fd=root_descriptor,
            )
            try:
                _write_all(descriptor, payload)
                os.utime(descriptor, (source_date_epoch, source_date_epoch))
                os.fchmod(descriptor, 0o400)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.utime(root_descriptor, (source_date_epoch, source_date_epoch))
        os.fchmod(root_descriptor, 0o500)
        os.fsync(root_descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (root_identity.st_dev, root_identity.st_ino) or not _parent_matches(
            path, parent_identity
        ):
            raise SourceBindingRejected("bound_distribution_changed")
        os.fsync(parent_descriptor)
    except FileExistsError as error:
        raise SourceBindingRejected("bound_distribution_exists") from error
    except (OSError, SourceBindingRejected) as error:
        if root_descriptor is not None and root_identity is not None:
            _clear_directory_fd(root_descriptor)
            try:
                current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (root_identity.st_dev, root_identity.st_ino):
                    os.rmdir(path.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
            except OSError:
                pass
        if isinstance(error, SourceBindingRejected):
            raise
        raise SourceBindingRejected("bound_distribution") from error
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
        os.close(parent_descriptor)


def _verify_release_paths(
    *,
    stage_path: Path,
    rebuild_stage_path: Path,
    dist_path: Path,
    rebuild_dist_path: Path,
    bound_dist_path: Path,
    tool_lock_path: Path,
) -> None:
    directories = (stage_path, rebuild_stage_path, dist_path, rebuild_dist_path)
    for path in directories:
        try:
            info = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise SourceBindingRejected("release_path") from error
        if not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or resolved != path:
            raise SourceBindingRejected("release_path")
    if (
        stage_path == rebuild_stage_path
        or dist_path == rebuild_dist_path
        or bound_dist_path in directories
        or not bound_dist_path.is_absolute()
    ):
        raise SourceBindingRejected("independent_build_paths")
    try:
        expected_lock = (stage_path / "uv.lock").resolve(strict=True)
        observed_lock = tool_lock_path.resolve(strict=True)
    except OSError as error:
        raise SourceBindingRejected("tool_lock_path") from error
    if not tool_lock_path.is_absolute() or expected_lock != observed_lock:
        raise SourceBindingRejected("tool_lock_path")


def _validate_bound_directory(path: Path, *, sealed: bool) -> None:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SourceBindingRejected("bound_distribution") from error
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or (sealed and stat.S_IMODE(info.st_mode) != 0o500)
        or info.st_mode & 0o022
        or resolved != path
        or info.st_uid != os.getuid()
    ):
        raise SourceBindingRejected("bound_distribution")


def bind_release(
    *,
    archive_path: Path,
    expected_revision: str,
    stage_path: Path,
    rebuild_stage_path: Path,
    receipt_path: Path,
    dist_path: Path,
    rebuild_dist_path: Path,
    bound_dist_path: Path,
    tool_lock_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    if not manifest_path.is_absolute() or not manifest_path.parent.is_dir():
        raise SourceBindingRejected("manifest_path")
    receipt, entries = _captured_entries(
        archive_path=archive_path, receipt_path=receipt_path, expected_revision=expected_revision
    )
    if _verify_stage(stage_path, entries) != receipt["source"]["digest"]:
        raise SourceBindingRejected("source_binding_mismatch")
    if _verify_stage(rebuild_stage_path, entries) != receipt["source"]["digest"]:
        raise SourceBindingRejected("rebuild_source_binding_mismatch")
    _verify_release_paths(
        stage_path=stage_path,
        rebuild_stage_path=rebuild_stage_path,
        dist_path=dist_path,
        rebuild_dist_path=rebuild_dist_path,
        bound_dist_path=bound_dist_path,
        tool_lock_path=tool_lock_path,
    )
    distribution, payloads = _distribution_binding(entries=entries, dist_path=dist_path, tool_lock_path=tool_lock_path)
    rebuilt, rebuilt_payloads = _distribution_binding(
        entries=entries, dist_path=rebuild_dist_path, tool_lock_path=tool_lock_path
    )
    if distribution != rebuilt or payloads != rebuilt_payloads:
        raise SourceBindingRejected("rebuild_artifact_mismatch")
    manifest = _manifest_document(receipt, distribution)
    _publish_bound_distribution(bound_dist_path, payloads, receipt["source"]["source_date_epoch"])
    try:
        _write_exclusive(manifest_path, _canonical_json(manifest))
    except Exception:
        _remove_stage(bound_dist_path)
        raise
    return manifest


def _validate_manifest(document: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "kind",
        "source",
        "archive",
        "version",
        "build",
        "tool_lock",
        "artifacts",
    }
    if set(document) != expected or document.get("kind") != "algo-cli-release-source-binding":
        raise SourceBindingRejected("manifest_shape")
    _validate_receipt(
        {
            "schema_version": document.get("schema_version"),
            "kind": "algo-cli-release-source-capture",
            "source": document.get("source"),
            "archive": document.get("archive"),
        }
    )
    if type(document.get("version")) is not str or _VERSION_RE.fullmatch(document["version"]) is None:
        raise SourceBindingRejected("manifest_version")
    if type(document.get("build")) is not dict or type(document.get("tool_lock")) is not dict:
        raise SourceBindingRejected("manifest_content")
    artifacts = document.get("artifacts")
    if type(artifacts) is not list or len(artifacts) != 2:
        raise SourceBindingRejected("manifest_artifacts")
    for artifact in artifacts:
        if (
            type(artifact) is not dict
            or set(artifact) != {"kind", "filename", "sha256", "size"}
            or artifact.get("kind") not in {"sdist", "wheel"}
            or type(artifact.get("filename")) is not str
            or _SAFE_NAME_RE.fullmatch(artifact["filename"]) is None
            or _DIGEST_RE.fullmatch(artifact.get("sha256", "")) is None
            or type(artifact.get("size")) is not int
            or not 1 <= artifact["size"] <= MAX_ARCHIVE_BYTES
        ):
            raise SourceBindingRejected("manifest_artifacts")
    return dict(document)


def verify_release(
    *,
    manifest_path: Path,
    archive_path: Path,
    expected_revision: str,
    stage_path: Path,
    rebuild_stage_path: Path,
    receipt_path: Path,
    dist_path: Path,
    rebuild_dist_path: Path,
    bound_dist_path: Path,
    tool_lock_path: Path,
) -> dict[str, Any]:
    manifest = _validate_manifest(_strict_json(manifest_path))
    receipt, entries = _captured_entries(
        archive_path=archive_path, receipt_path=receipt_path, expected_revision=expected_revision
    )
    if _verify_stage(stage_path, entries) != receipt["source"]["digest"]:
        raise SourceBindingRejected("source_binding_mismatch")
    if _verify_stage(rebuild_stage_path, entries) != receipt["source"]["digest"]:
        raise SourceBindingRejected("rebuild_source_binding_mismatch")
    _verify_release_paths(
        stage_path=stage_path,
        rebuild_stage_path=rebuild_stage_path,
        dist_path=dist_path,
        rebuild_dist_path=rebuild_dist_path,
        bound_dist_path=bound_dist_path,
        tool_lock_path=tool_lock_path,
    )
    _validate_bound_directory(bound_dist_path, sealed=True)
    distribution, payloads = _distribution_binding(entries=entries, dist_path=dist_path, tool_lock_path=tool_lock_path)
    rebuilt, rebuilt_payloads = _distribution_binding(
        entries=entries, dist_path=rebuild_dist_path, tool_lock_path=tool_lock_path
    )
    bound, bound_payloads = _distribution_binding(
        entries=entries, dist_path=bound_dist_path, tool_lock_path=tool_lock_path
    )
    if distribution != rebuilt or distribution != bound or payloads != rebuilt_payloads or payloads != bound_payloads:
        raise SourceBindingRejected("rebuild_artifact_mismatch")
    expected = _manifest_document(receipt, distribution)
    if manifest != expected:
        raise SourceBindingRejected("manifest_mismatch")
    return manifest


def verify_bound_release(
    *,
    manifest_path: Path,
    archive_path: Path,
    expected_revision: str,
    receipt_path: Path,
    bound_dist_path: Path,
) -> dict[str, Any]:
    manifest = _validate_manifest(_strict_json(manifest_path))
    receipt, entries = _captured_entries(
        archive_path=archive_path, receipt_path=receipt_path, expected_revision=expected_revision
    )
    _validate_bound_directory(bound_dist_path, sealed=False)
    distribution, _payloads = _distribution_binding(entries=entries, dist_path=bound_dist_path, tool_lock_path=None)
    expected = _manifest_document(receipt, distribution)
    if manifest != expected:
        raise SourceBindingRejected("manifest_mismatch")
    return manifest


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--repository", type=_absolute, default=ROOT)
    capture.add_argument("--revision", required=True)
    capture.add_argument("--archive", type=_absolute, required=True)
    capture.add_argument("--stage", type=_absolute, required=True)
    capture.add_argument("--receipt", type=_absolute, required=True)
    stage = commands.add_parser("verify-stage")
    stage.add_argument("--archive", type=_absolute, required=True)
    stage.add_argument("--stage", type=_absolute, required=True)
    stage.add_argument("--receipt", type=_absolute, required=True)
    stage.add_argument("--expected-revision", required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--archive", type=_absolute, required=True)
    materialize.add_argument("--stage", type=_absolute, required=True)
    materialize.add_argument("--receipt", type=_absolute, required=True)
    materialize.add_argument("--expected-revision", required=True)
    bind = commands.add_parser("bind")
    verify = commands.add_parser("verify")
    for command in (bind, verify):
        command.add_argument("--archive", type=_absolute, required=True)
        command.add_argument("--expected-revision", required=True)
        command.add_argument("--stage", type=_absolute, required=True)
        command.add_argument("--rebuild-stage", type=_absolute, required=True)
        command.add_argument("--receipt", type=_absolute, required=True)
        command.add_argument("--dist", type=_absolute, required=True)
        command.add_argument("--rebuild-dist", type=_absolute, required=True)
        command.add_argument("--bound-dist", type=_absolute, required=True)
        command.add_argument("--tool-lock", type=_absolute, required=True)
        command.add_argument("--manifest", type=_absolute, required=True)
    bound = commands.add_parser("verify-bound")
    bound.add_argument("--archive", type=_absolute, required=True)
    bound.add_argument("--expected-revision", required=True)
    bound.add_argument("--receipt", type=_absolute, required=True)
    bound.add_argument("--bound-dist", type=_absolute, required=True)
    bound.add_argument("--manifest", type=_absolute, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "capture":
            document = capture_revision(
                repository=arguments.repository,
                revision=arguments.revision,
                archive_path=arguments.archive,
                stage_path=arguments.stage,
                receipt_path=arguments.receipt,
            )
        elif arguments.command == "verify-stage":
            document = verify_stage(
                archive_path=arguments.archive,
                expected_revision=arguments.expected_revision,
                stage_path=arguments.stage,
                receipt_path=arguments.receipt,
            )
        elif arguments.command == "materialize":
            document = materialize_stage(
                archive_path=arguments.archive,
                expected_revision=arguments.expected_revision,
                stage_path=arguments.stage,
                receipt_path=arguments.receipt,
            )
        elif arguments.command == "bind":
            document = bind_release(
                archive_path=arguments.archive,
                expected_revision=arguments.expected_revision,
                stage_path=arguments.stage,
                rebuild_stage_path=arguments.rebuild_stage,
                receipt_path=arguments.receipt,
                dist_path=arguments.dist,
                rebuild_dist_path=arguments.rebuild_dist,
                bound_dist_path=arguments.bound_dist,
                tool_lock_path=arguments.tool_lock,
                manifest_path=arguments.manifest,
            )
        elif arguments.command == "verify":
            document = verify_release(
                manifest_path=arguments.manifest,
                archive_path=arguments.archive,
                expected_revision=arguments.expected_revision,
                stage_path=arguments.stage,
                rebuild_stage_path=arguments.rebuild_stage,
                receipt_path=arguments.receipt,
                dist_path=arguments.dist,
                rebuild_dist_path=arguments.rebuild_dist,
                bound_dist_path=arguments.bound_dist,
                tool_lock_path=arguments.tool_lock,
            )
        else:
            document = verify_bound_release(
                manifest_path=arguments.manifest,
                archive_path=arguments.archive,
                expected_revision=arguments.expected_revision,
                receipt_path=arguments.receipt,
                bound_dist_path=arguments.bound_dist,
            )
    except SourceBindingRejected as error:
        print(
            json.dumps(
                {"schema_version": SCHEMA_VERSION, "passed": False, "reason_code": str(error)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "passed": True,
                "kind": document["kind"],
                "source_revision": document["source"]["revision"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
