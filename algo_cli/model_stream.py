"""Bounded inference-only recovery for providers without a native retry adapter."""

from __future__ import annotations

from collections.abc import Callable, Generator
import logging
import time
from typing import Any

import httpx
from ollama import ResponseError

from .chat_protocol import get_attr


RETRY_DELAYS = (1.0, 2.0)
logger = logging.getLogger(__name__)


class EmptyModelResponseError(RuntimeError):
    """A provider returned neither an answer nor an executable tool call."""


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, httpx.TransportError)) or (
        isinstance(exc, ResponseError) and exc.status_code in {429, 500, 502, 503, 504}
    )


def recover_model_stream(
    request: Callable[[], Any], *, retries: bool = True,
) -> Generator[Any, None, None]:
    """Repeat only inference before usable output, never a dispatched tool.

    Providers with native recovery and journaled Agent Blocks opt out. Once any
    answer text or tool call has been delivered, even a timeout is propagated.
    """
    delays = RETRY_DELAYS if retries else ()
    for attempt in range(len(delays) + 1):
        delivered = False
        stream = None
        failure: Exception | None = None
        try:
            stream = request()
            for chunk in stream:
                message = get_attr(chunk, "message", {})
                delivered |= bool(str(get_attr(message, "content", "") or "").strip()) or bool(
                    get_attr(message, "tool_calls", None)
                )
                yield chunk
            if delivered:
                return
            failure = EmptyModelResponseError("Model returned no final answer or valid tool call.")
        except Exception as exc:
            if delivered or not _retryable(exc):
                raise
            failure = exc
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("Provider stream close failed", exc_info=True)
        if attempt == len(delays):
            assert failure is not None
            if isinstance(failure, EmptyModelResponseError):
                return
            raise failure
        yield {"message": {}, "model_retry": {
            "attempt": attempt + 1, "max_retries": len(delays),
            "delay_seconds": delays[attempt],
            "reason": "empty_response" if isinstance(failure, EmptyModelResponseError) else "transport_interrupted",
        }}
        time.sleep(delays[attempt])
