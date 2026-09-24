"""One cooperative cancel token per turn: semantics, process-tree kill, team members and approvals."""

from __future__ import annotations

import contextvars
import copy
import functools
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

import pytest

from algo_cli import agent_pipeline, cancellation, main, nathan_runtime, tools
from algo_cli.cancellation import ApprovalCancelled, Cancelled, CancelToken
from algo_cli.config import Config
from algo_cli.james_dispatch import DispatchInterrupted, dispatch_action
from test_agent_progress_recovery import run_script
from test_james_dispatch import _dependencies
from test_pipeline_fixes import _child_statuses, _interrupt_team_once_started, _join_team_workers, _quiet


# ---- token semantics ---------------------------------------------------------------------------


def test_cancel_is_idempotent_and_keeps_first_reason():
    token = CancelToken("turn")
    assert not token.is_cancelled and not token.is_set() and token.reason == ""
    token.raise_if_cancelled()

    assert token.cancel(cancellation.KEYBOARD_INTERRUPT) is True
    assert token.cancel(cancellation.TEAM_CANCELLED) is False

    assert token.is_cancelled and token.is_set()
    assert token.reason == cancellation.KEYBOARD_INTERRUPT
    with pytest.raises(Cancelled) as raised:
        token.raise_if_cancelled()
    assert isinstance(raised.value, KeyboardInterrupt)
    assert raised.value.reason == cancellation.KEYBOARD_INTERRUPT


def test_reason_must_be_a_bounded_identifier():
    with pytest.raises(ValueError):
        CancelToken().cancel("free text with spaces")
    with pytest.raises(ValueError):
        CancelToken().cancel("")


def test_raise_if_cancelled_names_team_and_approval_reasons():
    team = CancelToken()
    team.cancel(cancellation.TEAM_CANCELLED)
    with pytest.raises(Cancelled, match="Agent team cancelled"):
        team.raise_if_cancelled()
    approval = CancelToken()
    approval.cancel(cancellation.APPROVAL_CANCELLED)
    with pytest.raises(ApprovalCancelled):
        approval.raise_if_cancelled()


def test_wait_times_out_then_returns_when_cancelled_from_another_thread():
    token = CancelToken()
    assert token.wait(0.01) is False
    timer = threading.Timer(0.05, token.cancel)
    timer.start()
    try:
        assert token.wait(5) is True
    finally:
        timer.join()


def test_on_cancel_runs_once_immediately_when_late_and_can_unregister():
    token = CancelToken()
    calls: list[str] = []
    token.on_cancel(lambda t: calls.append(f"a:{t.reason}"))
    unregister = token.on_cancel(lambda _t: calls.append("removed"))
    token.on_cancel(lambda _t: 1 / 0)  # A failing listener must not stop the others.
    token.on_cancel(lambda _t: calls.append("b"))
    unregister()

    token.cancel("stop")
    token.cancel("again")
    token.on_cancel(lambda t: calls.append(f"late:{t.reason}"))

    assert calls == ["a:stop", "b", "late:stop"]


def test_parent_cancel_reaches_children_but_child_cancel_stays_local():
    team = CancelToken("team")
    scout, critic = team.child("scout"), team.child("critic")
    grandchild = scout.child("tool")

    critic.cancel("critic_only")
    assert not team.is_cancelled and not scout.is_cancelled

    team.cancel(cancellation.TEAM_CANCELLED)
    assert scout.reason == grandchild.reason == cancellation.TEAM_CANCELLED
    assert critic.reason == "critic_only"
    late = team.child("late")
    assert late.is_cancelled and late.reason == cancellation.TEAM_CANCELLED


def test_concurrent_cancel_has_exactly_one_winner_and_one_callback():
    token = CancelToken()
    callbacks: list[int] = []
    token.on_cancel(lambda _t: callbacks.append(1))
    barrier = threading.Barrier(16)
    wins: list[bool] = []

    def racer(index: int) -> None:
        barrier.wait(timeout=5)
        wins.append(token.cancel(f"racer-{index}"))

    threads = [threading.Thread(target=racer, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert wins.count(True) == 1 and len(wins) == 16
    assert callbacks == [1]


def test_config_copies_share_the_token_by_reference():
    token = CancelToken()
    assert copy.copy(token) is token
    assert copy.deepcopy({"token": token})["token"] is token


def test_turn_scope_binds_nests_restores_and_cancels_on_ctrl_c():
    assert cancellation.current_token() is None
    with cancellation.turn_scope() as outer:
        assert cancellation.current_token() is outer
        with pytest.raises(KeyboardInterrupt):
            with cancellation.turn_scope() as inner:
                assert inner.parent is outer
                raise KeyboardInterrupt
        assert inner.reason == cancellation.KEYBOARD_INTERRUPT
        assert not outer.is_cancelled
        assert cancellation.current_token() is outer
        with pytest.raises(ApprovalCancelled):
            with cancellation.turn_scope() as approval_turn:
                raise ApprovalCancelled()
        assert approval_turn.reason == cancellation.APPROVAL_CANCELLED
        seen = contextvars.copy_context().run(cancellation.current_token)
        assert seen is outer
    assert cancellation.current_token() is None


def test_scoped_turn_preserves_the_wrapped_signature():
    import inspect

    assert "task" in inspect.signature(agent_pipeline.run_agent_pipeline).parameters


# ---- run_shell / write_file --------------------------------------------------------------------


def _wait_for_pid(pid_file: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = pid_file.read_text().strip() if pid_file.exists() else ""
        if text:
            return int(text)
        time.sleep(0.02)
    raise AssertionError("shell never reported its child pid")


def _process_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - pid reused by another user
            return True
        time.sleep(0.05)
    return False


SLEEP_CHILD = "sleep 30 & echo $! > child.pid; wait"


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX process groups")
def test_turn_cancel_kills_run_shell_child_process_tree(tmp_path):
    token = CancelToken("turn")

    def cancel_once_child_runs() -> None:
        _wait_for_pid(tmp_path / "child.pid")
        token.cancel(cancellation.KEYBOARD_INTERRUPT)

    canceller = threading.Thread(target=cancel_once_child_runs)
    canceller.start()
    started = time.monotonic()
    out = tools.run_shell(SLEEP_CHILD, cwd=str(tmp_path), timeout=60, cancel_event=token)
    canceller.join(timeout=10)

    assert out == tools.TURN_CANCELLED_SHELL
    assert time.monotonic() - started < 10
    assert _process_gone(_wait_for_pid(tmp_path / "child.pid"))
    assert nathan_runtime.classify_tool_status(out, name="run_shell") == "failed"


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="needs POSIX signal delivery to the main thread")
def test_ctrl_c_during_run_shell_kills_child_process_tree(tmp_path):
    main_ident = threading.get_ident()

    def ctrl_c_once_child_runs() -> None:
        _wait_for_pid(tmp_path / "child.pid")
        signal.pthread_kill(main_ident, signal.SIGINT)

    sender = threading.Thread(target=ctrl_c_once_child_runs)
    sender.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            with cancellation.turn_scope() as turn:
                cfg = Config(cwd=str(tmp_path), continuum_enabled=False)
                cfg.safe_mode = False
                nathan_runtime.run_tool("run_shell", {"command": SLEEP_CHILD}, cfg)
    finally:
        sender.join(timeout=10)

    assert turn.reason == cancellation.KEYBOARD_INTERRUPT
    assert _process_gone(_wait_for_pid(tmp_path / "child.pid"))


def test_write_file_refuses_after_turn_cancel_with_turn_wording(tmp_path):
    token = CancelToken()
    token.cancel(cancellation.KEYBOARD_INTERRUPT)
    target = tmp_path / "late.txt"

    assert tools.write_file(str(target), "x", cancel_event=token) == tools.TURN_CANCELLED_WRITE
    assert tools.run_shell("printf x", cwd=str(tmp_path), cancel_event=token) == tools.TURN_CANCELLED_SHELL_NOT_RUN
    assert not target.exists()
    assert nathan_runtime.classify_tool_status(tools.TURN_CANCELLED_WRITE, name="write_file") == "failed"


class _FakeWindowsProcess:
    pid = 4242
    args = "ping -t localhost"
    returncode = None

    def __init__(self) -> None:
        self.terminated = threading.Event()

    def communicate(self, timeout=None):
        if self.terminated.is_set():
            return "", ""
        time.sleep(min(timeout or 0.05, 0.05))
        raise subprocess.TimeoutExpired(self.args, timeout)

    def kill(self) -> None:  # pragma: no cover - taskkill succeeds in this fixture
        self.terminated.set()


def test_simulated_windows_cancel_uses_new_process_group_and_taskkill_tree(monkeypatch, tmp_path):
    proc = _FakeWindowsProcess()
    popen_kwargs: dict = {}
    taskkills: list[list[str]] = []

    def fake_popen(_command, **kwargs):
        popen_kwargs.update(kwargs)
        return proc

    def fake_run(argv, **_kwargs):
        taskkills.append(list(argv))
        proc.terminated.set()
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    real_group_kwargs = tools._isolated_process_group_kwargs
    real_terminate = tools._terminate_process_tree
    monkeypatch.setattr(tools, "_isolated_process_group_kwargs", lambda: real_group_kwargs("nt"))
    monkeypatch.setattr(tools, "_terminate_process_tree", functools.partial(real_terminate, platform_name="nt"))
    monkeypatch.setattr(tools.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    team = CancelToken("team")
    member = team.child("scout")
    threading.Timer(0.2, team.cancel, args=(cancellation.TEAM_CANCELLED,)).start()

    out = tools.run_shell("ping -t localhost", cwd=str(tmp_path), timeout=30, cancel_event=member)

    assert out == tools.TEAM_CANCELLED_SHELL
    assert popen_kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert "start_new_session" not in popen_kwargs
    assert taskkills == [["taskkill", "/PID", "4242", "/T", "/F"]]


# ---- dispatch and approvals --------------------------------------------------------------------


def test_dispatch_never_invokes_after_the_ambient_token_is_cancelled(tmp_path):
    invoked: list[str] = []
    deps = _dependencies(tmp_path, lambda name, *_a: invoked.append(name) or "ran")
    deps.approve = lambda *_a, **_k: True
    token = CancelToken()
    token.cancel(cancellation.TEAM_CANCELLED)

    with cancellation.bind(token):
        result = dispatch_action(
            "read_file", {"path": "README.md"}, Config(cwd=str(tmp_path)), tool_call_id="late", dependencies=deps,
            render=False,
        )

    assert invoked == []
    assert result.outcome.invoked is False
    assert result.status == "cancelled"
    assert result.outcome.error_code == cancellation.TEAM_CANCELLED


def test_ctrl_c_at_approval_prompt_is_approval_cancelled(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), auto_mode=True)

    def ctrl_c(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", ctrl_c)
    with cancellation.turn_scope() as turn:
        with pytest.raises(ApprovalCancelled):
            nathan_runtime.ask_approval("remember", {"fact": "bounded"}, cfg)
        assert turn.reason == cancellation.APPROVAL_CANCELLED


def test_cancel_elsewhere_while_prompt_waits_does_not_approve(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), auto_mode=True)
    token = CancelToken()

    def answer_yes_after_cancel(_prompt=""):
        token.cancel(cancellation.TEAM_CANCELLED)
        return "y"

    monkeypatch.setattr("builtins.input", answer_yes_after_cancel)
    with cancellation.bind(token), pytest.raises(ApprovalCancelled):
        nathan_runtime.ask_approval("remember", {"fact": "bounded"}, cfg)
    # Already cancelled: no prompt opens at all.
    monkeypatch.setattr("builtins.input", lambda _prompt="": pytest.fail("prompt opened after cancel"))
    with cancellation.bind(token), pytest.raises(Cancelled):
        nathan_runtime.ask_approval("remember", {"fact": "bounded"}, cfg)


def test_dispatch_records_approval_cancelled_distinctly(tmp_path):
    deps = _dependencies(tmp_path, lambda *_a: pytest.fail("cancelled approval must not invoke"))

    def cancelled_prompt(*_a, **_k):
        raise ApprovalCancelled()

    deps.approve = cancelled_prompt
    with pytest.raises(DispatchInterrupted) as raised:
        dispatch_action(
            "read_file", {"path": "README.md"}, Config(cwd=str(tmp_path)), tool_call_id="w",
            dependencies=deps, render=False,
        )

    outcome = raised.value.result.outcome
    assert outcome.invoked is False
    assert outcome.error_code == cancellation.APPROVAL_CANCELLED
    assert "Approval was cancelled" in raised.value.result.result


def test_approval_cancel_tags_only_the_prompted_call_in_a_batch(tmp_path):
    from algo_cli.james_dispatch import DispatchCancellation

    invoked: list[str] = []
    deps = _dependencies(tmp_path, lambda name, args, _cfg: invoked.append(args["path"]) or "ok")
    prompted: list[str] = []

    def cancel_first_prompt(name, *_a, **_k):
        prompted.append(name)
        if len(prompted) == 1:
            raise ApprovalCancelled()
        return True

    deps.approve = cancel_first_prompt
    batch = DispatchCancellation()
    cfg = Config(cwd=str(tmp_path))
    with pytest.raises(DispatchInterrupted) as raised:
        dispatch_action(
            "read_file", {"path": "OTHER.md"}, cfg, tool_call_id="a", dependencies=deps, render=False,
            cancellation=batch,
        )
    batch.cancel(cancellation.KEYBOARD_INTERRUPT)  # what main.dispatch_in_batch does; first reason wins
    follower = dispatch_action(
        "read_file", {"path": "README.md"}, cfg, tool_call_id="b", dependencies=deps, render=False,
        cancellation=batch,
    )

    assert raised.value.result.outcome.error_code == cancellation.APPROVAL_CANCELLED
    assert prompted == ["read_file"]
    assert invoked == []
    assert follower.status == "cancelled"
    assert follower.outcome.invoked is False
    assert follower.outcome.error_code == cancellation.KEYBOARD_INTERRUPT
    assert "Approval was cancelled" not in follower.result
    # The batch still remembers why it stopped, so the turn raises ApprovalCancelled.
    assert batch.reason_code == cancellation.APPROVAL_CANCELLED
    assert isinstance(cancellation.interruption_for(batch.reason_code), ApprovalCancelled)


def test_token_cancelled_by_approval_does_not_label_unprompted_calls(tmp_path):
    deps = _dependencies(tmp_path, lambda *_a: pytest.fail("cancelled turn must not invoke"))
    deps.approve = lambda *_a, **_k: pytest.fail("cancelled turn must not prompt")
    token = CancelToken("turn")
    token.cancel(cancellation.APPROVAL_CANCELLED)

    with cancellation.bind(token):
        result = dispatch_action(
            "read_file", {"path": "README.md"}, Config(cwd=str(tmp_path)), tool_call_id="late",
            dependencies=deps, render=False,
        )

    assert result.status == "cancelled"
    assert result.outcome.error_code == cancellation.KEYBOARD_INTERRUPT
    assert "Approval was cancelled" not in result.result


def test_repl_messages_distinguish_approval_cancel_from_generation_interrupt():
    approval = main.generation_interrupted_message(ApprovalCancelled())
    generation = main.generation_interrupted_message(KeyboardInterrupt())

    assert approval == "Approval cancelled. The pending action was not approved or run."
    assert generation == "Generation interrupted."


def test_oneshot_approval_cancel_is_reported_apart_from_interrupt(monkeypatch, tmp_path):
    def cancelled_prompt(*_a, **_k):
        raise ApprovalCancelled()

    code, events, client, invoked, _captures = run_script(
        monkeypatch,
        tmp_path,
        lambda _turn: {
            "tool_calls": [
                {"id": name, "function": {"name": "run_shell", "arguments": {"command": f"printf {name}"}}}
                for name in ("first", "second")
            ]
        },
        approve=cancelled_prompt,
    )

    assert code == 2 and len(client.calls) == 1 and invoked == []
    assert [item["status"] for item in events if item["type"] == "tool_result"] == ["cancelled", "cancelled"]
    assert events[-1]["status_reason"] == "approval_cancelled"


# ---- team members ------------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX process groups")
def test_team_cancel_stops_a_members_in_flight_run_shell(monkeypatch, tmp_path):
    cfg = Config(cwd=str(tmp_path), continuum_enabled=False)
    cfg.safe_mode = False
    _quiet(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "create_client", lambda _cfg: object())
    monkeypatch.setattr(agent_pipeline, "TEAM_CANCEL_GRACE_SECONDS", 20.0)
    started = {"scout": threading.Event()}
    results: dict[str, str] = {}
    member_tokens: list[CancelToken | None] = []

    def shell_running_block(block, **kwargs):
        if block.role == "critic":
            while True:
                agent_pipeline._raise_if_team_cancelled(kwargs["cfg"])
                time.sleep(0.01)
        member_tokens.append(cancellation.current_token())
        started[block.role].set()
        results[block.role] = nathan_runtime.run_tool("run_shell", {"command": SLEEP_CHILD}, kwargs["cfg"])
        agent_pipeline._raise_if_team_cancelled(kwargs["cfg"])
        raise AssertionError("specialist kept running after team cancellation")

    monkeypatch.setattr(agent_pipeline, "run_agent_block", shell_running_block)

    def interrupted_as_completed(_futures):
        assert started["scout"].wait(timeout=10)
        _wait_for_pid(tmp_path / "child.pid")
        raise KeyboardInterrupt

    monkeypatch.setattr(agent_pipeline, "as_completed", interrupted_as_completed)
    began = time.monotonic()
    result = agent_pipeline.run_agent_team("Review auth", cfg, object(), roles=["scout", "critic"])
    _join_team_workers()

    assert (result.status, result.error) == ("cancelled", "Team run cancelled."), results
    assert time.monotonic() - began < 15
    assert results == {"scout": tools.TEAM_CANCELLED_SHELL}
    assert member_tokens[0] is not None and member_tokens[0].reason == cancellation.TEAM_CANCELLED
    assert _process_gone(_wait_for_pid(tmp_path / "child.pid"))
    assert _child_statuses(result) == ["cancelled", "cancelled"]
    assert [block["status_code"] for block in result.blocks] == ["", ""]
    assert cancellation.current_token() is None


def test_team_members_get_distinct_child_tokens_and_leave_no_threads(monkeypatch):
    cfg = Config()
    _quiet(monkeypatch)
    monkeypatch.setattr(agent_pipeline, "create_client", lambda _cfg: object())
    monkeypatch.setattr(agent_pipeline, "TEAM_CANCEL_GRACE_SECONDS", 20.0)
    started = {role: threading.Event() for role in ("scout", "critic")}
    tokens: dict[str, CancelToken | None] = {}

    def cooperative_block(block, **kwargs):
        tokens[block.role] = cancellation.current_token()
        started[block.role].set()
        while True:
            agent_pipeline._raise_if_team_cancelled(kwargs["cfg"])
            time.sleep(0.01)

    monkeypatch.setattr(agent_pipeline, "run_agent_block", cooperative_block)
    _interrupt_team_once_started(monkeypatch, started)
    before = {thread.ident for thread in threading.enumerate()}

    with cancellation.turn_scope() as turn:
        result = agent_pipeline.run_agent_team("Review auth", cfg, object(), roles=["scout", "critic"])
    _join_team_workers()

    assert result.status == "cancelled"
    scout, critic = tokens["scout"], tokens["critic"]
    assert scout is not None and critic is not None and scout is not critic
    assert scout.parent is critic.parent and scout.parent.parent is turn
    assert not turn.is_cancelled  # Team cancel is local to the team.
    assert {thread.ident for thread in threading.enumerate() if thread.is_alive()} <= before
