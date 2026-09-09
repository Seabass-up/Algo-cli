from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import select
import socket
import struct
import time

import pytest

from algo_cli.config import Config
from algo_cli.nathan_approval_channel import ApprovalChannel, ApprovalChannelError, SCHEMA
from algo_cli.samuel_policy_engine import resolve_action


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Inherited Unix approval sockets require POSIX")


@pytest.fixture
def channel_pair():
    if os.name != "posix":
        pytest.skip("Inherited Unix approval sockets require POSIX")
    child, supervisor = socket.socketpair()
    supervisor.settimeout(2)
    channel = ApprovalChannel.from_fd(child.detach(), timeout_seconds=1)
    try:
        yield channel, supervisor
    finally:
        channel.close()
        supervisor.close()


def _read_frame(peer):
    def exact(size):
        data = b""
        while len(data) < size:
            part = peer.recv(size - len(data))
            if not part:
                raise EOFError
            data += part
        return data

    size = struct.unpack("!I", exact(4))[0]
    return json.loads(exact(size))


def _reply(peer, request, **changes):
    response = {
        "schema": SCHEMA,
        "type": "approval_response",
        "request_id": request["request_id"],
        "action_digest": request["action_digest"],
        "decision": "approve",
    }
    response.update(changes)
    payload = json.dumps(response).encode()
    peer.sendall(struct.pack("!I", len(payload)) + payload)


def _request(channel, tmp_path):
    args = {"path": "result.txt", "content": "review this exact content"}
    action = resolve_action("write_file", args, cwd=str(tmp_path))
    return channel.confirm(action, args)


def _authorize(cfg, args):
    from algo_cli import execution_guardrails, nathan_runtime

    scope = execution_guardrails.begin_execution_scope(cfg.cwd)
    try:
        return nathan_runtime.ask_approval("write_file", args, cfg)
    finally:
        execution_guardrails.end_execution_scope(scope)


def test_channel_binds_exact_request_without_inheritable_descriptor(channel_pair, tmp_path):
    channel, peer = channel_pair
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, channel, tmp_path)
        request = _read_frame(peer)
        assert request["schema"] == SCHEMA
        assert request["type"] == "approval_request"
        assert request["arguments"]["content"] == "review this exact content"
        assert request["confirmation_mode"] == "action_time"
        assert request["name"] == "write_file"
        assert request["expires_at"] > request["created_at"]
        assert len(request["request_id"]) == 32
        assert not os.get_inheritable(channel.fileno())
        _reply(peer, request)
        assert pending.result(timeout=2) is True


def test_channel_rejects_none_confirmation_without_poisoning_the_next_review(channel_pair, tmp_path):
    channel, peer = channel_pair
    args = {"path": "."}
    action = resolve_action("list_directory", args, cwd=str(tmp_path))
    assert channel.confirm(action, args) is False
    assert select.select([peer], [], [], 0)[0] == []
    assert channel.last_reason == "approval_not_reviewable"

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, channel, tmp_path)
        request = _read_frame(peer)
        _reply(peer, request, decision="deny")
        assert pending.result(timeout=2) is False
    assert channel.last_reason == "approval_denied"


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "wrong"},
        {"action_digest": "wrong"},
        {"decision": True},
        {"decision": "always"},
        {"schema": "wrong"},
        {"extra": None},
    ],
)
def test_channel_rejects_mismatched_or_expanded_responses(channel_pair, tmp_path, changes):
    channel, peer = channel_pair
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, channel, tmp_path)
        _reply(peer, _read_frame(peer), **changes)
        assert pending.result(timeout=2) is False
    assert channel.last_reason == "approval_protocol_error"
    assert _request(channel, tmp_path) is False


def test_denial_keeps_channel_but_old_approval_cannot_authorize_next_request(channel_pair, tmp_path):
    channel, peer = channel_pair
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_request, channel, tmp_path)
        old = _read_frame(peer)
        _reply(peer, old, decision="deny")
        assert first.result(timeout=2) is False
        assert channel.last_reason == "approval_denied"
        second = pool.submit(_request, channel, tmp_path)
        fresh = _read_frame(peer)
        assert fresh["request_id"] != old["request_id"]
        _reply(peer, old)
        assert second.result(timeout=2) is False


@pytest.mark.parametrize(
    "payload",
    [
        b'{"decision":"deny","decision":"approve"}',
        b'{"decision": NaN}',
        b"[]",
        b"null",
        b"not-json",
        b"\xff",
    ],
)
def test_channel_rejects_malformed_json(channel_pair, tmp_path, payload):
    channel, peer = channel_pair
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, channel, tmp_path)
        _read_frame(peer)
        peer.sendall(struct.pack("!I", len(payload)) + payload)
        assert pending.result(timeout=2) is False


@pytest.mark.parametrize("length", [0, 2049, 2**32 - 1])
def test_channel_rejects_invalid_frame_lengths_before_reading_payload(channel_pair, tmp_path, length):
    channel, peer = channel_pair
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, channel, tmp_path)
        _read_frame(peer)
        peer.sendall(struct.pack("!I", length))
        assert pending.result(timeout=2) is False


def test_channel_timeout_and_disconnect_fail_closed(channel_pair, tmp_path):
    channel, peer = channel_pair
    start = time.monotonic()
    assert _request(channel, tmp_path) is False
    assert time.monotonic() - start < 2
    assert channel.last_reason == "approval_timeout"
    peer.close()
    assert _request(channel, tmp_path) is False


def test_channel_rejects_partial_response_on_disconnect(channel_pair, tmp_path):
    channel, peer = channel_pair
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, channel, tmp_path)
        _read_frame(peer)
        peer.sendall(b"\x00\x00")
        peer.close()
        assert pending.result(timeout=2) is False
    assert channel.last_reason == "approval_unavailable"


@pytest.mark.parametrize("fd", [-1, 0, 1, 2, True, "4"])
def test_channel_rejects_invalid_or_standard_descriptors(fd):
    with pytest.raises(ApprovalChannelError):
        ApprovalChannel.from_fd(fd)


def test_channel_rejects_network_socket():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as peer:
        fd = peer.detach()
        with pytest.raises(ApprovalChannelError):
            ApprovalChannel.from_fd(fd)
        with pytest.raises(OSError):
            os.fstat(fd)


def test_channel_closes_rejected_pipe_descriptor():
    reader, writer = os.pipe()
    try:
        with pytest.raises(ApprovalChannelError):
            ApprovalChannel.from_fd(reader)
        with pytest.raises(OSError):
            os.fstat(reader)
    finally:
        os.close(writer)


@pytest.mark.parametrize("timeout", [0, -1, 121, float("nan"), float("inf"), True])
def test_channel_rejects_invalid_timeout(timeout):
    with pytest.raises(ApprovalChannelError):
        ApprovalChannel.from_fd(999999, timeout_seconds=timeout)


def test_channel_never_approves_handoff_or_oversized_request(channel_pair, tmp_path):
    channel, _peer = channel_pair
    handoff = resolve_action("credential_get", {"key": "secret"}, cwd=str(tmp_path))
    assert channel.confirm(handoff, {"key": "secret"}) is False
    args = {"path": "result.txt", "content": "x" * (256 * 1024)}
    assert channel.confirm(resolve_action("write_file", args, cwd=str(tmp_path)), args) is False


def test_runtime_requires_new_approval_for_each_action(channel_pair, monkeypatch, tmp_path):
    channel, peer = channel_pair
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_channel = channel
    monkeypatch.setattr("builtins.input", lambda *_args: pytest.fail("No terminal fallback"))
    args = {"path": "result.txt", "content": "reviewed content"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_authorize, cfg, args)
        _reply(peer, _read_frame(peer))
        assert pending.result(timeout=2) is True
        pending = pool.submit(_authorize, cfg, args)
        _reply(peer, _read_frame(peer), decision="deny")
        assert pending.result(timeout=2) is False


def test_runtime_rejects_argument_drift_during_review(channel_pair, monkeypatch, tmp_path):
    channel, peer = channel_pair
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_channel = channel
    monkeypatch.setattr("builtins.input", lambda *_args: pytest.fail("No terminal fallback"))
    args = {"path": "result.txt", "content": "reviewed content"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_authorize, cfg, args)
        request = _read_frame(peer)
        args["content"] = "changed after request"
        _reply(peer, request)
        assert pending.result(timeout=2) is False


def test_confirmation_time_is_taken_after_review(channel_pair, monkeypatch, tmp_path):
    from algo_cli import nathan_runtime as runtime

    channel, peer = channel_pair
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_channel = channel
    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    receipts = []
    original_receipt = runtime.ConfirmationReceipt

    def receipt(**kwargs):
        receipts.append(kwargs)
        return original_receipt(**kwargs)

    monkeypatch.setattr(runtime, "ConfirmationReceipt", receipt)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_authorize, cfg, {"path": "result.txt", "content": "new"})
        request = _read_frame(peer)
        clock[0] = 1300.0
        _reply(peer, request)
        assert pending.result(timeout=2) is True
    assert receipts[0]["confirmed_at"] == 1300.0
    assert receipts[0]["expires_at"] == 1420.0


@pytest.mark.parametrize("mode", ["never", "auto"])
def test_channel_does_not_change_existing_approval_modes(channel_pair, monkeypatch, tmp_path, mode):
    channel, _peer = channel_pair
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_mode = mode
    cfg._nathan_approval_channel = channel
    monkeypatch.setattr(channel, "confirm", lambda *_args: pytest.fail("Mode must deny before requesting approval"))
    assert _authorize(cfg, {"path": "result.txt", "content": "new"}) is False


@pytest.mark.parametrize("field,value", [("safe_mode", False), ("auto_mode", True), ("_nathan_approval_mode", "auto")])
def test_runtime_rejects_authority_drift_during_review(channel_pair, tmp_path, field, value):
    channel, peer = channel_pair
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_channel = channel
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_authorize, cfg, {"path": "result.txt", "content": "new"})
        request = _read_frame(peer)
        setattr(cfg, field, value)
        _reply(peer, request)
        assert pending.result(timeout=2) is False


@pytest.mark.parametrize("cancel", [False, True])
def test_dispatch_requires_current_approval_and_preserves_cancellation(channel_pair, tmp_path, cancel):
    from algo_cli import execution_guardrails
    from algo_cli.arthur_outcomes import OutcomeStatus
    from algo_cli.james_dispatch import DispatchCancellation, dispatch_action

    channel, peer = channel_pair
    cfg = Config(cwd=str(tmp_path))
    cfg._nathan_approval_channel = channel
    cancellation = DispatchCancellation()

    def execute():
        scope = execution_guardrails.begin_execution_scope(tmp_path)
        try:
            return dispatch_action(
                "write_file",
                {"path": "result.txt", "content": "reviewed"},
                cfg,
                cancellation=cancellation,
                render=False,
            )
        finally:
            execution_guardrails.end_execution_scope(scope)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(execute)
        request = _read_frame(peer)
        assert not (tmp_path / "result.txt").exists()
        if cancel:
            cancellation.cancel()
        _reply(peer, request)
        result = pending.result(timeout=2)
    assert result.outcome.status is (OutcomeStatus.CANCELLED if cancel else OutcomeStatus.SUCCEEDED)
    assert result.outcome.invoked is not cancel
    if cancel:
        assert not (tmp_path / "result.txt").exists()
    else:
        assert (tmp_path / "result.txt").read_text() == "reviewed"


def test_fragmented_valid_response_is_accepted(channel_pair, tmp_path):
    channel, peer = channel_pair
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_request, channel, tmp_path)
        request = _read_frame(peer)
        body = json.dumps(
            {
                "schema": SCHEMA,
                "type": "approval_response",
                "request_id": request["request_id"],
                "action_digest": request["action_digest"],
                "decision": "approve",
            }
        ).encode()
        for byte in struct.pack("!I", len(body)) + body:
            peer.sendall(bytes([byte]))
        assert pending.result(timeout=2) is True


def test_oneshot_supervisor_channel_preserves_ndjson_and_closes_descriptor(monkeypatch, tmp_path):
    from algo_cli import main, oliver_oneshot

    child, peer = socket.socketpair()
    peer.settimeout(2)
    fd = child.detach()
    output = io.StringIO()
    cfg = Config(cwd=str(tmp_path))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main, "create_client", lambda _cfg: object())
    monkeypatch.setattr("builtins.input", lambda *_args: pytest.fail("No terminal fallback"))

    def task(_client, cfg, _prompt):
        assert _authorize(cfg, {"path": "result.txt", "content": "new"})

    monkeypatch.setattr(main, "agent_loop", task)
    with peer, ThreadPoolExecutor(max_workers=1) as pool:

        def supervise():
            _reply(peer, _read_frame(peer))

        approval = pool.submit(supervise)
        assert (
            oliver_oneshot.run_oneshot(prompt="synthetic", approval_mode="interactive", approval_fd=fd, stream=output)
            == 0
        )
        approval.result(timeout=2)
    assert [json.loads(line)["type"] for line in output.getvalue().splitlines()] == ["session_start", "done"]
    assert "reviewed content" not in output.getvalue()
    assert not hasattr(cfg, "_nathan_approval_channel")
    with pytest.raises(OSError):
        os.fstat(fd)


def test_oneshot_interactive_requires_explicit_channel():
    from algo_cli.oliver_oneshot import run_oneshot

    with pytest.raises(ValueError, match="approval_fd"):
        run_oneshot(prompt="test", approval_mode="interactive", stream=io.StringIO())


@pytest.mark.parametrize(
    "args",
    [
        ["--approval-fd", "4"],
        ["--oneshot", "--json", "--approval-mode", "interactive"],
        ["--oneshot", "--json", "--approval-mode", "auto", "--approval-fd", "4"],
        ["--oneshot", "--json", "--approval-mode", "interactive", "--approval-fd", "0"],
        ["--oneshot", "--approval-mode", "interactive", "--approval-fd", "4"],
    ],
)
def test_cli_rejects_implicit_or_invalid_approval_channel(args):
    from algo_cli.main import parse_args

    with pytest.raises(SystemExit) as error:
        parse_args(args)
    assert error.value.code == 2


def test_cli_forwards_explicit_approval_descriptor(monkeypatch):
    from algo_cli import main, oliver_oneshot

    args = main.parse_args(["--oneshot", "--json", "--approval-mode", "interactive", "--approval-fd", "4", "test"])
    observed = {}
    monkeypatch.setattr(oliver_oneshot, "run_oneshot", lambda **kwargs: observed.update(kwargs) or 0)
    assert main._run_oneshot_entry(args) == 0
    assert observed["approval_fd"] == 4
    assert observed["approval_mode"] == "interactive"
