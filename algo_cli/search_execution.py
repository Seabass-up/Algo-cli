"""Bounded search process I/O and the isolated standard-library matcher."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable


@dataclass
class _Capture:
    max_bytes: int
    max_lines: int | None = None
    data: bytearray = field(default_factory=bytearray)
    lines: int = 0
    truncated: bool = False
    error: Exception | None = None
    # A draining capture keeps reading (and discarding) past its cap so a noisy stderr, such as rg
    # permission errors, neither blocks nor ends the search; drain_limit still stops an unbounded writer.
    drain: bool = False
    drain_limit: int = 1_048_576
    discarded: int = 0

    @property
    def stopped(self) -> bool:
        return self.truncated and (not self.drain or self.discarded > self.drain_limit)

    def append(self, chunk: bytes) -> None:
        allowed = min(len(chunk), self.max_bytes - len(self.data))
        if self.max_lines is not None:
            position = 0
            remaining = self.max_lines - self.lines
            while position < allowed:
                if remaining == 0:
                    allowed = position
                    break
                newline = chunk.find(b"\n", position, allowed)
                if newline < 0:
                    break
                remaining -= 1
                position = newline + 1
        self.discarded += len(chunk) - allowed
        selected = chunk[:allowed]
        self.data.extend(selected)
        self.lines += selected.count(b"\n")
        self.truncated = self.truncated or allowed < len(chunk)


@dataclass(frozen=True)
class SearchProcessResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    truncated: bool
    stderr_truncated: bool
    timed_out: bool


def _capture_pipe(pipe: BinaryIO, capture: _Capture, finished: queue.Queue[_Capture]) -> None:
    try:
        while not capture.stopped:
            chunk = pipe.read1(4096)  # type: ignore[attr-defined]
            if not chunk:
                break
            capture.append(chunk)
    except Exception as exc:
        capture.error = exc
    finally:
        finished.put(capture)


def _write_input(pipe: BinaryIO, payload: bytes, status: _Capture, finished: queue.Queue[_Capture]) -> None:
    try:
        for start in range(0, len(payload), 65_536):
            pipe.write(payload[start : start + 65_536])
        pipe.flush()
    except BrokenPipeError:
        pass
    except Exception as exc:
        status.error = exc
    finally:
        try:
            pipe.close()
        except BrokenPipeError:
            pass
        except Exception as exc:
            status.error = exc
        finished.put(status)


def run_search_process(
    command: list[str],
    *,
    limit: int,
    max_bytes: int,
    timeout: float,
    process_kwargs: dict[str, Any],
    terminate: Callable[[subprocess.Popen[Any]], None],
    input_bytes: bytes | None = None,
) -> SearchProcessResult:
    """Stop at a global byte/line cap without first buffering the entire stream."""
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **process_kwargs,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout = _Capture(max_bytes, limit)
    stderr = _Capture(min(max_bytes, 4096), drain=True)
    finished: queue.Queue[_Capture] = queue.Queue()
    readers = [
        threading.Thread(target=_capture_pipe, args=(pipe, capture, finished), daemon=True)
        for pipe, capture in ((proc.stdout, stdout), (proc.stderr, stderr))
    ]
    input_status = _Capture(0)
    if input_bytes is not None:
        assert proc.stdin is not None
        readers.append(
            threading.Thread(
                target=_write_input,
                args=(proc.stdin, input_bytes, input_status, finished),
                daemon=True,
            )
        )
    timed_out = False
    try:
        for reader in readers:
            reader.start()
        for _ in readers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                captured = finished.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if captured.stopped or captured.error is not None:
                break
        else:
            try:
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        # This also runs for Ctrl+C, a reader error, and a process wait failure.
        if proc.poll() is None or any(reader.is_alive() for reader in readers):
            terminate(proc)
        try:
            proc.wait(timeout=5)
        finally:
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
    if any(reader.is_alive() for reader in readers):
        raise OSError("search output readers did not stop")
    if stdout.error is not None or stderr.error is not None or input_status.error is not None:
        raise OSError("search output could not be read safely")
    return SearchProcessResult(
        bytes(stdout.data),
        bytes(stderr.data),
        proc.returncode,
        stdout.truncated,
        stderr.truncated,
        timed_out,
    )


def _glob_regex(body: str) -> str:
    out: list[str] = []
    index = 0
    in_braces = False
    while index < len(body):
        char = body[index]
        if body.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if body.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = body.find("]", index + 2 if body[index + 1 : index + 2] in {"!", "^", "]"} else index + 1)
            if end < 0:
                out.append(re.escape(char))
            else:
                inner = body[index + 1 : end].replace("\\", "\\\\")
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append(f"[{inner}]")
                index = end
        elif char == "{" and not in_braces and "}" in body[index:]:
            in_braces = True
            out.append("(?:")
        elif char == "}" and in_braces:
            in_braces = False
            out.append(")")
        elif char == "," and in_braces:
            out.append("|")
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def glob_matcher(glob: str | None) -> Callable[[str], bool]:
    """Approximate ripgrep --glob semantics for the fallback: '/'-free globs match the
    file name, other globs match the root-relative path, and a leading '!' excludes."""
    if not glob:
        return lambda _relative: True
    negate = glob.startswith("!")
    body = glob[1:] if negate else glob
    anchored = "/" in body.rstrip("/")
    # Windows keeps the case-insensitive matching the earlier fnmatch-based fallback gave it.
    flags = re.DOTALL | (re.IGNORECASE if os.name == "nt" else 0)
    compiled = re.compile(_glob_regex(body.lstrip("/").rstrip("/")) + r"\Z", flags)

    def matches(relative: str) -> bool:
        target = relative if anchored else relative.rsplit("/", 1)[-1]
        return bool(compiled.match(target)) != negate

    return matches


def _python_search(request: dict[str, Any]) -> int:
    """Run only in the child so a backtracking regex cannot hang the harness."""
    root = Path(request["path"])
    pattern = re.compile(request["pattern"])
    glob = request["glob"]
    max_files = request["max_files"]
    max_file_bytes = request["max_file_bytes"]
    skip_dirs = set(request["skip_dirs"])
    matches_glob = glob_matcher(glob)
    scanned = 0
    found = 0

    def search_one(path: Path) -> None:
        nonlocal scanned, found
        relative = path.name if path == root else path.relative_to(root).as_posix()
        if not matches_glob(relative):
            return
        try:
            if path.stat().st_size > max_file_bytes:
                return
            scanned += 1
            with path.open("rb") as handle:
                payload = handle.read(max_file_bytes + 1)
            if len(payload) > max_file_bytes:
                return
        except OSError:
            return
        for line_number, line in enumerate(payload.decode("utf-8", errors="ignore").splitlines(), 1):
            if pattern.search(line):
                print(f"{path}:{line_number}:{line}", flush=True)
                found += 1
                if found > request["limit"]:
                    return

    if root.is_file():
        search_one(root)
    else:
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in skip_dirs]
            for filename in files:
                if scanned >= max_files:
                    print(f"...[truncated: stopped after scanning {max_files} files]", flush=True)
                    return 0
                search_one(Path(current) / filename)
                if found > request["limit"]:
                    return 0
    return 0 if found else 1


if __name__ == "__main__":
    getattr(sys.stdout, "reconfigure")(encoding="utf-8", errors="replace", newline="\n")
    getattr(sys.stderr, "reconfigure")(encoding="utf-8", errors="replace", newline="\n")
    try:
        raise SystemExit(_python_search(json.loads(sys.argv[1])))
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2) from None
