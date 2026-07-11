"""B59. Daemon Mode: Multi-Client Shared Agent.

HTTP+SSE server: multiple clients share one agent session.
Source: qwen-code pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ClientSession:
    client_id: str
    connected_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    subscriptions: set[str] = field(default_factory=set)


@dataclass
class SharedMessage:
    id: str
    role: str  # "user", "assistant", "system"
    content: str
    client_id: str | None = None  # who sent it (None = system)
    timestamp: float = field(default_factory=time.time)


class DaemonAgent:
    """Shared agent session that multiple clients can connect to."""

    def __init__(self, max_clients: int = 10, max_messages: int = 1000) -> None:
        self._clients: dict[str, ClientSession] = {}
        self._messages: list[SharedMessage] = []
        self._max_clients = max_clients
        self._max_messages = max_messages
        self._message_counter = 0
        self._event_handlers: dict[str, list[Callable]] = {}

    def connect(self, client_id: str) -> ClientSession:
        if len(self._clients) >= self._max_clients:
            raise RuntimeError(f"Max clients ({self._max_clients}) reached")
        if client_id in self._clients:
            return self._clients[client_id]
        session = ClientSession(client_id=client_id)
        self._clients[client_id] = session
        self._emit("client_connected", client_id)
        return session

    def disconnect(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        self._emit("client_disconnected", client_id)

    def send_message(self, client_id: str, role: str, content: str) -> SharedMessage:
        self._message_counter += 1
        msg = SharedMessage(
            id=f"msg_{self._message_counter}",
            role=role,
            content=content,
            client_id=client_id,
        )
        self._messages.append(msg)
        if len(self._messages) > self._max_messages:
            self._messages = self._messages[-self._max_messages:]
        self._emit("message", msg)
        return msg

    def get_messages(self, since_id: str | None = None) -> list[SharedMessage]:
        if since_id is None:
            return list(self._messages)
        idx = next(
            (i for i, m in enumerate(self._messages) if m.id == since_id),
            0,
        )
        return self._messages[idx + 1:]

    def subscribe(self, client_id: str, event: str) -> None:
        session = self._clients.get(client_id)
        if session:
            session.subscriptions.add(event)

    def on_event(self, event: str, handler: Callable) -> None:
        self._event_handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, data: Any) -> None:
        for handler in self._event_handlers.get(event, []):
            try:
                handler(data)
            except Exception:
                pass

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def message_count(self) -> int:
        return len(self._messages)