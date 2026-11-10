"""An interrupted ShellSession command must not keep running.

The shell runs in its own session so a timeout can kill its children. That also
means Ctrl-C at the terminal never reaches it, so the interrupt path has to kill
the process group itself before the interrupt propagates.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from algo_cli.intelligence.shell_session import ShellSession

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_keyboard_interrupt_kills_the_shell_and_its_children(monkeypatch, tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    started: list[subprocess.Popen] = []

    class InterruptedOnce(subprocess.Popen):
        def communicate(self, *args, **kwargs):
            if not started:
                started.append(self)
                # Interrupt only once the background child exists, as a real Ctrl-C would.
                assert _wait_until(lambda: child_pid_file.exists() and child_pid_file.read_text().strip())
                raise KeyboardInterrupt
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", InterruptedOnce)
    session = ShellSession(cwd=str(tmp_path))

    with pytest.raises(KeyboardInterrupt):
        session.execute(f"sleep 30 & echo $! > {child_pid_file.name}; wait", timeout=60)

    shell = started[0]
    child_pid = int(child_pid_file.read_text().strip())
    try:
        assert _wait_until(lambda: shell.poll() is not None), "the shell is still running after the interrupt"
        assert _wait_until(lambda: not _alive(child_pid)), "the shell's child is still running after the interrupt"
    finally:
        for pid in (shell.pid, child_pid):
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass


def test_timeout_still_kills_the_shell_and_its_children(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    session = ShellSession(cwd=str(tmp_path))

    record = session.execute(f"sleep 30 & echo $! > {child_pid_file.name}; wait", timeout=1)

    assert record.timeout_hit is True
    child_pid = int(child_pid_file.read_text().strip())
    try:
        assert _wait_until(lambda: not _alive(child_pid)), "the shell's child survived the timeout"
    finally:
        try:
            os.kill(child_pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
