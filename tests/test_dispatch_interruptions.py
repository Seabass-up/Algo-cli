"""User interruption must stop dispatch without losing uncertain-effect receipts."""

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time

import pytest

from algo_cli import agent_blocks, agent_pipeline, main, nathan_program_runtime as programs, tools
from algo_cli import nathan_runtime as runtime
from algo_cli.clara_effect_ledger import EffectState
from algo_cli.config import Config
from algo_cli.james_dispatch import DispatchInterrupted, dispatch_action
from test_agent_progress_recovery import run_script
from test_agent_pipeline import ScriptedClient, _quiet_display
from test_james_dispatch import _dependencies
from test_nathan_program_runtime import _authorization, _program_store


@pytest.mark.parametrize(
    "name,args,expected_status",
    [
        ("read_file", {"path": "README.md"}, "cancelled"),
        ("remember", {"fact": "interrupted fixture"}, "unknown_outcome"),
        ("x_account_post", {"text": "fixture only; never sent"}, "unknown_outcome"),
    ],
)
def test_dispatch_records_then_propagates_keyboard_interrupt(tmp_path, name, args, expected_status):
    def invoke(*_args):
        raise KeyboardInterrupt

    cfg = Config(cwd=str(tmp_path))
    deps = _dependencies(tmp_path, invoke)
    deps.approve = lambda *_args, **_kwargs: True
    with pytest.raises(DispatchInterrupted) as raised:
        dispatch_action(name, args, cfg, tool_call_id="interrupt-1", dependencies=deps, render=False)

    assert raised.value.result.outcome.invoked is True
    assert cfg.attempt_ledger[-1]["status"] == expected_status
    assert not hasattr(cfg, "_nathan_retry_slots")
    if expected_status == "unknown_outcome":
        signature = runtime.tool_attempt_signature(name, args)
        assert runtime.find_failed_attempt(cfg, signature)["retry_allowed"] is False
    if name == "x_account_post":
        assert deps.effect_ledger.get(raised.value.result.outcome.effect_id).state is EffectState.UNKNOWN


def test_oneshot_interrupt_stops_remaining_tools_and_model_rounds(monkeypatch, tmp_path):
    def responses(turn):
        if turn > 1:
            return {"content": "Incorrectly continued after cancellation."}
        return {
            "tool_calls": [
                {"id": "interrupted", "function": {"name": "run_shell", "arguments": {"command": "printf first"}}},
                {"id": "must-not-run", "function": {"name": "run_shell", "arguments": {"command": "printf second"}}},
            ]
        }

    def invoke(_name, args, _cfg):
        if args["command"] == "printf first":
            raise KeyboardInterrupt
        (tmp_path / "unexpected-effect").write_text("later action executed")
        return "second\n[exit code: 0]"

    code, events, client, invocations, captures = run_script(
        monkeypatch,
        tmp_path,
        responses,
        approve=lambda *_args, **_kwargs: True,
        invoke=invoke,
    )

    assert code == 2
    assert len(client.calls) == 1
    assert invocations == ["run_shell"]
    assert not (tmp_path / "unexpected-effect").exists()
    assert not any(item.get("completed") for item in captures)
    assert events[-1]["status_reason"] == "interrupted"
    calls = [event["call_id"] for event in events if event["type"] == "tool_call"]
    results = [event["call_id"] for event in events if event["type"] == "tool_result"]
    assert calls == results == ["interrupted", "must-not-run"]


def test_interrupt_during_approval_never_invokes_or_asks_about_next_action(monkeypatch, tmp_path):
    approvals = []

    def approve(*_args, **_kwargs):
        approvals.append(True)
        raise KeyboardInterrupt

    code, events, client, invoked, _captures = run_script(
        monkeypatch,
        tmp_path,
        lambda _turn: {
            "tool_calls": [
                {"id": name, "function": {"name": "run_shell", "arguments": {"command": f"printf {name}"}}}
                for name in ("first", "second")
            ]
        },
        approve=approve,
    )
    assert code == 2 and len(client.calls) == 1
    assert approvals == [True] and invoked == []
    assert [item["status"] for item in events if item["type"] == "tool_result"] == ["cancelled", "cancelled"]
    assert events[-1]["status_reason"] == "interrupted"


@pytest.mark.parametrize("origin", ["worker", "coordinator"])
def test_parallel_observation_interrupt_keeps_results_balanced(monkeypatch, tmp_path, origin):
    barrier = threading.Barrier(2)
    entered = threading.Event()
    release = threading.Event()

    def invoke(_name, args, _cfg):
        if origin == "worker":
            barrier.wait(timeout=5)
            if args["path"] == "first":
                raise KeyboardInterrupt
        else:
            entered.set()
            assert release.wait(timeout=5)
        return "observation already in flight"

    def interrupt_wait(_futures):
        assert entered.wait(timeout=5)
        release.set()
        raise KeyboardInterrupt

    if origin == "coordinator":
        monkeypatch.setattr(main, "as_completed", interrupt_wait)
    code, events, client, invoked, _captures = run_script(
        monkeypatch,
        tmp_path,
        lambda _turn: {
            "tool_calls": [
                {"id": path, "function": {"name": "read_file", "arguments": {"path": path}}}
                for path in ("first", "second")
            ]
        },
        invoke=invoke,
    )
    assert code == 2 and len(client.calls) == 1
    assert 1 <= len(invoked) <= 2
    assert [item["call_id"] for item in events if item["type"] == "tool_result"] == ["first", "second"]
    assert events[-1]["status_reason"] == "interrupted"


@pytest.mark.parametrize("interrupt_at", ["invocation", "verifier"])
def test_external_interrupt_preserves_unknown_effect_without_further_verification(tmp_path, interrupt_at):
    verified = []

    def invoke(*_args):
        if interrupt_at == "invocation":
            raise KeyboardInterrupt
        return '{"ok": true}'

    def verify(*_args):
        verified.append(True)
        raise KeyboardInterrupt

    cfg = Config(cwd=str(tmp_path))
    deps = _dependencies(tmp_path, invoke, verifiers={"x_account_post": verify})
    deps.approve = lambda *_args, **_kwargs: True
    with pytest.raises(DispatchInterrupted) as raised:
        dispatch_action(
            "x_account_post", {"text": "fixture only"}, cfg, tool_call_id="post", dependencies=deps, render=False
        )
    outcome = raised.value.result.outcome
    assert outcome.is_unknown and not outcome.retry_allowed
    assert deps.effect_ledger.get(outcome.effect_id).state is EffectState.UNKNOWN
    assert verified == ([] if interrupt_at == "invocation" else [True])
    assert not hasattr(cfg, "_nathan_retry_slots")


def test_pipeline_interrupt_retains_results_and_cancels_siblings(monkeypatch, tmp_path):
    _quiet_display(monkeypatch)
    calls = []

    def invoke(_name, args, _cfg):
        calls.append(args["path"])
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "run_tool", invoke)
    cfg = Config(cwd=str(tmp_path), model="fixture")
    block = agent_blocks.AgentBlock(role="review", prompt="Inspect files", allowed_tools=frozenset({"read_file"}))
    client = ScriptedClient(
        [
            {
                "tool_calls": [
                    {"id": name, "function": {"name": "read_file", "arguments": {"path": name}}}
                    for name in ("first", "second")
                ]
            }
        ]
    )
    with pytest.raises(KeyboardInterrupt):
        agent_pipeline.run_agent_block(block, task="Inspect files", completed=[], cfg=cfg, client=client)
    assert calls == ["first"] and len(client.calls) == 1
    assert block.status == "cancelled"
    assert [item["tool_call_id"] for item in block.messages if item["role"] == "tool"] == ["first", "second"]


@pytest.mark.parametrize("action", ["read_file", "run_shell"])
def test_program_interrupt_persists_its_receipt_before_propagating(monkeypatch, tmp_path, action):
    calls = []

    def invoke(name, _args, _cfg):
        calls.append(name)
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "run_tool", invoke)
    monkeypatch.setattr(runtime, "ask_approval", lambda *_args, **_kwargs: True)
    store = _program_store(tmp_path)
    steps = (
        [{"id": name, "kind": "action", "action": "read_file", "args": {"path": name}} for name in ("first", "second")]
        if action == "read_file"
        else [{"id": "effect", "kind": "action", "action": "run_shell", "args": {"command": "printf fixture"}}]
    )
    plan = {"version": 1, "steps": steps}
    with pytest.raises(KeyboardInterrupt):
        programs.execute_program(plan, Config(cwd=str(tmp_path)), authorization=_authorization(action), store=store)
    assert calls == [action]
    ledgers = list((store.root / "receipts").glob("*.jsonl"))
    assert len(ledgers) == 1
    records = [json.loads(line) for line in ledgers[0].read_text().splitlines()]
    expected = "cancelled" if action == "read_file" else "unknown_outcome"
    assert len(records) == 1 and records[0]["status"] == expected


def test_shell_interrupt_kills_children_then_propagates(monkeypatch, tmp_path):
    events = []

    class InterruptedProcess:
        def communicate(self, *, timeout):
            events.append("communicate")
            if len(events) == 1:
                raise KeyboardInterrupt
            return "", ""

    proc = InterruptedProcess()
    monkeypatch.setattr(tools.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(
        tools, "_terminate_process_tree", lambda value: events.append("kill") if value is proc else None
    )
    with pytest.raises(KeyboardInterrupt):
        tools.run_shell("fixture command", cwd=str(tmp_path))
    assert events == ["communicate", "kill", "communicate"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT and process-group qualification")
def test_real_shell_sigint_stops_child_processes(tmp_path):
    ready = tmp_path / "child.json"
    child_source = (
        "import json, os, time; from pathlib import Path; "
        f"ready = Path({str(ready)!r}); pending = ready.with_suffix('.tmp'); "
        "pending.write_text(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()})); "
        "pending.replace(ready); "
        "time.sleep(60)"
    )
    command = shlex.join([sys.executable, "-I", "-c", child_source])
    source_root = str(Path(tools.__file__).resolve().parents[1])
    runner_source = (
        f"import sys; sys.path.insert(0, {source_root!r})\n"
        "from algo_cli.tools import run_shell\n"
        "try:\n"
        f"    run_shell({command!r}, cwd={str(tmp_path)!r}, timeout=90)\n"
        "except KeyboardInterrupt:\n"
        "    print('INTERRUPTED', flush=True)\n"
        "    raise SystemExit(130)\n"
        "raise SystemExit('INTERRUPT_SWALLOWED')\n"
    )
    runner = subprocess.Popen(
        [sys.executable, "-I", "-c", runner_source],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and runner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "shell child did not become ready"
        child = json.loads(ready.read_text())
        os.kill(runner.pid, signal.SIGINT)
        stdout, stderr = runner.communicate(timeout=10)
        assert runner.returncode == 130, (stdout, stderr)
        assert stdout.strip() == "INTERRUPTED" and not stderr
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child["pid"])], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("interrupted shell child remained running")
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.communicate(timeout=10)
        if child is not None:
            try:
                os.killpg(child["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_program_receipt_failure_does_not_swallow_user_interruption(monkeypatch, tmp_path):
    def invoke(*_args):
        raise KeyboardInterrupt

    def reject_receipt(*_args):
        raise OSError("fixture receipt storage unavailable")

    monkeypatch.setattr(runtime, "run_tool", invoke)
    store = _program_store(tmp_path)
    monkeypatch.setattr(store, "write_receipts", reject_receipt)
    plan = {
        "version": 1,
        "steps": [{"id": "source", "kind": "action", "action": "read_file", "args": {"path": "README.md"}}],
    }
    with pytest.raises(KeyboardInterrupt):
        programs.execute_program(
            plan, Config(cwd=str(tmp_path)), authorization=_authorization("read_file"), store=store
        )
