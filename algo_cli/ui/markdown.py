"""Markdown that follows the active theme instead of Rich's Monokai defaults."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown, MarkdownElement
from rich.style import Style
from rich.syntax import PygmentsSyntaxTheme, Syntax, TokenType

from algo_cli.ui.contrast import lift_to_floor

# Same floor as muted and semantic text. Some Pygments styles ship tokens far below it
# (dracula Generic.Deleted #8b080b is 1.45:1, nord comments #616e87 are 2.43:1).
CODE_TOKEN_FLOOR = 4.5


class ReadableSyntaxTheme(PygmentsSyntaxTheme):
    """A Pygments style whose token colours are lifted to ``CODE_TOKEN_FLOOR`` against their own background."""

    def __init__(self, theme: str) -> None:
        super().__init__(theme)
        self._readable_cache: dict[TokenType, Style] = {}

    def get_style_for_token(self, token_type: TokenType) -> Style:
        cached = self._readable_cache.get(token_type)
        if cached is not None:
            return cached
        style = super().get_style_for_token(token_type)
        fg, bg = style.color, style.bgcolor or self._background_style.bgcolor
        if fg is not None and fg.triplet is not None and bg is not None and bg.triplet is not None:
            lifted = lift_to_floor(fg.triplet.hex, bg.triplet.hex, CODE_TOKEN_FLOOR)
            if lifted != fg.triplet.hex:
                style = style + Style(color=lifted)
        self._readable_cache[token_type] = style
        return style


class ThemedCodeBlock(CodeBlock):
    """Fenced code on the Pygments style's own painted background.

    The panel is painted because code-theme colours assume a dark background: left to
    the terminal's own background they vanish on light terminals, and styles that put
    diff text on a coloured token background (gruvbox) lose it entirely.
    """

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        yield Syntax(
            code,
            self.lexer_name,
            theme=ReadableSyntaxTheme(self.theme),
            word_wrap=True,
            padding=1,
        )


class ThemedMarkdown(Markdown):
    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "fence": ThemedCodeBlock,
        "code_block": ThemedCodeBlock,
    }

    def __init__(self, markup: str, *, code_theme: str, **kwargs: Any) -> None:
        super().__init__(markup, code_theme=code_theme, **kwargs)
