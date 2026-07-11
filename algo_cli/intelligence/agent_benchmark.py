"""B47. Gym-like Agent Evaluation Environment (TextWorld Pattern).

Benchmark Algo CLI agent performance with reproducible episodes, score
tracking, and step limits.  Uses a Gym-like API: reset() → (obs, infos),
step(action) → (obs, score, done, infos).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class EpisodeResult:
    task: str
    steps: int
    max_steps: int
    score: float
    done: bool
    success: bool
    tool_calls: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    duration_ms: float = 0.0


@dataclass
class BenchmarkScenario:
    """A single benchmark scenario definition."""
    name: str
    task: str
    seed: int = 42
    max_steps: int = 50
    setup_fn: Callable[[], dict] | None = None
    verify_fn: Callable[[str, dict], tuple[bool, float]] | None = None
    # verify_fn returns (success, score)


class AgentEnv:
    """Gym-like environment for a single benchmark episode."""

    def __init__(self, scenario: BenchmarkScenario):
        self.scenario = scenario
        self.task = scenario.task
        self.seed = scenario.seed
        self.max_steps = scenario.max_steps
        self._step = 0
        self._done = False
        self._score = 0.0
        self._tool_calls: list[dict] = []
        self._errors: list[str] = []
        self._trace: list[dict] = []
        self._state: dict[str, Any] = {}

    def reset(self) -> tuple[str, dict]:
        """Reset environment to initial state."""
        self._step = 0
        self._done = False
        self._score = 0.0
        self._tool_calls = []
        self._errors = []
        self._trace = []
        if self.scenario.setup_fn:
            self._state = self.scenario.setup_fn()
        else:
            self._state = {}
        return self.task, {"step": 0, "state": self._state}

    def step(self, action: dict) -> tuple[str, float, bool, dict]:
        """Execute one agent action.

        action is a dict with:
        - "type": "tool_call", "response", "done"
        - "tool": tool name (for tool_call)
        - "params": tool params
        - "content": response text
        """
        self._step += 1
        action_type = action.get("type", "response")
        reward = 0.0

        if action_type == "tool_call":
            self._tool_calls.append({
                "tool": action.get("tool", ""),
                "params": action.get("params", {}),
                "step": self._step,
            })
            self._trace.append({"step": self._step, "type": "tool_call", "tool": action.get("tool", "")})
            # Small penalty for each tool call (encourages efficiency)
            reward = -0.1

        elif action_type == "response":
            content = action.get("content", "")
            self._trace.append({"step": self._step, "type": "response", "content": content[:200]})
            # Check if the response is correct
            if self.scenario.verify_fn:
                success, score = self.scenario.verify_fn(content, self._state)
                reward = score
                if success:
                    self._done = True
            else:
                reward = 0.0

        elif action_type == "done":
            self._done = True

        elif action_type == "error":
            self._errors.append(action.get("content", "unknown error"))
            reward = -0.5

        self._score += reward

        if self._step >= self.max_steps:
            self._done = True

        infos = {
            "step": self._step,
            "score": self._score,
            "tool_calls": list(self._tool_calls),
            "errors": list(self._errors),
            "trace": list(self._trace),
            "success": self._done and self._score > 0,
        }
        return "", reward, self._done, infos


class AgentBenchmark:
    """Runs benchmark episodes and aggregates results."""

    def __init__(self, max_steps: int = 50):
        self.max_steps = max_steps
        self.scenarios: dict[str, BenchmarkScenario] = {}
        self.results: list[EpisodeResult] = []

    def add_scenario(self, scenario: BenchmarkScenario) -> None:
        self.scenarios[scenario.name] = scenario

    def run_episode(self, scenario_name: str, agent_fn: Callable[[str, dict], dict]) -> EpisodeResult:
        """Run a single episode with the given agent function."""
        scenario = self.scenarios.get(scenario_name)
        if not scenario:
            return EpisodeResult(
                task="", steps=0, max_steps=0, score=0,
                done=True, success=False, errors=["scenario not found"],
            )
        env = AgentEnv(scenario)
        obs, infos = env.reset()
        start = time.monotonic()
        while not env._done:
            action = agent_fn(obs, infos)
            obs, reward, done, infos = env.step(action)
            if done:
                break
        duration = (time.monotonic() - start) * 1000
        result = EpisodeResult(
            task=scenario.task,
            steps=env._step,
            max_steps=scenario.max_steps,
            score=env._score,
            done=env._done,
            success=infos.get("success", False),
            tool_calls=env._tool_calls,
            errors=env._errors,
            trace=env._trace,
            duration_ms=duration,
        )
        self.results.append(result)
        return result

    def report(self) -> dict[str, Any]:
        """Aggregate results across all episodes."""
        if not self.results:
            return {"total": 0}
        scores = [r.score for r in self.results]
        steps = [r.steps for r in self.results]
        successes = sum(1 for r in self.results if r.success)
        sorted_scores = sorted(scores)
        n = len(sorted_scores)
        p50 = sorted_scores[n // 2]
        p90 = sorted_scores[int(n * 0.9)] if n > 1 else sorted_scores[0]
        return {
            "total": len(self.results),
            "successes": successes,
            "success_rate": successes / len(self.results),
            "avg_score": sum(scores) / len(scores),
            "p50_score": p50,
            "p90_score": p90,
            "avg_steps": sum(steps) / len(steps),
            "avg_duration_ms": sum(r.duration_ms for r in self.results) / len(self.results),
        }


# ── built-in scenarios ────────────────────────────────────────────────


def make_find_file_scenario() -> BenchmarkScenario:
    """Scenario: find a file and extract a value."""
    state = {"target_file": "EST-2026-002_200A.html", "target_value": "8291.60"}

    def setup() -> dict:
        return state

    def verify(content: str, st: dict) -> tuple[bool, float]:
        if st["target_value"] in content:
            return True, 1.0
        return False, 0.0

    return BenchmarkScenario(
        name="find_file",
        task=f"Find the file {state['target_file']} and extract the total price.",
        setup_fn=setup,
        verify_fn=verify,
        max_steps=10,
    )


def make_pdf_extract_scenario() -> BenchmarkScenario:
    """Scenario: extract line items from a PDF."""

    def setup() -> dict:
        return {"expected_items": ["panel", "breakers", "conductors", "meter base", "grounding"]}

    def verify(content: str, st: dict) -> tuple[bool, float]:
        found = sum(1 for item in st["expected_items"] if item.lower() in content.lower())
        score = found / len(st["expected_items"])
        return (score >= 0.8, score)

    return BenchmarkScenario(
        name="pdf_extract",
        task="Read the attached PDF and list all line items.",
        setup_fn=setup,
        verify_fn=verify,
        max_steps=15,
    )