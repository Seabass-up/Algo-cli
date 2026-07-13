"""Bounded Git evidence capture for agent pipeline verification."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


MAX_STATUS_LINES = 40
MAX_TRACKED_DIFF_CHARS = 8_000
MAX_UNTRACKED_FILES = 30
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class GitSnapshot:
    """Immutable Git state with bounded display output and full-state digests."""

    available: bool
    error: str | None
    head: str | None
    status: str
    tracked_diff: str
    untracked_files: tuple[str, ...]
    tracked_diff_digest: str = ""
    untracked_digest: str = ""
    untracked_total: int = 0
    status_digest: str = ""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _untracked_state_digest(cwd: str | None, paths: tuple[str, ...]) -> str:
    """Hash untracked names and contents so edits to an existing file are visible."""

    if not paths:
        return _digest("")
    root = Path(cwd or ".").expanduser().resolve()
    digest = hashlib.sha256()
    for relative in sorted(paths):
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        candidate = (root / relative).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            digest.update(b"outside-workspace\0")
            continue
        try:
            if candidate.is_symlink():
                digest.update(b"symlink\0")
                digest.update(candidate.readlink().as_posix().encode("utf-8", errors="surrogateescape"))
            elif candidate.is_file():
                digest.update(b"file\0")
                with candidate.open("rb") as handle:
                    while chunk := handle.read(_HASH_CHUNK_BYTES):
                        digest.update(chunk)
            else:
                digest.update(b"missing-or-special\0")
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}".encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _run_git(args: list[str], cwd: str | None = None, timeout: int = 20) -> tuple[int, str]:
    workdir = Path(cwd or ".").expanduser().resolve()
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"git command timed out after {timeout} seconds"
    except Exception as exc:
        return 1, f"git command failed: {exc}"
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode, output


def _cap_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + f"\n... ({len(lines) - limit} more lines omitted)"


def _cap_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters omitted)"


def capture_git_snapshot(cwd: str | None = None) -> GitSnapshot:
    """Capture repository state without allowing display caps to hide changes."""

    rc, in_tree = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    if rc != 0 or in_tree.lower() != "true":
        return GitSnapshot(
            available=False,
            error=in_tree or "not a Git repository",
            head=None,
            status="",
            tracked_diff="",
            untracked_files=(),
        )

    head_rc, head_output = _run_git(["rev-parse", "--verify", "HEAD"], cwd)
    head = head_output if head_rc == 0 else None

    status_rc, full_status = _run_git(["status", "--short", "--branch"], cwd)
    diff_rc, full_diff = _run_git(["diff", "--no-ext-diff", "HEAD"], cwd)
    untracked_rc, untracked_output = _run_git(["ls-files", "--others", "--exclude-standard"], cwd)
    if status_rc != 0 or diff_rc != 0 or untracked_rc != 0:
        error = full_status if status_rc != 0 else full_diff if diff_rc != 0 else untracked_output
        return GitSnapshot(
            available=False,
            error=error or "unable to capture Git state",
            head=head,
            status="",
            tracked_diff="",
            untracked_files=(),
        )

    all_untracked = tuple(line.strip() for line in untracked_output.splitlines() if line.strip())
    return GitSnapshot(
        available=True,
        error=None,
        head=head,
        status=_cap_lines(full_status, MAX_STATUS_LINES),
        tracked_diff=_cap_chars(full_diff, MAX_TRACKED_DIFF_CHARS),
        untracked_files=all_untracked[:MAX_UNTRACKED_FILES],
        tracked_diff_digest=_digest(full_diff),
        untracked_digest=_untracked_state_digest(cwd, all_untracked),
        untracked_total=len(all_untracked),
        status_digest=_digest(full_status),
    )


def _state_changed(before: GitSnapshot, after: GitSnapshot) -> bool:
    return (
        before.status_digest != after.status_digest
        or before.tracked_diff_digest != after.tracked_diff_digest
        or before.untracked_digest != after.untracked_digest
    )


def _status_is_clean(status: str) -> bool:
    """Return whether bounded short status contains only its branch header."""

    lines = [line for line in status.splitlines() if line.strip()]
    if lines and lines[0].startswith("## "):
        lines = lines[1:]
    return not lines


def _baseline_is_clean(snapshot: GitSnapshot) -> bool:
    return (
        _status_is_clean(snapshot.status)
        and snapshot.tracked_diff_digest == _digest("")
        and snapshot.untracked_digest == _digest("")
    )


def snapshot_is_clean(snapshot: GitSnapshot) -> bool:
    """Return whether an available snapshot has no tracked or untracked changes."""

    return snapshot.available and _baseline_is_clean(snapshot)


def _format_untracked(snapshot: GitSnapshot) -> str:
    if not snapshot.untracked_files:
        return "(none)"
    shown = "\n".join(snapshot.untracked_files)
    omitted = snapshot.untracked_total - len(snapshot.untracked_files)
    return shown if omitted <= 0 else f"{shown}\n... ({omitted} more files omitted)"


def format_git_evidence(before: GitSnapshot, after: GitSnapshot) -> str:
    """Format evidence conservatively for review/final block context."""

    if not before.available or not after.available:
        return f"Git evidence unavailable.\nReason: {after.error or before.error or 'unknown error'}"

    if before.head != after.head:
        return (
            "ATTRIBUTION UNSAFE: Repository HEAD changed during block execution.\n"
            f"Before HEAD: {before.head or '(none)'}\nAfter HEAD: {after.head or '(none)'}\n"
            "Working-tree evidence cannot be reliably attributed to this block."
        )

    if not _state_changed(before, after):
        if _baseline_is_clean(before):
            return "No Git changes were introduced during this block (clean baseline maintained)."
        return "No Git delta was introduced during this block (pre-existing dirty state unchanged)."

    details = (
        f"Working tree status after execution:\n{after.status or '(clean)'}\n\n"
        f"Tracked diff after execution:\n{after.tracked_diff or '(none)'}\n\n"
        f"Untracked files after execution:\n{_format_untracked(after)}"
    )
    if _baseline_is_clean(before):
        return f"Verified Git state change introduced during this block.\n\n{details}"
    return (
        "Git state changed during this block on a previously dirty working tree.\n"
        "Attribution is conservative: verify changed sections before claiming implementation.\n\n"
        f"{details}"
    )


def has_verified_delta(before: GitSnapshot, after: GitSnapshot) -> bool:
    """Return whether a clean-baseline delta can be attributed to the block."""

    return (
        before.available
        and after.available
        and before.head == after.head
        and _baseline_is_clean(before)
        and _state_changed(before, after)
    )


def has_observed_delta(before: GitSnapshot, after: GitSnapshot) -> bool:
    """Return whether repository state changed without crossing a HEAD boundary."""

    return (
        before.available
        and after.available
        and before.head == after.head
        and _state_changed(before, after)
    )
