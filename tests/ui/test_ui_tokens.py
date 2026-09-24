"""Token table completeness: every theme defines every token, and every token parses."""

from __future__ import annotations

import pytest
from prompt_toolkit.styles import Style as PromptStyle
from pygments.styles import get_style_by_name
from rich.console import Console
from rich.style import Style

from algo_cli import display
from algo_cli.ui import tokens

THEMES = sorted(tokens.PALETTES)
EXPECTED_THEMES = {"tokyo-night", "catppuccin-mocha", "dracula", "nord", "gruvbox", "dolphie", "redeye"}


def test_seven_themes_ship_and_display_is_generated_from_tokens():
    assert set(tokens.PALETTES) == EXPECTED_THEMES
    assert set(display.THEME_COLORS) == EXPECTED_THEMES
    assert set(display.THEME_MAP) == EXPECTED_THEMES
    assert display.available_themes() == sorted(EXPECTED_THEMES)
    for name, palette in tokens.PALETTES.items():
        assert display.THEME_COLORS[name] == palette.colors()


@pytest.mark.parametrize("name", THEMES)
def test_every_theme_defines_every_palette_key_and_token(name):
    palette = tokens.PALETTES[name]
    assert tuple(palette.colors()) == tokens.PALETTE_KEYS
    reference = tokens.rich_styles(tokens.PALETTES["tokyo-night"])
    assert set(tokens.rich_styles(palette)) == set(reference)
    prompt_reference = tokens.prompt_toolkit_styles(tokens.PALETTES["tokyo-night"].colors())
    assert set(tokens.prompt_toolkit_styles(palette.colors())) == set(prompt_reference)


@pytest.mark.parametrize("name", THEMES)
def test_every_rich_token_parses_and_resolves_in_the_theme(name):
    theme = display.THEME_MAP[name]
    console = Console(theme=theme)
    for token, value in tokens.rich_styles(tokens.PALETTES[name]).items():
        parsed = Style.parse(value)
        assert console.get_style(token) == parsed, token


@pytest.mark.parametrize("name", THEMES)
def test_semantic_tokens_carry_their_attributes(name):
    palette = tokens.PALETTES[name]
    console = Console(theme=display.THEME_MAP[name])
    logo = console.get_style("brand.logo")
    assert logo.bold and logo.color is not None and logo.color.triplet.hex == palette.primary
    thinking = console.get_style("thinking")
    assert thinking.italic and thinking.color.triplet.hex == palette.muted
    error = console.get_style("notice.error")
    assert error.bold and error.color.triplet.hex == palette.error


@pytest.mark.parametrize("name", THEMES)
def test_no_rich_token_paints_a_background(name):
    for token, value in tokens.rich_styles(tokens.PALETTES[name]).items():
        assert Style.parse(value).bgcolor is None, token


@pytest.mark.parametrize("name", THEMES)
def test_prompt_toolkit_style_parses_and_themes_the_completion_menu(name):
    colors = tokens.PALETTES[name].colors()
    style = PromptStyle.from_dict(tokens.prompt_toolkit_styles(colors))
    current = style.get_attrs_for_style_str("class:completion-menu.completion.current")
    assert current.bgcolor == colors["primary"].lstrip("#")
    assert current.color == colors["bg"].lstrip("#")
    chip = style.get_attrs_for_style_str("class:bottom-toolbar class:footer.danger")
    assert chip.bold and chip.color == colors["error"].lstrip("#")
    assert chip.bgcolor == colors["surface"].lstrip("#")


@pytest.mark.parametrize("name", THEMES)
def test_code_theme_is_an_installed_pygments_style(name):
    get_style_by_name(tokens.code_theme(name))


def test_unknown_theme_falls_back_to_default_code_theme():
    assert tokens.code_theme("no-such-theme") == tokens.PALETTES["tokyo-night"].code_theme
