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


_HOSTILE = "failed at [/tmp/x] with d[key] and [Errno 2]"


def _wide_capture(monkeypatch, fn, *args, **kwargs) -> str:
    recorder = Console(record=True, width=400, color_system=None, theme=display.THEME_MAP[display.current_theme_name()])
    monkeypatch.setattr(display, "console", recorder)
    fn(*args, **kwargs)
    return recorder.export_text()


def test_agent_block_reason_and_footer_warning_render_literally(monkeypatch, config_dir):
    monkeypatch.setattr(display, "CONFIG_DIR", config_dir)
    out = _wide_capture(
        monkeypatch,
        display.show_agent_block_complete,
        "code[/x]-scout",
        "Done.",
        duration_ms=100,
        tool_calls=1,
        status="partial",
        status_reason=_HOSTILE,
        verification_warning="check [/tmp/x] and d[key]",
        status_code="write_blocked[/x]",
        model="m[/x]",
        policy_summary="policy d[key] [/tmp/x]",
    )

    assert _HOSTILE in out
    assert "Verification: check [/tmp/x] and d[key]" in out
    assert "Code: write_blocked[/x]" in out
    assert "policy d[key] [/tmp/x]" in out
    assert "Agent · code[/x]-scout" in out
    assert "m[/x]" in out
    assert "Status: PARTIAL" in out


def test_agent_block_header_warning_without_reason_renders_literally(monkeypatch, config_dir):
    monkeypatch.setattr(display, "CONFIG_DIR", config_dir)
    out = _wide_capture(
        monkeypatch,
        display.show_agent_block_complete,
        "implement",
        "Done.",
        duration_ms=100,
        tool_calls=2,
        status="complete",
        verification_warning="Git unavailable for [/tmp/x] d[key]",
    )

    assert "Verification warning: Git unavailable for [/tmp/x] d[key]" in out
    assert "2 tool calls" in out


def test_agent_block_start_keeps_model_policy_and_cwd_literal(monkeypatch):
    out = _wide_capture(
        monkeypatch,
        display.show_agent_block_start,
        "plan[/x]",
        "model[/x]",
        3,
        "deny d[key] under [/tmp/x]",
        policy_enforced=True,
        cwd="/work/[/tmp/x]",
    )

    assert "Agent · plan[/x]" in out
    assert "Model: model[/x]" in out
    assert "Runtime tools: 3" in out
    assert "Workspace: /work/[/tmp/x]" in out
    assert "Enforced policy: deny d[key] under [/tmp/x]" in out


def test_agent_recovery_title_keeps_role_literal(monkeypatch):
    out = _wide_capture(monkeypatch, display.show_agent_recovery_start, "impl[/x]", "reason", 8)

    assert "Recovery - impl[/x] retry" in out


def test_structured_overflow_and_preview_notes_have_no_literal_markup(monkeypatch, config_dir):
    monkeypatch.setattr(display, "CONFIG_DIR", config_dir)
    monkeypatch.setenv("ALGO_CLI_AGENT_PREVIEW", "20")
    bullets = "\n".join(f"- item {index}" for index in range(20))
    structured = _wide_capture(
        monkeypatch, display.show_agent_block_complete, "plan", f"Assumptions:\n{bullets}", duration_ms=1,
        tool_calls=0, status="complete",
    )
    plain = _wide_capture(
        monkeypatch, display.show_agent_block_complete, "review", "x" * 100, duration_ms=1,
        tool_calls=0, status="complete",
    )

    assert "… 4 more" in structured
    assert "Full text (" in structured
    assert "Preview (20 chars)" in plain
    assert "[muted]" not in structured + plain


def test_status_footer_keeps_model_literal(monkeypatch):
    unknown = _wide_capture(monkeypatch, display.show_status_footer, "m[/x]d[key]", 0, 0)
    known = _wide_capture(monkeypatch, display.show_status_footer, "m[/x]d[key]", 10, 100, True)

    assert "m[/x]d[key]  ·  ctx unknown" in unknown
    assert "m[/x]d[key]  ·  ▣ 10/100 (90% left)  ·  summary" in known


def test_show_memory_keeps_fact_brackets(monkeypatch):
    out = _wide_capture(monkeypatch, display.show_memory, ["use d[key] not [/tmp/x]"])

    assert "use d[key] not [/tmp/x]" in out


def test_show_help_keeps_usage_brackets(monkeypatch):
    out = _wide_capture(monkeypatch, display.show_help, "all")

    assert "/cloud [on|off|status]" in out
    assert "/auto [on|off|status]" in out


def test_session_overview_keeps_runtime_values_literal(monkeypatch):
    for width in (90, 160):
        recorder = Console(record=True, width=width, color_system=None, theme=display.THEME_MAP["tokyo-night"])
        monkeypatch.setattr(display, "console", recorder)
        display.show_session_overview(
            model="m[/x]",
            host="h[/x]",
            cwd="/w/[/x]",
            theme_name="tokyo-night",
            cloud=False,
            auto_mode=False,
            safe_mode=True,
            temperature=0.4,
            used_tokens=1,
            total_tokens=100,
            summary_active=False,
            tool_think_every=4,
            max_tool_iterations=20,
            memory_count=1,
            system_prompt="sys d[key]",
            messages=[{"role": "tool", "name": "t[/x]", "content": "ls [/tmp/x] d[key]"}],
            installed_models=[{"name": "q[/x]", "size": "1 GB", "quant": "Q4"}],
            running_models=[{"name": "r[/x]", "size_vram": "2 GB", "context": "8k"}],
            event_lines=["event d[key] [/x]"],
        )
        out = recorder.export_text()

        assert "ls [/tmp/x] d[key]" in out
        assert "TOOL t[/x]" in out
        assert "m[/x]" in out
        assert "h[/x]" in out
        assert "q[/x]" in out
        assert "r[/x]" in out
        assert "event d[key] [/x]" in out


def test_agent_block_reason_with_only_style_like_brackets_is_not_eaten(monkeypatch, config_dir):
    monkeypatch.setattr(display, "CONFIG_DIR", config_dir)
    out = _wide_capture(
        monkeypatch,
        display.show_agent_block_complete,
        "implement",
        "Done.",
        duration_ms=1,
        tool_calls=0,
        status="partial",
        status_reason="missing d[key] in [Errno 2] map",
        verification_warning="saw d[key]",
    )

    assert "missing d[key] in [Errno 2] map" in out
    assert "Verification: saw d[key]" in out


def _long_trace() -> str:
    opening = "OPENING-MARKER: plan the change carefully.\n"
    middle = "".join(f"step {i}: reasoning about the middle of the trace\n" for i in range(80))
    ending = "final check passes.\nEND-OF-REASONING\n"
    return opening + middle + ending


def test_settled_thinking_panel_keeps_opening_and_ending(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    monkeypatch.setattr(display.time, "monotonic", lambda: 2.0)
    text = _long_trace()
    assert len(text) > display._THINKING_VISIBLE_CHARS

    rendered = str(display._thinking_renderable(text, final=True).renderable)

    assert rendered.startswith("OPENING-MARKER")
    assert "END-OF-REASONING" in rendered
    assert "chars omitted ..." in rendered
    assert f"~{display._estimated_tokens(text)} tokens total" in rendered
    # Head and tail are cut at line boundaries, so no half-lines appear.
    for line in rendered.splitlines():
        if line.startswith("step "):
            assert line.endswith("reasoning about the middle of the trace")
    body_chars = sum(len(line) for line in rendered.splitlines() if not line.startswith("..."))
    assert body_chars <= display._THINKING_VISIBLE_CHARS


def test_settled_thinking_panel_omitted_marker_is_styled_not_markup(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    text = _long_trace()

    body = display._thinking_renderable(text, final=True).renderable

    marker = next(span for span in body.spans if "omitted" in body.plain[span.start : span.end])
    assert marker.style == "muted"
    assert "[muted]" not in body.plain


def test_live_thinking_tail_preserves_indentation(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    lines = [f"line {i:04d} of filler reasoning text" for i in range(60)]
    lines += ["def f():", "    keep_indent()", "    return 1"]
    text = "\n".join(lines)
    assert len(text) > display._THINKING_VISIBLE_CHARS

    rendered = str(display._thinking_renderable(text).renderable)

    assert "\n    keep_indent()\n" in rendered
    assert rendered.endswith("    return 1")
    # The window starts on a full line, never mid-line.
    first_body_line = rendered.split("\n", 2)[1]
    assert first_body_line.startswith("line ") and first_body_line.endswith("of filler reasoning text")


def test_live_thinking_tail_keeps_indent_of_first_window_line(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    tail = "\n".join(["    keep_indent()"] + [f"    call_{i:03d}()" for i in range(80)])
    text = "x" * 50 + "\n\n\n" + tail

    rendered = str(display._thinking_renderable(text).renderable)

    body = rendered.split("\n", 1)[1]
    assert body.startswith("    ")
    assert not body.startswith("\n")


def test_live_thinking_tail_strips_only_leading_blank_lines(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    tail = "    keep_indent()\n" + "".join(f"    call_{i:03d}()\n" for i in range(70))
    text = "prefix " * 20 + "\n" + "\n" * 5 + tail.rstrip("\n")
    body_start = len(text) - display._THINKING_VISIBLE_CHARS
    assert body_start < len("prefix " * 20) + 6

    rendered = str(display._thinking_renderable(text).renderable)

    body = rendered.split("\n", 1)[1]
    assert body.startswith("    keep_indent()\n")


def test_short_thinking_traces_render_unchanged(monkeypatch):
    monkeypatch.setattr(display, "_thinking_started_at", 1.0)
    text = "  first line\n    indented second\nEND-OF-REASONING"

    assert str(display._thinking_renderable(text).renderable) == text
    assert str(display._thinking_renderable(text, final=True).renderable) == text
    assert str(display._thinking_renderable("", final=False).renderable) == "Thinking..."
