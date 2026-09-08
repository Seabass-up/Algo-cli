from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import os
import select
import socket
import struct
import sys
import threading
import time

import pytest

from algo_cli.nathan_approval_channel import ApprovalChannel, ApprovalChannelError, MAX_REQUEST_BYTES, SCHEMA
from algo_cli.nathan_approval_reviewer import TerminalApprovalReviewer, validate_review_request
from algo_cli.samuel_policy_engine import resolve_action


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Terminal approval requires POSIX")


@pytest.fixture
def terminal():
    master, slave = os.openpty()
    descriptor = os.open(os.ttyname(slave), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    os.close(slave)
    reviewer = TerminalApprovalReviewer(descriptor)
    try:
        with reviewer:
            yield reviewer, master
    finally:
        reviewer.close()
        os.close(master)


def request(tmp_path):
    arguments = {"path": "result.txt", "content": "exact content\n\x1b[2Jnot a terminal instruction"}
    action = resolve_action("write_file", arguments, cwd=str(tmp_path))
    now = time.time()
    return {
        "schema": SCHEMA,
        "type": "approval_request",
        "request_id": "a" * 32,
        "action_digest": action.action_digest,
        "name": action.name,
        "target": action.target,
        "confirmation_mode": action.confirmation_mode.value,
        "effect_class": action.effect_class.value,
        "capabilities": ["write"],
        "arguments": arguments,
        "created_at": now,
        "expires_at": now + 60,
    }


def read_prompt(master):
    deadline = time.monotonic() + 3
    screen = bytearray()
    while b"\n> " not in screen:
        assert time.monotonic() < deadline, bytes(screen)
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            screen.extend(os.read(master, 65536))
    text = screen.decode("ascii").replace("\r", "")
    return json.loads(next(line for line in text.splitlines() if line.startswith("{"))), bytes(screen)


def client_for(reviewer, *, timeout=2):
    channel = ApprovalChannel.from_fd(os.dup(reviewer.fileno()), timeout_seconds=timeout)
    reviewer.child_started()
    return channel


@pytest.mark.parametrize("same_target", [False, True])
def test_parallel_observations_leave_the_reviewer_available_for_shell_review(
    terminal, monkeypatch, tmp_path, same_target
):
    from algo_cli import nathan_runtime
    from algo_cli.clara_effect_ledger import EffectLedger
    from algo_cli.config import Config
    from algo_cli.henry_effect_control import TargetLeaseManager
    from algo_cli.james_dispatch import DispatchDependencies, dispatch_action

    reviewer, master = terminal
    channel = client_for(reviewer)
    cfg = Config(cwd=str(tmp_path))
    setattr(cfg, "_nathan_approval_channel", channel)
    first_prepared = threading.Event()
    preflight_ready = threading.Barrier(2)
    second_authorized = threading.Event()
    invoked = []
    original_preflight = nathan_runtime.preflight_runtime_tool

    def ordered_preflight(name, args, cfg, **kwargs):
        if name == "list_directory" and args["limit"] == 20:
            assert first_prepared.wait(timeout=3)
        result = original_preflight(name, args, cfg, **kwargs)
        if name == "list_directory" and args["limit"] == 10:
            first_prepared.set()
        return result

    monkeypatch.setattr(nathan_runtime, "preflight_runtime_tool", ordered_preflight)

    def ordered_approval(name, args, cfg, **kwargs):
        if name == "list_directory":
            preflight_ready.wait(timeout=3)
            if args["limit"] == 10:
                assert second_authorized.wait(timeout=3)
            else:
                try:
                    return nathan_runtime.ask_approval(name, args, cfg, **kwargs)
                finally:
                    second_authorized.set()
        return nathan_runtime.ask_approval(name, args, cfg, **kwargs)

    def invoke(name, args, _cfg):
        invoked.append((name, args))
        return "fixture.txt" if name == "list_directory" else "[exit code: 0]"

    deps = DispatchDependencies(
        invoke=invoke,
        approve=ordered_approval,
        effect_ledger=EffectLedger.at_path(str(tmp_path / "effects.jsonl")),
        lease_manager=TargetLeaseManager(tmp_path / "leases"),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            reads = [
                pool.submit(dispatch_action, "list_directory", args, cfg, dependencies=deps, render=False)
                for args in (
                    {"path": "src", "limit": 10},
                    {"path": "src" if same_target else "tests", "limit": 20},
                )
            ]
            outcomes = [pending.result(timeout=5).outcome for pending in reads]
            assert all(outcome.invoked for outcome in outcomes), reviewer.receipt()
            assert reviewer.requests == 0
            assert reviewer.reason == ""
            assert select.select([master], [], [], 0)[0] == []

            shell = pool.submit(
                dispatch_action,
                "run_shell",
                {"command": "python -m pytest", "timeout": 30},
                cfg,
                dependencies=deps,
                render=False,
            )
            received, _ = read_prompt(master)
            assert received["name"] == "run_shell"
            assert received["confirmation_mode"] == "action_time"
            assert received["arguments"]["command"] == "python -m pytest"
            # This is a synthetic test terminal, not the operator's approval channel.
            os.write(master, b"deny\n")
            assert not shell.result(timeout=3).outcome.invoked
        assert reviewer.requests == 1
        assert reviewer.denied == 1
        assert reviewer.reason == ""
        assert [name for name, _args in invoked] == ["list_directory", "list_directory"]
    finally:
        channel.close()


@pytest.mark.parametrize("decision", ["approve", "yes", "always", "wrong-nonce", ""])
def test_terminal_decision_binds_real_runtime_request_and_escapes_controls(terminal, tmp_path, capsys, decision):
    reviewer, master = terminal
    channel = client_for(reviewer)
    data = request(tmp_path)
    action = resolve_action("write_file", data["arguments"], cwd=str(tmp_path))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(channel.confirm, action, data["arguments"])
            received, screen = read_prompt(master)
            assert received["action_digest"] == action.action_digest
            assert received["arguments"] == data["arguments"]
            assert b"\x1b" not in screen and b"\\u001b" in screen
            answer = f"approve {received['request_id']}" if decision == "approve" else decision
            os.write(master, (answer + "\n").encode())
            assert result.result(timeout=3) is (decision == "approve")
        assert reviewer.receipt()["approved_decisions"] == int(decision == "approve")
        assert not os.get_inheritable(channel.fileno())
        assert capsys.readouterr() == ("", "")
    finally:
        channel.close()


def test_an_old_terminal_nonce_cannot_approve_the_next_action(terminal, tmp_path):
    reviewer, master = terminal
    channel = client_for(reviewer)
    data = request(tmp_path)
    action = resolve_action("write_file", data["arguments"], cwd=str(tmp_path))
    previous = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            for expected in (True, False, True):
                result = pool.submit(channel.confirm, action, data["arguments"])
                received, _ = read_prompt(master)
                assert received["request_id"] != previous
                nonce = received["request_id"] if expected else previous
                os.write(master, f"approve {nonce}\n".encode())
                assert result.result(timeout=3) is expected
                previous = received["request_id"]
        assert reviewer.receipt()["approved_decisions"] == 2
        assert reviewer.receipt()["denied_decisions"] == 1
    finally:
        channel.close()


@pytest.mark.parametrize(
    "change",
    [
        {"schema": "wrong"},
        {"extra": None},
        {"request_id": "wrong"},
        {"action_digest": "bad"},
        {"confirmation_mode": "handoff_required"},
        {"confirmation_mode": "none"},
        {"effect_class": "unclassified"},
        {"capabilities": ["unclassified"]},
        {"capabilities": ["write", "write"]},
        {"capabilities": []},
        {"capabilities": None},
        {"capabilities": [True]},
        {"arguments": []},
        {"created_at": True},
        {"created_at": float("nan")},
        {"expires_at": None},
        {"expires_at": 10**1000},
        {"effect_class": []},
        {"confirmation_mode": []},
        {"created_at": 1, "expires_at": 2},
        {"name": ""},
        {"target": None},
    ],
)
def test_invalid_review_requests_are_rejected(tmp_path, change):
    data = request(tmp_path)
    data.update(change)
    with pytest.raises((ApprovalChannelError, TypeError)):
        validate_review_request(data, now=time.time())


def test_review_lifetime_cannot_exceed_channel_limit(tmp_path):
    data = request(tmp_path)
    data["expires_at"] = data["created_at"] + 121
    with pytest.raises(ApprovalChannelError):
        validate_review_request(data, now=time.time())


@pytest.mark.parametrize("payload", [b"null", b"[]", b'{"schema":1,"schema":2}', b'{"x":NaN}', b"\xff"])
def test_malformed_frames_close_without_terminal_output(terminal, payload):
    reviewer, master = terminal
    peer = socket.socket(fileno=os.dup(reviewer.fileno()))
    reviewer.child_started()
    peer.settimeout(2)
    try:
        peer.sendall(struct.pack("!I", len(payload)) + payload)
        assert peer.recv(1) == b""
        assert reviewer.reason == "review_protocol_error"
        assert select.select([master], [], [], 0)[0] == []
    finally:
        peer.close()


@pytest.mark.parametrize("size", [0, MAX_REQUEST_BYTES + 1])
def test_oversized_frame_is_rejected_before_reading_body(terminal, size):
    reviewer, _ = terminal
    peer = socket.socket(fileno=os.dup(reviewer.fileno()))
    reviewer.child_started()
    peer.settimeout(2)
    try:
        peer.sendall(struct.pack("!I", size))
        assert peer.recv(1) == b""
        assert reviewer.requests == 0
    finally:
        peer.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_unanswered_or_cancelled_review_cannot_approve(terminal, tmp_path, cancel):
    reviewer, master = terminal
    channel = client_for(reviewer, timeout=0.3 if not cancel else 2)
    data = request(tmp_path)
    action = resolve_action("write_file", data["arguments"], cwd=str(tmp_path))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(channel.confirm, action, data["arguments"])
            read_prompt(master)
            if cancel:
                reviewer.close()
            assert result.result(timeout=3) is False
        assert reviewer.approved == 0
    finally:
        channel.close()


def test_request_pipeline_is_rejected_while_operator_is_reviewing(terminal, tmp_path):
    reviewer, master = terminal
    peer = socket.socket(fileno=os.dup(reviewer.fileno()))
    reviewer.child_started()
    peer.settimeout(2)
    data = json.dumps(request(tmp_path)).encode()
    try:
        peer.sendall(struct.pack("!I", len(data)) + data)
        read_prompt(master)
        peer.sendall(b"unexpected next request")
        try:
            assert peer.recv(1) == b""
        except ConnectionResetError:
            pass
        assert reviewer.approved == 0
    finally:
        peer.close()


def test_cli_review_flag_builds_an_explicit_child_without_reviewer_flag(monkeypatch, tmp_path):
    from algo_cli import main, nathan_approval_reviewer

    args = main.parse_args(
        [
            "--oneshot",
            "--json",
            "--approval-mode",
            "interactive",
            "--review-actions",
            "--model",
            "test-model",
            "--cwd",
            str(tmp_path),
            "--thinking",
            "off",
            "--",
            "-private prompt",
        ]
    )
    commands = []
    monkeypatch.setattr(nathan_approval_reviewer, "run_reviewed_command", lambda make: commands.append(make(77)) or 0)
    assert main._run_oneshot_entry(args) == 0
    command = commands[0]
    assert "--review-actions" not in command
    assert command[command.index("--approval-fd") + 1] == "77"
    assert command[-2:] == ["--", "-private prompt"]
    assert "--model" in command and "test-model" in command and str(tmp_path) in command


@pytest.mark.parametrize(
    "flags",
    [
        ["--review-actions"],
        ["--oneshot", "--json", "--review-actions"],
        ["--oneshot", "--json", "--approval-mode", "auto", "--review-actions"],
        ["--oneshot", "--json", "--approval-mode", "interactive", "--review-actions", "--approval-fd", "55"],
    ],
)
def test_invalid_review_flags_do_not_run_an_agent(flags):
    from algo_cli import main

    with pytest.raises(SystemExit) as caught:
        main.parse_args(flags)
    assert caught.value.code == 2


def test_missing_terminal_cannot_fall_back_to_unattended_approval(monkeypatch, capsys):
    from algo_cli import main, nathan_approval_reviewer, oliver_oneshot

    args = main.parse_args(["--oneshot", "--json", "--approval-mode", "interactive", "--review-actions", "prompt"])

    def unavailable(_command):
        raise ApprovalChannelError("no controlling terminal")

    monkeypatch.setattr(nathan_approval_reviewer, "run_reviewed_command", unavailable)
    monkeypatch.setattr(oliver_oneshot, "run_oneshot", lambda **_kw: pytest.fail("no fallback"))
    assert main._run_oneshot_entry(args) == 64
    assert capsys.readouterr().out == ""


def test_large_action_is_displayed_without_silent_truncation(terminal, tmp_path):
    reviewer, master = terminal
    channel = client_for(reviewer, timeout=5)
    arguments = {"path": "large.txt", "content": "x" * 180000 + "END_OF_EXACT_ACTION"}
    action = resolve_action("write_file", arguments, cwd=str(tmp_path))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(channel.confirm, action, arguments)
            received, _ = read_prompt(master)
            assert received["arguments"] == arguments
            os.write(master, b"deny\n")
            assert result.result(timeout=3) is False
    finally:
        channel.close()


@pytest.mark.parametrize("capacity", [False, True])
def test_replayed_or_over_capacity_request_cannot_prompt_again(terminal, tmp_path, monkeypatch, capacity):
    from algo_cli import nathan_approval_reviewer

    reviewer, master = terminal
    if capacity:
        monkeypatch.setattr(nathan_approval_reviewer, "MAX_REVIEW_REQUESTS", 1)
    peer = socket.socket(fileno=os.dup(reviewer.fileno()))
    reviewer.child_started()
    peer.settimeout(2)
    data = request(tmp_path)
    try:
        payload = json.dumps(data).encode()
        peer.sendall(struct.pack("!I", len(payload)) + payload)
        read_prompt(master)
        os.write(master, b"deny\n")
        size = struct.unpack("!I", peer.recv(4))[0]
        assert json.loads(peer.recv(size))["decision"] == "deny"
        if capacity:
            data["request_id"] = "b" * 32
        payload = json.dumps(data).encode()
        peer.sendall(struct.pack("!I", len(payload)) + payload)
        assert peer.recv(1) == b""
        assert reviewer.requests == 1 and reviewer.approved == 0
    finally:
        peer.close()


def test_launcher_inherits_only_client_socket_and_keeps_review_off_stdout(terminal, tmp_path, monkeypatch, capfd):
    from algo_cli import nathan_approval_reviewer

    reviewer, master = terminal
    monkeypatch.setattr(TerminalApprovalReviewer, "open_tty", lambda: nullcontext(reviewer))
    target = tmp_path / "approved-fixture.txt"
    source = """
import json, os, sys
from pathlib import Path
from algo_cli.nathan_approval_channel import ApprovalChannel
from algo_cli.samuel_policy_engine import resolve_action
assert not os.isatty(int(sys.argv[3]))
channel = ApprovalChannel.from_fd(int(sys.argv[1]))
arguments = {"path": sys.argv[2], "content": "approved fixture only"}
action = resolve_action("write_file", arguments, cwd=str(Path(sys.argv[2]).parent))
try:
    approved = channel.confirm(action, arguments)
    if approved:
        Path(sys.argv[2]).write_text(arguments["content"])
    print(json.dumps({"approved": approved}))
finally:
    channel.close()
"""
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            nathan_approval_reviewer.run_reviewed_command,
            lambda fd: [sys.executable, "-c", source, str(fd), str(target), str(reviewer._tty)],
        )
        data, _ = read_prompt(master)
        assert data["arguments"]["path"] == str(target)
        os.write(master, f"approve {data['request_id']}\n".encode())
        assert result.result(timeout=5) == 0
    assert target.read_text() == "approved fixture only"
    captured = capfd.readouterr()
    assert json.loads(captured.out) == {"approved": True}
    assert captured.err == ""
    reviewer._thread.join(timeout=2)
    assert not reviewer._thread.is_alive()
    assert reviewer.reason == ""


@pytest.mark.parametrize("failure", ["cancel", "after-start"])
def test_launcher_reaps_child_on_cancel_or_start_callback_failure(terminal, monkeypatch, failure):
    from algo_cli import nathan_approval_reviewer

    reviewer, _ = terminal
    monkeypatch.setattr(TerminalApprovalReviewer, "open_tty", lambda: nullcontext(reviewer))
    calls = []

    class Process:
        pid = 1234567
        waits = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waits += 1
            if timeout is None:
                raise KeyboardInterrupt
            calls.append(("reaped", timeout))
            return -9

    process = Process()
    monkeypatch.setattr(nathan_approval_reviewer.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(nathan_approval_reviewer.os, "killpg", lambda pid, sig: calls.append(("killed", pid)))
    if failure == "after-start":

        def failed():
            raise OSError("client close failed")

        monkeypatch.setattr(reviewer, "child_started", failed)
    with pytest.raises(KeyboardInterrupt if failure == "cancel" else OSError):
        nathan_approval_reviewer.run_reviewed_command(lambda fd: ["fixture", str(fd)])
    assert calls == [("killed", process.pid), ("reaped", 5)]


def test_close_reaps_reviewer_thread_and_owned_descriptors(terminal):
    reviewer, _ = terminal
    descriptor = reviewer._tty
    reviewer.close()
    assert not reviewer._thread.is_alive()
    assert reviewer.fileno() == reviewer._peer.fileno() == -1
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_thread_start_failure_closes_owned_descriptors(monkeypatch):
    master, slave = os.openpty()
    descriptor = os.open(os.ttyname(slave), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    reviewer = TerminalApprovalReviewer(descriptor)

    def failed():
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(reviewer._thread, "start", failed)
    try:
        with pytest.raises(RuntimeError):
            reviewer.__enter__()
        assert reviewer.fileno() == reviewer._peer.fileno() == -1
        with pytest.raises(OSError):
            os.fstat(descriptor)
    finally:
        reviewer.close()
        os.close(master)
        os.close(slave)
