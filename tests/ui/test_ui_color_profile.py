"""One colour profile for Rich, prompt_toolkit and the sticky footer; ANSI and mono token maps."""

from __future__ import annotations

import io
import itertools
import os
import re
import subprocess
import sys

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.renderer import print_formatted_text
from prompt_toolkit.styles import Style as PromptStyle
from rich.color import Color, ColorSystem
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from algo_cli import display, main, sticky_status
from algo_cli.config import Config
from algo_cli.ui import detect, tokens
from algo_cli.ui.detect import ColorProfile

THEMES = sorted(tokens.PALETTES)
PROFILES = list(ColorProfile)
MEANINGS = ("success", "warning", "error", "info")
_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _sgr_params(output: str) -> list[list[int]]:
    return [[int(p) for p in match.split(";") if p] for match in _SGR.findall(output)]


def _colour_kinds(output: str) -> set[str]:
    """Foreground/background colour encodings present in ``output``: truecolor, 256, 16."""
    kinds: set[str] = set()
    for params in _sgr_params(output):
        i = 0
        while i < len(params):
            p = params[i]
            if p in (38, 48) and i + 1 < len(params):
                kinds.add("truecolor" if params[i + 1] == 2 else "256")
                i += 5 if params[i + 1] == 2 else 3
                continue
            if 30 <= p <= 37 or 40 <= p <= 47 or 90 <= p <= 97 or 100 <= p <= 107:
                kinds.add("16")
            i += 1
    return kinds


def _background_params(output: str) -> list[int]:
    found = []
    for params in _sgr_params(output):
        i = 0
        while i < len(params):
            p = params[i]
            if p == 38 and i + 1 < len(params):
                i += 5 if params[i + 1] == 2 else 3
                continue
            if p == 48 or 40 <= p <= 47 or 100 <= p <= 107:
                found.append(p)
            i += 1
    return found


def _fg_code(output: str) -> int:
    codes = [p for params in _sgr_params(output) for p in params if 30 <= p <= 37 or 90 <= p <= 97]
    assert len(codes) == 1, output
    return codes[0]


def _render_rich(profile: ColorProfile, renderable, *, theme=None) -> str:
    buffer = io.StringIO()
    console = display.make_console(profile, file=buffer, force_terminal=True, width=80, theme=theme)
    console.print(renderable)
    return buffer.getvalue()


def _render_prompt(depth, fragments, style: PromptStyle) -> str:
    buffer = io.StringIO()
    output = Vt100_Output(buffer, lambda: Size(rows=1, columns=120), term="xterm", default_color_depth=depth)
    print_formatted_text(output, fragments, style)
    return buffer.getvalue()


@pytest.fixture
def active_profile(monkeypatch):
    """Pretend the process runs on a terminal with the given profile; restores the theme after."""
    original_theme = display.current_theme_name()

    def use(profile: ColorProfile) -> None:
        monkeypatch.setattr(display, "COLOR_PROFILE", profile)
        monkeypatch.setattr(display, "_profile_active", True)

    yield use
    display.set_theme(original_theme)


# --- detection -------------------------------------------------------------------------

DETECTION_MATRIX = [
    ({}, "linux", None, ColorProfile.ANSI16),
    ({"TERM": "xterm"}, "linux", None, ColorProfile.ANSI16),
    ({"TERM": "linux"}, "linux", None, ColorProfile.ANSI16),
    ({"TERM": "xterm-256color"}, "darwin", None, ColorProfile.ANSI256),
    ({"TERM": "screen-256color"}, "linux", None, ColorProfile.ANSI256),
    ({"TERM": "xterm-256color", "COLORTERM": "truecolor"}, "darwin", None, ColorProfile.TRUECOLOR),
    ({"TERM": "xterm", "COLORTERM": "24bit"}, "linux", None, ColorProfile.TRUECOLOR),
    ({"TERM": "xterm-256color", "COLORTERM": "yes"}, "linux", None, ColorProfile.ANSI256),
    ({"NO_COLOR": "1", "COLORTERM": "truecolor"}, "linux", None, ColorProfile.NONE),
    ({"NO_COLOR": "", "TERM": "xterm-256color"}, "linux", None, ColorProfile.ANSI256),
    ({"NO_COLOR": "1", "FORCE_COLOR": "3"}, "linux", None, ColorProfile.NONE),
    ({"TERM": "dumb"}, "linux", None, ColorProfile.NONE),
    ({"TERM": "dumb", "COLORTERM": "truecolor"}, "linux", None, ColorProfile.NONE),
    ({"FORCE_COLOR": "1"}, "linux", None, ColorProfile.ANSI16),
    ({"FORCE_COLOR": "2"}, "linux", None, ColorProfile.ANSI256),
    ({"FORCE_COLOR": "3"}, "linux", None, ColorProfile.TRUECOLOR),
    ({"FORCE_COLOR": "true"}, "linux", None, ColorProfile.TRUECOLOR),
    ({"FORCE_COLOR": "0", "TERM": "xterm-256color"}, "linux", None, ColorProfile.ANSI256),
    # FORCE_COLOR is a minimum, never a cap on what the terminal reports.
    ({"TERM": "xterm-256color", "COLORTERM": "truecolor", "FORCE_COLOR": "1"}, "darwin", None, ColorProfile.TRUECOLOR),
    ({"TERM": "xterm-256color", "FORCE_COLOR": "1"}, "linux", None, ColorProfile.ANSI256),
    ({"TERM": "xterm", "FORCE_COLOR": "2"}, "linux", None, ColorProfile.ANSI256),
    ({"TERM": "xterm-256color", "FORCE_COLOR": "3"}, "linux", None, ColorProfile.TRUECOLOR),
    ({"WT_SESSION": "1", "FORCE_COLOR": "1"}, "win32", False, ColorProfile.TRUECOLOR),
    ({}, "win32", False, ColorProfile.ANSI16),
    ({}, "win32", True, ColorProfile.TRUECOLOR),
    ({"WT_SESSION": "1"}, "win32", False, ColorProfile.TRUECOLOR),
    ({"TERM": "xterm-256color"}, "win32", False, ColorProfile.ANSI256),
    ({"NO_COLOR": "1", "WT_SESSION": "1"}, "win32", True, ColorProfile.NONE),
]


@pytest.mark.parametrize(("env", "platform", "windows_vt", "expected"), DETECTION_MATRIX)
def test_detection_matrix(env, platform, windows_vt, expected):
    assert detect.detect_color_profile(env, platform=platform, windows_vt=windows_vt) is expected


def test_detection_reads_the_process_environment_by_default(monkeypatch):
    for name in ("NO_COLOR", "FORCE_COLOR", "COLORTERM", "WT_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert detect.detect_color_profile(platform="linux") is ColorProfile.ANSI256
    monkeypatch.setenv("NO_COLOR", "1")
    assert detect.detect_color_profile(platform="linux") is ColorProfile.NONE


# --- one depth for Rich, prompt_toolkit and the sticky footer ------------------------------

EXPECTED_KINDS = {
    ColorProfile.TRUECOLOR: {"truecolor"},
    ColorProfile.ANSI256: {"256"},
    ColorProfile.ANSI16: {"16"},
    ColorProfile.NONE: set(),
}


# Rich caches a Style's SGR string on the (interned) instance whatever the colour system,
# so each profile renders its own colour or a 16-colour render leaks into the next one.
_PROBE_COLOURS = dict(zip(PROFILES, ("#7aa2f7", "#7aa2f6", "#7aa2f5", "#7aa2f4")))


@pytest.mark.parametrize("profile", PROFILES)
def test_rich_and_prompt_toolkit_render_the_same_colour_depth(profile):
    colour = _PROBE_COLOURS[profile]
    rich_out = _render_rich(profile, Text("chip", style=Style(color=colour, bold=True)))
    style = PromptStyle.from_dict({"chip": f"bold {colour}"})
    prompt_out = _render_prompt(detect.prompt_color_depth(profile), [("class:chip", "chip")], style)

    assert _colour_kinds(rich_out) == _colour_kinds(prompt_out) == EXPECTED_KINDS[profile]
    # Attributes survive every profile, including NO_COLOR.
    assert any(1 in params for params in _sgr_params(rich_out))
    assert any(1 in params for params in _sgr_params(prompt_out))


@pytest.mark.parametrize("profile", PROFILES)
def test_display_hands_prompt_toolkit_the_consoles_profile(active_profile, profile):
    active_profile(profile)
    assert display.active_color_profile() is profile
    assert display.prompt_color_depth() == detect.prompt_color_depth(profile)


def test_piped_output_keeps_hex_tokens(monkeypatch):
    monkeypatch.setattr(display, "COLOR_PROFILE", ColorProfile.ANSI16)
    monkeypatch.setattr(display, "_profile_active", False)
    assert display.active_color_profile() is ColorProfile.TRUECOLOR
    assert display.profile_colors(display.theme_colors("nord")) == display.theme_colors("nord")


def _sticky_footer(cfg: Config) -> str:
    style = main.build_prompt_style(display.theme_colors(cfg.theme))
    line = sticky_status.FooterLine(main.build_status_toolbar(cfg), style)
    return sticky_status._visible_line(line, 160)


@pytest.fixture
def unsafe_footer(monkeypatch):
    cfg = Config()
    cfg.safe_mode = False
    monkeypatch.setattr(main, "RUNTIME_STATUS", {"mode": "cloud", "model": "demo-model"})
    return cfg


@pytest.mark.parametrize("profile", PROFILES)
def test_sticky_footer_renders_at_the_console_depth(active_profile, unsafe_footer, profile):
    active_profile(profile)
    assert _colour_kinds(_sticky_footer(unsafe_footer)) == EXPECTED_KINDS[profile]


def _rprompt_fragments(cfg: Config) -> list[tuple[str, str]]:
    # prompt_toolkit renders the rprompt inside its "class:rprompt" window.
    return [(f"class:rprompt {style}", text) for style, text, *_ in to_formatted_text(main.build_status_rprompt(cfg))]


def _unpainted_text(output: str) -> str:
    """Visible characters of ``output`` that carry no background colour."""
    text = Text.from_ansi(output)
    painted = [False] * len(text.plain)
    for span in text.spans:
        if span.style and span.style.bgcolor is not None:
            painted[span.start:span.end] = [True] * (span.end - span.start)
    return "".join(ch for ch, on in zip(text.plain, painted) if not on and not ch.isspace())


@pytest.mark.parametrize("profile", [ColorProfile.TRUECOLOR, ColorProfile.ANSI256])
@pytest.mark.parametrize("name", THEMES)
def test_hex_footer_and_rprompt_paint_their_bar(active_profile, unsafe_footer, profile, name):
    """Regression: near-white chips on an unpainted bar vanished on light terminals (1.07-1.45:1)."""
    active_profile(profile)
    unsafe_footer.theme = name
    footer = _sticky_footer(unsafe_footer)
    assert "demo-model" in Text.from_ansi(footer).plain
    assert _unpainted_text(footer) == ""

    style = main.build_prompt_style(display.theme_colors(name))
    rprompt = _render_prompt(display.prompt_color_depth(), _rprompt_fragments(unsafe_footer), style)
    assert Text.from_ansi(rprompt).plain.strip()
    assert _unpainted_text(rprompt) == ""


@pytest.mark.parametrize("profile", [ColorProfile.ANSI16, ColorProfile.NONE])
@pytest.mark.parametrize("name", THEMES)
def test_ansi_and_mono_footer_stay_on_the_terminal_background(active_profile, unsafe_footer, profile, name):
    # Their text is the terminal's default colour (or an attribute), legible on any background.
    active_profile(profile)
    unsafe_footer.theme = name
    footer = _sticky_footer(unsafe_footer)
    assert "demo-model" in Text.from_ansi(footer).plain
    assert _background_params(footer) == []

    style = main.build_prompt_style(display.theme_colors(name))
    rprompt = _render_prompt(display.prompt_color_depth(), _rprompt_fragments(unsafe_footer), style)
    assert _background_params(rprompt) == []


# --- 16-colour profile ------------------------------------------------------------------

def test_hex_palettes_collapse_meanings_when_quantised_to_16_colours():
    """Why the ANSI map exists: Rich's own downgrade merges meanings in several themes."""
    collapsed = set()
    for name, palette in tokens.PALETTES.items():
        slots = [Color.parse(getattr(palette, role)).downgrade(ColorSystem.STANDARD).number for role in MEANINGS]
        if len(set(slots)) < len(slots):
            collapsed.add(name)
    assert {"tokyo-night", "catppuccin-mocha", "nord"} <= collapsed


@pytest.mark.parametrize("name", THEMES)
def test_16_colour_profile_uses_the_ansi_map_not_quantised_hex(name):
    assert tokens.palette_for(name, ColorProfile.ANSI16) is tokens.ANSI_PALETTES["ansi-dark"]


@pytest.mark.parametrize("ansi_name", sorted(tokens.ANSI_PALETTES))
def test_ansi_palettes_use_only_named_terminal_slots(ansi_name):
    palette = tokens.ANSI_PALETTES[ansi_name]
    for role, value in palette.colors().items():
        assert value == "default" or Color.parse(value).number < 16, role
    for token, value in tokens.rich_styles(palette).items():
        parsed = Style.parse(value)
        assert parsed.bgcolor is None, token
        assert parsed.color is None or parsed.color.is_default or parsed.color.number < 16, token


@pytest.mark.parametrize("ansi_name", sorted(tokens.ANSI_PALETTES))
def test_ansi_meanings_take_distinct_hues_and_brand_never_shares_their_slot(ansi_name):
    colors = tokens.ANSI_PALETTES[ansi_name].colors()
    slots = {role: Color.parse(colors[role]).number for role in (*MEANINGS, "accent", "primary")}
    hues = [slots[role] % 8 for role in MEANINGS]
    assert len(set(hues)) == len(MEANINGS)
    for a, b in itertools.combinations(("success", "warning", "error", "accent"), 2):
        assert slots[a] != slots[b], (a, b)
    assert slots["primary"] not in {slots[role] for role in MEANINGS}


def test_ansi_light_moves_warning_and_info_off_low_contrast_yellow_and_cyan():
    light = tokens.ANSI_PALETTES["ansi-light"]
    assert light.warning == "magenta" and light.info == "blue"


@pytest.mark.parametrize("name", THEMES)
def test_16_colour_meanings_stay_distinct_after_rendering(active_profile, name):
    active_profile(ColorProfile.ANSI16)
    display.set_theme(name)
    theme = display._rich_theme_for(name, ColorProfile.ANSI16)
    rich_codes = {
        token: _fg_code(_render_rich(ColorProfile.ANSI16, Text("x", style=token), theme=theme))
        for token in ("tool.ok", "tool.denied", "tool.fail", "notice.info")
    }
    assert len(set(rich_codes.values())) == 4, rich_codes

    style = main.build_prompt_style(display.theme_colors(name))
    depth = display.prompt_color_depth()
    prompt_codes = {
        cls: _fg_code(_render_prompt(depth, [(f"class:bottom-toolbar class:{cls}", "x")], style))
        for cls in ("footer.ok", "footer.caution", "footer.alert", "footer.info")
    }
    assert len(set(prompt_codes.values())) == 4, prompt_codes


def test_set_theme_under_16_colours_pushes_the_ansi_tokens(active_profile):
    active_profile(ColorProfile.ANSI16)
    display.set_theme("nord")
    assert display.console.get_style("tool.ok").color.name == "green"
    assert display.themed_markdown("x").code_theme == "ansi_dark"


def test_ansi_code_theme_paints_fenced_code_on_a_named_slot_panel():
    """Regression: Rich's ansi_dark theme paints nothing, so code blended into prose at 16 colours."""
    theme = tokens.rich_theme(tokens.ANSI_PALETTES["ansi-dark"])
    markdown = display.ThemedMarkdown("Prose (x):\n\n```python\ndef f(x):\n    return x + 1\n```", code_theme="ansi_dark")
    out = _render_rich(ColorProfile.ANSI16, markdown, theme=theme)
    assert "return x + 1" in Text.from_ansi(out).plain
    assert _colour_kinds(out) == {"16"}
    assert set(_background_params(out)) == {100}  # bright_black panel
    code_lines = [line for line in out.splitlines() if "def" in Text.from_ansi(line).plain or "return" in line]
    assert code_lines and all(_unpainted_text(line) == "" for line in code_lines)
    prose = next(line for line in out.splitlines() if "Prose" in line)
    assert _background_params(prose) == []


def test_16_colour_completion_menu_paints_every_entry(active_profile):
    """Regression: surface_alt='default' left unselected entries unpainted over the scrollback."""
    active_profile(ColorProfile.ANSI16)
    style = main.build_prompt_style(display.theme_colors("tokyo-night"))
    fragments = [
        ("class:completion-menu.completion", " /theme "),
        ("class:completion-menu.completion.current", " /help "),
        ("class:completion-menu.multi-column-meta", " meta "),
    ]
    out = _render_prompt(display.prompt_color_depth(), fragments, style)
    assert _colour_kinds(out) == {"16"}
    assert _unpainted_text(out) == ""
    assert 44 in _background_params(out)  # ANSI blue menu surface
    assert 104 in _background_params(out)  # selected entry stands out on bright blue


def test_theme_command_explains_16_colour_rendering(active_profile, monkeypatch):
    active_profile(ColorProfile.ANSI16)
    cfg = Config()
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(main, "refresh_runtime_status", lambda *_a, **_k: None)
    notes: list[str] = []
    monkeypatch.setattr(main, "show_info", notes.append)

    main.handle_command("/theme dracula", cfg, None, None)  # type: ignore[arg-type]

    assert cfg.theme == "dracula"
    assert any("16-colour" in note and "ansi-dark" in note for note in notes)


# --- mono profile (NO_COLOR, TERM=dumb) ------------------------------------------------------

def test_mono_tokens_carry_attributes_only():
    for token, value in tokens.rich_styles(tokens.MONO).items():
        parsed = Style.parse(value)
        assert parsed.color is None and parsed.bgcolor is None, token
    style = PromptStyle.from_dict(tokens.prompt_toolkit_styles(tokens.MONO.colors()))
    for cls in tokens.prompt_toolkit_styles(tokens.MONO.colors()):
        attrs = style.get_attrs_for_style_str(f"class:{cls}")
        assert attrs.color == "" and attrs.bgcolor == "", cls
    assert style.get_attrs_for_style_str("class:footer.danger").reverse
    assert style.get_attrs_for_style_str("class:footer.warn").reverse
    assert style.get_attrs_for_style_str("class:footer.muted").dim


@pytest.mark.parametrize("name", THEMES)
def test_mono_profile_emits_no_colour_codes(active_profile, unsafe_footer, name):
    active_profile(ColorProfile.NONE)
    display.set_theme(name)
    theme = display._rich_theme_for(name, ColorProfile.NONE)
    table = Table("tool", "result")
    table.add_row(Text("run_shell", style="tool.name"), Text("exit 1", style="tool.fail"))
    sample = [
        Text("✓ ok", style="tool.ok"),
        Text("✗ failed", style="notice.error"),
        Text("thinking…", style="thinking"),
        Panel("approval", border_style="approval.border"),
        table,
        display.themed_markdown("# Heading\n\n`code` and **bold**\n\n```python\nx = 1\n```"),
    ]
    out = "".join(_render_rich(ColorProfile.NONE, item, theme=theme) for item in sample)
    assert _colour_kinds(out) == set()
    assert any(1 in params for params in _sgr_params(out))  # bold survives

    unsafe_footer.theme = name
    footer = _sticky_footer(unsafe_footer)
    assert _colour_kinds(footer) == set()
    assert any(7 in params for params in _sgr_params(footer))  # the "safe off" badge is reversed


# --- TERM=dumb on a real terminal ---------------------------------------------------------

def test_a_dumb_terminal_still_takes_the_detected_profile(monkeypatch):
    """Regression: Rich reports no colour system for TERM=dumb, which read as "piped" and fell back to truecolor."""
    monkeypatch.setenv("TERM", "dumb")
    tty = display.RuntimeConsole(file=io.StringIO(), force_terminal=True)
    assert tty.color_system is None
    assert display._terminal_takes_profile(tty) is True
    assert display._terminal_takes_profile(display.RuntimeConsole(file=io.StringIO(), force_terminal=False)) is False


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pseudo-terminal")
@pytest.mark.parametrize(("term", "profile", "depth"), [("dumb", "none", "DEPTH_1_BIT"), ("unknown", "ansi16", "DEPTH_4_BIT")])
def test_display_on_a_pty_uses_the_detected_profile(tmp_path, term, profile, depth):
    import pty

    # stdin and stdout are the pty (what Rich probes); the result goes to a stderr pipe.
    probe = (
        "import sys\n"
        "from algo_cli import display\n"
        "print(display.active_color_profile().value, display.prompt_color_depth().name, file=sys.stderr)\n"
    )
    env = {
        "TERM": term,
        "HOME": str(tmp_path),
        "ALGO_CLI_CONFIG_DIR": str(tmp_path / "config"),
        "PATH": os.environ.get("PATH", ""),
    }
    primary, secondary = pty.openpty()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe], stdin=secondary, stdout=secondary, stderr=subprocess.PIPE,
            env=env, cwd=str(tmp_path), timeout=60, check=False,
        )
    finally:
        os.close(secondary)
        os.close(primary)
    stderr = proc.stderr.decode(errors="replace")
    assert proc.returncode == 0, stderr
    assert stderr.strip().splitlines()[-1].split() == [profile, depth]
