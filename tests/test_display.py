from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from algo_cli import display


class _FakeLive:
    instances: list["_FakeLive"] = []

    def __init__(self, renderable, **kwargs):
        self.renderable = renderable
        self.kwargs = kwargs
        self.updates: list[tuple[object, bool]] = []
        self.refresh_count = 0
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        return None

    def update(self, renderable, *, refresh=False):
        self.renderable = renderable
        self.updates.append((renderable, refresh))

    def refresh(self):
        self.refresh_count += 1

    def stop(self):
        self.stopped = True


def test_render_opening_banner_has_algo_branding():
    console = Console(record=True, width=120, theme=display.THEME_MAP["tokyo-night"])
    console.print(display.render_opening_banner(version="9.9.9"))
    rendered = console.export_text()
    assert "Algo CLI" in rendered
    assert "v9.9.9" in rendered
    assert "agent runtime" in rendered
    assert "durable context" in rendered
    assert "understand" in rendered and "verify" in rendered and "remember" in rendered
    assert "Ollama local/cloud" in rendered
    assert "ChatGPT/Codex" in rendered
    assert "read_file" not in rendered


def test_render_opening_banner_fits_narrow_terminal():
    console = Console(record=True, width=72, theme=display.THEME_MAP["tokyo-night"])
    console.print(display.render_opening_banner(version="9.9.9"))

    rendered = console.export_text()

    assert max(len(line) for line in rendered.splitlines()) <= 72
    assert "Agent runtime for tools" in rendered
    assert "type / for commands" in rendered


def test_show_banner_skips_json_mode(monkeypatch):
    console = Console(record=True, width=120, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)
    monkeypatch.setattr(display, "json_mode_active", lambda: True)
    display.show_banner()
    assert console.export_text().strip() == ""


def test_show_banner_prints_block_logo(monkeypatch):
    console = Console(record=True, width=120, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)
    monkeypatch.setattr(display, "json_mode_active", lambda: False)
    display.show_banner()
    text = console.export_text()
    assert "█████" in text
    assert "ready — ask naturally or type /" in text
    assert "Available Tools" not in text


def test_help_leads_with_natural_language_guidance(monkeypatch):
    console = Console(record=True, width=100, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)

    display.show_help()

    rendered = console.export_text()
    assert "Command Reference" in rendered
    assert "Ask naturally for ordinary work" in rendered
    assert "type / to search inline" in rendered
    assert "Start here" in rendered


def test_runtime_overview_stacks_cleanly_on_standard_terminal(monkeypatch):
    console = Console(record=True, width=80, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)

    display.show_session_overview(
        model="gpt-5.5",
        host="http://localhost:11434",
        cwd="/Users/demo/project",
        theme_name="tokyo-night",
        cloud=False,
        auto_mode=False,
        safe_mode=True,
        temperature=0.4,
        used_tokens=12_000,
        total_tokens=128_000,
        summary_active=False,
        tool_think_every=4,
        max_tool_iterations=20,
        memory_count=12,
        messages=[{"role": "user", "content": "Improve the CLI interface"}],
        installed_models=[{"name": "qwen3:latest", "size": "8 GB", "quant": "Q4"}],
        running_models=[],
        event_lines=["mode execute", "harness ready"],
    )

    rendered = console.export_text()
    assert max(len(line) for line in rendered.splitlines()) <= 80
    assert "http://localhost:11434" in rendered
    assert "Runtime Overview" in rendered
    assert "Conversation" in rendered
    assert "Quick Actions" in rendered


def test_thinking_renderable_final_title(monkeypatch):
    monkeypatch.setattr(display.time, "monotonic", lambda: 12.4)
    monkeypatch.setattr(display, "_thinking_started_at", 10.0)

    panel = display._thinking_renderable("abcd" * 10, final=True)

    assert isinstance(panel, Panel)
    assert "Thinking - 2.4s - ~10 tokens" in str(panel.title)


def test_thinking_renderable_truncates_long_text(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    monkeypatch.setattr(display.time, "monotonic", lambda: 2.0)

    panel = display._thinking_renderable("x" * (display._THINKING_VISIBLE_CHARS + 100), final=True)

    rendered = str(panel.renderable)
    assert "... truncated" in rendered
    assert "~325 tokens total" in rendered


def test_parse_block_output_sections_finds_plan_headings():
    text = (
        "## Block Output\n\n"
        "Assumptions:\n"
        "- Task uses local files.\n"
        "- Email is a contact field.\n\n"
        "Risks:\n"
        "- PII exposure.\n\n"
        "Concrete Next Steps:\n"
        "1. List cwd.\n"
        "2. Grep for email.\n"
    )
    sections = display._parse_block_output_sections(text)
    assert "Assumptions" in sections
    assert "Risks" in sections
    assert "Next steps" in sections
    assert any("local files" in item for item in sections["Assumptions"])


def test_agent_preview_char_limit_zero_means_full(monkeypatch):
    monkeypatch.setenv("ALGO_CLI_AGENT_PREVIEW", "0")
    assert display._agent_preview_char_limit() is None


def test_agent_block_completion_structured_plan(monkeypatch):
    console = Console(record=True, width=120, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)
    monkeypatch.setenv("ALGO_CLI_AGENT_PREVIEW", "2000")

    display.show_agent_block_complete(
        "plan",
        "## Block Output\n\nAssumptions:\n- One\n\nRisks:\n- Two\n\nNext Steps:\n- Three",
        duration_ms=39400,
        tool_calls=0,
        status="complete",
        model="test-model",
    )

    rendered = console.export_text()
    assert "Assumptions" in rendered
    assert "Risks" in rendered
    assert "Next steps" in rendered or "Three" in rendered


def test_agent_block_completion_renders_status_reason(monkeypatch):
    console = Console(record=True, width=120, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)

    display.show_agent_block_complete(
        "implement",
        "## Block Output\n\nNo change.",
        duration_ms=100,
        tool_calls=1,
        status="partial",
        status_reason="Required change not verified.",
    )

    assert "Required change not verified." in console.export_text()


def test_agent_block_completion_renders_verification_warning(monkeypatch):
    console = Console(record=True, width=120, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)

    display.show_agent_block_complete(
        "implement",
        "Implementation done.",
        duration_ms=100,
        tool_calls=1,
        status="complete",
        verification_warning="Git verification was unavailable; review must manually confirm the written files.",
    )

    rendered = console.export_text()
    assert "Verification warning" in rendered
    assert "manually" in rendered and "confirm" in rendered
    assert "Implementation done." in rendered


def test_agent_recovery_start_renders_reason_and_budget(monkeypatch):
    console = Console(record=True, width=120, theme=display.THEME_MAP["tokyo-night"])
    monkeypatch.setattr(display, "console", console)

    display.show_agent_recovery_start("implement", "No verified write.", 8)

    rendered = console.export_text()
    assert "Recovery - implement retry" in rendered
    assert "No verified write." in rendered
    assert "at most 8" in rendered and "iterations" in rendered


def test_thinking_stream_disables_auto_refresh_and_flushes_pending(monkeypatch):
    _FakeLive.instances.clear()
    ticks = iter([1.0, 1.0, 1.0, 1.0, 1.01, 2.0])
    monkeypatch.setattr(display, "Live", _FakeLive)
    monkeypatch.setattr(display.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(display, "_thinking_live", None)

    display.show_thinking_text("first")
    display.show_thinking_text(" second")
    live = _FakeLive.instances[0]

    assert live.kwargs["auto_refresh"] is False
    assert live.refresh_count == 1
    assert len(live.updates) == 1

    display.finish_thinking_block()

    assert "first second" in str(live.updates[-1][0].renderable)
    assert live.updates[-1][1] is True


def test_response_stream_disables_auto_refresh_and_explicitly_refreshes(monkeypatch):
    _FakeLive.instances.clear()
    monkeypatch.setattr(display, "Live", _FakeLive)
    monkeypatch.setattr(display.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(display, "_stream_live", None)

    display.start_streaming_response()
    display.show_stream_text("answer")
    live = _FakeLive.instances[0]

    assert live.kwargs["auto_refresh"] is False
    assert live.updates[-1][1] is True

    display.finish_streaming_response()

    assert live.stopped is True
