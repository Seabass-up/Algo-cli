"""Sticky bottom status line while a generation / tool run is in progress.

prompt_toolkit's bottom_toolbar only paints during session.prompt(). During
agent_loop the prompt is idle, so this module reserves the last terminal row
(DECSTBM scroll region) and repaints the same status chips there.
"""

from __future__ import annotations

import shutil
import sys
import threading
from typing import Callable, TextIO

_render: Callable[[], str] | None = None
_stream: TextIO | None = None
_active = False
_lock = threading.RLock()
_last_line = ""
_rows = 0
_refresh_thread: threading.Thread | None = None
_stop_event = threading.Event()
_REFRESH_SECONDS = 0.4


def _enabled(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return max(20, size.columns), max(4, size.lines)


def _write(stream: TextIO, data: str) -> None:
    stream.write(data)
    stream.flush()


def _paint_locked(stream: TextIO, line: str, *, initial: bool = False) -> None:
    global _last_line, _rows
    cols, rows = _size()
    previous_rows = _rows
    _rows = rows
    visible = line.replace("\n", " ").replace("\r", " ")
    if len(visible) > cols - 1:
        visible = visible[: max(0, cols - 4)] + "..."
    resize_cleanup = ""
    if previous_rows and previous_rows != rows:
        # Reset the old margin before moving the footer. CUP safely clamps when
        # the terminal became shorter, so a stale status row cannot remain.
        resize_cleanup = f"\033[r\033[{previous_rows};1H\033[2K"
    restore = "\0338"
    if initial:
        # Generation starts after the submitted prompt. Keep subsequent output
        # inside the scroll region even when that prompt occupied the last row.
        restore += f"\033[{rows - 1};1H"
    _write(
        stream,
        (
            "\0337"
            f"{resize_cleanup}"
            f"\033[1;{rows - 1}r"
            f"\033[{rows};1H"
            "\033[2K"
            f"{visible}"
            f"{restore}"
        ),
    )
    _last_line = visible


def _clear_locked(stream: TextIO) -> None:
    global _last_line, _rows
    _cols, rows = _size()
    _rows = rows
    _write(
        stream,
        (
            "\0337"
            "\033[r"
            f"\033[{rows};1H"
            "\033[2K"
            "\0338"
        ),
    )
    _last_line = ""


def _refresh_loop() -> None:
    while not _stop_event.wait(_REFRESH_SECONDS):
        refresh()


def start(render: Callable[[], str], *, stream: TextIO | None = None) -> None:
    """Begin sticky footer for a generation. No-op when not a TTY."""
    global _render, _stream, _active, _refresh_thread, _stop_event
    if is_active():
        stop()
    target = stream if stream is not None else sys.__stderr__
    if target is None:
        return
    with _lock:
        _render = render
        _stream = target
        if not _enabled(target):
            _active = False
            return
        _active = True
        _stop_event = threading.Event()
        try:
            _paint_locked(target, render(), initial=True)
        except Exception:
            _active = False
            _render = None
            _stream = None
            return
        _refresh_thread = threading.Thread(
            target=_refresh_loop,
            name="algo-sticky-status",
            daemon=True,
        )
        _refresh_thread.start()


def refresh() -> None:
    """Repaint the sticky footer from the current render callback."""
    with _lock:
        if not _active or _render is None or _stream is None:
            return
        try:
            line = _render()
            cols_rows = _size()
            if line != _last_line or cols_rows[1] != _rows:
                _paint_locked(_stream, line)
        except Exception:
            pass


def stop() -> None:
    """Tear down sticky footer and restore the full scroll region."""
    global _render, _stream, _active, _refresh_thread
    with _lock:
        _stop_event.set()
        thread = _refresh_thread
        _refresh_thread = None
        stream = _stream
        was_active = _active
        _active = False
        _render = None
        _stream = None
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    if was_active and stream is not None:
        with _lock:
            try:
                _clear_locked(stream)
            except Exception:
                pass


def is_active() -> bool:
    with _lock:
        return _active


def _test_reset() -> None:
    stop()
    global _last_line, _rows
    with _lock:
        _last_line = ""
        _rows = 0
