"""Platform-independent Rich consoles and a simulated Windows host for tests.

Rich probes the host when a Console is built: on Windows runners without a
VT-enabled console it reports legacy Windows (ASCII box substitutions, the
"windows" colour system) and a cp1252 stdout makes it fall back to ASCII glyphs.
Tests that assert rendered text must not inherit those probes, and tests that
cover Windows-only branches must be runnable on macOS and Linux.
"""

from __future__ import annotations

import io
import os
import sys
import types
from typing import Any

import pytest
import rich.console
from rich._windows import WindowsConsoleFeatures
from rich.console import Console


def recording_console(
    *,
    width: int = 120,
    color_system: str | None = None,
    theme: Any = None,
    file: io.StringIO | None = None,
    **kwargs: Any,
) -> Console:
    """A recording Console that renders the same on every host.

    Pins the file (UTF-8 StringIO rather than the host's stdout), legacy Windows
    detection, terminal status, colour system, width and environment.
    """
    return Console(
        file=io.StringIO() if file is None else file,
        record=True,
        legacy_windows=False,
        force_terminal=True,
        color_system=color_system,
        width=width,
        theme=theme,
        _environ={},
        **kwargs,
    )


class _PlatformModule(types.ModuleType):
    """Stands in for ``os``/``sys`` inside one module, overriding a few names."""

    def __init__(self, real: types.ModuleType, **overrides: Any) -> None:
        super().__init__(real.__name__)
        self.__dict__["_real"] = real
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_real"], name)


class SimulatedWindows:
    """Makes Rich and chosen algo_cli modules take their Windows branches.

    ``os.name``/``sys.platform`` are replaced per module (``patch``), never on the
    real ``os``/``sys``: pathlib, subprocess and pytest itself keep the host's
    behaviour, so the simulation cannot break unrelated code in the same test.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        # Byte-backed so ``fileno()`` fails like a redirected runner stream and
        # Rich never reaches its real Win32 console renderer.
        self.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        self.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="backslashreplace")
        self.os = _PlatformModule(os, name="nt")
        # pytest re-installs its own sys.stdout for every phase, so the cp1252 streams
        # are exposed only through this per-module view.
        self.sys = _PlatformModule(sys, platform="win32", stdout=self.stdout, stderr=self.stderr)

    def activate(self) -> SimulatedWindows:
        features = WindowsConsoleFeatures(vt=False, truecolor=False)
        self._monkeypatch.setattr(rich.console, "WINDOWS", True)
        self._monkeypatch.setattr(rich.console, "get_windows_console_features", lambda: features)
        self._monkeypatch.setattr(rich.console, "detect_legacy_windows", lambda: True)
        self._monkeypatch.setattr(rich.console, "sys", self.sys)
        return self

    def patch(self, *modules: types.ModuleType) -> None:
        """Route the given modules' ``os``/``sys`` globals through the Windows view."""
        for module in modules:
            replaced = False
            if getattr(module, "os", None) is os:
                self._monkeypatch.setattr(module, "os", self.os)
                replaced = True
            if getattr(module, "sys", None) is sys:
                self._monkeypatch.setattr(module, "sys", self.sys)
                replaced = True
            if not replaced:
                raise AssertionError(f"{module.__name__} has no module-level os/sys to simulate")
