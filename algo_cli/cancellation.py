"""One cooperative cancel signal per user turn, with child tokens for team members."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import functools
import logging
import re
import threading
from typing import Any, TypeVar
import weakref

logger = logging.getLogger(__name__)

KEYBOARD_INTERRUPT = "keyboard_interrupt"
TEAM_CANCELLED = "team_cancelled"
APPROVAL_CANCELLED = "approval_cancelled"
PARENT_CANCELLED = "parent_cancelled"
CALLER_CANCELLED = "caller_cancelled"

_SAFE_REASON = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _normalize_reason(reason: str) -> str:
    normalized = str(reason or "").strip()
    if not _SAFE_REASON.fullmatch(normalized):
        raise ValueError("cancellation reason must be a bounded identifier")
    return normalized


class Cancelled(KeyboardInterrupt):
    """Raised by ``raise_if_cancelled``; a KeyboardInterrupt so every existing Ctrl+C path handles it."""

    def __init__(self, reason: str = CALLER_CANCELLED, message: str = "") -> None:
        super().__init__(message or f"cancelled: {reason}")
        self.reason = reason


class ApprovalCancelled(Cancelled):
    """The user cancelled while an approval prompt was waiting; no action was approved or run."""

    def __init__(self, message: str = "Approval cancelled") -> None:
        super().__init__(APPROVAL_CANCELLED, message)


class CancelToken:
    """Thread-safe, idempotent cancellation with a reason, callbacks and child tokens.

    Cancelling a token cancels every child it created; cancelling a child never cancels its parent.
    ``is_set`` mirrors ``threading.Event`` so tools written against an event accept a token.
    """

    def __init__(self, name: str = "", *, parent: CancelToken | None = None) -> None:
        self.name = name
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        self._callbacks: list[Callable[[CancelToken], Any]] = []
        self._children: weakref.WeakSet[CancelToken] = weakref.WeakSet()
        self.parent = parent

    def cancel(self, reason: str = CALLER_CANCELLED) -> bool:
        """Cancel once; later calls keep the first reason. Returns True only for the call that cancelled."""

        normalized = _normalize_reason(reason)
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = normalized
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
            children = list(self._children)
        for child in children:
            child.cancel(normalized)
        for callback in callbacks:
            self._run_callback(callback)
        return True

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            reason = self.reason
            if reason == APPROVAL_CANCELLED:
                raise ApprovalCancelled()
            raise Cancelled(reason, "Agent team cancelled" if reason == TEAM_CANCELLED else "")

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def on_cancel(self, callback: Callable[[CancelToken], Any]) -> Callable[[], None]:
        """Run ``callback(token)`` once on cancel (now, if already cancelled); return an unregister function."""

        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)

                def unregister() -> None:
                    with self._lock:
                        try:
                            self._callbacks.remove(callback)
                        except ValueError:
                            pass

                return unregister
        self._run_callback(callback)
        return lambda: None

    def child(self, name: str = "") -> CancelToken:
        token = CancelToken(name, parent=self)
        with self._lock:
            cancelled = self._event.is_set()
            if not cancelled:
                self._children.add(token)
        if cancelled:
            token.cancel(self.reason)
        return token

    def _run_callback(self, callback: Callable[[CancelToken], Any]) -> None:
        try:
            callback(self)
        except Exception as exc:  # A failing listener must not stop the others or the canceller.
            logger.debug("cancel callback failed: %s", type(exc).__name__)

    # A token is runtime authority shared by reference: config copies keep the same signal.
    def __copy__(self) -> CancelToken:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> CancelToken:
        return self

    def __repr__(self) -> str:
        state = f"cancelled:{self.reason}" if self.is_cancelled else "active"
        return f"CancelToken({self.name!r}, {state})"


_CURRENT: ContextVar[CancelToken | None] = ContextVar("algo_cli_cancel_token", default=None)


def current_token() -> CancelToken | None:
    return _CURRENT.get()


@contextmanager
def bind(token: CancelToken | None) -> Iterator[CancelToken | None]:
    """Make ``token`` the ambient token for this thread/context (copied contexts inherit it)."""

    reset = _CURRENT.set(token)
    try:
        yield token
    finally:
        _CURRENT.reset(reset)


def interrupt_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", "")
    if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason):
        return reason
    return KEYBOARD_INTERRUPT


def interruption_for(reason: str) -> KeyboardInterrupt:
    """The exception a caller raises to stop a turn after its cancellation was recorded."""

    if reason == APPROVAL_CANCELLED:
        return ApprovalCancelled()
    return KeyboardInterrupt()


@contextmanager
def turn_scope(name: str = "turn") -> Iterator[CancelToken]:
    """Bind a fresh token (a child of any enclosing one) and cancel it when Ctrl+C escapes the scope."""

    parent = current_token()
    token = parent.child(name) if parent is not None else CancelToken(name)
    with bind(token):
        try:
            yield token
        except KeyboardInterrupt as exc:
            token.cancel(interrupt_reason(exc))
            raise


_F = TypeVar("_F", bound=Callable[..., Any])


def scoped_turn(func: _F) -> _F:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with turn_scope(getattr(func, "__name__", "turn")):
            return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
