from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from algo_cli import display


def _capture(monkeypatch, fn, *args, **kwargs) -> str:
    recorder = Console(record=True, width=120, color_system=None, theme=display.THEME_MAP[display.current_theme_name()])
    monkeypatch.setattr(display, "console", recorder)
    fn(*args, **kwargs)
    return recorder.export_text()


def test_show_tool_call_keeps_bracket_arguments_literal(monkeypatch):
    out = _capture(monkeypatch, display.show_tool_call, "run_shell", {"command": "ls [/tmp]", "pattern": "d[key]"})

    assert "run_shell" in out
    assert "command=ls [/tmp]" in out
    assert "pattern=d[key]" in out


def test_show_tool_result_preview_keeps_brackets(monkeypatch):
    out = _capture(monkeypatch, display.show_tool_result, "read_file", "value = d[key]\nx[i] + y[j]\nls [/tmp]")

    assert "value = d[key]" in out
    assert "x[i] + y[j]" in out
    assert "ls [/tmp]" in out
    assert "OK read_file" in out


def test_show_recalled_context_escapes_ids_and_content(monkeypatch):
    blocks = [{"type": "note", "id": "a[/b]", "score": 0.5, "content": "d[key]"}]

    out = _capture(monkeypatch, display.show_recalled_context, blocks)

    assert "[NOTE] a[/b] (0.50)" in out
    assert "d[key]" in out


def test_show_info_does_not_swallow_usage_brackets(monkeypatch):
    out = _capture(monkeypatch, display.show_info, "Usage: /intelligence [status|query <term>|reindex|init] [/x]")

    assert "[status|query <term>|reindex|init] [/x]" in out


def test_show_error_renders_class_and_hint(monkeypatch):
    out = _capture(monkeypatch, display.show_error, "timed out [/x]", error_class="TimeoutError", hint="try /doctor")

    assert "Error (TimeoutError): timed out [/x] — try /doctor" in out


def test_show_error_json_mode_keeps_schema(monkeypatch):
    calls: list[dict] = []

    class _Sink:
        def error(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(display, "_json_sink", _Sink())
    display.show_error("boom", error_class="TimeoutError", hint="try /doctor")

    assert calls == [{"error_class": "internal", "message": "boom"}]


def test_live_thinking_panel_follows_tail(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    text = "HEAD" + "x" * display._THINKING_VISIBLE_CHARS + "TAIL"

    panel = display._thinking_renderable(text)

    rendered = str(panel.renderable)
    assert isinstance(panel, Panel)
    assert rendered.endswith("TAIL")
    assert "HEAD" not in rendered
    assert "... 8 earlier chars" in rendered
    assert "[muted]" not in rendered


def test_final_thinking_panel_truncation_note_is_styled_not_markup(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    monkeypatch.setattr(display.time, "monotonic", lambda: 2.0)

    panel = display._thinking_renderable("HEAD" + "x" * (display._THINKING_VISIBLE_CHARS + 100), final=True)

    rendered = str(panel.renderable)
    assert rendered.startswith("HEAD")
    assert "... truncated" in rendered
    assert "[muted]" not in rendered


def test_model_wait_label_shows_elapsed_and_switches_to_loading_for_local():
    local = display._ModelWaitLabel("qwen3", local=True, started_at=100.0)
    cloud = display._ModelWaitLabel("grok-4", local=False, started_at=100.0)

    assert local.label(now=101.25).plain == "waiting for qwen3 · 1.2s"
    assert local.label(now=104.0).plain == "loading qwen3 · 4.0s"
    assert cloud.label(now=104.0).plain == "waiting for grok-4 · 4.0s"


def test_model_wait_status_uses_buddy_spinner_and_live_label(monkeypatch):
    seen: dict = {}

    class _Recorder:
        def status(self, renderable, **kwargs):
            seen["renderable"] = renderable
            seen.update(kwargs)
            return "status-handle"

    monkeypatch.setattr(display, "console", _Recorder())

    handle = display.model_wait_status("qwen3", local=True)

    assert handle == "status-handle"
    assert seen["spinner"] == display.spinner_name(display.AIState.THINKING)
    assert isinstance(seen["renderable"], display._ModelWaitLabel)
    assert seen["renderable"].__rich__().plain.startswith("waiting for qwen3 · ")


def test_model_wait_local_flag_follows_actual_ollama_route(monkeypatch):
    from algo_cli import main, model_routing
    from algo_cli.config import Config

    monkeypatch.setattr(model_routing, "_runtime_ollama_api_key", lambda: "")
    # ":cloud" model through a signed-in local daemon: no local weight loading.
    assert main._model_wait_is_local(Config(model="gpt-oss:120b-cloud", cloud=False)) is False
    # Stale cloud=True without an API key still goes to the local daemon.
    assert main._model_wait_is_local(Config(model="qwen3", cloud=True)) is True
    assert main._model_wait_is_local(Config(model="qwen3", cloud=False)) is True

    monkeypatch.setattr(model_routing, "_runtime_ollama_api_key", lambda: "k")
    assert main._model_wait_is_local(Config(model="qwen3", cloud=True)) is False
