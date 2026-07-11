"""B36. Layered Agent Runtime + Workbench Boundary (AutoGen Pattern).

Separates agent message passing (runtime), agent logic (agent API), and
tool execution (workbench) into distinct layers with a trust boundary.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# ── message layer ─────────────────────────────────────────────────────


@dataclass
class Message:
    id: str
    role: str  # "user", "assistant", "tool", "system"
    content: str
    agent_name: str = ""
    tool_call_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceEvent:
    type: str  # "message", "tool_call", "tool_result", "cancel", "handoff"
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ── runtime layer ─────────────────────────────────────────────────────


class AgentRuntime:
    """Message bus + trace recorder + cancellation support."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.traces: list[TraceEvent] = []
        self._cancelled = False
        self._trace_id = str(uuid.uuid4())

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def post(self, msg: Message) -> None:
        self.messages.append(msg)
        self.traces.append(TraceEvent(type="message", data={"role": msg.role, "agent": msg.agent_name}))

    def cancel(self) -> None:
        self._cancelled = True
        self.traces.append(TraceEvent(type="cancel"))

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def get_history(self, agent_name: str | None = None) -> list[Message]:
        if agent_name:
            return [m for m in self.messages if m.agent_name == agent_name]
        return list(self.messages)

    def handoff(self, from_agent: str, to_agent: str, context: str = "") -> None:
        self.traces.append(TraceEvent(
            type="handoff",
            data={"from": from_agent, "to": to_agent, "context": context},
        ))


# ── agent API layer ───────────────────────────────────────────────────


class Agent(Protocol):
    """Protocol for agent implementations."""

    name: str
    role: str

    def respond(self, runtime: AgentRuntime, task: str) -> str: ...


class SimpleAgent:
    """A basic agent that uses a callable to generate responses."""

    def __init__(self, name: str, role: str, fn: Callable[[str, list[Message]], str]):
        self.name = name
        self.role = role
        self._fn = fn

    def respond(self, runtime: AgentRuntime, task: str) -> str:
        history = runtime.get_history()
        result = self._fn(task, history)
        runtime.post(Message(
            id=str(uuid.uuid4()),
            role="assistant",
            content=result,
            agent_name=self.name,
        ))
        return result


# ── workbench layer (tool execution boundary) ────────────────────────


@dataclass
class ToolAction:
    tool: str
    params: dict[str, Any]
    requires_approval: bool = False


@dataclass
class ToolResult:
    tool: str
    success: bool
    output: str
    error: str | None = None


class Workbench:
    """Untrusted tool execution boundary.

    Tools cannot directly mutate files or send externally — they return
    action proposals that the host gates.
    """

    def __init__(self, approval_fn: Callable[[ToolAction], bool] | None = None):
        self._tools: dict[str, Callable[[dict], str]] = {}
        self._approval_fn = approval_fn or (lambda a: not a.requires_approval)
        self._allowlisted: set[str] = set()

    def register_tool(self, name: str, fn: Callable[[dict], str], allowlisted: bool = False) -> None:
        self._tools[name] = fn
        if allowlisted:
            self._allowlisted.add(name)

    def execute(self, action: ToolAction) -> ToolResult:
        if action.tool not in self._tools:
            return ToolResult(tool=action.tool, success=False, output="", error="unknown tool")
        if action.requires_approval and not self._approval_fn(action):
            return ToolResult(tool=action.tool, success=False, output="", error="approval denied")
        try:
            output = self._tools[action.tool](action.params)
            return ToolResult(tool=action.tool, success=True, output=output)
        except Exception as e:
            return ToolResult(tool=action.tool, success=False, output="", error=str(e))

    @property
    def allowlisted_tools(self) -> set[str]:
        return set(self._allowlisted)


# ── agent-as-tool wrapper ─────────────────────────────────────────────


class AgentAsTool:
    """Wraps an agent so it can be invoked as a tool by another agent."""

    def __init__(self, agent: Agent, runtime: AgentRuntime):
        self._agent = agent
        self._runtime = runtime

    def __call__(self, params: dict) -> str:
        task = params.get("task", "")
        return self._agent.respond(self._runtime, task)