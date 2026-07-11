"""B48. Multi-Agent Group Chat Orchestration (Semantic Kernel Pattern).

Coordinates multiple specialist agents (writer/reviewer, planner/executer,
extractor/verifier) in a structured conversation with round limits and
role rotation.  Any agent can terminate early via should_stop().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class AgentMessage:
    agent_name: str
    content: str
    round: int
    role: str


@dataclass
class GroupChatResult:
    final_answer: str
    messages: list[AgentMessage] = field(default_factory=list)
    rounds_used: int = 0
    terminated_by: str = ""


class ChatAgent:
    """A participant in a group chat."""

    def __init__(
        self,
        name: str,
        role: str,
        respond_fn: Callable[[str, list[AgentMessage]], str],
        should_stop_fn: Callable[[str], bool] | None = None,
    ):
        self.name = name
        self.role = role
        self._respond_fn = respond_fn
        self._should_stop_fn = should_stop_fn or (lambda _: False)

    def respond(self, task: str, history: list[AgentMessage]) -> str:
        return self._respond_fn(task, history)

    def should_stop(self, response: str) -> bool:
        return self._should_stop_fn(response)


class GroupChatOrchestration:
    """Round-robin multi-agent conversation with termination conditions."""

    def __init__(self, agents: list[ChatAgent], max_rounds: int = 5):
        self.agents = agents
        self.max_rounds = max_rounds
        self.history: list[AgentMessage] = []

    def invoke(self, task: str) -> GroupChatResult:
        """Run the group chat until max_rounds or early termination."""
        if not self.agents:
            return GroupChatResult(final_answer="", messages=[], rounds_used=0, terminated_by="")
        current_round = 0
        for r in range(self.max_rounds):
            current_round = r + 1
            for agent in self.agents:
                response = agent.respond(task, list(self.history))
                msg = AgentMessage(
                    agent_name=agent.name,
                    content=response,
                    round=current_round,
                    role=agent.role,
                )
                self.history.append(msg)
                if agent.should_stop(response):
                    return GroupChatResult(
                        final_answer=response,
                        messages=list(self.history),
                        rounds_used=current_round,
                        terminated_by=agent.name,
                    )
        final = self.history[-1].content if self.history else ""
        return GroupChatResult(
            final_answer=final,
            messages=list(self.history),
            rounds_used=current_round,
            terminated_by="max_rounds",
        )

    def _build_context(self, task: str) -> str:
        parts = [f"Task: {task}"]
        for msg in self.history:
            parts.append(f"[{msg.agent_name} ({msg.role})]: {msg.content}")
        return "\n\n".join(parts)

    def clear(self) -> None:
        self.history.clear()


# ── common agent pair factories ───────────────────────────────────────


def make_writer_reviewer(
    writer_fn: Callable[[str, list[AgentMessage]], str],
    reviewer_fn: Callable[[str, list[AgentMessage]], str],
    max_rounds: int = 5,
) -> GroupChatOrchestration:
    """Create a writer/reviewer pair."""
    def reviewer_stop(response: str) -> bool:
        return "APPROVED" in response.upper()

    writer = ChatAgent("Writer", "writer", writer_fn)
    reviewer = ChatAgent("Reviewer", "reviewer", reviewer_fn, reviewer_stop)
    return GroupChatOrchestration([writer, reviewer], max_rounds=max_rounds)


def make_extractor_verifier(
    extractor_fn: Callable[[str, list[AgentMessage]], str],
    verifier_fn: Callable[[str, list[AgentMessage]], str],
    max_rounds: int = 3,
) -> GroupChatOrchestration:
    """Create an extractor/verifier pair."""
    def verifier_stop(response: str) -> bool:
        return "VERIFIED" in response.upper()

    extractor = ChatAgent("Extractor", "extractor", extractor_fn)
    verifier = ChatAgent("Verifier", "verifier", verifier_fn, verifier_stop)
    return GroupChatOrchestration([extractor, verifier], max_rounds=max_rounds)


def make_planner_executer(
    planner_fn: Callable[[str, list[AgentMessage]], str],
    executer_fn: Callable[[str, list[AgentMessage]], str],
    max_rounds: int = 5,
) -> GroupChatOrchestration:
    """Create a planner/executer pair."""
    def executer_stop(response: str) -> bool:
        return "COMPLETE" in response.upper()

    planner = ChatAgent("Planner", "planner", planner_fn)
    executer = ChatAgent("Executer", "executer", executer_fn, executer_stop)
    return GroupChatOrchestration([planner, executer], max_rounds=max_rounds)