"""Descriptor-bound, deadline-isolated search of non-memory text snapshots."""

from __future__ import annotations

from bisect import bisect_right
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Any, Iterator

from pathspec import GitIgnoreSpec
from wcmatch import glob as wcglob

from . import config
from .irene_memory_path_policy import (
    ProtectedMemoryPathError,
    ProtectedPathRules,
    protected_path_rules,
    require_allowed_path,
)
from .search_execution import _Capture, run_search_process

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_DEPTH = 64
_UNSAFE = "protected search could not validate stable file and policy boundaries"


class _PatternError(ValueError):
    pass


def _identity(info: os.stat_result) -> tuple[int, ...]:
    if stat.S_ISDIR(info.st_mode):
        return config._portable_directory_identity(info)
    return config._portable_state_identity(info)


@dataclass
class _Directory:
    path: Path
    fd: int | None
    ancestry: tuple[tuple[Path, tuple[int, ...]], ...]

    def check(self) -> None:
        config._recheck_directory_chain(self.ancestry)
        if self.fd is not None and _identity(os.fstat(self.fd)) != self.ancestry[-1][1]:
            raise OSError(_UNSAFE)

    def info(self, name: str) -> os.stat_result:
        if self.fd is None:
            return (self.path / name).lstat()
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)


@contextmanager
def _bound_directory(
    path: Path,
    rules: ProtectedPathRules,
    parent: _Directory | None = None,
) -> Iterator[_Directory]:
    if rules.denies(path):
        raise OSError(_UNSAFE)
    if os.name == "nt":
        with config._windows_pinned_directory_chain(path) as ancestry:
            # Reject alternate DOS/8.3 spellings before opening descendants.
            if os.path.realpath(path).casefold() != os.fspath(path).casefold() or any(
                identity[:3] in rules.identities for _path, identity in ancestry
            ):
                raise OSError(_UNSAFE)
            directory = _Directory(path, None, ancestry)
            yield directory
            directory.check()
        return
    if not config._directory_descriptor_io_supported() or not getattr(os, "O_NOFOLLOW", 0):
        raise OSError(_UNSAFE)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    with ExitStack() as stack:
        captured = list(parent.ancestry) if parent else []
        fd = parent.fd if parent else None
        paths = [path] if parent else list(reversed(path.parents)) + [path]
        for current in paths:
            before = current.lstat() if fd is None else os.stat(current.name, dir_fd=fd, follow_symlinks=False)
            if (
                config._path_is_reparse_point(current, before)
                or not stat.S_ISDIR(before.st_mode)
                or rules.denies_identity(before)
            ):
                raise OSError(_UNSAFE)
            opened = os.open(current if fd is None else current.name, flags, dir_fd=fd)
            stack.callback(os.close, opened)
            if _identity(os.fstat(opened)) != _identity(before):
                raise OSError(_UNSAFE)
            captured.append((current, _identity(before)))
            fd = opened
        directory = _Directory(path, fd, tuple(captured))
        directory.check()
        yield directory
        directory.check()


class _ScanLimit(Exception):
    pass


@dataclass
class _Budget:
    max_files: int
    max_file_bytes: int
    entries: int = 0
    files: int = 0
    bytes_read: int = 0
    incomplete: bool = False


def _read_payload(directory: _Directory, name: str, rules: ProtectedPathRules, budget: _Budget) -> bytes | None:
    path = directory.path / name
    if rules.denies(path):
        return None
    before = directory.info(name)
    if (
        config._path_is_reparse_point(path, before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or rules.denies_identity(before)
    ):
        return None
    if not 0 <= before.st_size <= budget.max_file_bytes:
        budget.incomplete = True
        return None
    if budget.files >= budget.max_files or budget.bytes_read + before.st_size > MAX_SCAN_BYTES:
        budget.incomplete = True
        raise _ScanLimit
    directory.check()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path if directory.fd is None else name, flags, dir_fd=directory.fd)
    try:
        opened = os.fstat(fd)
        if _identity(opened) != _identity(before) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError(_UNSAFE)
        if os.name == "nt":
            final_path = config._windows_descriptor_final_path(fd)
            if (
                final_path is None
                or rules.denies(final_path)
                or os.fspath(final_path).casefold() != os.fspath(path).casefold()
                or _identity(final_path.lstat()) != _identity(opened)
            ):
                raise OSError(_UNSAFE)
        chunks: list[bytes] = []
        remaining = min(budget.max_file_bytes, MAX_SCAN_BYTES - budget.bytes_read) + 1
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if (
            len(payload) != before.st_size
            or _identity(os.fstat(fd)) != _identity(opened)
            or _identity(directory.info(name)) != _identity(opened)
        ):
            raise OSError(_UNSAFE)
        directory.check()
        budget.files += 1
        budget.bytes_read += len(payload)
        return payload
    finally:
        os.close(fd)


def _local_ignores(
    directory: _Directory,
    relative: str,
    rules: ProtectedPathRules,
    budget: _Budget,
    inherited: list[tuple[str, GitIgnoreSpec]],
) -> list[tuple[str, GitIgnoreSpec]]:
    specs = list(inherited)
    for name in (".gitignore", ".ignore"):
        if rules.denies(directory.path / name):
            continue
        try:
            payload = _read_payload(directory, name, rules, budget)
        except FileNotFoundError:
            continue
        if payload is None or len(payload) > 65_536:
            raise OSError("protected search ignore rules are unavailable or oversized")
        specs.append((relative, GitIgnoreSpec.from_lines(payload.decode("utf-8", errors="strict").splitlines())))
    return specs


def _ignored(relative: str, specs: list[tuple[str, GitIgnoreSpec]]) -> bool:
    ignored = False
    for base, spec in specs:
        result = spec.check_file(relative[len(base) :], separators=("/",))
        if result.include is not None:
            ignored = result.include
    return ignored


def _snapshots(
    directory: _Directory,
    rules: ProtectedPathRules,
    request: dict[str, Any],
    budget: _Budget,
    selected: Any,
    *,
    relative: str = "",
    depth: int = 0,
    ignores: list[tuple[str, GitIgnoreSpec]] | None = None,
) -> Iterator[tuple[Path, bytes]]:
    specs = _local_ignores(directory, relative, rules, budget, ignores or [])
    positive_glob = bool(request["glob"]) and not request["glob"].startswith("!")
    with os.scandir(directory.path if directory.fd is None else directory.fd) as entries:
        for entry in entries:
            budget.entries += 1
            if budget.entries > MAX_ENTRIES:
                budget.incomplete = True
                raise _ScanLimit
            name = entry.name
            path = directory.path / name
            if rules.denies(path):
                continue
            info = directory.info(name)
            if config._path_is_reparse_point(path, info):
                continue
            key = relative + name
            if stat.S_ISDIR(info.st_mode):
                if name in request["skip_dirs"] or (_ignored(key + "/", specs) and not positive_glob):
                    continue
                if depth >= MAX_DEPTH:
                    budget.incomplete = True
                    continue
                with _bound_directory(path, rules, directory) as child:
                    yield from _snapshots(
                        child,
                        rules,
                        request,
                        budget,
                        selected,
                        relative=key + "/",
                        depth=depth + 1,
                        ignores=specs,
                    )
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if selected is not None and not selected.match(key):
                    continue
                if _ignored(key, specs) and not positive_glob:
                    continue
                payload = _read_payload(directory, name, rules, budget)
                if payload is not None and b"\x00" not in payload:
                    yield path, payload


def _matches(snapshots: list[tuple[Path, bytes]], request: dict[str, Any]) -> tuple[bytes, bool]:
    capture = _Capture(request["max_output_bytes"], request["limit"])
    if request["rg"]:
        segments: list[tuple[int, Path]] = []
        pieces: list[bytes] = []
        next_line = 1
        for path, payload in snapshots:
            if not payload:
                continue
            segments.append((next_line, path))
            piece = payload if payload.endswith(b"\n") else payload + b"\n"
            next_line += piece.count(b"\n")
            pieces.append(piece)
        result = run_search_process(
            [
                request["rg"],
                "--no-config",
                "--line-number",
                "--no-filename",
                "--no-heading",
                "--line-buffered",
                "--color=never",
                "--text",
                "--encoding=none",
                "--",
                request["pattern"],
                "-",
            ],
            limit=request["limit"] + 1,
            max_bytes=request["max_output_bytes"],
            timeout=request["timeout"],
            process_kwargs={},
            terminate=lambda proc: proc.kill(),
            input_bytes=b"".join(pieces),
        )
        if result.returncode == 2:
            raise _PatternError("search pattern was rejected by ripgrep")
        if result.timed_out or result.stderr_truncated or result.returncode > 1:
            raise OSError("protected search matcher did not complete")
        if not result.truncated and result.returncode not in {0, 1}:
            raise OSError("protected search matcher did not complete")
        starts = [start for start, _path in segments]
        for raw in result.stdout.split(b"\n"):
            if not raw:
                continue
            number, separator, line = raw.partition(b":")
            if not separator or not number.isdigit():
                if result.truncated:
                    break
                raise OSError("protected search matcher returned an invalid line")
            global_line = int(number)
            index = bisect_right(starts, global_line) - 1
            if index < 0 or global_line >= next_line:
                raise OSError("protected search matcher returned an invalid line")
            start, path = segments[index]
            capture.append(f"{path}:{global_line - start + 1}:".encode("utf-8") + line + b"\n")
            if capture.truncated:
                break
        return bytes(capture.data), capture.truncated or result.truncated
    try:
        pattern = re.compile(request["pattern"])
    except re.error as exc:
        raise _PatternError("search pattern is invalid for the Python fallback") from exc
    for path, payload in snapshots:
        for line_number, text_line in enumerate(payload.decode("utf-8", errors="ignore").splitlines(), 1):
            if pattern.search(text_line):
                capture.append(f"{path}:{line_number}:{text_line}\n".encode("utf-8"))
                if capture.truncated:
                    return bytes(capture.data), True
    return bytes(capture.data), False


def _search_snapshot(request: dict[str, Any]) -> bytes:
    rules = ProtectedPathRules(
        tuple(request["rules"]["roots"]),
        request["rules"]["residue_parent"],
        tuple(request["rules"]["residue_prefixes"]),
        tuple(tuple(identity) for identity in request["rules"]["identities"]),
    )
    root = Path(request["path"])
    if rules.denies(root) or _identity(root.lstat()) != tuple(request["root_identity"]):
        raise OSError(_UNSAFE)
    flags = wcglob.BRACE | wcglob.GLOBSTAR | wcglob.MATCHBASE | wcglob.DOTMATCH
    flags |= wcglob.NEGATE | wcglob.NEGATEALL | wcglob.CASE | wcglob.FORCEUNIX
    try:
        selected = wcglob.compile(request["glob"], flags=flags, limit=256) if request["glob"] else None
    except Exception as exc:
        raise _PatternError("search glob is invalid or exceeds its expansion budget") from exc
    budget = _Budget(request["max_files"], request["max_file_bytes"])
    is_dir = stat.S_ISDIR(root.lstat().st_mode)
    with _bound_directory(root if is_dir else root.parent, rules) as directory:
        if _identity(root.lstat()) != tuple(request["root_identity"]):
            raise OSError(_UNSAFE)
        snapshots: list[tuple[Path, bytes]] = []
        try:
            if is_dir:
                snapshots.extend(_snapshots(directory, rules, request, budget, selected))
            elif selected is None or selected.match(root.name):
                payload = _read_payload(directory, root.name, rules, budget)
                if payload is not None and b"\x00" not in payload:
                    snapshots.append((root, payload))
        except _ScanLimit:
            pass
        output, truncated = _matches(snapshots, request)
    if budget.incomplete:
        output += b"...[incomplete: protected search scan budget reached; narrow the path]\n"
    if truncated:
        output += b"...[truncated: narrow the path or pattern for remaining matches]\n"
    return output


def protected_search(pattern: str, path: str, cwd: str, glob: str | None, limit: int) -> str:
    from . import tools

    try:
        root = require_allowed_path(path, cwd=cwd)
        rules = protected_path_rules()
        if rules.denies(root):
            raise ProtectedMemoryPathError(_UNSAFE)
        info = root.lstat()
        request = {
            "path": str(root),
            "root_identity": _identity(info),
            "rules": asdict(rules),
            "pattern": pattern,
            "glob": glob,
            "limit": limit,
            "max_files": tools.SEARCH_FALLBACK_MAX_FILES,
            "max_file_bytes": tools.SEARCH_FALLBACK_MAX_FILE_BYTES,
            "max_output_bytes": tools.MAX_TOOL_RESULT - 512,
            "skip_dirs": sorted(tools.SEARCH_FALLBACK_SKIP_DIRS),
            "timeout": tools.SEARCH_TIMEOUT_SECONDS,
            "rg": None if stat.S_ISREG(info.st_mode) else shutil.which("rg"),
            "runtime_root": str(Path(__file__).parent.resolve()),
        }
        payload = json.dumps(request).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise ProtectedMemoryPathError(_UNSAFE)
        result = run_search_process(
            [sys.executable, "-I", "-m", "algo_cli.irene_search"],
            limit=limit,
            max_bytes=tools.MAX_TOOL_RESULT - 256,
            timeout=tools.SEARCH_TIMEOUT_SECONDS,
            process_kwargs=tools._isolated_process_group_kwargs(),
            terminate=tools._terminate_process_tree,
            input_bytes=payload,
        )
        if rules != protected_path_rules():
            raise ProtectedMemoryPathError(_UNSAFE)
    except (OSError, ValueError, ProtectedMemoryPathError):
        return f"Error: {_UNSAFE}; no search results were released."
    if result.timed_out:
        return f"Error: protected search timed out after {tools.SEARCH_TIMEOUT_SECONDS:g} seconds; no results released."
    if result.stderr_truncated or result.returncode > 1 or (not result.truncated and result.returncode not in {0, 1}):
        return tools._bounded_search_text(
            "Error: " + (result.stderr.decode("utf-8", errors="replace").strip() or _UNSAFE),
        )
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    output = "\n".join(lines[:limit])
    return tools._bounded_search_text(output or "No matches.", truncated=result.truncated or len(lines) > limit)


def _main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError(_UNSAFE)
        request = json.loads(raw)
        if request["runtime_root"] != str(Path(__file__).parent.resolve()):
            raise ValueError(_UNSAFE)
        output = _search_snapshot(request)
        sys.stdout.buffer.write(output)
        return 0 if output else 1
    except _PatternError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print(_UNSAFE, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
