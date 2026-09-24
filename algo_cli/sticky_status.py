"""Sticky bottom status line while a generation / tool run is in progress.

prompt_toolkit's bottom_toolbar only paints during session.prompt(). During
agent_loop the prompt is idle, so this module reserves the last terminal row
(DECSTBM scroll region) and repaints the same status chips there.
"""

from __future__ import annotations

import os
import io
from dataclasses import dataclass
import signal
import sys
import threading
import time
from typing import Any, Callable, TextIO

@dataclass(frozen=True)
class FooterLine:
    formatted: Any
    style: Any


_render: Callable[[], str | FooterLine] | None = None
_stream: TextIO | None = None
_active = False
_lock = threading.RLock()
_last_line = ""
_rows = 0
_refresh_thread: threading.Thread | None = None
_stop_event = threading.Event()
_signal_handlers: dict[int, Any] = {}
_REFRESH_SECONDS = 0.4


def _supports_vt(stream: TextIO) -> bool:
    if sys.platform != "win32":
        stream.fileno()
        terminal = os.environ.get("TERM", "").strip().casefold()
        return bool(terminal and terminal != "dumb")
    try:
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(stream.fileno())
        enabled = ctypes.windll.kernel32.GetConsoleMode(  # type: ignore[attr-defined]
            handle, ctypes.byref(mode)
        )
        return bool(enabled and mode.value & 0x0004)
    except (AttributeError, OSError, ValueError):
        return False


def _enabled(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty()) and _supports_vt(stream)
    except Exception:
        return False


def _size(stream: TextIO) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(stream.fileno())
    except (AttributeError, OSError, ValueError):
        size = os.terminal_size((80, 24))
    return max(20, size.columns), max(4, size.lines)


def _write(stream: TextIO, data: str) -> None:
    stream.write(data)
    stream.flush()


def _visible_line(line: str | FooterLine, cols: int) -> str:
    if isinstance(line, FooterLine):
        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.formatted_text import to_formatted_text
        from prompt_toolkit.output.vt100 import Vt100_Output
        from prompt_toolkit.renderer import print_formatted_text
        from prompt_toolkit.styles import default_ui_style, merge_styles
        from prompt_toolkit.utils import get_cwidth

        from .display import prompt_color_depth

        remaining = cols
        cropped = False
        fragments: list[tuple[str, str]] = []
        for style, text, *_ in to_formatted_text(line.formatted):
            visible = ""
            for char in text.replace("\n", " ").replace("\r", " "):
                width = get_cwidth(char)
                if width > remaining:
                    cropped = True
                    break
                visible += char
                remaining -= width
            fragments.append((f"class:bottom-toolbar {style}", visible))
            if cropped or not remaining:
                break
        # Pad with the toolbar class so a painted bar spans the full width.
        fragments.append(("class:bottom-toolbar", " " * remaining))
        buffer = io.StringIO()
        output = Vt100_Output(
            buffer, lambda: Size(rows=1, columns=cols), term=os.environ.get("TERM"),
            # The same depth as the Rich console and the prompt session (ui.detect).
            default_color_depth=prompt_color_depth(),
        )
        print_formatted_text(output, fragments, merge_styles([default_ui_style(), line.style]))
        return buffer.getvalue()
    visible = line.replace("\n", " ").replace("\r", " ")
    if len(visible) > cols - 1:
        return visible[: max(0, cols - 4)] + "..."
    return visible


def _paint_locked(
    stream: TextIO,
    line: str | FooterLine,
    *,
    initial: bool = False,
    dimensions: tuple[int, int] | None = None,
) -> None:
    global _last_line, _rows
    cols, rows = dimensions or _size(stream)
    previous_rows = _rows
    _rows = rows
    visible = _visible_line(line, cols)
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
    _cols, rows = _size(stream)
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


def _drain_terminal(stream: TextIO) -> None:
    drain = getattr(os, "tcdrain", None)
    if not callable(drain):
        return
    try:
        drain(stream.fileno())
    except (AttributeError, OSError, ValueError):
        pass


def _clear_for_signal(stream: TextIO) -> None:
    """Reset margins without re-entering a buffered stream interrupted by a signal."""
    global _last_line, _rows
    descriptor = stream.fileno()
    _cols, rows = _size(stream)
    payload = f"\0337\033[r\033[{rows};1H\033[2K\0338".encode("ascii")
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("terminal reset write did not advance")
        offset += written
    _last_line = ""
    _rows = rows


def _resume_signal_handler(signum: int, previous: Any) -> None:
    if not _active or _stream is None or _render is None:
        return
    signal.signal(signum, _handle_signal)
    _signal_handlers[signum] = previous
    _paint_locked(_stream, _render(), initial=True)


def _handle_signal(signum: int, frame: Any) -> None:
    """Restore terminal state, then preserve the handler that was installed before us."""
    with _lock:
        if _active and _stream is not None:
            try:
                _clear_for_signal(_stream)
                _drain_terminal(_stream)
            except Exception:
                pass
        previous = _signal_handlers.get(signum, signal.SIG_DFL)

    try:
        signal.signal(signum, previous)
        if previous == signal.SIG_IGN:
            pass
        elif callable(previous):
            previous(signum, frame)
        else:
            # Pseudo-terminals can discard their final queued bytes when the
            # slave closes immediately after a default terminating signal.
            time.sleep(0.01)
            os.kill(os.getpid(), signum)
    finally:
        # SIGTSTP resumes here after SIGCONT. A custom termination handler can
        # also return, so restore the sticky state whenever the process lives.
        with _lock:
            try:
                _resume_signal_handler(signum, previous)
            except Exception:
                pass


def _install_signal_handlers() -> None:
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        return
    for name in ("SIGTSTP", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None or signum in _signal_handlers:
            continue
        previous = signal.getsignal(signum)
        signal.signal(signum, _handle_signal)
        _signal_handlers[signum] = previous


def _restore_signal_handlers() -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    handlers = list(_signal_handlers.items())
    _signal_handlers.clear()
    for signum, previous in handlers:
        try:
            if signal.getsignal(signum) is _handle_signal:
                signal.signal(signum, previous)
        except (OSError, ValueError):
            pass


def _refresh_loop() -> None:
    while not _stop_event.wait(_REFRESH_SECONDS):
        refresh()


def start(render: Callable[[], str | FooterLine], *, stream: TextIO | None = None) -> None:
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
            _install_signal_handlers()
        except Exception:
            _active = False
            _render = None
            _stream = None
            _restore_signal_handlers()
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
            dimensions = _size(_stream)
            visible = _visible_line(line, dimensions[0])
            if visible != _last_line or dimensions[1] != _rows:
                _paint_locked(_stream, line, dimensions=dimensions)
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
    _restore_signal_handlers()
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
