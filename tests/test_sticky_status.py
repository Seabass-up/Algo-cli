"""Sticky generation status bar helpers."""

from __future__ import annotations

from io import StringIO
import os

import pytest

import algo_cli.sticky_status as sticky_status
from algo_cli import main
from algo_cli.config import Config


def test_format_status_toolbar_plain_matches_key_chips() -> None:
    cfg = Config()
    main.RUNTIME_STATUS.clear()
    main.RUNTIME_STATUS.update(
        {
            "model": "deepseek-v4.1-flash:cloud",
            "mode": "local",
            "context_used": 4800,
            "context_total": 1_000_000,
            "context_native": 2_000_000,
            "context_runtime_cap": 1_000_000,
            "context_pct_left": 99,
            "max_tool_iterations": 128,
            "tool_think_every": 50,
            "safe_mode": False,
            "auto_mode": True,
        }
    )
    line = main.format_status_toolbar_plain(cfg)
    assert "deepseek-v4.1-flash:cloud" in line
    assert "local" in line
    assert "▣" in line
    assert "4.8k/1M" in line
    assert "99%" in line
    assert "cap 1M" in line
    assert "tools 128" in line
    assert "reflect 50" in line
    assert "safe off" in line
    assert "auto on" in line


def test_sticky_status_noop_on_non_tty() -> None:
    sticky_status._test_reset()
    stream = StringIO()
    sticky_status.start(lambda: "hello status", stream=stream)
    assert sticky_status.is_active() is False
    assert stream.getvalue() == ""
    sticky_status.refresh()
    sticky_status.stop()


class _TtyStream(StringIO):
    def isatty(self) -> bool:
        return True


def test_size_queries_the_selected_stream(monkeypatch) -> None:
    stream = _TtyStream()
    monkeypatch.setattr(stream, "fileno", lambda: 42)
    observed: list[int] = []

    def fake_get_terminal_size(fd: int):
        observed.append(fd)
        return sticky_status.os.terminal_size((132, 48))

    monkeypatch.setattr(sticky_status.os, "get_terminal_size", fake_get_terminal_size)

    assert sticky_status._size(stream) == (132, 48)
    assert observed == [42]


def test_sticky_status_noop_without_vt_support(monkeypatch) -> None:
    sticky_status._test_reset()
    monkeypatch.setattr(sticky_status, "_supports_vt", lambda _stream: False)
    stream = _TtyStream()

    sticky_status.start(lambda: "hello status", stream=stream)

    assert sticky_status.is_active() is False
    assert stream.getvalue() == ""


def test_posix_vt_support_requires_terminal_type_and_descriptor(monkeypatch) -> None:
    if sticky_status.sys.platform == "win32":
        pytest.skip("POSIX terminal capability contract")
    stream = _TtyStream()
    monkeypatch.setattr(stream, "fileno", lambda: 2)

    monkeypatch.delenv("TERM", raising=False)
    assert sticky_status._supports_vt(stream) is False
    monkeypatch.setenv("TERM", "dumb")
    assert sticky_status._supports_vt(stream) is False
    monkeypatch.setenv("TERM", "xterm-256color")
    assert sticky_status._supports_vt(stream) is True


def test_sticky_status_paints_and_clears_on_tty(monkeypatch) -> None:
    sticky_status._test_reset()

    class _Size:
        columns = 80
        lines = 24

    monkeypatch.setattr(sticky_status, "_supports_vt", lambda _stream: True)
    monkeypatch.setattr(
        sticky_status, "_size", lambda _stream: (_Size.columns, _Size.lines)
    )
    stream = _TtyStream()
    sticky_status.start(lambda: "● model · local · tools 8", stream=stream)
    assert sticky_status.is_active() is True
    painted = stream.getvalue()
    assert "● model · local · tools 8" in painted
    assert "\033[1;23r" in painted
    assert painted.endswith("\0338\033[23;1H")
    sticky_status.stop()
    cleared = stream.getvalue()
    assert "\033[r" in cleared
    assert sticky_status.is_active() is False


def test_sticky_status_moves_and_clears_old_row_after_resize(monkeypatch) -> None:
    sticky_status._test_reset()
    dimensions = [24]

    class _Size:
        columns = 80

        @property
        def lines(self) -> int:
            return dimensions[0]

    monkeypatch.setattr(sticky_status, "_supports_vt", lambda _stream: True)
    monkeypatch.setattr(
        sticky_status, "_size", lambda _stream: (_Size.columns, _Size().lines)
    )
    stream = _TtyStream()
    sticky_status.start(lambda: "model · local", stream=stream)
    before_resize = len(stream.getvalue())

    dimensions[0] = 30
    sticky_status.refresh()

    resized = stream.getvalue()[before_resize:]
    assert "\033[r\033[24;1H\033[2K" in resized
    assert "\033[1;29r" in resized
    assert "\033[30;1H" in resized
    sticky_status.stop()


def test_sticky_status_does_not_repaint_unchanged_truncated_line(monkeypatch) -> None:
    sticky_status._test_reset()
    monkeypatch.setattr(sticky_status, "_supports_vt", lambda _stream: True)
    monkeypatch.setattr(sticky_status, "_size", lambda _stream: (20, 24))
    stream = _TtyStream()
    sticky_status.start(lambda: "a status line that is much too long", stream=stream)
    after_start = stream.getvalue()

    sticky_status.refresh()

    assert stream.getvalue() == after_start
    sticky_status.stop()


def test_agent_loop_starts_and_stops_sticky(monkeypatch) -> None:
    calls: list[str] = []

    def fake_start(render, *, stream=None):
        calls.append(f"start:{render()}")

    def fake_stop():
        calls.append("stop")

    monkeypatch.setattr(sticky_status, "start", fake_start)
    monkeypatch.setattr(sticky_status, "stop", fake_stop)
    monkeypatch.setattr(main, "json_sink", lambda: None)

    def boom(*_a, **_k):
        calls.append("body")
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_agent_loop_body", boom)
    cfg = Config()
    main.RUNTIME_STATUS.update(
        {
            "model": "m",
            "mode": "local",
            "safe_mode": True,
            "auto_mode": False,
            "max_tool_iterations": 8,
            "tool_think_every": 10,
        }
    )

    try:
        main.agent_loop(object(), cfg, "hi")
    except RuntimeError as exc:
        assert str(exc) == "boom"

    assert calls[0].startswith("start:")
    assert "body" in calls
    assert calls[-1] == "stop"


def test_agent_loop_skips_sticky_status_for_json_sink(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sticky_status, "start", lambda *_a, **_k: calls.append("start"))
    monkeypatch.setattr(sticky_status, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(main, "json_sink", lambda: object())
    monkeypatch.setattr(
        main, "_agent_loop_body", lambda *_a, **_k: calls.append("body")
    )

    main.agent_loop(object(), Config(), "hi")

    assert calls == ["body"]


def test_signal_handler_restores_terminal_before_forwarding(monkeypatch) -> None:
    sticky_status._test_reset()
    events: list[object] = []
    stream = _TtyStream()
    signum = sticky_status.signal.SIGTERM

    monkeypatch.setattr(
        sticky_status, "_clear_for_signal", lambda _stream: events.append("clear")
    )
    monkeypatch.setattr(
        sticky_status, "_drain_terminal", lambda _stream: events.append("drain")
    )
    monkeypatch.setattr(
        sticky_status,
        "_paint_locked",
        lambda _stream, _line, **_kwargs: events.append("paint"),
    )
    monkeypatch.setattr(
        sticky_status.signal,
        "signal",
        lambda observed, handler: events.append(("signal", observed, handler)),
    )
    monkeypatch.setattr(
        sticky_status.os,
        "kill",
        lambda pid, observed: events.append(("kill", pid, observed)),
    )

    with sticky_status._lock:
        sticky_status._active = True
        sticky_status._stream = stream
        sticky_status._render = lambda: "status"
        sticky_status._signal_handlers[signum] = sticky_status.signal.SIG_DFL

    sticky_status._handle_signal(signum, None)

    assert events[0] == "clear"
    assert events[1] == "drain"
    assert events[2] == ("signal", signum, sticky_status.signal.SIG_DFL)
    assert events[3] == ("kill", sticky_status.os.getpid(), signum)
    assert events[-1] == "paint"

    with sticky_status._lock:
        sticky_status._active = False
        sticky_status._stream = None
        sticky_status._render = None
        sticky_status._signal_handlers.clear()


def test_sigterm_restores_real_pty_before_process_exit() -> None:
    if os.name != "posix":
        pytest.skip("POSIX PTY signal semantics")

    import errno
    import pty
    import select
    import signal
    import subprocess
    import sys
    import time

    master, slave = pty.openpty()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                "from algo_cli import sticky_status; "
                "sticky_status.start(lambda: 'signal-test', stream=sys.stderr); "
                "print('READY', flush=True); time.sleep(30)"
            ),
        ],
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        start_new_session=True,
    )
    os.close(slave)
    captured = bytearray()
    sent = False
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(master, 65_536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                captured.extend(chunk)
                if b"READY" in captured and not sent:
                    child.send_signal(signal.SIGTERM)
                    sent = True
            if child.poll() is not None and not readable:
                break
        child.wait(timeout=2)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        os.close(master)

    assert sent is True
    assert child.returncode == -signal.SIGTERM
    assert b"signal-test" in captured
    assert b"\033[r" in captured
    assert captured.count(b"\033[2K") >= 2
