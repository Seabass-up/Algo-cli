"""Prompt/run footer parity and Rich viewport regression checks."""

import io

from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from rich.live import Live
from rich.text import Text

from algo_cli import display, main, sticky_status
from algo_cli.config import Config


def test_footer_uses_prompt_fragments_and_background(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("PROMPT_TOOLKIT_COLOR_DEPTH", "DEPTH_24_BIT")
    cfg = Config()
    monkeypatch.setattr(main, "RUNTIME_STATUS", {"mode": "cloud", "safe_mode": False, "auto_mode": True})
    formatted = main.build_status_toolbar(cfg)
    style = main.build_prompt_style(display.theme_colors(cfg.theme))
    rendered = sticky_status._visible_line(sticky_status.FooterLine(formatted, style), 180)
    text = Text.from_ansi(rendered)
    expected = fragment_list_to_text(to_formatted_text(formatted))
    assert text.plain == expected.ljust(180)
    assert any(span.style.bgcolor is not None for span in text.spans)
    assert any(span.style.bold for span in text.spans)
    assert len({str(span.style.color) for span in text.spans}) >= 3


def test_footer_truncates_by_terminal_cells(monkeypatch):
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.styles import Style
    from prompt_toolkit.utils import get_cwidth

    rendered = sticky_status._visible_line(
        sticky_status.FooterLine(HTML("<b>\u754c\u754c\u754c</b> tail"), Style.from_dict({"bottom-toolbar": "bg:#112233"})), 5,
    )
    assert get_cwidth(Text.from_ansi(rendered).plain) == 5


def test_long_live_answer_fits_above_footer_and_finishes_complete(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sticky_status, "is_active", lambda: True)
    output = io.StringIO()
    console = display.RuntimeConsole(file=output, force_terminal=True, force_interactive=True, width=60, height=10)
    answer = "\n".join(f"answer line {number}" for number in range(30))
    with Live(Text(answer), console=console, auto_refresh=False) as live:
        for _ in range(4):
            live.refresh()
            assert live._live_render.last_render_height == 9
    assert "answer line 29" in output.getvalue()


def test_normal_console_keeps_full_terminal_height(monkeypatch):
    monkeypatch.setattr(sticky_status, "is_active", lambda: False)
    assert display.RuntimeConsole(width=60, height=10).size.height == 10
