"""Contrast and distinctness gate over every theme's assumed dark background."""

from __future__ import annotations

import itertools

import pytest
from prompt_toolkit.styles import Style as PromptStyle

from algo_cli.ui import contrast, tokens

THEMES = sorted(tokens.PALETTES)

# WCAG 2.x ratios against the palette's reference background.
FLOORS = {
    "text": 7.0,
    "muted": 4.5,
    "primary": 4.5,
    "secondary": 4.5,
    "accent": 4.5,
    "success": 4.5,
    "warning": 4.5,
    "error": 4.5,
    "info": 4.5,
    "border": 3.0,
    "border_accent": 3.0,
}
FOOTER_FLOOR = 4.5
DISTINCT_DELTA_E = 20.0


def _worst_ratio(fg: str, bg: str) -> float:
    # prompt_toolkit paints the footer at 8-bit depth by default, so check what it emits too.
    return min(
        contrast.contrast(fg, bg),
        contrast.contrast(contrast.quantize_256(fg), contrast.quantize_256(bg)),
    )


def test_contrast_helpers_match_wcag_reference_values():
    assert contrast.contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast.contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)
    assert contrast.delta_e2000_lab((50, 2.6772, -79.7751), (50, 0, -82.7485)) == pytest.approx(2.0425, abs=1e-4)
    assert contrast.delta_e2000_lab((50, 2.5, 0), (73, 25, -18)) == pytest.approx(27.1492, abs=1e-4)
    assert contrast.quantize_256("#ffffff") == "#ffffff"


@pytest.mark.parametrize("name", THEMES)
def test_text_semantic_and_border_colours_meet_floors(name):
    colors = tokens.PALETTES[name].colors()
    failures = {
        key: round(contrast.contrast(colors[key], colors["bg"]), 2)
        for key, floor in FLOORS.items()
        if contrast.contrast(colors[key], colors["bg"]) < floor
    }
    assert failures == {}


@pytest.mark.parametrize("name", THEMES)
def test_footer_and_rprompt_chips_meet_floor_on_their_bars(name):
    colors = tokens.PALETTES[name].colors()
    style = PromptStyle.from_dict(tokens.prompt_toolkit_styles(colors))
    footer_classes = [key for key in tokens.prompt_toolkit_styles(colors) if key.startswith("footer.")]
    assert footer_classes
    failures = {}
    for bar_class, bar_key in (("bottom-toolbar", "surface_alt"), ("rprompt", "surface")):
        for footer_class in footer_classes:
            attrs = style.get_attrs_for_style_str(f"class:{bar_class} class:{footer_class}")
            assert attrs.bgcolor == colors[bar_key].lstrip("#")
            ratio = _worst_ratio("#" + attrs.color, "#" + attrs.bgcolor)
            if ratio < FOOTER_FLOOR:
                failures[f"{bar_class}/{footer_class}"] = round(ratio, 2)
    assert failures == {}


@pytest.mark.parametrize("name", THEMES)
def test_completion_menu_items_meet_floor(name):
    colors = tokens.PALETTES[name].colors()
    style = PromptStyle.from_dict(tokens.prompt_toolkit_styles(colors))
    for cls in (
        "completion-menu.completion",
        "completion-menu.completion.current",
        "completion-menu.meta.completion",
        "completion-menu.meta.completion.current",
    ):
        attrs = style.get_attrs_for_style_str(f"class:{cls}")
        assert _worst_ratio("#" + attrs.color, "#" + attrs.bgcolor) >= FOOTER_FLOOR, cls


@pytest.mark.parametrize("name", THEMES)
def test_ok_warn_err_and_brand_are_pairwise_distinct(name):
    colors = tokens.PALETTES[name].colors()
    for a, b in itertools.combinations(("success", "warning", "error", "primary"), 2):
        assert contrast.delta_e2000(colors[a], colors[b]) >= DISTINCT_DELTA_E, (a, b)


@pytest.mark.parametrize("name", THEMES)
def test_meaning_bearing_roles_never_share_a_colour(name):
    colors = tokens.PALETTES[name].colors()
    roles = ("text", "muted", "primary", "secondary", "accent", "success", "warning", "error", "info",
             "border", "border_accent")
    values = [colors[role].lower() for role in roles]
    assert len(set(values)) == len(values)


def test_redeye_keeps_red_for_brand_only():
    colors = tokens.PALETTES["redeye"].colors()
    for role in ("error", "info", "success", "warning"):
        assert contrast.delta_e2000(colors[role], colors["primary"]) >= DISTINCT_DELTA_E, role
