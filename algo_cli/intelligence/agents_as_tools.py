"""B73. Agents-as-Tools + Handoffs.

Delegate to other agents via tool calls or handoffs.
Source: OpenAI Agents SDK pattern.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable


class DelegationMode(Enum):
    AS_TOOL = auto()    # parent calls agent, gets result, continues
    HANDOFF = auto()    # parent transfers control entirely


@dataclass
class AgentTool:
    name: str
    description: str
    agent_id: str
    run_fn: Callable[[str], str]
    mode: DelegationMode = DelegationMode.AS_TOOL


@dataclass
class HandoffResult:
    transferred_to: str
    reason: str = ""
    context_summary: str = ""


class AgentsAsTools:
    """Register agents as callable tools or handoff targets."""

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}
        self._handoff_targets: dict[str, AgentTool] = {}

    def register_as_tool(self, tool: AgentTool) -> None:
        tool.mode = DelegationMode.AS_TOOL
        self._tools[tool.name] = tool

    def register_as_handoff(self, tool: AgentTool) -> None:
        tool.mode = DelegationMode.HANDOFF
        self._handoff_targets[tool.agent_id] = tool

    def call_tool(self, tool_name: str, input: str) -> str:
        """Call an agent as a tool — parent continues after."""
        tool = self._tools.get(tool_name)
        if not tool:
            raise KeyError(f"Tool '{tool_name}' not found")
        return tool.run_fn(input)

    def handoff(self, agent_id: str, context: str, reason: str = "") -> HandoffResult:
        """Transfer control to another agent entirely."""
        tool = self._handoff_targets.get(agent_id)
        if not tool:
            raise KeyError(f"Handoff target '{agent_id}' not found")
        result = HandoffResult(transferred_to=agent_id, reason=reason, context_summary=context[:500])
        tool.run_fn(context)
        return result

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": t.name, "description": t.description, "agent": t.agent_id}
                for t in self._tools.values()]

    def list_handoff_targets(self) -> list[str]:
        return list(self._handoff_targets.keys())