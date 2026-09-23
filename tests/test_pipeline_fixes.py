from __future__ import annotations

import io
import json
import signal
import threading
import time
from typing import Any

import pytest

from algo_cli import agent_blocks, agent_pipeline, agent_threads, grace_key_store, irene_privacy_views
from algo_cli import oliver_oneshot as oneshot
from algo_cli import tool_runtime
from algo_cli.config import Config
from algo_cli.grace_key_store import StaticKeyStore
from algo_cli.grace_memory_receipts import ElsieReceiptAuthority
from algo_cli.irene_privacy_views import PRIVACY_KEY_LABEL


@pytest.fixture(autouse=True)
def _isolated_receipts(monkeypatch):
    monkeypatch.setattr(irene_privacy_views, "_PRIVACY_KEY", b"p" * 32)
    authority = ElsieReceiptAuthority.from_key_store(store=StaticKeyStore({PRIVACY_KEY_LABEL: b"p" * 32}))
    monkeypatch.setattr(ElsieReceiptAuthority, "from_key_store", classmethod(lambda _cls, **_kw: authority))
    monkeypatch.setattr(ElsieReceiptAuthority, "from_existing_key_store", classmethod(lambda _cls, **_kw: authority))

    def forbid_live_receipt_store(*_args, **_kwargs):
        raise AssertionError("tests must not open live receipt stores")

    monkeypatch.setattr(grace_key_store, "KeyringKeyStore", forbid_live_receipt_store)
    monkeypatch.setattr(grace_key_store, "GraceReceiptAnchorStore", forbid_live_receipt_store)


def _quiet(monkeypatch) -> list[str]:
    from algo_cli import continuum_memory

    monkeypatch.setattr(continuum_memory, "doctor", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(continuum_memory, "prompt_context", lambda *_a, **_k: "")
    errors: list[str] = []
    for name in (
        "show_agent_block_start",
        "show_agent_block_complete",
        "show_agent_recovery_start",
        "show_agent_pipeline_complete",
        "show_info",
        "finish_thinking_block",
        "show_recalled_context",
        "record_chat_metrics",
        "flush_perf_records",
    ):
        monkeypatch.setattr(agent_pipeline, name, lambda *_a, **_k: None)
    monkeypatch.setattr(agent_pipeline, "show_error", lambda message: errors.append(str(message)))
    monkeypatch.setattr(tool_runtime, "show_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(tool_runtime, "show_tool_result", lambda *_a, **_k: None)
    return errors


def test_oneshot_emits_done_when_config_save_refuses(monkeypatch):
    from algo_cli import main as main_module

    monkeypatch.setattr(main_module, "agent_loop", lambda *_a: None)
    monkeypatch.setattr(main_module, "create_client", lambda _cfg: object())

    def refuse_save(_self):
        raise RuntimeError("Memory configuration requires repair")

    # run_oneshot imports Config at call time; patch the live class so a module
    # reload elsewhere in the suite cannot leave this test holding a stale one.
    import algo_cli.config as config_module

    monkeypatch.setattr(config_module.Config, "save", refuse_save)
    buf = io.StringIO()

    exit_code = oneshot.run_oneshot(prompt="hi", stream=buf)

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "partial"
    assert any(e["type"] == "error" and "requires repair" in e["message"] for e in events)
    assert exit_code == 2


def test_run_agent_block_marks_model_stream_failure_as_failed(monkeypatch):
    _quiet(monkeypatch)
    completed: dict[str, Any] = {}
    monkeypatch.setattr(agent_pipeline, "show_agent_block_complete", lambda *_a, **kw: completed.update(kw))

    class DownClient:
        def chat(self, **_kwargs):
            raise ConnectionError("ollama is down")

    block = agent_blocks.AgentBlock(role="plan", prompt="p", allowed_tools=agent_blocks.NO_TOOLS)

    with pytest.raises(ConnectionError):
        agent_pipeline.run_agent_block(block, task="plan", completed=[], cfg=Config(), client=DownClient())

    assert block.status == "failed"
    assert block.status_code == "model_error"
    assert block.status_reason == "ConnectionError: ollama is down"
    assert completed["status"] == "failed"


@pytest.mark.parametrize("action", ["show", "switch", "resume", "fork"])
def test_plain_language_task_starting_with_thread_verb_runs_as_task(monkeypatch, action):
    _quiet(monkeypatch)
    started: list[str] = []

    def fake_run_pipeline(task, _cfg, _client, pipeline_name="default", **_kwargs):
        started.append(task)
        return agent_pipeline.AgentRunResult(thread_id="t", status="complete", pipeline=pipeline_name, output="ok")

    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", fake_run_pipeline)
    monkeypatch.setattr(agent_pipeline.memory_runtime, "capture_completed_user_turn", lambda *_a, **_k: {})

    agent_pipeline.execute_agent_command(f"{action} the failing tests and fix them", Config(), object())

    assert started == [f"{action} the failing tests and fix them"]


def test_unknown_thread_error_has_no_stray_quotes(monkeypatch):
    errors = _quiet(monkeypatch)

    result = agent_pipeline.execute_agent_command("show deadbeef", Config(), object())

    assert result == "Error: Unknown agent thread 'deadbeef'. Use /agent threads to list runs."
    assert errors == ["Unknown agent thread 'deadbeef'. Use /agent threads to list runs."]


def test_pipeline_flag_survives_apostrophes_and_keeps_inner_quotes():
    assert agent_pipeline.parse_agent_invocation_checked("--pipeline code-change Fix the user's login bug") == (
        "code-change",
        "Fix the user's login bug",
        "",
    )
    assert agent_pipeline.parse_agent_invocation('--pipeline code-change Replace "foo bar" with "baz"') == (
        "code-change",
        'Replace "foo bar" with "baz"',
    )


def test_team_task_with_apostrophe_is_accepted():
    roles, task, error = agent_pipeline.parse_agent_team_invocation("--roles scout,critic Review the user's auth flow")

    assert (roles, task, error) == (["scout", "critic"], "Review the user's auth flow", "")


def test_resume_task_with_apostrophe_is_accepted(monkeypatch):
    errors = _quiet(monkeypatch)
    resolved: list[str] = []

    def fake_resolve(ref, **_kwargs):
        resolved.append(ref)
        raise KeyError(f"Unknown agent thread '{ref}'. Use /agent threads to list runs.")

    monkeypatch.setattr(agent_threads, "resolve_thread", fake_resolve)

    agent_pipeline.execute_agent_command("resume abc12345 fix the user's bug", Config(), object())

    assert resolved == ["abc12345"]
    assert errors == ["Unknown agent thread 'abc12345'. Use /agent threads to list runs."]


def _interrupt_team_once_started(monkeypatch, started: dict[str, threading.Event]) -> None:
    def interrupted_as_completed(_futures):
        for event in started.values():
            assert event.wait(timeout=10)
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_pipeline, "as_completed", interrupted_as_completed)


def _join_team_workers() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("algo-agent"):
            thread.join(timeout=10)
            assert not thread.is_alive()


def _child_statuses(result) -> list[str]:
    return [agent_threads.resolve_thread(thread_id)["status"] for thread_id in result.children]


def test_team_interrupt_waits_for_cooperative_specialists_to_acknowledge(monkeypatch):
    cfg = Config()
    _quiet(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "create_client", lambda _cfg: object())
    monkeypatch.setattr(agent_pipeline, "TEAM_CANCEL_GRACE_SECONDS", 30.0)
    started = {role: threading.Event() for role in ("scout", "critic")}
    acknowledged = {role: threading.Event() for role in ("scout", "critic")}

    def cooperative_run_block(block, **kwargs):
        started[block.role].set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                agent_pipeline._raise_if_team_cancelled(kwargs["cfg"])
            except KeyboardInterrupt:
                acknowledged[block.role].set()
                raise
            time.sleep(0.01)
        raise AssertionError("specialist never observed team cancellation")

    monkeypatch.setattr(agent_pipeline, "run_agent_block", cooperative_run_block)
    _interrupt_team_once_started(monkeypatch, started)

    began = time.monotonic()
    result = agent_pipeline.run_agent_team("Review auth", cfg, object(), roles=["scout", "critic"])
    elapsed = time.monotonic() - began

    assert result.status == "cancelled"
    assert result.error == "Team run cancelled."
    assert all(event.is_set() for event in acknowledged.values())
    # Generous bound: returning must not wait out the 30s grace when workers cooperate.
    assert elapsed < 20
    assert _child_statuses(result) == ["cancelled", "cancelled"]
    assert [block["status_code"] for block in result.blocks] == ["", ""]
    _join_team_workers()


def test_team_interrupt_reports_non_cooperative_specialist_as_detached(monkeypatch):
    cfg = Config()
    _quiet(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "create_client", lambda _cfg: object())
    monkeypatch.setattr(agent_pipeline, "TEAM_CANCEL_GRACE_SECONDS", 0.2)
    started = {role: threading.Event() for role in ("scout", "critic")}
    release = threading.Event()

    def mixed_run_block(block, **kwargs):
        started[block.role].set()
        if block.role == "critic":
            assert release.wait(timeout=30)
            block.status = "complete"
            block.output = "## Block Output\nlate evidence"
            return
        while True:
            agent_pipeline._raise_if_team_cancelled(kwargs["cfg"])
            time.sleep(0.01)

    monkeypatch.setattr(agent_pipeline, "run_agent_block", mixed_run_block)
    _interrupt_team_once_started(monkeypatch, started)

    try:
        result = agent_pipeline.run_agent_team("Review auth", cfg, object(), roles=["scout", "critic"])
    finally:
        release.set()
    _join_team_workers()

    assert result.status == "cancelled"
    assert "detached: critic." in result.error
    assert [(block["role"], block["status_code"]) for block in result.blocks] == [
        ("scout", ""),
        ("critic", "specialist_detached"),
    ]
    assert _child_statuses(result) == ["cancelled", "cancelled"]


def test_late_specialist_cannot_overwrite_cancelled_thread_status(monkeypatch):
    from algo_cli import git_evidence

    cfg = Config()
    _quiet(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "create_client", lambda _cfg: object())
    monkeypatch.setattr(agent_pipeline, "TEAM_CANCEL_GRACE_SECONDS", 0.2)
    started = {role: threading.Event() for role in ("scout", "critic")}
    late_threads: set[str] = set()
    at_late_write = threading.Event()
    release = threading.Event()
    real_snapshot = git_evidence.capture_git_snapshot

    def paused_snapshot(*args, **kwargs):
        snapshot = real_snapshot(*args, **kwargs)
        if threading.current_thread().name in late_threads:
            # The block passed its cancellation check; hold it just before the status write.
            at_late_write.set()
            assert release.wait(timeout=30)
        return snapshot

    def finishing_run_block(block, **kwargs):
        block.status = "complete"
        block.output = "## Block Output\nevidence"
        if block.role == "critic":
            late_threads.add(threading.current_thread().name)
        started[block.role].set()
        if block.role == "scout":
            while True:
                agent_pipeline._raise_if_team_cancelled(kwargs["cfg"])
                time.sleep(0.01)

    def interrupted_as_completed(_futures):
        for event in started.values():
            assert event.wait(timeout=10)
        assert at_late_write.wait(timeout=10)
        raise KeyboardInterrupt

    writes: list[tuple[str, str]] = []
    real_update = agent_threads.update_thread

    def recording_update(thread_id, **kwargs):
        writes.append((thread_id, str(kwargs.get("status", ""))))
        return real_update(thread_id, **kwargs)

    monkeypatch.setattr(git_evidence, "capture_git_snapshot", paused_snapshot)
    monkeypatch.setattr(agent_threads, "update_thread", recording_update)
    monkeypatch.setattr(agent_pipeline, "run_agent_block", finishing_run_block)
    monkeypatch.setattr(agent_pipeline, "as_completed", interrupted_as_completed)

    try:
        result = agent_pipeline.run_agent_team("Review auth", cfg, object(), roles=["scout", "critic"])
        assert _child_statuses(result) == ["cancelled", "cancelled"]
    finally:
        release.set()
    _join_team_workers()

    assert "detached: critic." in result.error
    critic_id = result.children[1]
    assert (critic_id, "running") not in writes
    assert _child_statuses(result) == ["cancelled", "cancelled"]


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="needs POSIX signal delivery to the main thread")
def test_second_ctrl_c_during_status_lock_wait_still_cancels_specialists(monkeypatch):
    cfg = Config()
    _quiet(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "create_client", lambda _cfg: object())
    monkeypatch.setattr(agent_pipeline, "TEAM_CANCEL_GRACE_SECONDS", 10.0)
    in_locked_write = threading.Event()
    release = threading.Event()
    observed_cancel: list[str] = []
    real_capture = agent_pipeline._capture_thread_workspace

    def paused_capture(capture_cfg, block=None):
        # The specialist's begin-turn capture runs inside _team_status_write, holding the lock.
        if threading.current_thread().name.startswith("algo-agent") and block is None:
            if not in_locked_write.is_set():
                in_locked_write.set()
                assert release.wait(timeout=30)
        return real_capture(capture_cfg, block)

    def cooperative_run_block(block, **kwargs):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                agent_pipeline._raise_if_team_cancelled(kwargs["cfg"])
            except KeyboardInterrupt:
                observed_cancel.append(block.role)
                raise
            time.sleep(0.01)
        raise AssertionError("specialist never observed team cancellation")

    main_ident = threading.get_ident()

    def second_interrupt_then_release():
        signal.pthread_kill(main_ident, signal.SIGINT)
        time.sleep(0.3)
        release.set()

    def interrupted_as_completed(_futures):
        assert in_locked_write.wait(timeout=10)
        # The user presses Ctrl+C again while the handler waits for the status lock.
        threading.Timer(0.3, second_interrupt_then_release).start()
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_pipeline, "_capture_thread_workspace", paused_capture)
    monkeypatch.setattr(agent_pipeline, "run_agent_block", cooperative_run_block)
    monkeypatch.setattr(agent_pipeline, "as_completed", interrupted_as_completed)

    try:
        result = agent_pipeline.run_agent_team("Review auth", cfg, object(), roles=["scout", "critic"])
    finally:
        release.set()
    _join_team_workers()

    assert result.status == "cancelled"
    # The paused specialist may stop at its next status write instead of inside the block.
    assert observed_cancel
    assert _child_statuses(result) == ["cancelled", "cancelled"]


def test_run_agent_block_runs_in_delegated_scope_on_worker_threads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from algo_cli import session_mode

    observed: list[bool] = []

    def spy_policy(*_args, **_kwargs):
        observed.append(session_mode._DELEGATED.get())
        raise RuntimeError("stop after scope check")

    monkeypatch.setattr(agent_pipeline.tool_policy, "compute_policy", spy_policy)
    block = agent_blocks.AgentBlock(role="scout", prompt="look")

    def run_in_worker():
        with pytest.raises(RuntimeError):
            agent_pipeline.run_agent_block(block, task="plan", completed=[], cfg=Config(), client=object())

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run_in_worker).result()

    assert observed == [True]
    assert session_mode._DELEGATED.get() is False


@pytest.mark.parametrize(
    ("command", "ref"),
    [
        ("show ab", "ab"),
        ("switch a1", "a1"),
        ("resume a1b", "a1b"),
        ("fork 9f fix it", "9f"),
        ("resume abcx", "abcx"),
    ],
)
def test_short_and_mistyped_thread_refs_resolve_instead_of_running_task(monkeypatch, command, ref):
    errors = _quiet(monkeypatch)
    resolved: list[str] = []

    def fake_resolve(value, **_kwargs):
        resolved.append(value)
        raise KeyError(f"Unknown agent thread '{value}'. Use /agent threads to list runs.")

    def forbid_pipeline(*_args, **_kwargs):
        raise AssertionError("thread command must not start a pipeline")

    monkeypatch.setattr(agent_threads, "resolve_thread", fake_resolve)
    monkeypatch.setattr(agent_pipeline, "show_agent_thread", lambda value, _cfg: fake_resolve(value))
    monkeypatch.setattr(agent_pipeline, "run_agent_pipeline", forbid_pipeline)

    result = agent_pipeline.execute_agent_command(command, Config(), object())

    assert resolved == [ref]
    assert result == f"Error: Unknown agent thread '{ref}'. Use /agent threads to list runs."
    assert errors == [f"Unknown agent thread '{ref}'. Use /agent threads to list runs."]


@pytest.mark.parametrize(
    "arg",
    [
        "review the code --roles planner,critic",
        "review the code --roles=planner,critic",
        "review --roles planner,critic the code",
    ],
)
def test_team_roles_option_is_parsed_anywhere(arg):
    roles, task, error = agent_pipeline.parse_agent_team_invocation(arg)

    assert error == ""
    assert roles == ["planner", "critic"]
    assert task == "review the code"


def test_team_trailing_roles_without_value_is_usage_error():
    assert agent_pipeline.parse_agent_team_invocation("review the code --roles") == (
        [],
        "",
        agent_pipeline.AGENT_TEAM_USAGE,
    )
