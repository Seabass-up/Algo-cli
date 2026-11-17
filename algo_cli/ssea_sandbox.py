"""Closed macOS execution boundary for pinned, target-free REA static inspectors.

No shell, inherited credentials, network, child processes, or host target reads
are admitted to the child. Unsupported hosts and runtime drift fail closed.
"""
from contextlib import contextmanager
from collections.abc import Iterator
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time
from typing import Any, BinaryIO

from .irene_memory_path_policy import ProtectedPathRules, protected_path_rules, require_allowed_path
from .search_execution import run_search_process

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 65_536
MANIFEST = Path(__file__).parent / "resources" / "ssea-runtime.json"


class BoundaryError(RuntimeError):
    """Content-free boundary rejection."""


@contextmanager
def regular_reader(path: Path, rules: ProtectedPathRules | None = None) -> Iterator[BinaryIO]:
    """Open trusted runtime/metadata files with the same nofollow discipline."""
    rules = rules or protected_path_rules()
    path = require_allowed_path(path, cwd=path.parent, rules=rules)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
            if rules.denies_identity(os.fstat(descriptor)):
                raise BoundaryError("ssea_protected_runtime_path")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or rules.denies_identity(info):
                raise BoundaryError("ssea_runtime_not_regular_unaliased_file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(fd)
    finally:
        os.close(descriptor)


def file_digest(path: Path, rules: ProtectedPathRules | None = None) -> str:
    digest = hashlib.sha256()
    with regular_reader(path, rules) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > 128 * 1024 * 1024:
            raise BoundaryError("ssea_runtime_file_too_large")
        consumed = 0
        while chunk := stream.read(1024 * 1024):
            consumed += len(chunk)
            if consumed > 128 * 1024 * 1024:
                raise BoundaryError("ssea_runtime_file_too_large")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise BoundaryError("ssea_runtime_file_changed")
    return digest.hexdigest()


def _cancelled(cfg: Any) -> bool:
    event = getattr(cfg, "_algo_team_cancellation", None)
    return event is not None and event.is_set()


def installation(cfg: Any = None) -> tuple[Path, Path]:
    root = Path(getattr(cfg, "ssea_install_root", "") or
                Path.home() / ".local/share/rea/lib/node_modules/rea-agents").expanduser()
    node = Path(getattr(cfg, "ssea_node_path", "") or
                Path.home() / ".local/share/rea/node-v24.22.2-darwin-arm64/bin/node").expanduser()
    rules = protected_path_rules()
    root = require_allowed_path(root, cwd=os.getcwd(), rules=rules)
    node = require_allowed_path(node, cwd=os.getcwd(), rules=rules)
    if not root.is_absolute() or not node.is_absolute() or not root.is_dir() or not node.is_file():
        raise BoundaryError("ssea_runtime_missing")
    return root, node


def verify_runtime(cfg: Any, deadline: float) -> tuple[Path, Path]:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise BoundaryError("ssea_containment_unavailable")
    root, node = installation(cfg)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rules = protected_path_rules()
    if file_digest(node, rules) != manifest["node_sha256"]:
        raise BoundaryError("ssea_node_integrity_mismatch")
    suffixes = {".js", ".mjs", ".cjs", ".json", ".node"}
    observed = sorted(p for p in root.rglob("*")
                      if p.is_file() and p.suffix in suffixes and ".bin" not in p.relative_to(root).parts)
    tree = hashlib.sha256()
    for index, path in enumerate(observed):
        if index % 32 == 0:
            if _cancelled(cfg):
                raise BoundaryError("ssea_cancelled")
            if time.monotonic() >= deadline:
                raise BoundaryError("ssea_deadline_exhausted")
        relative = path.relative_to(root).as_posix()
        tree.update(relative.encode("utf-8") + b"\0")
        tree.update(bytes.fromhex(file_digest(path, rules)))
    if len(observed) != manifest["file_count"] or tree.hexdigest() != manifest["tree_sha256"]:
        raise BoundaryError("ssea_runtime_integrity_mismatch")
    return root, node


def snapshot(target: str, destination: Path, cwd: str) -> dict[str, Any]:
    """Validate then open every ancestor without following a replacement link."""
    rules = protected_path_rules()
    path = require_allowed_path(target, cwd=cwd, rules=rules)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
            if rules.denies_identity(os.fstat(descriptor)):
                raise BoundaryError("ssea_protected_target")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or rules.denies_identity(before):
                raise BoundaryError("ssea_target_not_regular_unaliased_file")
            if before.st_size > MAX_INPUT:
                raise BoundaryError("ssea_input_too_large")
            with os.fdopen(os.dup(fd), "rb") as source:
                content = source.read(MAX_INPUT + 1)
            after = os.fstat(fd)
            if (len(content) > MAX_INPUT or len(content) != before.st_size or
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise BoundaryError("ssea_target_changed")
        finally:
            os.close(fd)
    finally:
        os.close(descriptor)
    destination.write_bytes(content)
    destination.chmod(0o400)
    return {"target": str(path), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def profile(root: Path, node: Path, scratch: Path) -> str:
    """Default-deny Seatbelt policy. No network, mach-lookup or process-fork grant."""
    rules = protected_path_rules()
    for path in (root, node, scratch):
        require_allowed_path(path, cwd=scratch, rules=rules)
    # Never grant /System wholesale: /System/Volumes/Data aliases the home tree.
    reads = [root, node.parent, scratch, Path("/System/Library"), Path("/usr/lib"),
             Path("/System/Volumes/Preboot/Cryptexes/OS"), Path("/System/Cryptexes/OS")]
    def literal(p: Any) -> str:
        return json.dumps(str(p), ensure_ascii=True)
    ancestors = sorted({str(p) for path in (root, node, scratch) for p in path.parents if str(p) != "/"})
    result = ["(version 1)", "(deny default)",
              f"(allow process-exec (literal {literal(node)}))",
              "(allow sysctl-read)",
              '(allow file-read* (literal "/") (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))',
              "(allow file-read* " + " ".join(f"(subpath {literal(p)})" for p in reads) + ")",
              "(allow file-read-metadata " + " ".join(f"(literal {literal(p)})" for p in ancestors) + ")",
              f"(allow file-write* (subpath {literal(scratch)}))",
              f"(deny file-write* (literal {literal(scratch / 'target.bin')}))"]
    # Explicit denies take precedence even if a user-defined memory root lies
    # below a normally readable runtime/system path. No protected-store content
    # is supplied to the child, including for this deny configuration.
    for protected in rules.roots:
        result.append(f"(deny file-read* file-write* (subpath {literal(protected)}))")
    return "\n".join(result) + "\n"


def terminate(proc: Any) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run(root: Path, node: Path, scratch: Path, arguments: list[str], cfg: Any,
        deadline: float, *, script: str | None = None) -> Any:
    policy = scratch / "containment.sb"
    policy.write_text(profile(root, node, scratch), encoding="utf-8")
    command = ["/usr/bin/sandbox-exec", "-f", str(policy), str(node), "--max-old-space-size=256", "--no-addons"]
    command += ["-e", script] if script is not None else [str(root / "scripts/rea.mjs"), *arguments]
    remaining = deadline - time.monotonic()
    if remaining <= 0 or _cancelled(cfg):
        raise BoundaryError("ssea_cancelled_or_deadline_exhausted")
    return run_search_process(
        command, limit=100_000, max_bytes=MAX_OUTPUT, timeout=remaining,
        process_kwargs={"cwd": str(scratch), "env": {
            "HOME": str(scratch), "TMPDIR": str(scratch), "XDG_CACHE_HOME": str(scratch / "cache"),
            "PATH": "/usr/bin:/bin", "LANG": "C", "REA_LOG_LEVEL": "error",
        }, "start_new_session": True, "shell": False, "close_fds": True},
        terminate=terminate, cancel_event=getattr(cfg, "_algo_team_cancellation", None),
    )
