"""Exact-action confirmations from an explicitly inherited local supervisor socket.

The supervisor endpoint is an authority boundary, not a model/tool interface.
Frames contain private action arguments and must not be copied to public logs.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import socket
import struct
import threading
import time
from typing import Any

from .marcus_authority import CapabilityMask, ConfirmationMode, ResolvedAction


SCHEMA = "algo-cli-approval-v1"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2048
MAX_WAIT_SECONDS = 120.0


class ApprovalChannelError(ValueError):
    """The explicitly selected supervisor channel cannot be used safely."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApprovalChannelError("duplicate approval field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ApprovalChannelError("nonfinite approval value")


def decode_approval_object(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if type(value) is not dict:
        raise ApprovalChannelError("approval frame must be an object")
    return value


class ApprovalChannel:
    def __init__(self, peer: socket.socket, timeout_seconds: float) -> None:
        self._peer = peer
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._closed = False
        self.last_reason = ""

    @classmethod
    def from_fd(cls, fd: int, *, timeout_seconds: float = MAX_WAIT_SECONDS) -> ApprovalChannel:
        """Take ownership of a connected POSIX Unix socket, never a stdio/network FD."""
        if os.name != "posix" or type(fd) is not int or fd < 3:
            raise ApprovalChannelError("approval_fd must be an inherited POSIX Unix socket")
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= MAX_WAIT_SECONDS
        ):
            raise ApprovalChannelError("approval timeout must be finite and between 0 and 120 seconds")
        peer = None
        try:
            peer = socket.socket(fileno=fd)
            if (
                peer.family != socket.AF_UNIX
                or peer.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM
            ):
                raise ApprovalChannelError("approval_fd must be a Unix stream socket")
            peer.getpeername()
            peer.set_inheritable(False)
        except (OSError, ValueError) as exc:
            if peer is not None:
                peer.close()
            else:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise ApprovalChannelError("approval channel setup failed") from exc
        return cls(peer, float(timeout_seconds))

    def fileno(self) -> int:
        return self._peer.fileno()

    def close(self) -> None:
        self._closed = True
        self._peer.close()

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _receive_exact(self, size: int, deadline: float) -> bytes:
        parts = bytearray()
        while len(parts) < size:
            self._peer.settimeout(self._remaining(deadline))
            data = self._peer.recv(size - len(parts))
            if not data:
                raise EOFError
            parts.extend(data)
        return bytes(parts)

    def confirm(self, action: ResolvedAction, arguments: dict[str, Any]) -> bool:
        """Return one exact approval or fail closed; never issue reusable consent."""
        if self._closed:
            return False
        if action.confirmation_mode is ConfirmationMode.HANDOFF_REQUIRED:
            self.last_reason = "approval_handoff_required"
            return False
        if action.confirmation_mode not in {ConfirmationMode.ACTION_TIME, ConfirmationMode.SESSION_PREAPPROVAL}:
            self.last_reason = "approval_not_reviewable"
            return False
        deadline = time.monotonic() + self._timeout_seconds
        if not self._lock.acquire(timeout=self._timeout_seconds):
            self.last_reason = "approval_timeout"
            return False
        try:
            if self._closed:
                return False
            created_at = time.time()
            request = {
                "schema": SCHEMA,
                "type": "approval_request",
                "request_id": secrets.token_hex(16),
                "action_digest": action.action_digest,
                "name": action.name,
                "target": action.target,
                "confirmation_mode": action.confirmation_mode.value,
                "effect_class": action.effect_class.value,
                "capabilities": list(CapabilityMask(action.capability_mask).names()),
                "arguments": arguments,
                "created_at": created_at,
                "expires_at": created_at + self._remaining(deadline),
            }
            payload = json.dumps(request, sort_keys=True, allow_nan=False, ensure_ascii=True).encode("utf-8")
            if len(payload) > MAX_REQUEST_BYTES:
                self.last_reason = "approval_request_too_large"
                return False
            self._peer.settimeout(self._remaining(deadline))
            self._peer.sendall(struct.pack("!I", len(payload)) + payload)
            size = struct.unpack("!I", self._receive_exact(4, deadline))[0]
            if not 0 < size <= MAX_RESPONSE_BYTES:
                raise ApprovalChannelError("approval frame size")
            response = decode_approval_object(self._receive_exact(size, deadline))
            self._remaining(deadline)
            if (
                type(response) is not dict
                or set(response) != {"schema", "type", "request_id", "action_digest", "decision"}
                or response["schema"] != SCHEMA
                or response["type"] != "approval_response"
                or response["request_id"] != request["request_id"]
                or response["action_digest"] != action.action_digest
                or response["decision"] not in ("approve", "deny")
            ):
                raise ApprovalChannelError("approval response does not bind the request")
            allowed = response["decision"] == "approve"
            self.last_reason = "" if allowed else "approval_denied"
            return allowed
        except TimeoutError:
            self.last_reason = "approval_timeout"
            self.close()
        except (ValueError, TypeError, RecursionError):
            self.last_reason = "approval_protocol_error"
            self.close()
        except (OSError, EOFError):
            self.last_reason = "approval_unavailable"
            self.close()
        except BaseException:
            self.close()
            raise
        finally:
            self._lock.release()
        return False
