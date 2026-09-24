"""Colour profile after startup environment setup, and prompt_toolkit 3.0.x style compatibility."""

from __future__ import annotations

import io
import types

import pytest
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style as PromptStyle

from algo_cli import cli_config, display, main
from algo_cli.ui import detect, tokens
from algo_cli.ui.detect import ColorProfile

_COLOUR_ENV = ("NO_COLOR", "FORCE_COLOR", "COLORTERM", "TERM", "WT_SESSION")


@pytest.fixture
def isolated_display(monkeypatch):
    """A throwaway terminal console as ``display.console``; profile globals restore after the test."""
    for name in ("COLOR_PROFILE", "_profile_active", "_legacy_windows", "_theme_pushed",
                 "_base_layer_pushed", "_active_theme_name"):
        monkeypatch.setattr(display, name, getattr(display, name, False), raising=False)
    monkeypatch.setattr(display, "_theme_pushed", False)
    monkeypatch.setattr(display, "_base_layer_pushed", False, raising=False)
    monkeypatch.setattr(display, "_active_theme_name", display._base_theme_name)
    fresh = display.RuntimeConsole(file=io.StringIO(), force_terminal=True, width=80)
    monkeypatch.setattr(display, "console", fresh)
    # The pytest process has no terminal; the refreshed profile must still apply.
    monkeypatch.setattr(display, "_terminal_takes_profile", lambda _probe: True)
    for key in _COLOUR_ENV:
        monkeypatch.delenv(key, raising=False)
    return fresh


def test_env_file_no_color_reaches_rich_prompt_and_footer(isolated_display, monkeypatch, tmp_path):
    """Regression: detection ran at import, before main() loaded the env file, so its NO_COLOR was ignored."""
    env_file = tmp_path / "env"
    env_file.write_text("NO_COLOR=1\n", encoding="utf-8")
    monkeypatch.setenv("ALGO_CLI_ENV_FILE", str(env_file))
    monkeypatch.setenv("NO_COLOR", "")  # registers NO_COLOR so the env file's value is undone afterwards
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(display, "COLOR_PROFILE", ColorProfile.TRUECOLOR)  # what import-time detection saw
    monkeypatch.setattr(display, "_profile_active", True)

    lifecycle: list[str] = []
    seen: dict[str, object] = {}
    monkeypatch.setattr(main.sys, "argv", ["algo-cli", "config", "status"])
    monkeypatch.setattr(main, "_force_utf8_console", lambda: lifecycle.append("vt"))
    monkeypatch.setattr(main, "has_legacy_data", lambda: False)
    monkeypatch.setattr(main, "migrate_legacy_sidecar_files", lambda: [])

    def run(_argv):
        seen["profile"] = display.active_color_profile()
        seen["depth"] = display.prompt_color_depth()
        seen["no_color"] = display.console.no_color
        seen["style"] = main.build_prompt_style(display.theme_colors("tokyo-night"))
        return 0

    monkeypatch.setattr(cli_config, "run", run)
    with pytest.raises(SystemExit):
        main.main()

    assert lifecycle == ["vt"]
    assert seen["profile"] is ColorProfile.NONE
    assert seen["depth"] == ColorDepth.DEPTH_1_BIT
    assert seen["no_color"] is True
    style = seen["style"]
    assert isinstance(style, PromptStyle)
    assert style.get_attrs_for_style_str("class:footer.danger").reverse


def test_refresh_sees_windows_vt_enabled_after_import(isolated_display, monkeypatch):
    """Regression: a console whose VT mode _force_utf8_console enabled stayed at the import-time ANSI16."""
    vt = {"on": False}
    monkeypatch.setattr(detect, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(detect, "_windows_console_vt", lambda: vt["on"])

    assert display.refresh_color_profile() is ColorProfile.ANSI16
    assert isolated_display.color_system == "standard"

    vt["on"] = True
    assert display.refresh_color_profile() is ColorProfile.TRUECOLOR
    assert isolated_display.color_system == "truecolor"
    assert display.prompt_color_depth() == ColorDepth.DEPTH_24_BIT


def test_refresh_keeps_the_active_theme_at_the_new_profile(isolated_display, monkeypatch):
    monkeypatch.setenv("COLORTERM", "truecolor")
    display.refresh_color_profile()
    display.set_theme("dracula")
    assert isolated_display.get_style("tool.ok").color is not None

    monkeypatch.setenv("NO_COLOR", "1")
    assert display.refresh_color_profile() is ColorProfile.NONE
    assert display.current_theme_name() == "dracula"
    assert isolated_display.get_style("tool.ok").color is None
    assert isolated_display.no_color is True

    # set_theme pops only its own layer; the re-detected base layer stays underneath.
    display.set_theme(display._base_theme_name)
    assert isolated_display.get_style("tool.ok").color is None


# prompt_toolkit 3.0.0 through 3.0.51 accept these attributes; "dim" arrived in 3.0.52.
_PTK_30_ATTRIBUTES = {"bold", "italic", "underline", "reverse", "noreverse", "hidden"}


def _unsupported_attributes(styles: dict[str, str]) -> set[str]:
    bad = set()
    for value in styles.values():
        for part in value.split():
            if part.startswith(("bg:", "fg:", "#", "ansi")) or part in {"default", ""}:
                continue
            if part not in _PTK_30_ATTRIBUTES:
                bad.add(part)
    return bad


def test_mono_prompt_style_uses_only_attributes_older_prompt_toolkit_parses():
    """Regression: "dim" made Style.from_dict raise on prompt_toolkit < 3.0.52, dropping the prompt style."""
    styles = tokens.prompt_toolkit_styles(tokens.MONO.colors())
    assert _unsupported_attributes(styles) == set()
    PromptStyle.from_dict(styles)


@pytest.mark.parametrize("profile", list(ColorProfile))
@pytest.mark.parametrize("name", sorted(tokens.PALETTES))
def test_every_profile_prompt_style_uses_only_older_prompt_toolkit_attributes(name, profile):
    styles = tokens.prompt_toolkit_styles(tokens.palette_for(name, profile).colors())
    assert _unsupported_attributes(styles) == set()
