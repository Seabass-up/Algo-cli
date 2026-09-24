"""Windows-only branches exercised on any host through the `simulated_windows` fixture."""

from __future__ import annotations

import ctypes
import os
import sys
import types
from pathlib import Path

from rich.panel import Panel

from _consoles import recording_console
from algo_cli import animations, main, search_execution, sticky_status
from algo_cli import theodore_runtime_services as runtime_services


class _FakeKernel32:
    def __init__(self, console_mode: int = 0x0003) -> None:
        self.console_mode = console_mode
        self.calls: list[tuple[str, tuple]] = []

    def SetConsoleOutputCP(self, code_page):  # noqa: N802 - Win32 name
        self.calls.append(("SetConsoleOutputCP", (code_page,)))
        return 1

    def SetConsoleCP(self, code_page):  # noqa: N802 - Win32 name
        self.calls.append(("SetConsoleCP", (code_page,)))
        return 1

    def GetStdHandle(self, handle_id):  # noqa: N802 - Win32 name
        return 1000 + handle_id

    def GetConsoleMode(self, handle, mode_ref):  # noqa: N802 - Win32 name
        mode_ref._obj.value = self.console_mode
        return 1

    def SetConsoleMode(self, handle, mode):  # noqa: N802 - Win32 name
        self.calls.append(("SetConsoleMode", (handle, mode)))
        return 1


def _install_windll(monkeypatch, kernel32: _FakeKernel32) -> None:
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=kernel32), raising=False)


# --- the fixture itself -------------------------------------------------------------


def test_simulation_is_scoped_to_patched_modules(simulated_windows, tmp_path):
    simulated_windows.patch(search_execution)

    assert search_execution.os.name == "nt"
    assert search_execution.os.path is os.path
    assert search_execution.sys.platform == "win32"
    assert os.name != "nt" or sys.platform == "win32"
    # The real modules are untouched, so pathlib keeps working in the same test.
    (tmp_path / "probe.txt").write_text("ok", encoding="utf-8")
    assert Path(tmp_path / "probe.txt").read_text(encoding="utf-8") == "ok"
    assert search_execution.sys.stdout.encoding == "cp1252"
    assert sys.stdout is not simulated_windows.stdout


def test_simulation_reproduces_runner_glyph_substitution(simulated_windows):
    from rich.console import Console

    unpinned = Console(record=True, width=40)
    unpinned.print(Panel("hi"))

    text = unpinned.export_text()
    assert unpinned.legacy_windows is True
    assert unpinned.encoding == "cp1252"
    assert "╭" not in text
    assert "+" in text and "hi" in text


def test_recording_console_ignores_simulated_runner(simulated_windows):
    pinned = recording_console(width=40, color_system="truecolor")
    pinned.print(Panel("[bold]hi[/bold]"))

    assert pinned.legacy_windows is False
    assert pinned.color_system == "truecolor"
    assert "╭" in pinned.export_text()
    assert "\x1b[1m" in pinned.file.getvalue()


# --- search fallback -----------------------------------------------------------------


def test_glob_matcher_is_case_insensitive_on_simulated_windows(simulated_windows):
    if os.name != "nt":
        assert not search_execution.glob_matcher("*.PY")("setup.py")

    simulated_windows.patch(search_execution)

    assert search_execution.glob_matcher("*.PY")("setup.py")
    assert search_execution.glob_matcher("Docs/*.md")("docs/README.MD")
    assert not search_execution.glob_matcher("!*.PY")("setup.py")


def test_python_search_fallback_matches_glob_case_insensitively(simulated_windows, tmp_path, capsys):
    (tmp_path / "Setup.PY").write_text("needle\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("needle\n", encoding="utf-8")
    request = {
        "path": str(tmp_path),
        "pattern": "needle",
        "glob": "*.py",
        "max_files": 100,
        "max_file_bytes": 1024,
        "skip_dirs": [],
        "limit": 10,
    }
    simulated_windows.patch(search_execution)

    assert search_execution._python_search(request) == 0
    out = capsys.readouterr().out
    assert "Setup.PY" in out
    assert "notes.txt" not in out


# --- console setup -------------------------------------------------------------------


def test_force_utf8_switches_simulated_windows_console(simulated_windows, monkeypatch):
    kernel32 = _FakeKernel32()
    _install_windll(monkeypatch, kernel32)
    simulated_windows.patch(animations)
    assert animations.supports_unicode() is False

    assert animations.force_utf8() is True

    assert ("SetConsoleOutputCP", (65001,)) in kernel32.calls
    assert ("SetConsoleCP", (65001,)) in kernel32.calls
    assert simulated_windows.stdout.encoding == "utf-8"


def test_startup_console_enables_vt_processing_on_simulated_windows(simulated_windows, monkeypatch):
    kernel32 = _FakeKernel32(console_mode=0x0003)
    _install_windll(monkeypatch, kernel32)
    simulated_windows.patch(main)

    main._force_utf8_console()

    modes = [args for name, args in kernel32.calls if name == "SetConsoleMode"]
    assert modes == [(1000 - 11, 0x0007), (1000 - 12, 0x0007)]
    assert simulated_windows.stdout.encoding == "utf-8"


def test_startup_console_keeps_existing_vt_mode(simulated_windows, monkeypatch):
    kernel32 = _FakeKernel32(console_mode=0x0007)
    _install_windll(monkeypatch, kernel32)
    simulated_windows.patch(main)

    main._force_utf8_console()

    assert not [name for name, _args in kernel32.calls if name == "SetConsoleMode"]


def test_sticky_status_reads_vt_flag_from_windows_console(simulated_windows, monkeypatch):
    kernel32 = _FakeKernel32(console_mode=0x0003)
    _install_windll(monkeypatch, kernel32)
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: 500 + fd))
    simulated_windows.patch(sticky_status)
    stream = types.SimpleNamespace(fileno=lambda: 1)

    assert sticky_status._supports_vt(stream) is False
    kernel32.console_mode = 0x0007
    assert sticky_status._supports_vt(stream) is True


def test_sticky_status_treats_missing_console_api_as_no_vt(simulated_windows, monkeypatch):
    monkeypatch.delattr(ctypes, "windll", raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: fd))
    simulated_windows.patch(sticky_status)

    assert sticky_status._supports_vt(types.SimpleNamespace(fileno=lambda: 1)) is False


# --- path handling -------------------------------------------------------------------


def test_gateway_command_uses_exe_binary_on_simulated_windows(simulated_windows, monkeypatch, tmp_path):
    for variable in ("ALGO_CLI_GATEWAY_BIN", "OLLAMA_CLI_GATEWAY_BIN"):
        monkeypatch.delenv(variable, raising=False)
    (tmp_path / "harness-gateway").write_text("posix", encoding="utf-8")
    (tmp_path / "harness-gateway.exe").write_text("windows", encoding="utf-8")
    monkeypatch.setattr(runtime_services, "gateway_source_dir", lambda: tmp_path)

    if sys.platform != "win32":
        assert runtime_services.gateway_command() == ([str(tmp_path / "harness-gateway")], tmp_path)

    simulated_windows.patch(runtime_services)

    assert runtime_services.gateway_command() == ([str(tmp_path / "harness-gateway.exe")], tmp_path)
