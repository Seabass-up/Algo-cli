"""Style lint over algo_cli: no compound theme-name styles, and a ratchet on raw-colour markup.

Rich 15 cannot resolve a theme name inside a compound style ("bold primary"), so
the whole style is dropped and the text renders unstyled. Call sites use a named
token that carries its attributes ("heading", "brand.logo") instead.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

from algo_cli.ui import tokens

PACKAGE = Path(__file__).resolve().parents[2] / "algo_cli"

THEME_NAMES = set(tokens.PALETTE_KEYS) | set(tokens.semantic_tokens(tokens.PALETTES["tokyo-night"]))
ATTRIBUTES = {"bold", "italic", "dim", "underline", "reverse", "blink", "strike", "not", "on", "b", "i", "u", "s", "r", "d"}
MARKUP_TAG = re.compile(r"\[([a-z_. ]+)\]")
FSTRING_COMPOUND = re.compile(r"^(?:bold|italic|dim|underline|reverse)(?: \w+)* \{\}$|\[(?:bold|italic|dim) \{\}\]")

RAW_COLOUR = r"(?:bright_)?(?:black|red|green|yellow|blue|magenta|cyan|white)|grey\d*|gray\d*|#[0-9a-fA-F]{6}"
RAW_MARKUP = re.compile(r"\[(?:(?:bold|italic|dim|underline|reverse|not|on) )*(?:" + RAW_COLOUR + r")(?: [a-z_ ]+)?\]")
RAW_STYLE = re.compile(r"^(?:(?:bold|italic|dim|underline|reverse|not|on) )*(?:" + RAW_COLOUR + r")(?: [a-z]+)*$")
STYLE_KEYWORDS = {"style", "border_style", "header_style", "title_style", "spinner_style"}

# Raw colours bypass the theme. Existing uses are listed here and may only shrink;
# the approval prompt and setup screens move to tokens in a later slice.
RAW_COLOUR_ALLOWLIST = {
    "algo_cli/cli_config.py": 17,
    "algo_cli/main.py": 17,
    "algo_cli/nathan_runtime.py": 3,
    "algo_cli/oliver_slash_dispatch.py": 8,
}


def _modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path.relative_to(PACKAGE.parent).as_posix(), ast.parse(path.read_text(encoding="utf-8-sig"))


def _is_compound_theme_style(value: str) -> bool:
    words = value.split()
    return (
        len(words) >= 2
        and any(word in THEME_NAMES for word in words)
        and any(word in ATTRIBUTES for word in words)
        and all(word in THEME_NAMES or word in ATTRIBUTES for word in words)
    )


def compound_theme_styles() -> list[str]:
    hits: list[str] = []
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if _is_compound_theme_style(value.strip()) or any(
                    _is_compound_theme_style(tag) for tag in MARKUP_TAG.findall(value)
                ):
                    hits.append(f"{rel}:{node.lineno}: {value[:60]!r}")
            elif isinstance(node, ast.JoinedStr):
                shape = "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
                if FSTRING_COMPOUND.search(shape):
                    hits.append(f"{rel}:{node.lineno}: f{shape[:60]!r}")
    return hits


def raw_colour_counts() -> Counter[str]:
    counts: Counter[str] = Counter()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                counts[rel] += len(RAW_MARKUP.findall(node.value))
            elif (
                isinstance(node, ast.keyword)
                and node.arg in STYLE_KEYWORDS
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and RAW_STYLE.match(node.value.value)
            ):
                counts[rel] += 1
    return +counts


def test_detectors_catch_known_shapes():
    assert _is_compound_theme_style("bold primary")
    assert _is_compound_theme_style("secondary italic")
    assert not _is_compound_theme_style("error code")
    assert not _is_compound_theme_style("heading")
    assert FSTRING_COMPOUND.search("bold {}")
    assert RAW_MARKUP.findall("[bold cyan]First run[/] [red]x[/] [muted]ok[/]") == ["[bold cyan]", "[red]"]
    assert RAW_STYLE.match("green") and not RAW_STYLE.match("success")


def test_no_compound_theme_styles_in_package():
    assert compound_theme_styles() == []


def test_raw_colour_markup_only_shrinks():
    counts = raw_colour_counts()
    grown = {path: n for path, n in counts.items() if n > RAW_COLOUR_ALLOWLIST.get(path, 0)}
    assert grown == {}, "new raw colour styles; use a theme token instead"
    shrunk = {path: (counts.get(path, 0), n) for path, n in RAW_COLOUR_ALLOWLIST.items() if counts.get(path, 0) < n}
    assert shrunk == {}, "raw colours were removed; lower RAW_COLOUR_ALLOWLIST to the new counts"
