"""Recording-console snapshots of the themed surfaces, checked cell by cell in every theme.

Each surface is rendered through a truecolor recording console, parsed back from
ANSI, and the style at known text is compared with the palette. Before tokens,
the logo, banner title, error label and thinking text rendered with no style.
"""

from __future__ import annotations

import io

import pytest
from pygments.styles import get_style_by_name
from rich.console import Console
from rich.style import Style
from rich.text import Text

from algo_cli import display
from algo_cli.ui import tokens

THEMES = sorted(tokens.PALETTES)

MARKDOWN_SAMPLE = """# Title

Use `inline` here.

```python
print("hi")
```

- item

> quoted

[docs](https://example.invalid/)
"""


@pytest.fixture
def recorder(monkeypatch):
    def make(name: str, width: int = 120) -> Console:
        # Pin UTF-8, non-legacy output: on Windows runners Rich otherwise swaps box
        # glyphs for ASCII, and these tests assert theme styling, not platform fallback.
        console = Console(
            file=io.StringIO(),
            record=True,
            width=width,
            force_terminal=True,
            legacy_windows=False,
            color_system="truecolor",
            theme=display.THEME_MAP[name],
        )
        monkeypatch.setattr(display, "console", console)
        monkeypatch.setattr(display, "_active_theme_name", name)
        monkeypatch.setattr(display, "_json_sink", None)
        return console

    return make


def _rendered(console: Console) -> Text:
    return Text.from_ansi(console.export_text(styles=True))


def _style_at(text: Text, needle: str, console: Console) -> Style:
    index = text.plain.find(needle)
    assert index >= 0, needle
    return text.get_style_at_offset(console, index)


def _hex(style: Style) -> str | None:
    return style.color.triplet.hex if style.color is not None and style.color.triplet else None


def _assert_no_background(text: Text) -> None:
    # SGR 49 (the terminal's own background) is allowed; any explicit colour is a painted slab.
    painted = [
        span
        for span in text.spans
        if (bg := Style.parse(str(span.style)).bgcolor) is not None and not bg.is_default
    ]
    assert painted == []


@pytest.mark.parametrize("name", THEMES)
def test_logo_renders_in_brand_colour(recorder, name):
    palette = tokens.PALETTES[name]
    console = recorder(name, width=120)
    display._print_algo_logo()
    text = _rendered(console)
    glyph_offsets = [i for i, ch in enumerate(text.plain) if not ch.isspace()]
    assert glyph_offsets
    for offset in glyph_offsets:
        style = text.get_style_at_offset(console, offset)
        assert style.bold and _hex(style) == palette.primary

    narrow = recorder(name, width=60)
    display._print_algo_logo()
    small = _rendered(narrow)
    assert _hex(_style_at(small, "ALGO", narrow)) == palette.primary
    assert _style_at(small, "CLI", narrow).bold
    assert _hex(_style_at(small, "CLI", narrow)) == palette.secondary


@pytest.mark.parametrize("name", THEMES)
def test_banner_title_flow_and_labels_are_styled(recorder, name):
    palette = tokens.PALETTES[name]
    console = recorder(name, width=100)
    console.print(display.render_opening_banner(version="0.0.0"))
    text = _rendered(console)
    title = _style_at(text, "Algo CLI", console)
    assert title.bold and _hex(title) == palette.primary
    for word, colour in (
        ("understand", palette.secondary),
        ("route", palette.primary),
        ("act ", palette.accent),
        ("verify", palette.success),
        ("remember", palette.secondary),
    ):
        style = _style_at(text, word, console)
        assert style.bold and _hex(style) == colour, word
    label = _style_at(text, "Inference", console)
    assert label.bold and _hex(label) == palette.secondary
    assert _hex(_style_at(text, "╭", console)) == palette.border_accent
    _assert_no_background(text)


@pytest.mark.parametrize("name", THEMES)
def test_error_label_is_bold_error_colour(recorder, name):
    palette = tokens.PALETTES[name]
    console = recorder(name)
    display.show_error("disk full", error_class="io", hint="free space")
    text = _rendered(console)
    label = _style_at(text, "Error (io):", console)
    assert label.bold and _hex(label) == palette.error
    assert _hex(_style_at(text, "disk full", console)) == palette.text
    assert _hex(_style_at(text, "free space", console)) == palette.muted


@pytest.mark.parametrize("name", THEMES)
def test_thinking_body_is_italic_muted(recorder, name):
    palette = tokens.PALETTES[name]
    console = recorder(name)
    console.print(display._thinking_renderable("weighing the options", final=True))
    text = _rendered(console)
    body = _style_at(text, "weighing the options", console)
    assert body.italic and _hex(body) == palette.muted
    assert _hex(_style_at(text, "╭", console)) == palette.border


def _painted_spans(text: Text) -> list:
    return [
        span
        for span in text.spans
        if (bg := Style.parse(str(span.style)).bgcolor) is not None and not bg.is_default
    ]


@pytest.mark.parametrize("name", THEMES)
def test_markdown_follows_theme_and_paints_only_the_code_panel(recorder, name):
    palette = tokens.PALETTES[name]
    console = recorder(name, width=80)
    rendered = display.themed_markdown(MARKDOWN_SAMPLE)
    assert rendered.code_theme == palette.code_theme
    console.print(rendered)
    text = _rendered(console)
    assert _hex(_style_at(text, "Title", console)) == palette.primary
    inline = _style_at(text, "inline", console)
    assert _hex(inline) == palette.secondary and inline.bgcolor is None
    assert _hex(_style_at(text, "•", console)) == palette.primary
    quote = _style_at(text, "quoted", console)
    assert quote.italic and _hex(quote) == palette.muted
    assert _hex(_style_at(text, "docs", console)) == palette.info
    assert "print" in text.plain
    # Only the fenced code panel is painted, and with the code style's own background.
    code_bg = get_style_by_name(palette.code_theme).background_color.lower()
    code_start = text.plain.index("print")
    code_line_start = text.plain.rfind("\n", 0, code_start) + 1
    code_line_end = text.plain.index("\n", code_start)
    for span in _painted_spans(text):
        assert code_line_start - 81 <= span.start and span.end <= code_line_end + 82, text.plain[span.start:span.end]
    code_style = _style_at(text, "print", console)
    assert code_style.bgcolor is not None and code_style.bgcolor.triplet.hex == code_bg
