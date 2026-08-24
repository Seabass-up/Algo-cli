"""DM2. Strict JSON-RPC 2.0 dispatch over a local Unix socket.

Frames are UTF-8 JSON objects terminated by a newline.  The transport accepts
named parameters only because handlers are invoked with keyword arguments.
Source: ``docs/ALGO.md`` Track M, pattern DM2.
"""
from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 error codes.
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603

# A valid notification has no ``id`` member.  ``None`` cannot represent that
# state because JSON-RPC permits an explicit null id (although it discourages
# clients from using one).
NOTIFICATION_ID = object()


@dataclass
class Telemetry:
    """Per-method call telemetry."""

    call_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    last_called: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        if self.call_count == 0:
            return 0.0
        return self.total_latency_ms / self.call_count


@dataclass
class RPCRegistry:
    """Thread-safe RPC method registry with bounded, aggregate telemetry."""

    _methods: dict[str, Callable[..., Any]] = field(default_factory=dict)
    _telemetry: dict[str, Telemetry] = field(default_factory=dict)
    _active_streams: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        """Register or replace an RPC method handler."""
        if not isinstance(name, str) or not name:
            raise ValueError("RPC method name must be a non-empty string")
        if not callable(handler):
            raise TypeError("RPC handler must be callable")
        with self._lock:
            self._methods[name] = handler
            self._telemetry.setdefault(name, Telemetry())

    def has_method(self, name: str) -> bool:
        with self._lock:
            return name in self._methods

    @property
    def method_names(self) -> list[str]:
        with self._lock:
            return sorted(self._methods)

    @property
    def active_streams(self) -> int:
        with self._lock:
            return self._active_streams

    @staticmethod
    def _validate_params(handler: Callable[..., Any], params: dict[str, Any]) -> None:
        """Validate keyword arguments without exposing signature details."""
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            # Some extension/builtin callables do not expose a signature.
            return
        try:
            signature.bind(**params)
        except TypeError as exc:
            raise RPCError(ERR_INVALID_PARAMS, "Invalid params") from exc

    def _handler_for(self, method: str) -> Callable[..., Any]:
        with self._lock:
            handler = self._methods.get(method)
        if handler is None:
            raise RPCError(ERR_METHOD_NOT_FOUND, "Method not found")
        return handler

    def _record_call(self, method: str, *, failed: bool, elapsed_ms: float) -> None:
        with self._lock:
            tel = self._telemetry.setdefault(method, Telemetry())
            tel.call_count += 1
            if failed:
                tel.error_count += 1
            tel.total_latency_ms += elapsed_ms
            tel.last_called = time.time()

    def dispatch(self, method: str, params: dict[str, Any] | None) -> Any:
        """Dispatch one call, returning its result or raising :class:`RPCError`."""
        handler = self._handler_for(method)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RPCError(ERR_INVALID_PARAMS, "Invalid params")

        t0 = time.monotonic()
        failed = False
        try:
            self._validate_params(handler, params)
            return handler(**params)
        except RPCError:
            failed = True
            raise
        except Exception as exc:
            failed = True
            # Full detail stays in the owner-only daemon log.  RPC clients get
            # a stable message that cannot leak paths, credentials, or values.
            logger.exception("RPC method %s failed", method)
            raise RPCError(ERR_INTERNAL, "Internal error") from exc
        finally:
            self._record_call(
                method,
                failed=failed,
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

    def dispatch_stream(
        self, method: str, params: dict[str, Any] | None
    ) -> Iterator[dict[str, Any]]:
        """Dispatch a streaming call and yield JSON-RPC notification objects."""
        handler = self._handler_for(method)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RPCError(ERR_INVALID_PARAMS, "Invalid params")

        t0 = time.monotonic()
        failed = False
        with self._lock:
            self._active_streams += 1
        try:
            self._validate_params(handler, params)
            for chunk in handler(**params):
                yield {
                    "jsonrpc": "2.0",
                    "method": "stream",
                    "params": {"chunk": chunk},
                }
        except RPCError:
            failed = True
            raise
        except Exception as exc:
            failed = True
            logger.exception("RPC stream %s failed", method)
            raise RPCError(ERR_INTERNAL, "Internal error") from exc
        finally:
            with self._lock:
                self._active_streams -= 1
            self._record_call(
                method,
                failed=failed,
                elapsed_ms=(time.monotonic() - t0) * 1000,
            )

    def telemetry_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a consistent copy of per-method telemetry."""
        with self._lock:
            items = list(self._telemetry.items())
            return {
                name: {
                    "call_count": tel.call_count,
                    "error_count": tel.error_count,
                    "avg_latency_ms": round(tel.avg_latency_ms, 2),
                    "last_called": tel.last_called,
                }
                for name, tel in items
            }


class RPCError(Exception):
    """JSON-RPC error with a public code and sanitized message."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _encode(obj: dict[str, Any]) -> bytes:
    """Encode standards-compliant JSON without non-finite numeric extensions."""
    return (
        json.dumps(
            obj,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def make_response(result: Any, req_id: int | str | None) -> bytes:
    """Build a JSON-RPC success response frame."""
    return _encode({"jsonrpc": "2.0", "result": result, "id": req_id})


def make_error(code: int, message: str, req_id: int | str | None) -> bytes:
    """Build a JSON-RPC error response frame."""
    return _encode(
        {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": req_id,
        }
    )


def make_stream_chunk(chunk: str | dict[str, Any]) -> bytes:
    """Build a stream notification frame."""
    return _encode(
        {"jsonrpc": "2.0", "method": "stream", "params": {"chunk": chunk}}
    )


def _valid_request_id(value: Any) -> bool:
    # bool is an int subclass in Python but is not a useful JSON-RPC id.
    return value is None or isinstance(value, str) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def parse_frame(
    line: bytes,
) -> tuple[str | None, dict[str, Any] | None, object, RPCError | None]:
    """Parse and strictly validate one newline-delimited JSON-RPC request.

    Returns ``(method, params, id, error)``.  ``id is NOTIFICATION_ID`` marks
    a valid notification, while ``None`` is an explicit null id or the id used
    for an error whose request identity could not be established.
    """
    try:
        text = line.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None, None, None, RPCError(ERR_PARSE_ERROR, "Parse error")
    if not text:
        return None, None, None, None

    def reject_non_finite(value: str) -> Any:
        raise ValueError(f"Non-finite JSON number: {value}")

    try:
        obj = json.loads(text, parse_constant=reject_non_finite)
    except (json.JSONDecodeError, ValueError):
        return None, None, None, RPCError(ERR_PARSE_ERROR, "Parse error")

    if not isinstance(obj, dict):
        return (
            None,
            None,
            None,
            RPCError(ERR_INVALID_REQUEST, "Invalid Request"),
        )

    raw_id = obj.get("id") if "id" in obj else NOTIFICATION_ID
    # A malformed object is not yet a valid notification, so structural
    # errors use a null id. Once version/method/id are valid, preserve the
    # sentinel so parameter/method errors for notifications stay response-free.
    error_id = (
        raw_id
        if raw_id is not NOTIFICATION_ID and _valid_request_id(raw_id)
        else None
    )

    if obj.get("jsonrpc") != "2.0":
        return (
            None,
            None,
            error_id,
            RPCError(ERR_INVALID_REQUEST, "Invalid Request"),
        )

    method = obj.get("method")
    if not isinstance(method, str) or not method:
        return (
            None,
            None,
            error_id,
            RPCError(ERR_INVALID_REQUEST, "Invalid Request"),
        )

    if raw_id is not NOTIFICATION_ID and not _valid_request_id(raw_id):
        return (
            None,
            None,
            None,
            RPCError(ERR_INVALID_REQUEST, "Invalid Request"),
        )

    params = obj.get("params", {})
    if not isinstance(params, dict):
        return (
            None,
            None,
            raw_id if raw_id is NOTIFICATION_ID else error_id,
            RPCError(ERR_INVALID_PARAMS, "Invalid params"),
        )

    return method, params, raw_id, None
