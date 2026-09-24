"""Theme tokens: one table generates the Rich themes and the prompt_toolkit style.

Two layers. A ``Palette`` holds raw colours per theme. ``semantic_tokens`` maps a
palette to named Rich style strings whose attributes live inside the token
("bold #7aa2f7"), so call sites never compose "bold primary": Rich 15 cannot
parse a theme name inside a compound style and drops the whole style.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from rich.theme import Theme

# Colour roles every palette defines; ``display.THEME_COLORS`` exposes exactly these.
PALETTE_KEYS: tuple[str, ...] = (
    "bg",
    "primary",
    "secondary",
    "accent",
    "surface",
    "surface_alt",
    "border",
    "border_accent",
    "text",
    "muted",
    "success",
    "warning",
    "error",
    "info",
)


@dataclass(frozen=True)
class Palette:
    bg: str  # assumed terminal background: used for contrast checks only, never painted by Rich
    primary: str
    secondary: str
    accent: str
    surface: str  # rprompt background
    surface_alt: str  # bottom toolbar background
    border: str
    border_accent: str
    text: str
    muted: str
    success: str
    warning: str
    error: str
    info: str
    code_theme: str  # Pygments style for fenced code blocks

    def colors(self) -> dict[str, str]:
        values = asdict(self)
        return {key: values[key] for key in PALETTE_KEYS}


PALETTES: dict[str, Palette] = {
    "tokyo-night": Palette(
        bg="#1a1b26",
        primary="#7aa2f7",
        secondary="#bb9af7",
        accent="#2ac3de",
        surface="#111827",
        surface_alt="#0f172a",
        border="#6e7491",
        border_accent="#8189b3",
        text="#e5e9f0",
        muted="#8a93bd",
        success="#9ece6a",
        warning="#e0af68",
        error="#f7768e",
        info="#7dcfff",
        code_theme="one-dark",
    ),
    "catppuccin-mocha": Palette(
        bg="#1e1e2e",
        primary="#89b4fa",
        secondary="#cba6f7",
        accent="#94e2d5",
        surface="#11111b",
        surface_alt="#181825",
        border="#6c7086",
        border_accent="#7f849c",
        text="#cdd6f4",
        muted="#9399b2",
        success="#a6e3a1",
        warning="#f9e2af",
        error="#f38ba8",
        info="#89dceb",
        code_theme="one-dark",
    ),
    "dracula": Palette(
        bg="#282a36",
        primary="#bd93f9",
        secondary="#ff79c6",
        accent="#8be9fd",
        surface="#21222c",
        surface_alt="#1e1f29",
        border="#6272a4",
        border_accent="#7c86b8",
        text="#f8f8f2",
        muted="#a0a9cb",
        success="#50fa7b",
        warning="#f1fa8c",
        error="#ff5555",
        info="#72b8ff",
        code_theme="dracula",
    ),
    "nord": Palette(
        bg="#2e3440",
        primary="#88c0d0",
        secondary="#c9a5c2",
        accent="#8fbcbb",
        surface="#292e39",
        surface_alt="#272c36",
        border="#74819b",
        border_accent="#81a1c1",
        text="#eceff4",
        muted="#a3adc2",
        success="#a3be8c",
        warning="#f0b76b",
        error="#eb8f97",
        info="#9dc4ea",
        code_theme="nord",
    ),
    "gruvbox": Palette(
        bg="#282828",
        primary="#83a598",
        secondary="#d3869b",
        accent="#8ec07c",
        surface="#282828",
        surface_alt="#1d2021",
        border="#7c6f64",
        border_accent="#928374",
        text="#ebdbb2",
        muted="#a89984",
        success="#a9c93b",
        warning="#fabd2f",
        error="#fb5643",
        info="#76b5c4",
        code_theme="gruvbox-dark",
    ),
    # Brand theme matching the Redeye mark: red chrome on near-black. Red carries
    # brand only; error moves to pink-red and info to steel so meaning stays distinct.
    "redeye": Palette(
        bg="#0a0a0a",
        primary="#e54040",
        secondary="#ff7a7a",
        accent="#ff9e6b",
        surface="#0a0a0a",
        surface_alt="#111111",
        border="#7a5555",
        border_accent="#9c6565",
        text="#f2f2f2",
        muted="#a39494",
        success="#6fcf6f",
        warning="#ffb347",
        error="#ff70b0",
        info="#9fb4c8",
        code_theme="monokai",
    ),
    "dolphie": Palette(
        bg="#0f1525",
        primary="#bbc8e8",
        secondary="#91abec",
        accent="#8f9fc1",
        surface="#0f1525",
        surface_alt="#0a0e1b",
        border="#697490",
        border_accent="#7585ad",
        text="#e9e9e9",
        muted="#8390ad",
        success="#54efae",
        warning="#f0e357",
        error="#f05757",
        info="#6fc3df",
        code_theme="github-dark",
    ),
}


def semantic_tokens(p: Palette) -> dict[str, str]:
    """Named styles used at call sites. Every value is a complete Rich style string."""
    b = "bold "
    return {
        # brand and headings
        "brand.logo": b + p.primary,
        "brand.logo.alt": b + p.secondary,
        "heading": b + p.primary,
        "subheading": b + p.secondary,
        "label": b + p.primary,
        "model.name": b + p.primary,
        "strong": b + p.text,
        "link": "underline " + p.info,
        # emphasis in the palette's roles (banner flow line)
        "emph.primary": b + p.primary,
        "emph.secondary": b + p.secondary,
        "emph.accent": b + p.accent,
        "emph.success": b + p.success,
        # turn
        "thinking": "italic " + p.muted,
        "thinking.label": p.muted,
        # tool lifecycle
        "tool.name": b + p.text,
        "tool.arg": p.muted,
        "tool.meta": p.muted,
        "tool.running": p.primary,
        "tool.ok": p.success,
        "tool.fail": p.error,
        "tool.denied": p.warning,
        # notices
        "notice.info": p.info,
        "notice.warn": b + p.warning,
        "notice.error": b + p.error,
        "notice.success": p.success,
        "notice.hint": p.muted,
        # agent pipeline
        "agent.role": b + p.secondary,
        "agent.ok": p.success,
        "agent.partial": p.warning,
        "agent.fail": p.error,
        # cards
        "panel.border": p.border,
        "approval.border": p.warning,
        "approval.title": b + p.warning,
        "error.border": p.error,
        # diff
        "diff.add": p.success,
        "diff.del": p.error,
        "diff.hunk": p.info,
        "diff.meta": b + p.text,
    }


def builtin_overrides(p: Palette) -> dict[str, str]:
    """Rich's built-in style names, recoloured so assistant content follows the theme.

    No override paints a background: the stock ``markdown.code`` is ``on black``.
    """
    return {
        "markdown.h1": "bold underline " + p.primary,
        "markdown.h1.border": p.border,
        "markdown.h2": "bold " + p.primary,
        "markdown.h3": "bold " + p.secondary,
        "markdown.h4": "italic " + p.secondary,
        "markdown.code": p.secondary,
        "markdown.code_block": p.text,
        "markdown.link": p.info,
        "markdown.link_url": "underline " + p.info,
        "markdown.list": p.primary,
        "markdown.item.bullet": "bold " + p.primary,
        "markdown.item.number": p.primary,
        "markdown.block_quote": "italic " + p.muted,
        "markdown.hr": p.border,
        "markdown.table.border": p.border,
        "markdown.table.header": "bold " + p.text,
        "markdown.kbd": "bold " + p.warning,
        "rule.line": p.border,
        "status.spinner": p.primary,
        "repr.number": "bold not italic " + p.accent,
        "repr.number_complex": "bold not italic " + p.accent,
        "repr.str": "not bold not italic " + p.success,
        "repr.bool_true": "italic " + p.success,
        "repr.bool_false": "italic " + p.error,
        "repr.none": "italic " + p.secondary,
        "repr.url": "not bold not italic underline " + p.info,
        "repr.path": p.secondary,
        "repr.filename": p.secondary,
        "repr.ellipsis": p.warning,
        "repr.ipv4": "bold " + p.success,
        "repr.ipv6": "bold " + p.success,
        "repr.uuid": "not bold " + p.warning,
        "repr.call": "bold " + p.secondary,
        "repr.tag_name": "bold " + p.secondary,
        "repr.attrib_name": "not italic " + p.warning,
        "repr.attrib_value": "not italic " + p.secondary,
        "table.header": "bold " + p.text,
        "prompt.choices": "bold " + p.primary,
        "prompt.default": "bold " + p.accent,
        "prompt.invalid": p.error,
        "prompt.invalid.choice": p.error,
    }


def rich_styles(p: Palette) -> dict[str, str]:
    return {**p.colors(), **semantic_tokens(p), **builtin_overrides(p)}


def rich_theme(p: Palette) -> Theme:
    return Theme(rich_styles(p), inherit=True)


def prompt_toolkit_styles(colors: Mapping[str, str]) -> dict[str, str]:
    """prompt_toolkit classes for the footer, rprompt and completion menu.

    Takes the ``Palette.colors()`` mapping so ``main.build_prompt_style`` keeps its
    signature. The bar backgrounds stay painted in this slice.
    """
    c = colors
    bar = f"noreverse bg:{c['surface_alt']} {c['text']}"
    return {
        # noreverse: prompt_toolkit defaults reverse video on toolbars (white bar bug).
        "bottom-toolbar": bar,
        "bottom-toolbar.off": bar,
        "bottom-toolbar.on": bar,
        "bottom-toolbar.text": f"noreverse {c['text']}",
        "rprompt": f"noreverse bg:{c['surface']} {c['muted']}",
        "rprompt.text": f"noreverse {c['muted']}",
        "footer.text": c["text"],
        "footer.model": "bold " + c["text"],
        "footer.muted": c["muted"],
        "footer.sep": c["muted"],
        "footer.info": c["info"],
        "footer.ok": c["success"],
        "footer.caution": c["warning"],
        "footer.alert": c["error"],
        "footer.warn": "bold " + c["warning"],
        "footer.danger": "bold " + c["error"],
        "footer.theme": c["primary"],
        "completion-menu": f"bg:{c['surface_alt']} {c['text']}",
        "completion-menu.completion": f"bg:{c['surface_alt']} {c['text']}",
        "completion-menu.completion.current": f"noreverse bold bg:{c['primary']} {c['bg']}",
        "completion-menu.meta.completion": f"bg:{c['surface_alt']} {c['muted']}",
        "completion-menu.meta.completion.current": f"noreverse bg:{c['primary']} {c['bg']}",
        "completion-menu.multi-column-meta": f"bg:{c['surface_alt']} {c['muted']}",
        "scrollbar.background": f"bg:{c['surface_alt']}",
        "scrollbar.button": f"bg:{c['border_accent']}",
    }


def code_theme(name: str) -> str:
    palette = PALETTES.get(name)
    return palette.code_theme if palette else PALETTES["tokyo-night"].code_theme
