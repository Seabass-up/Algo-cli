"""One apply_theme() path for /theme and /reload, covering Rich and the prompt_toolkit style."""

from __future__ import annotations

import pytest

from algo_cli import display, main
from algo_cli.config import Config
from algo_cli.ui import tokens


class _FakeApp:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1


class _FakeSession:
    def __init__(self) -> None:
        self.style = None
        self.app = _FakeApp()


@pytest.fixture(autouse=True)
def restore_theme():
    original = display.current_theme_name()
    yield
    display.set_theme(original)


def _toolbar_bg(session: _FakeSession) -> str:
    return session.style.get_attrs_for_style_str("class:bottom-toolbar").bgcolor


def test_apply_theme_switches_rich_and_prompt_style_together():
    cfg = Config()
    session = _FakeSession()

    assert main.apply_theme(cfg, session, "nord") == "nord"

    assert cfg.theme == "nord"
    assert display.current_theme_name() == "nord"
    assert display.console.get_style("primary").color.triplet.hex == tokens.PALETTES["nord"].primary
    assert _toolbar_bg(session) == tokens.PALETTES["nord"].surface_alt.lstrip("#")
    assert session.app.invalidations == 1


def test_apply_theme_rejects_unknown_name_and_keeps_current():
    cfg = Config()
    main.apply_theme(cfg, None, "dracula")
    with pytest.raises(ValueError):
        main.apply_theme(cfg, None, "no-such-theme")
    assert cfg.theme == "dracula"
    assert display.current_theme_name() == "dracula"


def test_theme_command_rebuilds_session_style(monkeypatch):
    cfg = Config()
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(main, "show_info", lambda _msg: None)
    monkeypatch.setattr(main, "refresh_runtime_status", lambda *_a, **_k: None)
    session = _FakeSession()

    handled, _client = main.handle_command("/theme gruvbox", cfg, None, session)  # type: ignore[arg-type]

    assert handled is True
    assert cfg.theme == "gruvbox"
    assert _toolbar_bg(session) == tokens.PALETTES["gruvbox"].surface_alt.lstrip("#")


def test_reload_rebuilds_session_style_for_the_reloaded_theme(monkeypatch):
    cfg = Config(theme="tokyo-night")
    session = _FakeSession()
    session.style = main.build_prompt_style(display.theme_colors("tokyo-night"))
    loaded = Config(theme="redeye")
    monkeypatch.setattr(main, "reload_runtime", lambda: loaded)
    monkeypatch.setattr(main, "create_client", lambda _cfg: object())
    monkeypatch.setattr(main, "show_info", lambda _msg: None)

    handled, _client = main.handle_command("/reload", cfg, object(), session)  # type: ignore[arg-type]

    assert handled is True
    assert cfg.theme == "redeye"
    assert display.current_theme_name() == "redeye"
    # Before apply_theme, /reload left the previous theme's bar colours on the footer.
    assert _toolbar_bg(session) == tokens.PALETTES["redeye"].surface_alt.lstrip("#")
