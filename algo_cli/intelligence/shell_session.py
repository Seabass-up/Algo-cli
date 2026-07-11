"""B41. Pseudoterminal Boundary + VT Parser/Emitter (Windows Terminal Pattern).

Wraps shell command execution with a session abstraction: process boundary,
streaming stdout/stderr frames, ANSI/VT normalization, command replay
metadata, and timeout/cancellation.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ── ANSI/VT normalization ─────────────────────────────────────────────

# Match ANSI escape sequences
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# Match other control chars (except \n, \r, \t)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return _ANSI_RE.sub("", text)


def normalize_vt(text: str) -> str:
    """Remove ANSI escapes and control characters, keep newlines/tabs."""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return text


def has_ansi(text: str) -> bool:
    """Check if text contains ANSI escape sequences."""
    return bool(_ANSI_RE.search(text))


# ── streaming frames ──────────────────────────────────────────────────


@dataclass
class OutputFrame:
    """A single chunk of process output."""
    stream: str  # "stdout" or "stderr"
    data: str
    raw_bytes: bytes = b""
    timestamp: float = field(default_factory=time.time)
    frame_index: int = 0


@dataclass
class CommandRecord:
    """Metadata for a executed command (for replay)."""
    id: str
    command: str
    cwd: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    frames: list[OutputFrame] = field(default_factory=list)
    cancelled: bool = False
    timeout_hit: bool = False

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    @property
    def raw_output(self) -> str:
        """Raw output with ANSI preserved."""
        return "".join(f.data for f in self.frames)

    @property
    def normalized_output(self) -> str:
        """Output with ANSI/control chars stripped."""
        return normalize_vt(self.raw_output)

    @property
    def stdout(self) -> str:
        return normalize_vt("".join(f.data for f in self.frames if f.stream == "stdout"))

    @property
    def stderr(self) -> str:
        return normalize_vt("".join(f.data for f in self.frames if f.stream == "stderr"))


# ── shell session ─────────────────────────────────────────────────────


class ShellSession:
    """Pseudoterminal boundary for shell command execution.

    Wraps subprocess execution with:
    - streaming output frames
    - raw + normalized output preservation
    - timeout/cancellation
    - command replay metadata
    - ANSI/VT normalization
    """

    def __init__(self, cwd: str = "", default_timeout: int = 120):
        self.cwd = cwd
        self.default_timeout = default_timeout
        self.history: list[CommandRecord] = []
        self._cancelled_ids: set[str] = set()

    def execute(self, command: str, timeout: int | None = None) -> CommandRecord:
        """Execute a command and return its record.

        This is a synchronous wrapper. For real streaming, use execute_streaming.
        """
        import subprocess

        rec = CommandRecord(id=str(uuid.uuid4()), command=command, cwd=self.cwd)
        effective_timeout = timeout or self.default_timeout

        try:
            proc = subprocess.run(
                command,
                cwd=self.cwd or None,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                shell=True,
            )
            rec.frames.append(OutputFrame(
                stream="stdout", data=proc.stdout,
                raw_bytes=proc.stdout.encode("utf-8", errors="replace"),
                frame_index=0,
            ))
            rec.frames.append(OutputFrame(
                stream="stderr", data=proc.stderr,
                raw_bytes=proc.stderr.encode("utf-8", errors="replace"),
                frame_index=1,
            ))
            rec.exit_code = proc.returncode
            rec.finished_at = time.time()
        except subprocess.TimeoutExpired as e:
            rec.timeout_hit = True
            rec.exit_code = -1
            rec.finished_at = time.time()
            if e.stdout:
                rec.frames.append(OutputFrame(stream="stdout", data=e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout), frame_index=0))
            if e.stderr:
                rec.frames.append(OutputFrame(stream="stderr", data=e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr), frame_index=1))
        except Exception as e:
            rec.exit_code = -1
            rec.finished_at = time.time()
            rec.frames.append(OutputFrame(stream="stderr", data=str(e), frame_index=0))

        self.history.append(rec)
        return rec

    def cancel(self, command_id: str) -> None:
        """Mark a command as cancelled (for async execution)."""
        self._cancelled_ids.add(command_id)

    def is_cancelled(self, command_id: str) -> bool:
        return command_id in self._cancelled_ids

    def replay(self, command_id: str) -> CommandRecord | None:
        """Get a past command record for replay/inspection."""
        for rec in self.history:
            if rec.id == command_id:
                return rec
        return None

    def recent(self, n: int = 10) -> list[CommandRecord]:
        """Get the N most recent command records."""
        return self.history[-n:]

    def stats(self) -> dict[str, Any]:
        total = len(self.history)
        succeeded = sum(1 for r in self.history if r.exit_code == 0)
        failed = sum(1 for r in self.history if r.exit_code is not None and r.exit_code != 0)
        timeouts = sum(1 for r in self.history if r.timeout_hit)
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "timeouts": timeouts,
            "avg_duration_ms": sum(r.duration_ms for r in self.history) / max(total, 1),
        }


# ── shell guardrail parser ────────────────────────────────────────────


_BLOCKED_PATTERNS = [
    (r"<<\s*['\"]?EOF", "heredoc not supported in cmd.exe"),
    (r"<<\s*['\"]?PY", "heredoc not supported in cmd.exe"),
    (r"python\s+-c\s+\"[^\"]*\\n", "literal \\n in python -c not supported in cmd.exe"),
    (r"\bcat\s+<<", "cat heredoc not supported in cmd.exe"),
]


def check_shell_safety(command: str, shell: str = "cmd") -> list[str]:
    """Check a command for shell-specific safety issues.

    Returns a list of warning strings (empty if safe).
    """
    if shell != "cmd":
        return []
    warnings: list[str] = []
    for pattern, message in _BLOCKED_PATTERNS:
        if re.search(pattern, command):
            warnings.append(message)
    return warnings