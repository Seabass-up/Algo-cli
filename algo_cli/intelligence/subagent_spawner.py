"""B69. Sub-Agent Spawning with Virtual File Isolation.

Spawn focused sub-agents with fresh context, explicit file passing,
and tool scoping.  Source: Sub-Agent-MCP pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class AgentState(Enum):
    PENDING = auto()
    RUNNING = auto()
    DONE = auto()
    FAILED = auto()
    CANCELLED = auto()


@dataclass
class SubAgentConfig:
    id: str
    name: str
    system_prompt: str = ""
    model: str = ""  # empty = use parent's model
    allowed_tools: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)  # explicit file passing
    max_iterations: int = 5
    timeout_s: float = 60.0


@dataclass
class SubAgentResult:
    agent_id: str
    state: AgentState = AgentState.PENDING
    output: str = ""
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    tool_calls: int = 0
    duration_s: float = 0.0
    error: str | None = None


class SubAgentSpawner:
    """Spawn sub-agents with virtual file isolation."""

    def __init__(self) -> None:
        self._configs: dict[str, SubAgentConfig] = {}
        self._results: dict[str, SubAgentResult] = {}
        self._active: dict[str, SubAgentResult] = {}

    def register(self, config: SubAgentConfig) -> None:
        self._configs[config.id] = config

    def spawn(self, agent_id: str, task: str,
              run_fn: Callable[[SubAgentConfig, str], str] | None = None) -> SubAgentResult:
        config = self._configs.get(agent_id)
        if not config:
            raise KeyError(f"Agent {agent_id} not registered")

        result = SubAgentResult(agent_id=agent_id, state=AgentState.RUNNING)
        self._active[agent_id] = result
        start = time.time()

        try:
            if run_fn:
                output = run_fn(config, task)
            else:
                output = f"[{config.name}: {task}]"
            result.output = output
            result.state = AgentState.DONE
        except Exception as e:
            result.error = str(e)
            result.state = AgentState.FAILED
        finally:
            result.duration_s = time.time() - start
            self._results[agent_id] = result
            self._active.pop(agent_id, None)

        return result

    def cancel(self, agent_id: str) -> bool:
        result = self._active.get(agent_id)
        if result and result.state == AgentState.RUNNING:
            result.state = AgentState.CANCELLED
            return True
        return False

    def get_result(self, agent_id: str) -> SubAgentResult | None:
        return self._results.get(agent_id)

    def list_active(self) -> list[str]:
        return [aid for aid, r in self._active.items() if r.state == AgentState.RUNNING]

    def can_access_file(self, agent_id: str, file_path: str) -> bool:
        config = self._configs.get(agent_id)
        if not config:
            return False
        if not config.allowed_files:
            return True  # no restriction
        return file_path in config.allowed_files

    def can_use_tool(self, agent_id: str, tool_name: str) -> bool:
        config = self._configs.get(agent_id)
        if not config:
            return False
        if not config.allowed_tools:
            return True
        return tool_name in config.allowed_tools