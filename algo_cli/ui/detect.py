"""One colour-profile detection shared by Rich, prompt_toolkit and the sticky footer.

Rich and prompt_toolkit each guess colour depth on their own, and they disagreed:
Rich drew truecolor while the footer drew 8-bit. The profile is detected once from
the environment and every renderer is configured from it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from enum import Enum

from prompt_toolkit.output import ColorDepth


class ColorProfile(str, Enum):
    NONE = "none"  # NO_COLOR or TERM=dumb: bold, dim and reverse only
    ANSI16 = "ansi16"
    ANSI256 = "ansi256"
    TRUECOLOR = "truecolor"


_RICH_COLOR_SYSTEM: dict[ColorProfile, str] = {
    # NONE keeps Rich's attribute rendering; the console also gets no_color=True.
    ColorProfile.NONE: "standard",
    ColorProfile.ANSI16: "standard",
    ColorProfile.ANSI256: "256",
    ColorProfile.TRUECOLOR: "truecolor",
}

_PROMPT_COLOR_DEPTH: dict[ColorProfile, ColorDepth] = {
    ColorProfile.NONE: ColorDepth.DEPTH_1_BIT,
    ColorProfile.ANSI16: ColorDepth.DEPTH_4_BIT,
    ColorProfile.ANSI256: ColorDepth.DEPTH_8_BIT,
    ColorProfile.TRUECOLOR: ColorDepth.DEPTH_24_BIT,
}

_PROFILE_ORDER: tuple[ColorProfile, ...] = (
    ColorProfile.NONE,
    ColorProfile.ANSI16,
    ColorProfile.ANSI256,
    ColorProfile.TRUECOLOR,
)

_FORCE_COLOR_LEVELS: dict[str, ColorProfile] = {
    "1": ColorProfile.ANSI16,
    "2": ColorProfile.ANSI256,
    "3": ColorProfile.TRUECOLOR,
}


def _windows_console_vt() -> bool:
    try:
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(sys.__stdout__.fileno())  # type: ignore[attr-defined,union-attr,unused-ignore]
        enabled = ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))  # type: ignore[attr-defined]
        return bool(enabled and mode.value & 0x0004)
    except (AttributeError, OSError, ValueError):
        return False


def detect_color_profile(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    windows_vt: bool | None = None,
) -> ColorProfile:
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR", ""):
        return ColorProfile.NONE
    term = env.get("TERM", "").strip().casefold()
    if term == "dumb":
        return ColorProfile.NONE
    detected = _terminal_profile(env, term, platform or sys.platform, windows_vt)
    force = env.get("FORCE_COLOR", "").strip().casefold()
    if force and force not in {"0", "false"}:
        # A minimum, as in supports-color: FORCE_COLOR=1 in a truecolor terminal stays truecolor.
        forced = _FORCE_COLOR_LEVELS.get(force, ColorProfile.TRUECOLOR)
        return max(detected, forced, key=_PROFILE_ORDER.index)
    return detected


def _terminal_profile(env: Mapping[str, str], term: str, platform: str, windows_vt: bool | None) -> ColorProfile:
    if env.get("COLORTERM", "").strip().casefold() in {"truecolor", "24bit"}:
        return ColorProfile.TRUECOLOR
    if "256color" in term:
        return ColorProfile.ANSI256
    if platform == "win32":
        if env.get("WT_SESSION"):
            return ColorProfile.TRUECOLOR
        # A VT-enabled console (Windows 10+) takes 24-bit SGR; legacy conhost has 16 colours.
        vt = _windows_console_vt() if windows_vt is None else windows_vt
        return ColorProfile.TRUECOLOR if vt else ColorProfile.ANSI16
    return ColorProfile.ANSI16


def rich_color_system(profile: ColorProfile) -> str:
    return _RICH_COLOR_SYSTEM[profile]


def prompt_color_depth(profile: ColorProfile) -> ColorDepth:
    return _PROMPT_COLOR_DEPTH[profile]
