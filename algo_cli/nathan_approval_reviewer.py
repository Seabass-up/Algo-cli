"""Operator-owned terminal review for the private one-shot approval channel.

Only the client socket is inherited by the agent. The controlling terminal and
reviewer endpoint stay in its parent; neither action arguments nor responses are
written to the agent's public stdout/stderr streams.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import threading
import time
from typing import Any

from .marcus_authority import Capability, ConfirmationMode, EffectClass
from .nathan_approval_channel import (
    ApprovalChannelError,
    MAX_REQUEST_BYTES,
    MAX_WAIT_SECONDS,
    SCHEMA,
    decode_approval_object,
)


POLL_SECONDS = 0.1
MAX_DECISION_BYTES = 128
MAX_REVIEW_REQUESTS = 1024
REQUEST_FIELDS = frozenset(
    {
        "schema",
        "type",
        "request_id",
        "action_digest",
        "name",
        "target",
        "confirmation_mode",
        "effect_class",
        "capabilities",
        "arguments",
        "created_at",
        "expires_at",
    }
)


def validate_review_request(request: dict[str, Any], *, now: float) -> float:
    """Validate the closed frame and return its remaining wall-clock lifetime."""
    if set(request) != REQUEST_FIELDS or request["schema"] != SCHEMA or request["type"] != "approval_request":
        raise ApprovalChannelError("review request schema")
    for key, pattern in (("request_id", r"[0-9a-f]{32}"), ("action_digest", r"[0-9a-f]{64}")):
        if not isinstance(request[key], str) or re.fullmatch(pattern, request[key]) is None:
            raise ApprovalChannelError("review request identity")
    if any(not isinstance(request[key], str) or not request[key].strip() for key in ("name", "target")):
        raise ApprovalChannelError("review request action")
    if type(request["confirmation_mode"]) is not str or request["confirmation_mode"] not in {
        ConfirmationMode.ACTION_TIME.value,
        ConfirmationMode.SESSION_PREAPPROVAL.value,
    }:
        raise ApprovalChannelError("review request authority")
    if type(request["effect_class"]) is not str or request["effect_class"] not in {
        item.value for item in EffectClass if item is not EffectClass.UNCLASSIFIED
    }:
        raise ApprovalChannelError("review request effect")
    capabilities = request["capabilities"]
    names = {item.name.lower() for item in Capability if item is not Capability.UNCLASSIFIED}
    if (
        type(capabilities) is not list
        or not capabilities
        or any(type(name) is not str or name not in names for name in capabilities)
        or len(set(capabilities)) != len(capabilities)
        or type(request["arguments"]) is not dict
    ):
        raise ApprovalChannelError("review request arguments")
    created, expires = request["created_at"], request["expires_at"]
    if (
        any(type(value) not in {int, float} or not -1e15 < value < 1e15 for value in (created, expires, now))
        or created > now + 1
        or not 0 < expires - created <= MAX_WAIT_SECONDS
        or not 0 < expires - now <= MAX_WAIT_SECONDS
    ):
        raise ApprovalChannelError("review request expired")
    return float(expires - now)


class TerminalApprovalReviewer:
    def __init__(self, tty_fd: int) -> None:
        """Take ownership of an explicitly opened read/write terminal after setup succeeds."""
        if os.name != "posix":
            raise ApprovalChannelError("terminal review requires POSIX")
        if type(tty_fd) is not int or tty_fd < 3 or not os.isatty(tty_fd):
            raise ApprovalChannelError("terminal review requires a private terminal descriptor")
        self._tty = tty_fd
        os.set_inheritable(self._tty, False)
        os.set_blocking(self._tty, False)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="algo-terminal-review", daemon=True)
        self._client, self._peer = socket.socketpair()
        try:
            self._peer.setblocking(False)
        except BaseException:
            self._client.close()
            self._peer.close()
            raise
        self._closed = False
        self.reason = ""
        self.requests = 0
        self.approved = 0
        self.denied = 0
        self.review_seconds = 0.0
        self._seen_requests: set[str] = set()

    @classmethod
    def open_tty(cls) -> TerminalApprovalReviewer:
        if os.name != "posix":
            raise ApprovalChannelError("terminal review requires POSIX")
        try:
            descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError:
            raise ApprovalChannelError("terminal review requires a controlling terminal") from None
        try:
            return cls(descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def fileno(self) -> int:
        return self._client.fileno()

    def child_started(self) -> None:
        """Drop the parent's client copy immediately after subprocess inheritance."""
        self._client.close()

    def __enter__(self) -> TerminalApprovalReviewer:
        try:
            self._thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._client.close()
        try:
            self._peer.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self._thread.ident is not None:
            self._thread.join(timeout=2)
        self._peer.close()
        os.close(self._tty)

    def receipt(self) -> dict[str, Any]:
        return {
            "mode": "operator_terminal",
            "requests": self.requests,
            "approved_decisions": self.approved,
            "denied_decisions": self.denied,
            "review_seconds": round(self.review_seconds, 6),
            "reason": self.reason,
            "scope": "operator decisions only; not execution or independent approval attestation",
        }

    def _wait(self, descriptor: int, deadline: float, *, writing: bool = False, watch_peer: bool = False) -> None:
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            readers = ([] if writing else [descriptor]) + ([self._peer.fileno()] if watch_peer else [])
            ready, writable, _ = select.select(
                readers, [descriptor] if writing else [], [], min(POLL_SECONDS, remaining)
            )
            if watch_peer and self._peer.fileno() in ready:
                if self._peer.recv(1, socket.MSG_PEEK):
                    raise ApprovalChannelError("pipelined review request")
                raise EOFError
            if descriptor in ready or descriptor in writable:
                return
        raise EOFError

    def _receive(self, size: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < size:
            self._wait(self._peer.fileno(), deadline)
            try:
                part = self._peer.recv(size - len(result))
            except BlockingIOError:
                continue
            if not part:
                if result:
                    raise ApprovalChannelError("truncated review frame")
                raise EOFError
            result.extend(part)
        return bytes(result)

    def _write(self, descriptor: int, payload: bytes, deadline: float, *, terminal: bool = False) -> None:
        while payload:
            self._wait(descriptor, deadline, writing=True, watch_peer=terminal)
            try:
                written = os.write(descriptor, payload[:4096])
            except BlockingIOError:
                continue
            if written <= 0:
                raise EOFError
            payload = payload[written:]

    def _review(self, request: dict[str, Any], deadline: float) -> bool:
        nonce = request["request_id"]
        # JSON quoting escapes every terminal control and displays the whole bounded action.
        content = json.dumps(request, sort_keys=True, ensure_ascii=True, allow_nan=False)
        screen = (
            f"\nALGO ACTION REVIEW\n{content}\nType approve {nonce} to allow this action; anything else denies:\n> "
        )
        self._write(self._tty, screen.encode("ascii"), deadline, terminal=True)
        line = bytearray()
        while len(line) <= MAX_DECISION_BYTES:
            self._wait(self._tty, deadline, watch_peer=True)
            try:
                part = os.read(self._tty, MAX_DECISION_BYTES + 1)
            except BlockingIOError:
                continue
            if not part:
                raise EOFError
            line.extend(part)
            if b"\n" in line:
                return bytes(line) == f"approve {nonce}\n".encode("ascii")
        return False

    def _serve(self) -> None:
        inflight = False
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([self._peer], [], [], POLL_SECONDS)
                if not ready:
                    continue
                deadline = time.monotonic() + MAX_WAIT_SECONDS
                size = struct.unpack("!I", self._receive(4, deadline))[0]
                inflight = True
                if not 0 < size <= MAX_REQUEST_BYTES:
                    raise ApprovalChannelError("review frame size")
                request = decode_approval_object(self._receive(size, deadline))
                remaining = validate_review_request(request, now=time.time())
                if request["request_id"] in self._seen_requests or len(self._seen_requests) >= MAX_REVIEW_REQUESTS:
                    raise ApprovalChannelError("review request replay or capacity")
                self._seen_requests.add(request["request_id"])
                deadline = min(deadline, time.monotonic() + remaining)
                self.requests += 1
                started = time.monotonic()
                try:
                    allowed = self._review(request, deadline)
                finally:
                    self.review_seconds += time.monotonic() - started
                response = {
                    "schema": SCHEMA,
                    "type": "approval_response",
                    "request_id": request["request_id"],
                    "action_digest": request["action_digest"],
                    "decision": "approve" if allowed else "deny",
                }
                payload = json.dumps(response, ensure_ascii=True).encode("ascii")
                self.approved += int(allowed)
                self.denied += int(not allowed)
                self._write(self._peer.fileno(), struct.pack("!I", len(payload)) + payload, deadline)
                inflight = False
        except TimeoutError:
            self.reason = "review_timeout"
        except (ValueError, TypeError, RecursionError):
            self.reason = "review_protocol_error"
        except (OSError, EOFError) as exc:
            self.reason = (
                "" if self._stop.is_set() or (isinstance(exc, EOFError) and not inflight) else "review_disconnected"
            )
        finally:
            try:
                self._peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def run_reviewed_command(command: Callable[[int], Sequence[str]]) -> int:
    """Launch the CLI child, keeping terminal approval and process-group cleanup in its parent."""
    with TerminalApprovalReviewer.open_tty() as reviewer:
        process = subprocess.Popen(
            command(reviewer.fileno()),
            stdin=subprocess.DEVNULL,
            pass_fds=(reviewer.fileno(),),
            start_new_session=True,
        )
        try:
            reviewer.child_started()
            code = process.wait()
            return code if code >= 0 else 128 - code
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
