"""Redeye companion — Unicode state animations for the Algo CLI display.

The CLI has a *buddy*: a red scanning eye (think KITT / Cylon) that lives in
the status line and reacts to what the agent is doing. Every state has its
own motion signature so you can read the agent's mode from across the room:

    THINKING   the eye sweeps left-right across a track   ▰▰●▰▰  (scanning)
    DOING      a bolt strikes across a progress rail      ⚡▰▰▱▱  (striking)
    ANSWERING  the eye emits chevrons toward you          ◉ ❯❯❯  (speaking)
    IDLE       the eye breathes, with a slow blink        ◉ … ─  (watching)
    ERROR      the eye locks wide open, no motion         ⊘      (alarmed)

Architecture
------------
- Pure rich + stdlib; zero threads of our own. Two animation channels:
  1. ``animate()`` / ``spinner_name()`` — registered rich spinners, animated
     by rich's status refresh thread.
  2. ``current_frame()`` / ``buddy_frame()`` — wall-clock frame pickers for
     callers that already re-render (Live panels, prompt redraws). Each call
     returns the frame for *now*, so motion is free wherever updates happen.
- Windows-safe: ``force_utf8()`` switches the console to UTF-8 (code page
  65001 + stream reconfigure); if that fails, every frame set has an ASCII
  twin chosen at import so nothing ever renders as mojibake.
- Styles are theme style *names* (primary/accent/error/...), so the buddy
  automatically wears whatever palette is active — it bleeds red under the
  ``redeye`` theme and adapts elsewhere.
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator

from rich.spinner import SPINNERS
from rich.text import Text


# ---------------------------------------------------------------------------
# Console capability
# ---------------------------------------------------------------------------

def force_utf8() -> bool:
    """Switch the Windows console + Python streams to UTF-8.

    prompt_toolkit and rich both emit UTF-8; on a cp1252 console that turns
    into mojibake (``â<9d>¯`` instead of ``❯``). Returns True when the
    console can take full Unicode afterwards.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    return supports_unicode()


def supports_unicode() -> bool:
    """True when stdout's encoding can represent every buddy glyph."""
    sample = "⠋▰▱❯◉◎⚡⊘●─"
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        sample.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_UNICODE_OK = force_utf8()


# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------

class AIState(str, Enum):
    THINKING = "thinking"
    DOING = "doing"
    ANSWERING = "answering"
    IDLE = "idle"
    ERROR = "error"


@dataclass(frozen=True)
class StateAnimation:
    """One state's complete visual definition."""

    frames: tuple[str, ...]        # compact frames (spinner / inline titles)
    buddy: tuple[str, ...]         # wide "companion" frames (status lines)
    ascii_frames: tuple[str, ...]
    ascii_buddy: tuple[str, ...]
    interval_ms: int
    style: str                     # rich theme style name
    glyph: str                     # static symbol for one-off lines
    ascii_glyph: str
    label: str                     # default status label
    tagline: str                   # buddy's voice for this state


def _scan(track: str, pupil: str, width: int) -> tuple[str, ...]:
    """Build a KITT-style scanner: a pupil sweeping across a track and back."""
    positions = list(range(width)) + list(range(width - 2, 0, -1))
    return tuple(track * i + pupil + track * (width - 1 - i) for i in positions)


_ANIMATIONS: dict[AIState, StateAnimation] = {
    AIState.THINKING: StateAnimation(
        frames=_scan("▰", "●", 5),
        buddy=tuple(f"⟦{frame}⟧" for frame in _scan("▰", "●", 7)),
        ascii_frames=_scan("=", "0", 5),
        ascii_buddy=tuple(f"[{frame}]" for frame in _scan("=", "0", 7)),
        interval_ms=70,
        style="secondary",
        glyph="◉",
        ascii_glyph="0",
        label="thinking",
        tagline="scanning the problem",
    ),
    AIState.DOING: StateAnimation(
        frames=("⚡▱▱▱▱", "▰⚡▱▱▱", "▰▰⚡▱▱", "▰▰▰⚡▱", "▰▰▰▰⚡", "▰▰▰▰▰", "▱▰▰▰▰", "▱▱▰▰▰", "▱▱▱▰▰", "▱▱▱▱▰"),
        buddy=("⟦⚡▱▱▱▱▱⟧", "⟦▰⚡▱▱▱▱⟧", "⟦▰▰⚡▱▱▱⟧", "⟦▰▰▰⚡▱▱⟧", "⟦▰▰▰▰⚡▱⟧", "⟦▰▰▰▰▰⚡⟧", "⟦▰▰▰▰▰▰⟧", "⟦▱▱▱▱▱▱⟧"),
        ascii_frames=("*----", "#*---", "##*--", "###*-", "####*", "#####", "-####", "--###"),
        ascii_buddy=("[*-----]", "[#*----]", "[##*---]", "[###*--]", "[####*-]", "[#####*]", "[######]", "[------]"),
        interval_ms=75,
        style="accent",
        glyph="⚡",
        ascii_glyph="*",
        label="working",
        tagline="executing",
    ),
    AIState.ANSWERING: StateAnimation(
        frames=("◉   ", "◉❯  ", "◉❯❯ ", "◉❯❯❯", "◉ ❯❯", "◉  ❯"),
        buddy=("⟦ ◉ ⟧   ", "⟦ ◉ ⟧❯  ", "⟦ ◉ ⟧❯❯ ", "⟦ ◉ ⟧❯❯❯", "⟦ ◉ ⟧ ❯❯", "⟦ ◉ ⟧  ❯"),
        ascii_frames=("0   ", "0>  ", "0>> ", "0>>>", "0 >>", "0  >"),
        ascii_buddy=("[ 0 ]   ", "[ 0 ]>  ", "[ 0 ]>> ", "[ 0 ]>>>", "[ 0 ] >>", "[ 0 ]  >"),
        interval_ms=100,
        style="primary",
        glyph="❯",
        ascii_glyph=">",
        label="answering",
        tagline="streaming the answer",
    ),
    AIState.IDLE: StateAnimation(
        # Slow breath with an occasional blink (the ─ frame).
        frames=("◉", "◉", "◉", "◎", "○", "◎", "◉", "◉", "─", "◉"),
        buddy=("⟦ ◉ ⟧", "⟦ ◉ ⟧", "⟦ ◉ ⟧", "⟦ ◎ ⟧", "⟦ ○ ⟧", "⟦ ◎ ⟧", "⟦ ◉ ⟧", "⟦ ◉ ⟧", "⟦ ─ ⟧", "⟦ ◉ ⟧"),
        ascii_frames=("0", "0", "0", "o", ".", "o", "0", "0", "-", "0"),
        ascii_buddy=("[ 0 ]", "[ 0 ]", "[ 0 ]", "[ o ]", "[ . ]", "[ o ]", "[ 0 ]", "[ 0 ]", "[ - ]", "[ 0 ]"),
        interval_ms=350,
        style="muted",
        glyph="◉",
        ascii_glyph="0",
        label="ready",
        tagline="ready for work",
    ),
    AIState.ERROR: StateAnimation(
        # Deliberately static: errors should arrest, not flicker.
        frames=("⊘", "⊘"),
        buddy=("⟦ ⊘ ⟧", "⟦ ⊘ ⟧"),
        ascii_frames=("X", "X"),
        ascii_buddy=("[ X ]", "[ X ]"),
        interval_ms=500,
        style="error",
        glyph="⊘",
        ascii_glyph="X",
        label="error",
        tagline="something broke",
    ),
}


# ---------------------------------------------------------------------------
# Frame access
# ---------------------------------------------------------------------------

def animation_for(state: AIState | str) -> StateAnimation:
    return _ANIMATIONS[AIState(state)]


def frames_for(state: AIState | str, *, buddy: bool = False) -> tuple[str, ...]:
    anim = animation_for(state)
    if buddy:
        return anim.buddy if _UNICODE_OK else anim.ascii_buddy
    return anim.frames if _UNICODE_OK else anim.ascii_frames


def glyph(state: AIState | str) -> Text:
    """Static styled symbol for prefixing one-off lines (no animation)."""
    anim = animation_for(state)
    return Text(anim.glyph if _UNICODE_OK else anim.ascii_glyph, style=anim.style)


def current_frame(state: AIState | str) -> str:
    """Wall-clock frame for callers that re-render on their own cadence.

    Any display that updates regularly (a Live panel refreshed per token
    batch, a prompt redraw) shows smooth motion just by calling this on
    every render — no timer ownership needed.
    """
    return _pick_frame(frames_for(state), animation_for(state).interval_ms)


def buddy_frame(state: AIState | str) -> Text:
    """The companion eye, styled, at this instant — for status/footer lines."""
    anim = animation_for(state)
    frame = _pick_frame(frames_for(state, buddy=True), anim.interval_ms)
    return Text(frame, style=anim.style)


def _pick_frame(frames: tuple[str, ...], interval_ms: int) -> str:
    # perf_counter, not monotonic: display tests stub time.monotonic with a
    # finite tick iterator, and frame picking must never consume those ticks.
    return frames[int(time.perf_counter() * 1000 / interval_ms) % len(frames)]


def state_line(state: AIState | str, message: str = "") -> Text:
    """Single styled line: buddy + message — e.g. ``⟦ ◉ ⟧ ready for work``."""
    anim = animation_for(state)
    line = buddy_frame(state)
    line.append(f" {message or anim.tagline}", style=anim.style)
    return line


# ---------------------------------------------------------------------------
# Rich spinner channel
# ---------------------------------------------------------------------------

_SPINNERS_REGISTERED = False


def register_spinners() -> None:
    """Register buddy spinners (``algo-<state>``) with rich. Idempotent."""
    global _SPINNERS_REGISTERED
    if _SPINNERS_REGISTERED:
        return
    for state, anim in _ANIMATIONS.items():
        SPINNERS[f"algo-{state.value}"] = {
            "interval": anim.interval_ms,
            "frames": list(anim.buddy if _UNICODE_OK else anim.ascii_buddy),
        }
    _SPINNERS_REGISTERED = True


def spinner_name(state: AIState | str) -> str:
    register_spinners()
    return f"algo-{AIState(state).value}"


@contextmanager
def animate(console: Any, state: AIState | str, message: str = "") -> Iterator[None]:
    """Animated buddy status for a state::

        with animate(console, AIState.DOING, "search_files"):
            run_tool()
    """
    anim = animation_for(state)
    label = f"[{anim.style}]{message or anim.tagline}[/]"
    with console.status(label, spinner=spinner_name(state), spinner_style=anim.style):
        yield


# ---------------------------------------------------------------------------
# Set pieces
# ---------------------------------------------------------------------------

def intro_sweep(console: Any, *, duration: float = 0.9) -> None:
    """Boot animation: one scanner sweep under the logo, then settle to idle.

    Skipped automatically when stdout is not a live terminal (pipes, CI,
    JSON bridges) so it never pollutes captured output.
    """
    if not getattr(console, "is_terminal", False):
        return
    anim = animation_for(AIState.THINKING)
    deadline = time.perf_counter() + duration
    try:
        with console.status("", spinner=spinner_name(AIState.THINKING), spinner_style=anim.style):
            while time.perf_counter() < deadline:
                time.sleep(min(0.05, anim.interval_ms / 1000))
    except Exception:
        return


def demo() -> None:  # pragma: no cover - manual visual check
    """Walk the buddy through every state. Run: ``python -m algo_cli.animations``"""
    from algo_cli.display import console, show_error

    console.print()
    console.print(state_line(AIState.IDLE))
    intro_sweep(console, duration=1.2)
    with animate(console, AIState.THINKING, "scanning the problem"):
        time.sleep(2.4)
    with animate(console, AIState.DOING, "executing search_files"):
        time.sleep(2.4)
    with animate(console, AIState.ANSWERING, "streaming the answer"):
        time.sleep(1.8)
    console.print(state_line(AIState.ANSWERING, "answer delivered"))
    console.print()
    show_error("simulated failure — eye locks wide open, no flicker")
    console.print(state_line(AIState.IDLE, "recovered — ready for work"))
    console.print()


if __name__ == "__main__":  # pragma: no cover
    demo()
