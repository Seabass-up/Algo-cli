from __future__ import annotations

from html import escape
import threading
import time

import pytest

from algo_cli import main
from algo_cli import oliver_slash_dispatch as dispatch
from algo_cli.config import Config


def _prime_status(monkeypatch, cfg: Config) -> None:
    monkeypatch.setattr(main._model_info_module, "resolve_model_info", lambda _cfg, _client: {})
    monkeypatch.setattr(main, "context_status", lambda *_a, **_k: (100, 1000, 900, 1000, 1000))
    monkeypatch.setattr(main, "local_model_names", lambda _cfg: [])
    main.refresh_runtime_status(cfg, None, force=True)


def test_footer_shows_safety_toggle_inside_refresh_throttle(monkeypatch):
    cfg = Config(model="test-model")
    cfg.safe_mode = True
    cfg.auto_mode = False
    _prime_status(monkeypatch, cfg)
    assert "safe off" not in main.format_status_toolbar_plain(cfg)

    cfg.safe_mode = False
    cfg.auto_mode = True
    main.refresh_runtime_status(cfg, None)  # throttled: RUNTIME_STATUS keeps the old flags

    plain = main.format_status_toolbar_plain(cfg)
    rich = main.build_status_toolbar(cfg).value
    assert "safe off" in plain and "auto on" in plain
    assert "safe off" in rich and "auto on" in rich


def test_rprompt_reads_theme_and_cwd_live(monkeypatch, tmp_path):
    cfg = Config(model="test-model")
    cfg.theme = "tokyo-night"
    _prime_status(monkeypatch, cfg)

    cfg.theme = "dracula"
    cfg.cwd = str(tmp_path)
    main.refresh_runtime_status(cfg, None)

    rprompt = main.build_status_rprompt(cfg).value
    assert "dracula" in rprompt
    assert escape(main.compact_path(str(tmp_path), 32)) in rprompt


@pytest.mark.parametrize("command", ["/safe off", "/auto on", "/policy on", "/clear", "/mode status"])
def test_state_changing_slash_commands_force_status_refresh(monkeypatch, command):
    cfg = Config(model="test-model")
    monkeypatch.setattr(cfg, "save", lambda: None)
    monkeypatch.setattr(main, "show_info", lambda _msg: None)
    monkeypatch.setattr(main.console, "print", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "clear_session_pipeline_blocks", lambda: None)
    refreshes: list[bool] = []
    invalidations: list[object] = []
    monkeypatch.setattr(main, "refresh_runtime_status", lambda _cfg, _client, *, force=False: refreshes.append(force))
    monkeypatch.setattr(main, "invalidate_prompt_toolbar", invalidations.append)
    session = object()

    handled, _client = dispatch.handle_command(command, cfg, object(), session, user_initiated=True)

    assert handled is True
    assert refreshes == [True]
    assert invalidations == [session]


def test_read_only_slash_command_does_not_force_refresh(monkeypatch):
    cfg = Config(model="test-model")
    monkeypatch.setattr(dispatch.display, "show_help", lambda *_a: None)
    refreshes: list[bool] = []
    monkeypatch.setattr(main, "refresh_runtime_status", lambda *_a, **_k: refreshes.append(True))

    dispatch.handle_command("/help", cfg, object(), None)

    assert refreshes == []


@pytest.mark.parametrize(
    ("buffer", "last", "expected"),
    [
        ("half typed prompt", None, "clear"),
        ("half typed prompt", 99.5, "clear"),
        ("", None, "hint"),
        ("   ", 90.0, "hint"),
        ("", 99.0, "exit"),
    ],
)
def test_prompt_interrupt_action(buffer, last, expected):
    assert main.prompt_interrupt_action(buffer, 100.0, last) == expected


class _FakeBuffer:
    def __init__(self) -> None:
        self.text = ""


class _FakeSession:
    """Mimics prompt_toolkit: Ctrl+C raises KeyboardInterrupt with the typed text still in the buffer."""

    def __init__(self, inputs: list[object]) -> None:
        self.inputs = inputs
        self.default_buffer = _FakeBuffer()

    def prompt(self, *_args, **_kwargs):
        item = self.inputs.pop(0)
        if isinstance(item, tuple):
            exc, buffered = item
            self.default_buffer.text = buffered
            raise exc
        self.default_buffer.text = ""
        return item


def _read_all(monkeypatch, inputs: list[object], clock: list[float]) -> tuple[list[str | None], list[str]]:
    printed: list[str] = []
    monkeypatch.setattr(main.console, "print", lambda *args, **_k: printed.append(" ".join(map(str, args))))
    monkeypatch.setattr(main.time, "monotonic", lambda: clock.pop(0))
    session = _FakeSession(inputs)
    state = main.PromptInterruptState()
    results: list[str | None] = []
    while session.inputs:
        results.append(main.read_repl_input(session, state))
        if results[-1] is None:
            break
    return results, printed


def test_ctrl_c_with_typed_text_clears_line_without_exiting(monkeypatch):
    results, printed = _read_all(
        monkeypatch,
        [(KeyboardInterrupt(), "a long half-typed prompt"), "hello"],
        clock=[10.0],
    )
    assert results == ["", "hello"]
    assert printed == []


def test_ctrl_c_on_empty_line_needs_second_press_to_exit(monkeypatch):
    results, printed = _read_all(
        monkeypatch,
        [(KeyboardInterrupt(), ""), (KeyboardInterrupt(), "")],
        clock=[10.0, 11.0],
    )
    assert results == ["", None]
    assert any("Ctrl+D to exit" in line for line in printed)


def test_slow_second_ctrl_c_only_warns_again(monkeypatch):
    results, _printed = _read_all(
        monkeypatch,
        [(KeyboardInterrupt(), ""), (KeyboardInterrupt(), "")],
        clock=[10.0, 20.0],
    )
    assert results == ["", ""]


def test_ctrl_d_exits_immediately(monkeypatch):
    results, _printed = _read_all(monkeypatch, [(EOFError(), "")], clock=[])
    assert results == [None]


def _stub_agent_loop(monkeypatch) -> None:
    monkeypatch.setattr(main.identity, "detect_changes", lambda: [])
    monkeypatch.setattr(main, "ensure_lessons_index", lambda _cfg: False)
    monkeypatch.setattr(main, "ensure_harness_index", lambda _cfg, _local=None: False)
    monkeypatch.setattr(main, "prune_stale_tool_messages", lambda _cfg: None)
    monkeypatch.setattr(main, "maybe_compact_context", lambda _client, _cfg, **_: None)
    monkeypatch.setattr(main._model_info_module, "ensure_model_info", lambda _client, _model: {})
    monkeypatch.setattr(main, "record_chat_metrics", lambda _cfg, _chunk: None)
    monkeypatch.setattr(main, "start_streaming_response", lambda: None)
    monkeypatch.setattr(main, "show_stream_text", lambda _text: None)
    monkeypatch.setattr(main, "finish_streaming_response", lambda: None)
    monkeypatch.setattr(
        main.memory_runtime, "capture_completed_user_turn", lambda *_a, **_k: {"status": "skipped"}
    )


def test_ctrl_c_mid_stream_keeps_partial_answer_in_history(monkeypatch):
    cfg = Config(model="test-model")
    cfg.skill_crystallize_enabled = False
    closed: list[bool] = []

    class InterruptedStream:
        def __iter__(self):
            yield {"message": {"content": "Partial answer."}}
            raise KeyboardInterrupt

        def close(self):
            closed.append(True)

    class InterruptedClient:
        def chat(self, **_kwargs):
            return InterruptedStream()

    _stub_agent_loop(monkeypatch)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        main.agent_loop(InterruptedClient(), cfg, "explain the codebase")  # type: ignore[arg-type]

    assert cfg.messages[-2]["role"] == "user"
    assert cfg.messages[-1] == {
        "role": "assistant",
        "content": "Partial answer." + main.GENERATION_INTERRUPTED_MARKER,
    }
    assert closed == [True]
    assert "Partial response kept" in main.generation_interrupted_message(excinfo.value)


def test_ctrl_c_before_any_text_reports_nothing_kept(monkeypatch):
    cfg = Config(model="test-model")
    cfg.skill_crystallize_enabled = False

    class InterruptedClient:
        def chat(self, **_kwargs):
            def chunks():
                raise KeyboardInterrupt
                yield {}

            return chunks()

    _stub_agent_loop(monkeypatch)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        main.agent_loop(InterruptedClient(), cfg, "explain the codebase")  # type: ignore[arg-type]

    assert all(message.get("role") != "assistant" for message in cfg.messages)
    assert "No response text" in main.generation_interrupted_message(excinfo.value)
    assert main.generation_interrupted_message(KeyboardInterrupt()) == "Generation interrupted."


def test_ctrl_c_after_earlier_round_text_does_not_claim_nothing_received(monkeypatch):
    cfg = Config(model="test-model")
    cfg.skill_crystallize_enabled = False
    calls: list[int] = []

    class TwoRoundClient:
        def chat(self, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                return iter(
                    [
                        {
                            "message": {
                                "content": "Here is what I found so far.",
                                "tool_calls": [{"function": {"name": "list_directory", "arguments": {"path": "."}}}],
                            }
                        }
                    ]
                )

            def chunks():
                raise KeyboardInterrupt
                yield {}

            return chunks()

    _stub_agent_loop(monkeypatch)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        main.agent_loop(TwoRoundClient(), cfg, "explain the codebase")  # type: ignore[arg-type]

    assert len(calls) == 2
    assert any(
        m.get("role") == "assistant" and m.get("content") == "Here is what I found so far." for m in cfg.messages
    )
    assert main.generation_interrupted_message(excinfo.value) == "Generation interrupted."


def test_repl_error_hint_explains_common_failures():
    from algo_cli.config import Config

    cfg = Config()
    cfg.cloud = False
    cfg.model = "qwen3"
    cfg.host = "http://localhost:11434"

    class ConnectError(Exception):
        pass

    class ResponseError(Exception):
        status_code = 404

    assert "ollama serve" in main.repl_error_hint(ConnectionRefusedError("refused"), cfg)
    assert "ollama serve" in main.repl_error_hint(ConnectError("boom"), cfg)
    assert "timed out" in main.repl_error_hint(TimeoutError(), cfg)
    assert "ollama pull qwen3" in main.repl_error_hint(ResponseError("model 'qwen3' not found"), cfg)
    assert main.repl_error_hint(ValueError("bad"), cfg) is None


class _NotFoundError(Exception):
    status_code = 404


def test_repl_error_hint_suggests_pull_for_local_404():
    cfg = Config(model="qwen3")
    cfg.cloud = False

    hint = main.repl_error_hint(_NotFoundError("model 'qwen3' not found"), cfg)

    assert "ollama pull qwen3" in hint


def test_repl_error_hint_skips_pull_for_direct_cloud_404(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    cfg = Config(model="gpt-oss:120b")
    cfg.cloud = True
    assert main.uses_ollama_cloud(cfg)

    hint = main.repl_error_hint(_NotFoundError("model 'gpt-oss:120b' not found"), cfg)

    assert "ollama pull" not in hint
    assert "gpt-oss:120b" in hint and "/models" in hint


def test_repl_error_hint_suggests_pull_for_cloud_tag_via_local_daemon(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    cfg = Config(model="gpt-oss:120b-cloud")
    cfg.cloud = False
    assert not main.uses_ollama_cloud(cfg)

    hint = main.repl_error_hint(_NotFoundError("model 'gpt-oss:120b-cloud' not found"), cfg)

    assert "ollama pull gpt-oss:120b-cloud" in hint


@pytest.mark.parametrize("route", ["xai", "chatgpt"])
def test_repl_error_hint_skips_pull_for_provider_404(monkeypatch, route):
    cfg = Config(model="some-model")
    cfg.cloud = False
    if route == "xai":
        monkeypatch.setattr(main, "routes_to_xai", lambda _cfg, *_a: True)
    elif route == "chatgpt":
        monkeypatch.setattr(main, "routes_to_chatgpt", lambda _cfg, *_a: True)

    hint = main.repl_error_hint(_NotFoundError("model not found"), cfg)

    assert "ollama pull" not in hint
    assert "/models" in hint


def test_show_repl_error_prints_class_and_hint(monkeypatch):
    from algo_cli.config import Config

    seen = {}
    monkeypatch.setattr(main, "show_error", lambda msg, **kw: seen.update(msg=msg, **kw))
    cfg = Config()
    cfg.cloud = False
    main.show_repl_error(TimeoutError(), cfg)
    assert seen["msg"] == "TimeoutError"
    assert seen["error_class"] == "TimeoutError"
    assert "timed out" in seen["hint"]


def _render(monkeypatch, handler) -> str:
    from algo_cli import agent_blocks

    blocks = [
        agent_blocks.AgentBlock(
            role=role,
            prompt="p",
            status="partial",
            output="ok",
            requires_change=True,
            git_evidence="diff --git a/x b/x\n+[bold]kept[/bold] d[key]",
            status_reason="write outside [/tmp] was refused",
            verification_warning="lookup d[key] failed [Errno 2]",
            successful_writes=["a[b].py"],
            mutation_actions=["write_file: c[d].py"],
        )
        for role in ("code-scout", "planner")
    ]
    main._session_pipeline_blocks[:] = blocks
    try:
        with main.console.capture() as captured:
            handler()
    finally:
        main.clear_session_pipeline_blocks()
    return captured.get()


def test_diff_command_prints_role_and_bracketed_values_literally(monkeypatch):
    out = _render(monkeypatch, main.handle_diff_command)

    assert "Diff captured by [planner] block" in out
    assert "write outside [/tmp] was refused" in out
    assert "lookup d[key] failed [Errno 2]" in out
    assert "a[b].py" in out
    assert "+[bold]kept[/bold] d[key]" in out


def test_changes_command_prints_roles_and_bracketed_values_literally(monkeypatch):
    out = _render(monkeypatch, main.handle_changes_command)

    assert "[code-scout]" in out and "[planner]" in out
    assert out.count("write outside [/tmp] was refused") == 2
    assert "lookup d[key] failed [Errno 2]" in out
    assert "a[b].py" in out and "c[d].py" in out


def test_ctrl_c_during_parallel_batch_does_not_wait_for_hung_tool(monkeypatch, tmp_path):
    from test_agent_progress_recovery import run_script

    hung_entered = threading.Event()
    fast_done = threading.Event()
    release = threading.Event()
    configs: list = []

    def invoke(_name, args, _cfg):
        if args["path"] == "hung":
            hung_entered.set()
            release.wait(timeout=30)
            return "late observation"
        fast_done.set()
        return "fast observation"

    def interrupt_wait(_futures):
        assert hung_entered.wait(timeout=5) and fast_done.wait(timeout=5)
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "as_completed", interrupt_wait)
    monkeypatch.setattr(main, "PARALLEL_INTERRUPT_GRACE_SECONDS", 0.3)
    started = time.monotonic()
    try:
        code, events, client, _invoked, _captures = run_script(
            monkeypatch,
            tmp_path,
            lambda _turn: {
                "tool_calls": [
                    {"id": path, "function": {"name": "read_file", "arguments": {"path": path}}}
                    for path in ("fast", "hung")
                ]
            },
            invoke=invoke,
            configure=configs.append,
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert elapsed < 5
    assert code == 2 and len(client.calls) == 1
    results = {item["call_id"]: item for item in events if item["type"] == "tool_result"}
    assert list(results) == ["fast", "hung"]
    assert results["hung"]["status"] == "cancelled"
    assert events[-1]["status_reason"] == "interrupted"

    messages = configs[0].messages
    call_ids = [
        call.get("id")
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert call_ids == ["fast", "hung"]
    assert [message.get("tool_call_id") for message in tool_messages] == call_ids
    assert "fast observation" in tool_messages[0]["content"]
    assert tool_messages[1]["content"] == main.PARALLEL_INTERRUPTED_RESULT


def test_google_list_lines_render_muted_span_without_literal_tags():
    from algo_cli import google_workspace

    lines = google_workspace.format_drive_files(
        {"files": [{"id": "abc", "name": "Report", "mimeType": "text/plain", "size": 10}]}
    ) + google_workspace.format_calendar_events(
        {"items": [{"summary": "Sync", "id": "e1", "start": {"date": "2026-09-23"}}]}
    )
    with main.console.capture() as capture:
        main._google_print_lines(lines)
    out = capture.get()
    assert "[muted]" not in out and "[/]" not in out
    assert "  - Report  (text/plain  10B)  id=abc" in out
    assert "  - Sync  (2026-09-23)  id=e1" in out

    text = main._google_line_text(lines[0])
    muted = [text.plain[span.start:span.end] for span in text.spans if span.style == "muted"]
    assert muted == ["(text/plain  10B)"]


def test_google_list_lines_keep_api_names_literal():
    from algo_cli import google_workspace

    lines = google_workspace.format_drive_files(
        {"files": [{"id": "x[1]", "name": "[/tmp] d[key] [bold]", "mimeType": "text/plain"}]}
    ) + ["  (no files)", "  - odd [muted]name"]
    with main.console.capture() as capture:
        main._google_print_lines(lines)
    out = capture.get()
    assert "  - [/tmp] d[key] [bold]  (text/plain)  id=x[1]" in out
    assert "  (no files)" in out
    assert "  - odd [muted]name" in out
