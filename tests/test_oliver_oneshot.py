"""One-shot JSON event mode: schema + framing + approval contract."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from algo_cli import display, oliver_oneshot as oneshot


def _drain(stream: io.StringIO) -> list[dict[str, Any]]:
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_resolve_version_uses_distribution_name(monkeypatch):
    requested: list[str] = []

    def fake_version(name: str) -> str:
        requested.append(name)
        return "0.14.0"

    monkeypatch.setattr("importlib.metadata.version", fake_version)

    assert oneshot._resolve_version() == "0.14.0"
    assert requested == ["algo-cli-runtime"]


def test_json_event_sink_session_start_and_done_frame(monkeypatch):
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf, approval_mode="never")
    sink.session_start(model="m", host="h", cwd="/c", version="0.0.0")
    sink.content("hello")
    sink.done(status="complete", status_reason="", duration_ms=42.0)

    events = _drain(buf)
    assert events[0]["type"] == "session_start"
    assert events[0]["approval_mode"] == "never"
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "complete"
    assert events[-1]["tool_calls"] == 0
    assert events[-1]["duration_ms"] == 42.0
    assert events[-1]["usage"]["total_tokens"] == 0


def test_json_event_sink_accumulates_chat_usage():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    sink.chat_usage(prompt_tokens=100, completion_tokens=20)
    sink.chat_usage(prompt_tokens=140, completion_tokens=10)
    sink.done(status="complete", status_reason="", duration_ms=1)

    usage = _drain(buf)[0]["usage"]
    assert usage == {"prompt_tokens": 240, "completion_tokens": 30, "total_tokens": 270}


def test_json_event_sink_emits_bounded_round_receipts_and_summary():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    sink.model_round(
        round=1,
        phase="execution",
        trigger="initial_plan",
        prompt_tokens=120,
        completion_tokens=20,
        prompt_eval_ms=30.5,
        generation_ms=12.25,
        context_build_ms=1.5,
        context_sources={"tool_schemas": 40},
    )
    sink.model_round(
        round=2,
        phase="response",
        trigger="free-form-is-not-allowed",
        prompt_tokens=180,
        completion_tokens=10,
        prompt_eval_ms=40.0,
        generation_ms=8.0,
        context_build_ms=1.0,
        context_sources={"tool_schemas": 40},
    )
    sink.done(status="complete", status_reason="", duration_ms=100)

    events = _drain(buf)
    assert events[0]["type"] == "model_round"
    assert events[0]["trigger"] == "initial_plan"
    assert events[1]["trigger"] == "unexpected_retry"
    assert events[-1]["rounds"] == {
        "count": 2,
        "max_prompt_tokens": 180,
        "prompt_eval_ms": 70.5,
        "generation_ms": 20.25,
    }


def test_json_event_sink_tool_call_then_result(monkeypatch):
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    cid = sink.next_call_id()
    sink.tool_call(call_id=cid, name="read_file", args={"path": "x"})
    sink.tool_result(call_id=cid, name="read_file", result="hello world", duration_ms=12.34)

    events = _drain(buf)
    assert events[0]["type"] == "tool_call"
    assert events[0]["call_id"] == cid
    assert events[0]["name"] == "read_file"
    assert events[0]["args"] == {"path": "x"}
    assert events[1]["type"] == "tool_result"
    assert events[1]["call_id"] == cid
    assert events[1]["status"] == "ok"
    assert events[1]["duration_ms"] == 12.34
    assert events[1]["summary"] == "hello world"
    assert events[1]["truncated"] is False


def test_json_event_sink_summarizes_long_results():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    long_result = "x" * (oneshot.SUMMARY_LIMIT + 200)
    sink.tool_result(call_id="c1", name="search_files", result=long_result, duration_ms=1.0)
    events = _drain(buf)
    assert events[0]["truncated"] is True
    assert len(events[0]["summary"]) <= oneshot.SUMMARY_LIMIT + 3


def test_json_event_sink_status_classification():
    assert oneshot._tool_status_from_result("ok contents") == "ok"
    assert oneshot._tool_status_from_result("User denied this operation.") == "denied"
    assert oneshot._tool_status_from_result("Skipped repeated failed attempt.") == "skipped"
    assert oneshot._tool_status_from_result("Error: not found") == "failed"
    assert oneshot._tool_status_from_result("Tool error for read_file: x") == "failed"
    assert oneshot._tool_status_from_result("tests failed\n[exit code: 1]") == "failed"
    assert oneshot._tool_status_from_result("tests passed\n[exit code: 0]") == "ok"
    assert oneshot._tool_status_from_result("detail\n\nUnknown outcome: reconcile") == "unknown_outcome"
    assert oneshot._tool_status_from_result("detail\n\nTimed out outcome: elapsed") == "timed_out"
    assert oneshot._tool_status_from_result("detail\n\nCancelled outcome: stopped") == "cancelled"
    assert oneshot._tool_status_from_result("Blocked by runtime authority: no grant") == "denied"


def test_json_event_sink_typed_status_cannot_be_spoofed_by_result_text():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    sink.tool_result(
        call_id="c1",
        name="read_file",
        result="Unknown outcome: this is ordinary file content",
        duration_ms=1.0,
        outcome_status="succeeded",
    )

    assert _drain(buf)[0]["status"] == "ok"


def test_runtime_typed_result_bypasses_legacy_text_classification():
    from algo_cli.arthur_outcomes import OutcomeStatus
    from algo_cli import tool_runtime

    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    display.install_json_sink(sink)
    try:
        tool_runtime.show_typed_tool_result(
            "read_file",
            "Unknown outcome: ordinary untrusted file content",
            outcome_status=OutcomeStatus.SUCCEEDED,
            duration_ms=2.0,
            call_id="typed-1",
        )
    finally:
        display.uninstall_json_sink()

    assert _drain(buf)[0]["status"] == "ok"


def test_json_event_sink_rejects_unknown_typed_status():
    sink = oneshot.JsonEventSink(stream=io.StringIO())

    with pytest.raises(ValueError, match="unsupported typed tool outcome"):
        sink.tool_result(
            call_id="c1",
            name="read_file",
            result="contents",
            duration_ms=1.0,
            outcome_status="maybe",
        )


def test_json_event_sink_tool_denied():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf, approval_mode="never")
    sink.tool_denied(call_id="c2", name="write_file", reason="approval-mode never; tool requires approval")
    events = _drain(buf)
    assert events[0]["type"] == "tool_denied"
    assert events[0]["call_id"] == "c2"
    assert events[0]["name"] == "write_file"
    assert "approval-mode never" in events[0]["reason"]


def test_json_event_sink_one_object_per_line_no_embedded_newlines():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    sink.content("line one\nline two\nline three")
    raw = buf.getvalue()
    # One line in stdout, even though content contains newlines.
    physical_lines = [line for line in raw.splitlines() if line.strip()]
    assert len(physical_lines) == 1
    decoded = json.loads(physical_lines[0])
    assert decoded["text"] == "line one\nline two\nline three"


def test_json_mode_suppresses_rich_tool_status_output():
    from algo_cli.display import console, tool_execution_status

    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    display.install_json_sink(sink)
    try:
        with tool_execution_status("[muted]executing read_file...[/]"):
            pass
        console.print("must not appear in json mode")
    finally:
        display.uninstall_json_sink()
    assert buf.getvalue() == ""


def test_display_helpers_route_through_sink_when_installed(monkeypatch):
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    display.install_json_sink(sink)
    try:
        display.show_tool_call("read_file", {"path": "x.py", "cwd": "/strip-me"}, call_id="c1")
        display.show_tool_result("read_file", "the contents", duration_ms=7.0, call_id="c1")
        display.show_response("model said hi")
        display.show_stream_text("token chunk")
        display.show_thinking_text("internal reasoning")
        display.show_error("policy violation")
        display.show_info("this should be dropped")
    finally:
        display.uninstall_json_sink()

    events = _drain(buf)
    types = [e["type"] for e in events]
    assert types == ["tool_call", "tool_result", "content", "content", "thinking", "error"]
    # cwd was stripped from emitted args (it's an internal field, not model-supplied).
    assert events[0]["args"] == {"path": "x.py"}
    assert events[1]["call_id"] == "c1"
    assert events[5]["class"] == "internal"


def test_display_generated_call_ids_match_results_and_preserve_fifo():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf)
    display.install_json_sink(sink)
    try:
        display.show_tool_call("read_file", {"path": "first.py"})
        display.show_tool_call("read_file", {"path": "second.py"})
        display.show_tool_result("read_file", "first")
        display.show_tool_result("read_file", "second")
    finally:
        display.uninstall_json_sink()

    events = _drain(buf)
    call_ids = [event["call_id"] for event in events if event["type"] == "tool_call"]
    result_ids = [event["call_id"] for event in events if event["type"] == "tool_result"]
    assert result_ids == call_ids


def test_display_tool_denied_event_on_user_denied_result():
    buf = io.StringIO()
    sink = oneshot.JsonEventSink(stream=buf, approval_mode="never")
    display.install_json_sink(sink)
    try:
        display.show_tool_result("write_file", "User denied this operation.", approved=False, call_id="c1")
    finally:
        display.uninstall_json_sink()

    events = _drain(buf)
    assert events[0]["type"] == "tool_denied"
    assert events[0]["call_id"] == "c1"


def test_run_oneshot_emits_framing_even_when_agent_loop_raises(monkeypatch):
    """End-to-end: run_oneshot must still emit session_start and done framing on failure."""
    from algo_cli import main as main_module

    buf = io.StringIO()

    def _boom_agent_loop(_client, _cfg, _msg):
        raise RuntimeError("synthetic failure")

    def _stub_create_client(_cfg):
        return object()

    monkeypatch.setattr(main_module, "agent_loop", _boom_agent_loop)
    monkeypatch.setattr(main_module, "create_client", _stub_create_client)

    exit_code = oneshot.run_oneshot(prompt="hi", approval_mode="never", stream=buf)

    events = _drain(buf)
    assert events[0]["type"] == "session_start"
    assert events[0]["approval_mode"] == "never"
    assert any(e["type"] == "error" and "synthetic failure" in e["message"] for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "failed"
    assert "synthetic failure" in events[-1]["status_reason"]
    assert exit_code == 2


def test_run_oneshot_completes_cleanly_when_agent_loop_succeeds(monkeypatch):
    from algo_cli import display as display_module
    from algo_cli import main as main_module

    buf = io.StringIO()

    def _scripted_agent_loop(_client, _cfg, _msg):
        cid = display_module.json_sink().next_call_id()
        display_module.show_tool_call("read_file", {"path": "main.py"}, call_id=cid)
        display_module.show_tool_result("read_file", "file contents", duration_ms=3.5, call_id=cid)
        display_module.show_stream_text("final answer text")

    monkeypatch.setattr(main_module, "agent_loop", _scripted_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    exit_code = oneshot.run_oneshot(prompt="read main.py", approval_mode="never", stream=buf)

    events = _drain(buf)
    assert exit_code == 0
    types = [e["type"] for e in events]
    assert types[0] == "session_start"
    assert types[-1] == "done"
    assert "tool_call" in types and "tool_result" in types
    assert events[-1]["status"] == "complete"
    assert events[-1]["tool_calls"] == 1


def test_run_oneshot_auto_mode_sets_cfg_auto_mode(monkeypatch):
    from algo_cli import main as main_module

    captured_cfg = {}

    def _spy_agent_loop(_client, cfg, _msg):
        captured_cfg["auto_mode"] = cfg.auto_mode
        captured_cfg["skill_crystallize_enabled"] = cfg.skill_crystallize_enabled

    monkeypatch.setattr(main_module, "agent_loop", _spy_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    oneshot.run_oneshot(prompt="x", approval_mode="auto", stream=io.StringIO())
    assert captured_cfg["auto_mode"] is True
    # Skill crystallization is always disabled in oneshot regardless of approval mode.
    assert captured_cfg["skill_crystallize_enabled"] is False


def test_run_oneshot_uses_adaptive_thinking_and_allows_override(monkeypatch):
    from algo_cli import main as main_module

    captured: list[bool] = []

    def _spy_agent_loop(_client, cfg, _msg):
        captured.append(cfg.show_thinking)

    monkeypatch.setattr(main_module, "agent_loop", _spy_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    oneshot.run_oneshot(prompt="Fix the failing test", stream=io.StringIO())
    oneshot.run_oneshot(
        prompt="Fix the failing test",
        cfg_overrides={"show_thinking": True},
        stream=io.StringIO(),
    )

    assert captured == [False, True]


def test_oneshot_ask_approval_denies_dangerous_under_never(monkeypatch):
    """When approval-mode=never, the patched ask_approval must deny dangerous tools."""
    from algo_cli import main as main_module, tool_runtime

    decisions: list[tuple[str, bool]] = []

    def _spy_agent_loop(_client, cfg, _msg):
        # Exercise the patched ask_approval directly to confirm the contract.
        for name in ["read_file", "write_file", "run_shell", "session_command"]:
            args = {"command": "/safe off"} if name == "session_command" else {}
            decisions.append((name, main_module.ask_approval(name, args, cfg)))
            decisions.append((f"runtime:{name}", tool_runtime.ask_approval(name, args, cfg)))

    monkeypatch.setattr(main_module, "agent_loop", _spy_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    oneshot.run_oneshot(prompt="x", approval_mode="never", stream=io.StringIO())
    assert decisions == [
        ("read_file", True),
        ("runtime:read_file", True),
        ("write_file", False),
        ("runtime:write_file", False),
        ("run_shell", False),
        ("runtime:run_shell", False),
        ("session_command", False),
        ("runtime:session_command", False),
    ]


def test_oneshot_never_uses_registry_approval_policy(monkeypatch):
    """Never mode must deny every mutation declared by the central action registry."""
    from algo_cli import main as main_module

    decisions: dict[str, bool] = {}
    approval_required = [
        "model_pull",
        "model_copy",
        "harness_refresh",
        "reindex_knowledge_graph",
        "plugins_load",
        "credential_helpers_store",
        "x_account_post",
    ]

    def _spy_agent_loop(_client, cfg, _msg):
        for name in approval_required:
            decisions[name] = main_module.ask_approval(name, {}, cfg)

    monkeypatch.setattr(main_module, "agent_loop", _spy_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    oneshot.run_oneshot(prompt="x", approval_mode="never", stream=io.StringIO())

    assert decisions == {name: False for name in approval_required}


def test_oneshot_ask_approval_never_overrides_persisted_auto_mode(monkeypatch):
    from algo_cli import main as main_module
    from algo_cli.config import Config

    decisions: list[tuple[str, bool]] = []

    cfg = Config()
    cfg.auto_mode = True
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))

    def _spy_agent_loop(_client, loaded_cfg, _msg):
        assert loaded_cfg.auto_mode is False
        for name in ["read_file", "write_file", "run_shell"]:
            decisions.append((name, main_module.ask_approval(name, {}, loaded_cfg)))

    monkeypatch.setattr(main_module, "agent_loop", _spy_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    oneshot.run_oneshot(prompt="x", approval_mode="never", stream=io.StringIO())
    assert decisions == [("read_file", True), ("write_file", False), ("run_shell", False)]


def test_run_oneshot_marks_done_partial_when_agent_loop_emits_error(monkeypatch):
    from algo_cli import display as display_module
    from algo_cli import main as main_module

    buf = io.StringIO()

    def _partial_agent_loop(_client, _cfg, _msg):
        display_module.show_stream_text("partial output")
        display_module.show_error("Response stream interrupted after partial output: socket closed")

    monkeypatch.setattr(main_module, "agent_loop", _partial_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    exit_code = oneshot.run_oneshot(prompt="x", approval_mode="never", stream=buf)
    events = _drain(buf)
    assert any(e["type"] == "content" and e["text"] == "partial output" for e in events)
    assert any(e["type"] == "error" and "socket closed" in e["message"] for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "partial"
    assert exit_code == 2


def test_run_oneshot_session_start_uses_cloud_host(monkeypatch):
    from algo_cli.config import Config

    monkeypatch.setenv("OLLAMA_API_KEY", "token")
    cfg = Config(model="minimax-m3", cloud=True, host="http://localhost:11434")
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        "algo_cli.main.agent_loop",
        lambda _client, _cfg, _msg: None,
    )
    monkeypatch.setattr("algo_cli.main.create_client", lambda _cfg: object())

    buf = io.StringIO()
    oneshot.run_oneshot(prompt="ping", approval_mode="never", stream=buf)
    events = _drain(buf)
    assert events[0]["host"] == "https://ollama.com"


def test_run_oneshot_restores_session_summary_after_run(monkeypatch, config_dir):
    from algo_cli.config import CONFIG_FILE, Config

    cfg = Config()
    cfg.session_summary = "prior audit thread must survive bridge runs"
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text('{"session_summary": "prior audit thread must survive bridge runs"}', encoding="utf-8")

    seen_summary: list[str] = []

    def _spy_agent_loop(_client, loaded_cfg, _msg):
        seen_summary.append(loaded_cfg.session_summary)

    monkeypatch.setattr("algo_cli.main.agent_loop", _spy_agent_loop)
    monkeypatch.setattr("algo_cli.main.create_client", lambda _cfg: object())

    oneshot.run_oneshot(prompt="hi", approval_mode="never", stream=io.StringIO())
    assert seen_summary == [""]
    reloaded = Config.load()
    assert reloaded.session_summary == "prior audit thread must survive bridge runs"


def test_run_oneshot_does_not_persist_transient_mode_fields(monkeypatch, config_dir):
    from algo_cli.config import CONFIG_FILE, Config

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(
            {
                "auto_mode": False,
                "skill_crystallize_enabled": True,
                "model": "original-model",
            }
        ),
        encoding="utf-8",
    )

    def _spy_agent_loop(_client, cfg, _msg):
        assert cfg.auto_mode is True
        assert cfg.skill_crystallize_enabled is False
        assert cfg.model == "bridge-model"
        cfg.save()

    monkeypatch.setattr("algo_cli.main.agent_loop", _spy_agent_loop)
    monkeypatch.setattr("algo_cli.main.create_client", lambda _cfg: object())

    oneshot.run_oneshot(
        prompt="hi",
        approval_mode="auto",
        cfg_overrides={"model": "bridge-model"},
        stream=io.StringIO(),
    )

    reloaded = Config.load()
    assert reloaded.auto_mode is False
    assert reloaded.skill_crystallize_enabled is True
    assert reloaded.model == "original-model"


def test_oneshot_auto_cannot_bypass_action_time_confirmation(monkeypatch):
    from algo_cli import main as main_module

    decisions: list[tuple[str, bool]] = []

    def _spy_agent_loop(_client, cfg, _msg):
        for name in ["read_file", "write_file", "run_shell"]:
            decisions.append((name, main_module.ask_approval(name, {}, cfg)))

    monkeypatch.setattr(main_module, "agent_loop", _spy_agent_loop)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    oneshot.run_oneshot(prompt="x", approval_mode="auto", stream=io.StringIO())
    assert decisions == [("read_file", True), ("write_file", False), ("run_shell", False)]
