"""B52. Session Forking + Compaction Circuit Breaker.

Fork sessions with parent pointers (no message duplication).
Compaction circuit breaker prevents infinite compaction loops.
Source: aloop pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str  # "user", "assistant", "tool"
    content: str
    tool_call_id: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class SessionFork:
    id: str
    parent_id: str | None = None
    messages: list[Message] = field(default_factory=list)
    fork_point: int = 0  # index in parent's messages where fork occurred
    depth: int = 0
    created_at: float = field(default_factory=time.time)
    materialized: bool = False

    MAX_FORK_DEPTH = 10


class SessionStore:
    """Manage sessions with fork support and compaction circuit breaker."""

    def __init__(self, max_compaction_failures: int = 3) -> None:
        self._sessions: dict[str, SessionFork] = {}
        self._compaction_failures: dict[str, int] = {}
        self._max_compaction_failures = max_compaction_failures

    def create(self, session_id: str, messages: list[Message] | None = None) -> SessionFork:
        s = SessionFork(id=session_id, messages=list(messages or []))
        self._sessions[session_id] = s
        return s

    def fork(
        self,
        parent_id: str,
        fork_id: str,
        fork_point: int | None = None,
    ) -> SessionFork:
        parent = self._sessions.get(parent_id)
        if parent is None:
            raise KeyError(f"Parent session {parent_id} not found")
        if parent.depth >= SessionFork.MAX_FORK_DEPTH:
            raise ValueError(f"Max fork depth ({SessionFork.MAX_FORK_DEPTH}) exceeded")

        fp = fork_point if fork_point is not None else len(parent.messages)
        child = SessionFork(
            id=fork_id,
            parent_id=parent_id,
            fork_point=fp,
            depth=parent.depth + 1,
        )
        # Child does NOT copy messages — uses parent pointer
        self._sessions[fork_id] = child
        return child

    def get_messages(self, session_id: str) -> list[Message]:
        """Resolve messages by walking parent chain."""
        s = self._sessions.get(session_id)
        if s is None:
            return []
        if s.parent_id is None:
            return list(s.messages)
        parent_msgs = self.get_messages(s.parent_id)[: s.fork_point]
        return parent_msgs + list(s.messages)

    def materialize(self, session_id: str) -> list[Message]:
        """Flatten fork chain into a single message list."""
        msgs = self.get_messages(session_id)
        s = self._sessions[session_id]
        s.materialized = True
        s.messages = list(msgs)
        s.parent_id = None
        return msgs

    def can_compact(self, session_id: str) -> bool:
        """Circuit breaker: prevent compaction if too many failures."""
        return self._compaction_failures.get(session_id, 0) < self._max_compaction_failures

    def record_compaction_failure(self, session_id: str) -> None:
        self._compaction_failures[session_id] = self._compaction_failures.get(session_id, 0) + 1

    def reset_compaction(self, session_id: str) -> None:
        self._compaction_failures.pop(session_id, None)

    def list_forks(self, parent_id: str) -> list[str]:
        return [sid for sid, s in self._sessions.items() if s.parent_id == parent_id]