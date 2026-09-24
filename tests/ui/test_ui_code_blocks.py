"""Fenced code blocks stay readable in every theme and on any terminal background.

Regressions guarded here: code-theme tokens drawn on the terminal's own background
(unreadable on light terminals, gruvbox diff text #282828 on #282828), dracula
Generic.Deleted #8b080b at 1.45:1, and nord comments #616e87 at 2.43:1.
"""

from __future__ import annotations

import pytest
from pygments.styles import get_style_by_name
from rich.console import Console
from rich.style import Style
from rich.text import Text

from algo_cli.ui import contrast, tokens
from algo_cli.ui.markdown import CODE_TOKEN_FLOOR, ReadableSyntaxTheme, ThemedMarkdown

THEMES = sorted(tokens.PALETTES)

DIFF_FENCE = """```diff
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-    return old_value
+    return new_value
```
"""

PYTHON_FENCE = """```python
# a comment
def run(value):
    raise ValueError("bad")  # 42
```
"""


def _cells(name: str, markup: str) -> list[tuple[str, Style]]:
    palette = tokens.PALETTES[name]
    console = Console(
        record=True,
        width=80,
        force_terminal=True,
        color_system="truecolor",
        theme=tokens.rich_theme(palette),
    )
    console.print(ThemedMarkdown(markup, code_theme=palette.code_theme))
    text = Text.from_ansi(console.export_text(styles=True))
    return [
        (ch, text.get_style_at_offset(console, i))
        for i, ch in enumerate(text.plain)
        if not ch.isspace()
    ]


def _line_cells(cells: list[tuple[str, Style]], needle: str) -> list[Style]:
    joined = "".join(ch for ch, _ in cells)
    index = joined.index(needle.replace(" ", ""))
    return [style for _, style in cells[index : index + len(needle.replace(" ", ""))]]


def _ratio(style: Style) -> float:
    assert style.color is not None and style.color.triplet is not None
    assert style.bgcolor is not None and not style.bgcolor.is_default, "code cell left on the terminal background"
    return contrast.contrast(style.color.triplet.hex, style.bgcolor.triplet.hex)


def test_lift_to_floor_keeps_passing_colours_and_lifts_failing_ones():
    assert contrast.lift_to_floor("#ff4689", "#272822", 4.5) == "#ff4689"
    lifted = contrast.lift_to_floor("#8b080b", "#282a36", 4.5)
    assert contrast.contrast(lifted, "#282a36") >= 4.5
    darker = contrast.lift_to_floor("#282828", "#fb4934", 4.5)
    assert contrast.contrast(darker, "#fb4934") >= 4.5
    assert contrast.luminance(darker) <= contrast.luminance("#282828")


@pytest.mark.parametrize("name", THEMES)
def test_every_code_token_meets_floor_on_its_own_background(name):
    code_theme = tokens.PALETTES[name].code_theme
    theme = ReadableSyntaxTheme(code_theme)
    panel = theme.get_background_style().bgcolor
    assert panel is not None and panel.triplet is not None
    failures = {}
    for token, _ in get_style_by_name(code_theme):
        style = theme.get_style_for_token(token)
        bg = style.bgcolor or panel
        ratio = contrast.contrast(style.color.triplet.hex, bg.triplet.hex)
        if ratio < CODE_TOKEN_FLOOR:
            failures[str(token)] = round(ratio, 2)
    assert failures == {}


@pytest.mark.parametrize("name", THEMES)
@pytest.mark.parametrize("line", ["-    return old_value", "+    return new_value", "--- a/app.py"])
def test_diff_fence_lines_are_painted_and_readable(name, line):
    cells = _cells(name, DIFF_FENCE)
    for style in _line_cells(cells, line):
        assert _ratio(style) >= CODE_TOKEN_FLOOR, (name, line)


@pytest.mark.parametrize("name", THEMES)
@pytest.mark.parametrize("needle", ["# a comment", "# 42", "ValueError", "def run"])
def test_python_fence_tokens_are_painted_and_readable(name, needle):
    cells = _cells(name, PYTHON_FENCE)
    for style in _line_cells(cells, needle):
        assert _ratio(style) >= CODE_TOKEN_FLOOR, (name, needle)


@pytest.mark.parametrize("name", THEMES)
def test_code_panel_does_not_depend_on_the_terminal_background(name):
    # Every glyph inside the fence carries its own painted background, so a light
    # terminal cannot sit behind dark-theme token colours.
    panel = get_style_by_name(tokens.PALETTES[name].code_theme).background_color.lower()
    cells = _cells(name, PYTHON_FENCE)
    assert cells
    for _, style in cells:
        assert style.bgcolor is not None and not style.bgcolor.is_default
        assert style.bgcolor.triplet.hex == panel
        assert contrast.contrast(style.color.triplet.hex, panel) >= CODE_TOKEN_FLOOR
