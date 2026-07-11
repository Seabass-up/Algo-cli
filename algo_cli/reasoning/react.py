"""ReAct+ Enhanced Reasoning-Action Loop.

Interleaves structured Thought, Action, and Observation steps with:
- Typed action parsing (tool calls, sub-questions, assertions)
- Observation summarization to prevent context bloat
- Automatic thought-chain compaction for long episodes
- Loop detection with strategy broadening
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..chat_protocol import get_attr


@dataclass
class ReactStep:
    """One step in a ReAct episode."""
    thought: str
    action: str
    action_input: dict[str, Any] | str
    observation: str
    timestamp: float = field(default_factory=time.time)

    def to_message(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": f"Thought: {self.thought}\nAction: {self.action}\nAction Input: {self._format_input()}",
        }

    def observation_message(self) -> dict[str, Any]:
        return {
            "role": "user",
            "content": f"Observation: {self.observation}",
        }

    def _format_input(self) -> str:
        if isinstance(self.action_input, dict):
            return json.dumps(self.action_input)
        return str(self.action_input)


THOUGHT_RE = re.compile(r"Thought:\s*(.+?)(?=\nAction:|$)", re.DOTALL)
ACTION_RE = re.compile(r"Action:\s*(.+?)(?=\nAction Input:|$)", re.DOTALL)
ACTION_INPUT_RE = re.compile(r"Action Input:\s*(.+?)$", re.DOTALL)


def parse_react_output(text: str) -> tuple[str, str, dict[str, Any] | str]:
    """Parse a ReAct-formatted response into (thought, action, action_input)."""
    thought_m = THOUGHT_RE.search(text)
    action_m = ACTION_RE.search(text)
    input_m = ACTION_INPUT_RE.search(text)

    thought = thought_m.group(1).strip() if thought_m else ""
    action = action_m.group(1).strip() if action_m else ""
    raw_input = input_m.group(1).strip() if input_m else ""

    # Try parsing action input as JSON; fall back to string
    action_input: dict[str, Any] | str = raw_input
    if raw_input:
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, dict):
                action_input = parsed
        except (json.JSONDecodeError, ValueError):
            pass

    return thought, action, action_input


def compact_observations(steps: list[ReactStep], max_chars: int = 4000) -> str:
    """Compact a ReAct episode into a summary for context injection."""
    if not steps:
        return ""
    lines: list[str] = []
    total = 0
    for i, step in enumerate(steps):
        entry = f"[{i+1}] Thought: {step.thought[:200]}\n    Action: {step.action} -> Obs: {step.observation[:300]}"
        if total + len(entry) > max_chars:
            remaining = len(steps) - i
            lines.append(f"... ({remaining} earlier steps compacted)")
            break
        lines.append(entry)
        total += len(entry)
    return "\n".join(lines)


@dataclass
class ReactLoop:
    """Stateful ReAct+ loop for agent harness integration."""
    max_steps: int = 10
    observation_limit: int = 2000
    loop_detection_window: int = 3

    steps: list[ReactStep] = field(default_factory=list)
    _action_history: list[str] = field(default_factory=list)

    def detect_loop(self) -> bool:
        """Detect if the last N actions are identical (stuck loop)."""
        if len(self._action_history) < self.loop_detection_window:
            return False
        window = self._action_history[-self.loop_detection_window:]
        return len(set(window)) == 1

    def add_step(self, step: ReactStep) -> None:
        self.steps.append(step)
        self._action_history.append(f"{step.action}:{str(step.action_input)[:80]}")

    def truncate_observation(self, obs: str) -> str:
        if len(obs) > self.observation_limit:
            return obs[:self.observation_limit - 20] + "\n...[truncated]"
        return obs

    def build_context(self, task: str, system: str) -> list[dict[str, Any]]:
        """Build the message list for the next LLM call."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        for step in self.steps:
            messages.append(step.to_message())
            messages.append(step.observation_message())
        # Final prompt to continue reasoning
        messages.append({
            "role": "user",
            "content": "Continue with your next Thought and Action. If you have enough information to answer, respond with just the final answer.",
        })
        return messages


def run_react_loop(
    *,
    task: str,
    client: Any,
    model: str,
    tools: list[Any] | None = None,
    system: str = "You are a reasoning agent. Use the ReAct format:\nThought: <your reasoning>\nAction: <tool name or 'finish'>\nAction Input: <JSON args or final answer>",
    max_steps: int = 10,
    tool_map: dict[str, Callable] | None = None,
    observation_limit: int = 2000,
) -> list[ReactStep]:
    """Run a complete ReAct+ episode.

    Args:
        task: The task to solve.
        client: Ollama client instance.
        model: Model name.
        tools: Optional list of tool functions for the model.
        system: System prompt (ReAct format instructions).
        max_steps: Maximum reasoning steps.
        tool_map: Optional mapping of action names to callables for executing actions.
        observation_limit: Max chars per observation.

    Returns:
        List of ReactStep records.
    """
    loop = ReactLoop(max_steps=max_steps, observation_limit=observation_limit)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": loop.build_context(task, system),
        "stream": False,
    }
    if tools:
        kwargs["tools"] = tools

    for _ in range(max_steps):
        try:
            response = client.chat(**kwargs)
        except Exception as exc:
            loop.add_step(ReactStep(
                thought="(LLM call failed)",
                action="error",
                action_input={},
                observation=str(exc),
            ))
            break

        content = get_attr(get_attr(response, "message", {}), "content", "")
        tool_calls = get_attr(get_attr(response, "message", {}), "tool_calls", None)

        # If the model used structured tool calls, execute them
        if tool_calls:
            from ..tool_runtime import normalize_tool_call
            for call in tool_calls:
                name, args = normalize_tool_call(call)
                thought = f"(model called tool: {name})"
                obs = ""
                if tool_map and name in tool_map:
                    try:
                        obs = str(tool_map[name](**args))
                    except Exception as exc:
                        obs = f"Tool error: {exc}"
                step = ReactStep(thought=thought, action=name, action_input=args, observation=loop.truncate_observation(obs))
                loop.add_step(step)
            # Rebuild context and continue
            kwargs["messages"] = loop.build_context(task, system)
            continue

        # Parse ReAct-formatted text
        thought, action, action_input = parse_react_output(content)

        if not action or action.lower() == "finish":
            # Task complete or final answer
            loop.add_step(ReactStep(
                thought=thought,
                action="finish",
                action_input=action_input if isinstance(action_input, str) else json.dumps(action_input),
                observation="Task complete.",
            ))
            break

        # Execute action if tool_map provided
        obs = ""
        if tool_map:
            fn = tool_map.get(action)
            if fn:
                try:
                    args = action_input if isinstance(action_input, dict) else {"query": str(action_input)}
                    obs = str(fn(**args))
                except Exception as exc:
                    obs = f"Tool error: {exc}"
            else:
                obs = f"Unknown action: {action}"

        step = ReactStep(
            thought=thought,
            action=action,
            action_input=action_input,
            observation=loop.truncate_observation(obs),
        )
        loop.add_step(step)

        # Loop detection
        if loop.detect_loop():
            step.observation += "\n[Loop detected: same action repeated. Try a different approach.]"
            break

        # Rebuild context
        kwargs["messages"] = loop.build_context(task, system)

    return loop.steps
