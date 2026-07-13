"""Rich terminal display for the CLI."""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from rich.align import Align
from rich.console import Capture, Console
from rich.console import Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich import box
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from algo_cli.animations import (
    AIState,
    animation_for,
    buddy_frame,
    current_frame,
    glyph,
    register_spinners,
    spinner_name,
    state_line,
)

try:
    from algo_cli import __version__ as _CLI_VERSION
except Exception:
    _CLI_VERSION = "dev"

try:
    from algo_cli.config import CONFIG_DIR, _atomic_write_text
except Exception:
    CONFIG_DIR = Path.home() / ".algo_cli"
    _atomic_write_text = None  # type: ignore[assignment]


DEFAULT_THEME_NAME = (
    os.environ.get("ALGO_CLI_THEME")
    or os.environ.get("OLLAMA_CLI_THEME")
    or "tokyo-night"
).strip().lower() or "tokyo-night"


def _theme(styles: dict[str, str]) -> Theme:
    return Theme(styles)


THEME_COLORS: dict[str, dict[str, str]] = {
    "tokyo-night": {
        "primary": "#7aa2f7",
        "secondary": "#bb9af7",
        "accent": "#2ac3de",
        "surface": "#111827",
        "surface_alt": "#0f172a",
        "border": "#1f2a44",
        "border_accent": "#3b4b72",
        "text": "#e5e9f0",
        "muted": "#7b88a8",
        "success": "#9ece6a",
        "warning": "#e0af68",
        "error": "#f7768e",
        "info": "#7dcfff",
    },
    "catppuccin-mocha": {
        "primary": "#89b4fa",
        "secondary": "#cba6f7",
        "accent": "#94e2d5",
        "surface": "#11111b",
        "surface_alt": "#1e1e2e",
        "border": "#313244",
        "border_accent": "#45475a",
        "text": "#cdd6f4",
        "muted": "#6c7086",
        "success": "#a6e3a1",
        "warning": "#f9e2af",
        "error": "#f38ba8",
        "info": "#89dceb",
    },
    "dracula": {
        "primary": "#bd93f9",
        "secondary": "#ff79c6",
        "accent": "#8be9fd",
        "surface": "#282a36",
        "surface_alt": "#1e1f29",
        "border": "#44475a",
        "border_accent": "#6272a4",
        "text": "#f8f8f2",
        "muted": "#6272a4",
        "success": "#50fa7b",
        "warning": "#f1fa8c",
        "error": "#ff5555",
        "info": "#8be9fd",
    },
    "nord": {
        "primary": "#88c0d0",
        "secondary": "#b48ead",
        "accent": "#81a1c1",
        "surface": "#2e3440",
        "surface_alt": "#3b4252",
        "border": "#4c566a",
        "border_accent": "#5e81ac",
        "text": "#eceff4",
        "muted": "#81a1c1",
        "success": "#a3be8c",
        "warning": "#ebcb8b",
        "error": "#bf616a",
        "info": "#8fbcbb",
    },
    "gruvbox": {
        "primary": "#83a598",
        "secondary": "#d3869b",
        "accent": "#8ec07c",
        "surface": "#282828",
        "surface_alt": "#3c3836",
        "border": "#504945",
        "border_accent": "#665c54",
        "text": "#ebdbb2",
        "muted": "#928374",
        "success": "#b8bb26",
        "warning": "#fabd2f",
        "error": "#fb4934",
        "info": "#83a598",
    },
    # Brand theme matching the Redeye mark: bold red on near-black,
    # high contrast, minimal mid-tones.
    "redeye": {
        "primary": "#e22b2b",
        "secondary": "#ff6b6b",
        "accent": "#ff3b3b",
        "surface": "#0a0a0a",
        "surface_alt": "#111111",
        "border": "#2a1212",
        "border_accent": "#7a1f1f",
        "text": "#f2f2f2",
        "muted": "#8a7070",
        "success": "#6fcf6f",
        "warning": "#ffb347",
        "error": "#ff2e2e",
        "info": "#ff8a8a",
    },
    "dolphie": {
        "primary": "#bbc8e8",
        "secondary": "#91abec",
        "accent": "#8f9fc1",
        "surface": "#0f1525",
        "surface_alt": "#0a0e1b",
        "border": "#1b233a",
        "border_accent": "#32416a",
        "text": "#e9e9e9",
        "muted": "#5e6b87",
        "success": "#54efae",
        "warning": "#f0e357",
        "error": "#f05757",
        "info": "#8f9fc1",
    },
}

THEME_MAP: dict[str, Theme] = {name: _theme(colors) for name, colors in THEME_COLORS.items()}

_base_theme_name = DEFAULT_THEME_NAME if DEFAULT_THEME_NAME in THEME_MAP else "tokyo-night"
_active_theme_name = _base_theme_name
_theme_pushed = False
console = Console(theme=THEME_MAP[_base_theme_name])
register_spinners()  # make algo-* state spinners available to console.status
_stream_live: Live | None = None
_stream_buffer = ""
_stream_pending = ""
_last_render_time = 0.0
_RENDER_INTERVAL = 1.0 / 12  # cap Markdown re-parses at 12 fps regardless of token rate
_thinking_live: Live | None = None
_thinking_buffer = ""
_thinking_pending = ""
_thinking_last_render_time = 0.0
_thinking_started_at = 0.0
_THINKING_VISIBLE_CHARS = 1200

# One-shot JSON event sink. When set, display helpers route to it instead of
# rendering Rich panels. Installed by algo_cli.oneshot.run_oneshot().
_json_sink: Any = None
_console_capture_active: ContextVar[bool] = ContextVar(
    "algo_cli_console_capture_active",
    default=False,
)


def install_json_sink(sink: Any) -> None:
    global _json_sink
    _json_sink = sink


def uninstall_json_sink() -> None:
    global _json_sink
    _json_sink = None


def json_sink() -> Any:
    return _json_sink


def json_mode_active() -> bool:
    """True when --oneshot --json (or another bridge) owns stdout."""
    return _json_sink is not None


@contextmanager
def capture_console_output() -> Iterator[Capture]:
    """Capture this context's Rich output, including JSON-suppressed notices.

    Rich stores capture buffers per thread, and the ContextVar keeps async/task
    contexts isolated. Direct interactive rendering is therefore unaffected.
    """

    token = _console_capture_active.set(True)
    try:
        with console.capture() as captured:
            yield captured
    finally:
        _console_capture_active.reset(token)


@contextmanager
def tool_execution_status(label: str, *, spinner: str | None = None) -> Iterator[None]:
    """Rich spinner for interactive mode only; no-op for JSON/bridge consumers.

    Defaults to the branded DOING progress sweep; pass any rich spinner name
    to override.
    """
    if _json_sink is not None:
        yield
        return
    doing = animation_for(AIState.DOING)
    with console.status(label, spinner=spinner or spinner_name(AIState.DOING), spinner_style=doing.style):
        yield


def available_themes() -> list[str]:
    return sorted(THEME_MAP)


def current_theme_name() -> str:
    return _active_theme_name


def set_theme(name: str) -> str:
    global _active_theme_name, _theme_pushed

    candidate = (name or "").strip().lower().replace("_", "-")
    if not candidate:
        candidate = "tokyo-night"
    if candidate not in THEME_MAP:
        raise ValueError(f"Unknown theme: {name}")
    if candidate == _active_theme_name:
        return candidate
    if _theme_pushed:
        console.pop_theme()
        _theme_pushed = False
    if candidate != _base_theme_name:
        console.push_theme(THEME_MAP[candidate])
        _theme_pushed = True
    _active_theme_name = candidate
    return candidate


def theme_colors(name: str | None = None) -> dict[str, str]:
    candidate = (name or _active_theme_name or _base_theme_name).strip().lower().replace("_", "-")
    if candidate not in THEME_COLORS:
        candidate = _base_theme_name
    return THEME_COLORS[candidate]


# Block logo (ansi_shadow-style), matching Hermes Agent banner weight.
_ALGO_CLI_LOGO: tuple[str, ...] = (
    " █████╗ ██╗      ██████╗  ██████╗      ██████╗██╗     ██╗",
    "██╔══██╗██║     ██╔════╝ ██╔═══██╗    ██╔════╝██║     ██║",
    "███████║██║     ██║  ███╗██║   ██║    ██║     ██║     ██║",
    "██╔══██║██║     ██║   ██║██║   ██║    ██║     ██║     ██║",
    "██║  ██║███████╗╚██████╔╝╚██████╔╝    ╚██████╗███████╗██║",
    "╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝      ╚═════╝╚══════╝╚═╝",
)

_PRODUCT_TAGLINE = "Agent runtime for tools, durable context, and verified work."
_BANNER_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("Inference", "Ollama local/cloud · xAI Grok · ChatGPT/Codex"),
    ("Context", "skills · memory · repository intelligence · knowledge graph"),
    ("Workflows", "direct chat · routed agent pipelines · one-shot JSON"),
)


def _banner_flow_line() -> Text:
    line = Text()
    for index, (label, style) in enumerate((
        ("understand", "secondary"),
        ("route", "primary"),
        ("act", "accent"),
        ("verify", "success"),
        ("remember", "secondary"),
    )):
        if index:
            line.append("  →  ", style="muted")
        line.append(label, style=f"bold {style}")
    return line


def _banner_body_layout() -> Group:
    capabilities = Table.grid(padding=(0, 2), expand=True)
    capabilities.add_column(width=10, style="muted", no_wrap=True)
    capabilities.add_column(ratio=1, style="text")
    for label, detail in _BANNER_CAPABILITIES:
        capabilities.add_row(Text(label, style="bold secondary"), detail)
    return Group(
        Text(_PRODUCT_TAGLINE, style="text"),
        Text(""),
        _banner_flow_line(),
        Text(""),
        capabilities,
    )


def _print_algo_logo() -> None:
    if console.width < 70:
        console.print(Align.center(Text.assemble(
            ("ALGO", "bold primary"),
            (" / ", "muted"),
            ("CLI", "bold secondary"),
        )))
        return
    for line in _ALGO_CLI_LOGO:
        console.print(Align.center(Text(line.rstrip(), style="bold primary")))


def render_opening_banner(*, version: str | None = None) -> Panel:
    version_line = version or _CLI_VERSION
    panel_title = (
        f"[bold primary]Algo CLI[/] [muted]v{version_line}[/] "
        f"[muted]·[/] [accent]agent runtime[/]"
    )
    return Panel(
        _banner_body_layout(),
        title=panel_title,
        subtitle="[muted]ask naturally  ·  type / for commands[/]",
        border_style="border_accent",
        box=box.ROUNDED,
        padding=(1, 2),
    )


def show_banner() -> None:
    if json_mode_active():
        return
    console.print()
    _print_algo_logo()
    console.print()

    console.print(render_opening_banner())
    console.print(state_line(AIState.IDLE, "ready — ask naturally or type /"))
    console.print()


def compact_path(path: str, limit: int = 42) -> str:
    text = str(path).strip()
    if len(text) <= limit:
        return text
    p = Path(text)
    parts = p.parts
    if len(parts) >= 2:
        tail = Path(*parts[-2:]).as_posix().replace("/", "\\") if "\\" in text else Path(*parts[-2:]).as_posix()
        if len(tail) + 1 < limit:
            return f"...{tail}"
    return f"{text[: max(1, limit - 3)]}..."


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(style="muted", no_wrap=True)
    table.add_column(style="text", overflow="ellipsis")
    for label, value in rows:
        table.add_row(f"{label}:", value)
    return table


def _format_bytes(value: Any) -> str:
    try:
        size = int(value)
    except Exception:
        return str(value) if value is not None else "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{size} B"


def _format_dt(value: Any) -> str:
    if not value:
        return "?"
    try:
        return value.strftime("%Y-%m-%d")
    except Exception:
        return str(value)


def _message_preview(message: dict[str, Any], limit: int = 220) -> str:
    content = str(message.get("content") or message.get("thinking") or "").strip()
    if not content:
        return "(empty)"
    content = " ".join(content.split())
    return content if len(content) <= limit else content[: limit - 1] + "..."


def _render_model_rows(items: list[dict[str, str]], *, max_rows: int = 4) -> Table:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(style="text", ratio=2, overflow="ellipsis")
    table.add_column(style="text", justify="right", no_wrap=True)
    table.add_column(style="text", justify="right", no_wrap=True)
    for row in items[:max_rows]:
        table.add_row(
            row.get("name", "?"),
            row.get("size", "?"),
            row.get("quant", "?"),
        )
    if not items:
        table.add_row("[muted]No models found[/]", "", "")
    return table


def _render_running_rows(items: list[dict[str, str]], *, max_rows: int = 3) -> Table:
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(style="text", ratio=2, overflow="ellipsis")
    table.add_column(style="text", justify="right", no_wrap=True)
    table.add_column(style="text", justify="right", no_wrap=True)
    for row in items[:max_rows]:
        table.add_row(
            row.get("name", "?"),
            row.get("size_vram", row.get("size", "?")),
            row.get("context", "?"),
        )
    if not items:
        table.add_row("[muted]No running models[/]", "", "")
    return table


def _render_chat_panel(cfg: Any) -> Panel:
    recent = cfg.messages[-3:]
    blocks: list[Any] = []
    if not recent:
        blocks.append(
            Panel(
                "[muted]No messages yet.[/]\n[dim]Type a prompt to start the session.[/]",
                border_style="border",
                box=box.ROUNDED,
            )
        )
    else:
        for message in recent:
            role = str(message.get("role", "message")).upper()
            title = role
            if message.get("name"):
                title = f"{role} {message['name']}"
            border = "border_accent" if role == "ASSISTANT" else "border"
            blocks.append(
                Panel(
                    _message_preview(message),
                    title=title,
                    border_style=border,
                    box=box.ROUNDED,
                )
            )

    num_ctx = getattr(cfg, "num_ctx", None)
    ctx_label = str(num_ctx) if num_ctx is not None else "?"
    hint_row = Text(
        f"Tab complete   Enter send   Ctrl+C stop   slash commands   temp {cfg.temperature}   ctx {ctx_label}",
        style="muted",
    )
    blocks.append(hint_row)
    blocks.append(
        Panel(
            Text("draft: ready for a prompt or slash command", style="muted"),
            border_style="border",
            box=box.ROUNDED,
        )
    )
    return Panel(Group(*blocks), title="Conversation", border_style="border_accent", box=box.ROUNDED)


def _render_inspector_panel(
    cfg: Any,
    *,
    installed_models: list[dict[str, str]],
    running_models: list[dict[str, str]],
    event_lines: list[str],
) -> Panel:
    sections: list[Any] = []

    session_box = _kv_table(
        [
            ("Current model", f"[bold primary]{cfg.model}[/]"),
            ("Mode", f"[text]{'cloud' if cfg.cloud else 'local'}[/]"),
            ("System prompt", f"[text]{(cfg.system.splitlines()[0] if cfg.system else '').strip() or 'default'}[/]"),
            ("Parameters", f"[text]temp {cfg.temperature}, ctx {cfg.num_ctx}, reflect {max(1, int(cfg.tool_think_every))}[/]"),
            ("cwd", f"[muted]{compact_path(cfg.cwd, 34)}[/]"),
        ]
    )
    sections.append(Panel(session_box, title="Session", border_style="border", box=box.ROUNDED))

    model_body = _render_model_rows(installed_models)
    sections.append(Panel(model_body, title=f"Model  {len(installed_models)} installed", border_style="border_accent", box=box.ROUNDED))

    runtime_body = Table.grid(padding=(0, 1), expand=True)
    runtime_body.add_column(style="text", ratio=2, overflow="ellipsis")
    runtime_body.add_column(style="text", justify="right", no_wrap=True)
    runtime_body.add_column(style="text", justify="right", no_wrap=True)
    runtime_body.add_row("[bold]Server[/]", f"[info]{cfg.host}[/]", f"[text]{'cloud' if cfg.cloud else 'local'}[/]")
    runtime_body.add_row("[bold]Context[/]", f"[text]{cfg.num_ctx}[/]", f"[text]{'summary' if cfg.session_summary.strip() else 'live'}[/]")
    runtime_body.add_row("[bold]Running[/]", f"[text]{len(running_models)}[/]", f"[text]{'ready' if running_models else 'idle'}[/]")
    if running_models:
        runtime_body.add_row("", "", "")
        runtime_body.add_row("[muted]Active[/]", f"[text]{running_models[0].get('name', '?')}[/]", f"[text]{running_models[0].get('size_vram', running_models[0].get('size', '?'))}[/]")
    sections.append(Panel(runtime_body, title=f"Runtime  {len(running_models)} running", border_style="border", box=box.ROUNDED))

    actions_body = Text(
        "/status          Runtime summary\n"
        "/doctor          Readiness checks\n"
        "/harness status  Context health\n"
        "/changes         Last agent run\n"
        "/help            Command reference",
        style="text",
    )
    sections.append(Panel(actions_body, title="Inspect", border_style="border", box=box.ROUNDED))

    logs_body = Table.grid(padding=(0, 1), expand=True)
    logs_body.add_column(style="muted", no_wrap=True)
    logs_body.add_column(style="text", overflow="ellipsis")
    if event_lines:
        for index, line in enumerate(event_lines[:5], 1):
            logs_body.add_row(f"{index:02d}", line)
    else:
        logs_body.add_row("01", "No events yet")
    sections.append(Panel(logs_body, title="Logs", border_style="border", box=box.ROUNDED))

    return Panel(Group(*sections), title="Runtime", border_style="border", box=box.ROUNDED)


def show_session_overview(
    model: str,
    host: str,
    cwd: str,
    theme_name: str,
    cloud: bool,
    auto_mode: bool,
    safe_mode: bool,
    temperature: float,
    used_tokens: int,
    total_tokens: int,
    summary_active: bool,
    tool_think_every: int,
    max_tool_iterations: int,
    memory_count: int,
    provider_mode: str | None = None,
    system_prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    installed_models: list[dict[str, str]] | None = None,
    running_models: list[dict[str, str]] | None = None,
    event_lines: list[str] | None = None,
) -> None:
    used = min(max(used_tokens, 0), total_tokens) if total_tokens > 0 else used_tokens
    remaining = max(total_tokens - used, 0) if total_tokens > 0 else 0
    context_line = f"{used}/{total_tokens} ({int(round((remaining / total_tokens) * 100))}% left)" if total_tokens > 0 else "unknown"
    mode_label = provider_mode or ("cloud" if cloud else "local")
    execution_label = "Ollama Cloud API" if mode_label == "cloud" else host
    installed_models = installed_models or []
    running_models = running_models or []
    event_lines = event_lines or []

    if console.width < 100:
        header_status: Any = _kv_table(
            [
                ("Connected", f"[text]{execution_label}[/]"),
                ("Model", f"[bold primary]{model}[/]"),
                ("Mode", f"[info]{mode_label}[/]"),
                ("Context", f"[text]{context_line}[/]"),
                ("Safety", f"[text]{'safe' if safe_mode else 'safe off'} · {'manual approval' if not auto_mode else 'auto approval'}[/]"),
                ("Memory", f"[text]{memory_count} saved · {'summary active' if summary_active else 'live context'}[/]"),
                ("Theme", f"[secondary]{theme_name}[/]"),
            ]
        )
    else:
        header_status = Table.grid(expand=True)
        header_status.add_column(ratio=2)
        header_status.add_column(ratio=3)
        header_status.add_column(ratio=2)
        header_status.add_row(
            Panel(
                _kv_table(
                    [
                        ("Connected", f"[text]{execution_label}[/]"),
                        ("Session", f"[text]{model}[/]"),
                    ]
                ),
                border_style="border",
                box=box.ROUNDED,
            ),
            Panel(
                _kv_table(
                    [
                        ("Mode", f"[info]{mode_label}[/]"),
                        ("Context", f"[text]{context_line}[/]"),
                        ("Theme", f"[secondary]{theme_name}[/]"),
                    ]
                ),
                border_style="border",
                box=box.ROUNDED,
            ),
            Panel(
                _kv_table(
                    [
                        ("Auto", f"[text]{'on' if auto_mode else 'off'}[/]"),
                        ("Safe", f"[text]{'on' if safe_mode else 'off'}[/]"),
                        ("Memories", f"[text]{memory_count}[/]"),
                    ]
                ),
                border_style="border",
                box=box.ROUNDED,
            ),
        )
    header = Panel(
        Group(
            Text("Runtime Overview", style="bold primary"),
            Text("Model, context, safety, and agent activity at a glance.", style="muted"),
            header_status,
        ),
        border_style="border_accent",
        box=box.ROUNDED,
        padding=(1, 2),
    )

    chat_cfg = type(
        "_ChatCfg",
        (),
        {
            "messages": messages or [],
            "temperature": temperature,
            "num_ctx": total_tokens if total_tokens > 0 else None,
        },
    )()
    inspector_cfg = type(
        "_InspectorCfg",
        (),
        {
            "model": model,
            "cloud": cloud,
            "system": system_prompt or "",
            "temperature": temperature,
            "num_ctx": total_tokens,
            "tool_think_every": tool_think_every,
            "cwd": cwd,
            "host": host,
            "session_summary": "active" if summary_active else "",
        },
    )()

    left = _render_chat_panel(chat_cfg)
    inspector = _render_inspector_panel(
        inspector_cfg,
        installed_models=installed_models,
        running_models=running_models,
        event_lines=event_lines or [
            f"mode {mode_label}",
            f"context {context_line}",
            f"tool max {max_tool_iterations}",
            f"reflect every {tool_think_every} calls",
        ],
    )

    layout = Table.grid(expand=True)
    if console.width < 110:
        layout.add_column(ratio=1)
        layout.add_row(left)
        layout.add_row(inspector)
    else:
        layout.add_column(ratio=3)
        layout.add_column(ratio=2)
        layout.add_row(left, inspector)

    console.print(header)
    console.print(layout)
    console.print(
        Panel(
            "[bold primary]Work[/]: /agent TASK  /route TASK  /changes  /diff\n"
            "[bold primary]Git[/]: /worktree status  /ship status\n"
            "[bold primary]Context[/]: /status  /context  /harness  /hsearch  /memories\n"
            "[bold primary]Safety[/]: /doctor  /safe  /auto  /policy\n"
            "[bold primary]Setup[/]: /model  /models  /theme  /reload  /actions",
            title="Quick Actions",
            border_style="border",
            box=box.ROUNDED,
        )
    )
    console.print()


def _short_value(value: Any, limit: int = 80) -> str:
    text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def redact_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Remove secret-bearing values before display, JSON events, or audit previews."""

    sensitive_keys = {"api_key", "access_token", "refresh_token", "password", "secret", "token"}
    if name == "credential_helpers_store":
        sensitive_keys.add("value")
    return {
        key: "<redacted>" if key.lower() in sensitive_keys else value
        for key, value in args.items()
    }


def show_tool_call(name: str, args: dict, *, call_id: str | None = None) -> None:
    visible_args = redact_tool_args(
        name,
        {key: value for key, value in args.items() if key not in {"cwd", "safe_mode"}},
    )
    if _json_sink is not None:
        cid = call_id or _json_sink.next_call_id()
        _json_sink.tool_call(call_id=cid, name=name, args=visible_args)
        return
    rendered = " ".join(f"[secondary]{key}[/]={_short_value(value)}" for key, value in visible_args.items())
    suffix = f" {rendered}" if rendered else ""
    # DOING glyph (bolt) marks tool dispatch — matches the buddy's strike animation.
    console.print(f"[accent]{glyph(AIState.DOING).plain}[/] [bold]{name}[/]{suffix}", highlight=False)


def show_tool_result(
    name: str,
    result: str,
    approved: bool = True,
    duration_ms: float | None = None,
    *,
    call_id: str | None = None,
) -> None:
    if _json_sink is not None:
        if not approved and str(result).strip().lower().startswith("user denied"):
            _json_sink.tool_denied(call_id=call_id, name=name, reason="approval-mode never; tool requires approval")
        else:
            _json_sink.tool_result(call_id=call_id, name=name, result=result, duration_ms=duration_ms)
        return
    status = "[success]OK[/]" if approved else "[error]ERR[/]"
    lines = str(result).splitlines()
    byte_count = len(str(result).encode("utf-8", errors="replace"))
    duration = f"  {duration_ms:.0f}ms" if duration_ms is not None else ""
    console.print(f"{status} [bold]{name}[/]{duration}  {_format_bytes(byte_count)}  {len(lines)} lines")
    preview = lines[:5] or [str(result)[:160]]
    for line in preview:
        console.print(f"  [muted]{_short_value(line, 180)}[/]", highlight=False)
    if len(lines) > 5:
        console.print(f"  [muted]... {len(lines) - 5} more lines[/]")


def show_recalled_context(blocks: list[dict[str, Any]]) -> None:
    if not blocks:
        return
    if _json_sink is not None:
        # Internal RAG detail — bridge consumers don't need this in the event stream.
        return
    lines: list[str] = []
    for block in blocks[:5]:
        block_type = str(block.get("type", "note")).upper()
        block_id = str(block.get("id", "?"))
        score = float(block.get("score", 0.0) or 0.0)
        content = _short_value(block.get("content", ""), 180)
        lines.append(f"[bold secondary][{block_type}][/bold secondary] [muted]{block_id}[/] [info]({score:.2f})[/]")
        if content:
            lines.append(f"  [text]{content}[/]")
    if len(blocks) > 5:
        lines.append(f"[muted]... {len(blocks) - 5} more recalled block(s)[/]")
    console.print(
        Panel(
            "\n".join(lines),
            title=f"Recalled - {len(blocks)} block{'s' if len(blocks) != 1 else ''}",
            border_style="border",
            box=box.ROUNDED,
        )
    )


_DEFAULT_AGENT_PREVIEW_CHARS = 900
_AGENT_SECTION_HEADER = re.compile(
    r"^(?:#{1,3}\s*)?"
    r"(Assumptions|Risks|Concrete\s+Next\s+Steps|Next\s+Steps)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)
_AGENT_BLOCK_OUTPUT_HEADER = re.compile(r"^#+\s*block\s+output\s*$", re.IGNORECASE)


def _agent_preview_char_limit() -> int | None:
    """None means show full output (ALGO_CLI_AGENT_PREVIEW=0 or 'full')."""
    raw = os.environ.get("ALGO_CLI_AGENT_PREVIEW", "").strip().lower()
    if raw in {"", "default"}:
        return _DEFAULT_AGENT_PREVIEW_CHARS
    if raw in {"0", "full", "none", "off"}:
        return None
    try:
        value = int(raw)
        return None if value <= 0 else value
    except ValueError:
        return _DEFAULT_AGENT_PREVIEW_CHARS


def _agent_block_border_style(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized == "complete":
        return "success"
    if normalized == "partial":
        return "warning"
    if normalized in {"failed", "error", "cancelled"}:
        return "error"
    return "border"


def _strip_block_output_header(text: str) -> str:
    lines = text.splitlines()
    if lines and _AGENT_BLOCK_OUTPUT_HEADER.match(lines[0].strip()):
        return "\n".join(lines[1:]).strip()
    return text.strip()


def _lines_to_bullets(lines: list[str]) -> list[str]:
    bullets: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^[-*•]\s+", "", stripped)
        stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
        if stripped:
            bullets.append(stripped)
    return bullets


def _parse_block_output_sections(text: str) -> dict[str, list[str]]:
    """Parse Assumptions / Risks / Next Steps style sections into bullet lists."""
    body = _strip_block_output_header(text)
    if not body:
        return {}
    sections: dict[str, list[str]] = {}
    current_label = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, current_label
        if not current_label:
            return
        bullets = _lines_to_bullets(buffer)
        if bullets:
            key = current_label
            if key in sections:
                sections[key].extend(bullets)
            else:
                sections[key] = bullets
        buffer = []

    for line in body.splitlines():
        header_match = _AGENT_SECTION_HEADER.match(line.strip())
        if header_match:
            flush()
            label = header_match.group(1).lower()
            if "concrete" in label or label == "next steps":
                current_label = "Next steps"
            elif label.startswith("risk"):
                current_label = "Risks"
            else:
                current_label = "Assumptions"
            continue
        if current_label:
            buffer.append(line)
        else:
            buffer.append(line)

    flush()
    if not sections and buffer:
        lead = _lines_to_bullets(buffer)
        if lead:
            sections["Summary"] = lead[:12]
    return sections


def _write_agent_block_dump(role: str, output: str) -> Path | None:
    text = (output or "").strip()
    if not text:
        return None
    safe_role = re.sub(r"[^a-zA-Z0-9_-]+", "-", (role or "block").strip()) or "block"
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / f"last-block-{safe_role}.md"
        text_with_eol = text + ("\n" if not text.endswith("\n") else "")
        if _atomic_write_text is not None:
            _atomic_write_text(path, text_with_eol)
        else:
            path.write_text(text_with_eol, encoding="utf-8")
        return path
    except OSError:
        return None


def _agent_block_title(
    role: str,
    *,
    duration_ms: float,
    tool_calls: int,
    status: str,
    status_code: str = "",
    model: str = "",
    write_count: int = 0,
) -> str:
    duration_s = duration_ms / 1000.0
    calls_label = "tool call" if tool_calls == 1 else "tool calls"
    parts = [f"Agent · {role}", f"{duration_s:.1f}s", f"{tool_calls} {calls_label}", status]
    if status_code:
        parts.insert(-1, status_code)
    if model:
        parts.append(model)
    if write_count > 0:
        parts.append(f"writes:{write_count}")
    return " — ".join(parts)


def _render_structured_sections(sections: dict[str, list[str]]) -> Group:
    blocks: list[Any] = []
    for title, bullets in sections.items():
        blocks.append(Text(title, style="bold primary"))
        for item in bullets[:16]:
            blocks.append(Text(f"  • {item}", style="text"))
        if len(bullets) > 16:
            blocks.append(Text(f"  [muted]… {len(bullets) - 16} more[/]", style="muted"))
        blocks.append(Text(""))
    return Group(*blocks)


def _render_agent_block_body(
    role: str,
    output: str,
    *,
    preview_limit: int | None,
) -> tuple[Any, Path | None]:
    text = (output or "").strip() or "(no output produced)"
    dump_path = _write_agent_block_dump(role, text if text != "(no output produced)" else "")

    sections = _parse_block_output_sections(text)
    use_structured = role == "plan" or len(sections) >= 2

    if use_structured and sections:
        body: Any = _render_structured_sections(sections)
        remainder = _strip_block_output_header(text)
        if preview_limit is not None and len(remainder) > preview_limit:
            body = Group(
                body,
                Text(
                    f"[muted]Full text ({len(remainder)} chars) — see file or raise ALGO_CLI_AGENT_PREVIEW[/]",
                    style="muted",
                ),
            )
        return body, dump_path

    if preview_limit is None:
        return Markdown(text), dump_path
    if len(text) <= preview_limit:
        return Markdown(text), dump_path
    preview = _short_value(text, preview_limit)
    return (
        Group(
            Markdown(preview),
            Text(
                f"[muted]Preview ({preview_limit} chars). Set ALGO_CLI_AGENT_PREVIEW=0 for full panel text.[/]",
                style="muted",
            ),
        ),
        dump_path,
    )


def show_agent_block_start(
    role: str,
    model: str,
    tool_count: int,
    policy_summary: str = "",
    *,
    policy_enforced: bool = False,
    cwd: str = "",
) -> None:
    if _json_sink is not None:
        return
    label = "Enforced policy" if policy_enforced else "Advisory policy"
    policy_line = f"\n[text]{label}:[/] {policy_summary}" if policy_summary else ""
    cwd_line = f"\n[text]Workspace:[/] [muted]{compact_path(cwd, 72)}[/]" if cwd else ""
    console.print(
        Panel(
            f"[text]Model:[/] [primary]{model}[/]\n[text]Runtime tools:[/] {tool_count}{cwd_line}{policy_line}",
            title=f"Agent · {role}",
            border_style="border_accent",
            box=box.ROUNDED,
        )
    )


def show_agent_block_complete(
    role: str,
    output: str,
    *,
    duration_ms: float,
    tool_calls: int,
    status: str,
    status_reason: str = "",
    verification_warning: str = "",
    status_code: str = "",
    model: str = "",
    policy_summary: str = "",
    successful_writes: list[str] | None = None,
) -> None:
    if _json_sink is not None:
        return
    preview_limit = _agent_preview_char_limit()
    body_renderable, dump_path = _render_agent_block_body(
        role, output, preview_limit=preview_limit
    )
    writes = successful_writes or []
    write_count = len(writes)

    header_lines: list[str] = []
    status_style = _agent_block_border_style(status)
    header_lines.append(f"[{status_style}]Status: {status.upper()}[/]")
    if status_code:
        header_lines.append(f"[muted]Code:[/] {status_code}")
    if status_reason:
        header_lines.append(f"[text]{_short_value(status_reason, 220)}[/]")
    if verification_warning and not status_reason:
        header_lines.append(
            f"[warning]Verification warning:[/] {_short_value(verification_warning, 200)}"
        )

    footer_parts = [
        f"[muted]{tool_calls} tool call{'s' if tool_calls != 1 else ''}[/]",
        f"[muted]{duration_ms / 1000:.1f}s[/]",
    ]
    if policy_summary:
        footer_parts.append(f"[muted]{_short_value(policy_summary, 120)}[/]")
    if dump_path is not None:
        footer_parts.append(f"[info]Full output:[/] [accent]{dump_path}[/]")
    if write_count:
        footer_parts.append(f"[success]writes: {write_count}[/]")

    header_renderable = (
        Text.from_markup("\n".join(header_lines)) if header_lines else Text("")
    )
    footer_renderable = Text.from_markup("  ·  ".join(footer_parts)) if footer_parts else Text("")
    panel_body = Group(
        header_renderable,
        Text(""),
        body_renderable,
        Text(""),
        footer_renderable,
    )
    if verification_warning and status_reason:
        panel_body = Group(
            panel_body,
            Text(""),
            Text.from_markup(
                f"[warning]Verification:[/] {_short_value(verification_warning, 200)}"
            ),
        )

    console.print(
        Panel(
            panel_body,
            title=_agent_block_title(
                role,
                duration_ms=duration_ms,
                tool_calls=tool_calls,
                status=status,
                status_code=status_code,
                model=model,
                write_count=write_count,
            ),
            border_style=status_style,
            box=box.ROUNDED,
        )
    )


def show_agent_recovery_start(role: str, reason: str, retry_iterations: int) -> None:
    console.print(
        Panel(
            Markdown(
                f"**Reason:** {reason}\n\n"
                f"**Recovery:** one tool-free replan, then one focused retry "
                f"with at most {retry_iterations} iterations."
            ),
            title=f"Recovery - {role} retry",
            border_style="warning",
            box=box.ROUNDED,
        )
    )


def show_agent_pipeline_complete(output: str, *, block_count: int, duration_ms: float) -> None:
    console.print(
        Panel(
            Markdown(output.strip() or "(no final output produced)"),
            title=f"Pipeline complete - {block_count} blocks - {duration_ms / 1000:.1f}s",
            border_style="success",
            box=box.ROUNDED,
        )
    )


def _estimated_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _thinking_renderable(text: str, *, final: bool = False) -> Panel:
    visible = text[:_THINKING_VISIBLE_CHARS]
    token_count = _estimated_tokens(text)
    truncated = len(text) > _THINKING_VISIBLE_CHARS
    if truncated:
        visible = visible.rstrip() + f"\n\n[muted]... truncated, ~{token_count} tokens total[/]"
    elapsed = time.monotonic() - _thinking_started_at if _thinking_started_at else 0.0
    # Non-final title carries a time-derived frame: each Live update (token
    # batches arrive faster than the 90ms frame interval) advances the pulse,
    # so the panel animates without any extra timer.
    title = (
        f"Thinking - {elapsed:.1f}s - ~{token_count} tokens"
        if final
        else f"{current_frame(AIState.THINKING)} Thinking..."
    )
    return Panel(
        Text(visible or "Thinking...", style="secondary italic"),
        title=title,
        border_style="border",
        box=box.ROUNDED,
    )


def start_thinking_block() -> None:
    global _thinking_live, _thinking_buffer, _thinking_pending, _thinking_last_render_time, _thinking_started_at
    if _json_sink is not None:
        return
    if _thinking_live is not None:
        return
    finish_streaming_response()
    _thinking_buffer = ""
    _thinking_pending = ""
    _thinking_last_render_time = 0.0
    _thinking_started_at = time.monotonic()
    _thinking_live = Live(_thinking_renderable(""), console=console, auto_refresh=False, transient=False)
    _thinking_live.start()
    _thinking_live.refresh()


def show_thinking_text(text: str) -> None:
    global _thinking_buffer, _thinking_pending, _thinking_last_render_time
    if not text:
        return
    if _json_sink is not None:
        _json_sink.thinking(text)
        return
    if _thinking_live is None:
        start_thinking_block()
    _thinking_pending += text
    now = time.monotonic()
    if _thinking_live is not None and now - _thinking_last_render_time >= _RENDER_INTERVAL:
        _thinking_buffer += _thinking_pending
        _thinking_pending = ""
        _thinking_last_render_time = now
        _thinking_live.update(_thinking_renderable(_thinking_buffer), refresh=True)


def finish_thinking_block() -> None:
    global _thinking_live, _thinking_buffer, _thinking_pending, _thinking_started_at
    if _json_sink is not None:
        return
    if _thinking_live is not None:
        if _thinking_pending:
            _thinking_buffer += _thinking_pending
            _thinking_pending = ""
        _thinking_live.update(_thinking_renderable(_thinking_buffer, final=True), refresh=True)
        _thinking_live.stop()
        _thinking_live = None
        _thinking_buffer = ""
        _thinking_started_at = 0.0


def show_response(text: str) -> None:
    if _json_sink is not None:
        _json_sink.content(text)
        return
    if text.strip():
        console.print(Markdown(text))
        console.print()


def start_streaming_response() -> None:
    global _stream_live, _stream_buffer, _stream_pending, _last_render_time
    if _json_sink is not None:
        return
    finish_streaming_response()
    # Answering transition: a left-anchored eye+chevron rule separates the
    # response from tool chatter, so the state flip reads instantly.
    rule_title = Text.assemble(
        glyph(AIState.IDLE), (" ", ""), glyph(AIState.ANSWERING), (" answering", "primary")
    )
    console.rule(rule_title, style="border", align="left")
    _stream_buffer = ""
    _stream_pending = ""
    _last_render_time = 0.0
    _stream_live = Live(Markdown(""), console=console, auto_refresh=False, transient=False)
    _stream_live.start()


def show_stream_text(text: str) -> None:
    """Accumulate tokens and flush to the Live display at most 12 times per second.

    Calling Markdown() on every token is O(n²) — the whole buffer is re-parsed
    each time. This coalescing buffer ensures Markdown() is called at most
    refresh_per_second times regardless of how fast tokens arrive.
    """
    global _stream_buffer, _stream_pending, _last_render_time
    if _json_sink is not None:
        _json_sink.content(text)
        return
    if _stream_live is None:
        console.print(text, end="", highlight=False)
        return
    _stream_pending += text
    now = time.monotonic()
    if now - _last_render_time >= _RENDER_INTERVAL:
        _stream_buffer += _stream_pending
        _stream_pending = ""
        _last_render_time = now
        _stream_live.update(Markdown(_stream_buffer), refresh=True)


def finish_streaming_response() -> None:
    global _stream_live, _stream_buffer, _stream_pending
    if _json_sink is not None:
        return
    if _stream_live is not None:
        if _stream_pending:
            _stream_buffer += _stream_pending
            _stream_pending = ""
        _stream_live.update(Markdown(_stream_buffer or ""), refresh=True)
        _stream_live.stop()
        _stream_live = None
        _stream_buffer = ""


def show_thinking_token(text: str) -> None:
    show_thinking_text(text)


def show_error(msg: str) -> None:
    if _json_sink is not None and not _console_capture_active.get():
        _json_sink.error(error_class="internal", message=msg)
        return
    marker = Text.assemble(glyph(AIState.ERROR), (" Error: ", "bold error"))
    console.print(marker.append(msg, style="text"))


def show_info(msg: str) -> None:
    if _json_sink is not None and not _console_capture_active.get():
        # info chatter is dropped in JSON mode; bridge consumers only want events.
        return
    console.print(f"[info]{msg}[/]")


def show_status_footer(model: str, used_tokens: int, total_tokens: int, summary_active: bool = False) -> None:
    """Fallback status line when prompt_toolkit toolbar is unavailable."""
    if json_mode_active():
        return
    # The companion eye fronts the footer; frame is wall-clock derived so
    # repeated footer redraws make it breathe/blink.
    idle_glyph = buddy_frame(AIState.IDLE).plain
    if total_tokens <= 0:
        console.print(f"[info]{idle_glyph} {model}  ·  ctx unknown[/]")
        return
    used = min(max(used_tokens, 0), total_tokens)
    remaining = max(total_tokens - used, 0)
    pct_left = int(round((remaining / total_tokens) * 100))
    ctx_color = "info" if pct_left >= 50 else ("warning" if pct_left >= 20 else "error")
    summary_flag = "  ·  summary" if summary_active else ""
    console.print(
        f"[info]{idle_glyph}[/] [bold]{model}[/]  ·  "
        f"[{ctx_color}]▣ {used}/{total_tokens} ({pct_left}% left)[/]{summary_flag}"
    )


def show_memory(facts: list[str]) -> None:
    if not facts:
        console.print("[info]No memories stored.[/]")
        return
    table = Table(title="Memories", show_header=True, header_style="primary", box=box.ROUNDED)
    table.add_column("#", justify="right")
    table.add_column("Fact")
    for index, fact in enumerate(facts, 1):
        table.add_row(str(index), fact)
    console.print(table)


def show_help() -> None:
    table = Table.grid(padding=(0, 2), expand=True)
    table.add_column(style="muted", no_wrap=True)
    table.add_column(style="primary", no_wrap=True)
    table.add_column(style="text")

    from . import slash_dispatch

    def _group(command: str) -> str:
        if command in {
            "/help",
            "/dashboard",
            "/status",
            "/actions",
            "/reload",
            "/theme",
            "/info",
            "/safe",
            "/auto",
            "/policy",
            "/thinking",
            "/verify",
            "/perf",
            "/metrics",
            "/doctor",
            "/selfcheck",
            "/exit",
            "/quit",
        }:
            return "Session"
        if command in {
            "/model",
            "/models",
            "/host",
            "/cloud",
            "/cloudauto",
            "/login",
            "/keepalive",
            "/ctx",
            "/temp",
            "/toolmax",
            "/thinkevery",
            "/system",
        }:
            return "Model"
        if command.startswith("/google"):
            return "Google"
        if command.startswith("/chatgpt"):
            return "ChatGPT"
        if command.startswith("/xai") or command.startswith("/x-account"):
            return "xAI"
        if (
            command.startswith("/harness")
            or command.startswith("/hsearch")
            or command.startswith("/hread")
            or command in {"/hs", "/hr"}
        ):
            return "Harness"
        if command in {
            "/reason",
            "/reflex",
            "/goal",
            "/agent",
            "/route",
            "/icl",
            "/model-check",
            "/context",
        }:
            return "Agent"
        if command.startswith("/kernel"):
            return "Knowledge"
        if command in {
            "/identity",
            "/lesson",
            "/lessons",
            "/skills",
            "/remember",
            "/memories",
            "/forget",
            "/intuition",
            "/intelligence",
            "/intel",
            "/intelagence",
        }:
            return "Knowledge"
        if command in {"/cd", "/ls", "/read", "/save", "/load", "/diff", "/changes", "/clear", "/mode"}:
            return "Workspace"
        if command in {"/embed", "/vision", "/pdf"}:
            return "Media"
        if command in {"/plugins", "/credentials", "/url-scheme"}:
            return "Integrations"
        return "Other"

    groups: dict[str, list[tuple[str, str]]] = {
        "Session": [],
        "Model": [],
        "Workspace": [],
        "Knowledge": [],
        "Agent": [],
        "xAI": [],
        "Google": [],
        "ChatGPT": [],
        "Harness": [],
        "Media": [],
        "Integrations": [],
        "Other": [],
    }
    for command, description in slash_dispatch.SLASH_COMMANDS:
        groups[_group(command)].append((command, description))

    for group in [
        "Session",
        "Model",
        "Workspace",
        "Knowledge",
        "Agent",
        "xAI",
        "Google",
        "ChatGPT",
        "Harness",
        "Media",
        "Integrations",
        "Other",
    ]:
        rows = groups[group]
        if not rows:
            continue
        for index, (command, description) in enumerate(rows):
            table.add_row(group if index == 0 else "", command, description)
        table.add_row("", "", "")
    intro = Text.assemble(
        ("Ask naturally for ordinary work. ", "text"),
        ("Slash commands control the runtime and session.\n", "muted"),
        ("Start here  ", "bold secondary"),
        ("/status", "primary"),
        ("  ·  ", "muted"),
        ("/agent TASK", "primary"),
        ("  ·  ", "muted"),
        ("/hsearch QUERY", "primary"),
        ("  ·  ", "muted"),
        ("/doctor", "primary"),
    )
    console.print(
        Panel(
            Group(intro, Text(""), table),
            title="Command Reference",
            subtitle="[muted]type / to search inline[/]",
            border_style="border_accent",
            box=box.ROUNDED,
        )
    )
