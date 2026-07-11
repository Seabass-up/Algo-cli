"""Tree-of-Thoughts (ToT) Reasoning.

Explores multiple reasoning paths as a tree with:
- BFS: Breadth-first exploration of K best thoughts per level
- DFS: Depth-first with backtracking on dead ends
- MCTS: Monte Carlo Tree Search integration (delegates to mcts.py)

Each thought is evaluated by a value function (LLM-based or heuristic).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..chat_protocol import get_attr


class SearchStrategy(Enum):
    BFS = "bfs"
    DFS = "dfs"


@dataclass
class ThoughtNode:
    """A node in the Tree-of-Thought."""
    thought: str
    value: float  # 0.0-1.0 evaluation score
    depth: int
    children: list["ThoughtNode"] = field(default_factory=list)
    parent: "ThoughtNode | None" = field(default=None, repr=False)
    state: str = ""  # accumulated reasoning state
    visited: bool = False
    pruned: bool = False
    timestamp: float = field(default_factory=time.time)

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    @property
    def path_from_root(self) -> list["ThoughtNode"]:
        """Walk up to root, return path from root to this node."""
        path = []
        node: ThoughtNode | None = self
        while node is not None:
            path.append(node)
            node = node.parent
        path.reverse()
        return path

    def reasoning_chain(self) -> str:
        """Human-readable chain from root to this node."""
        nodes = self.path_from_root
        return "\n".join(f"Step {i+1} (val={n.value:.2f}): {n.thought}" for i, n in enumerate(nodes))


EVALUATION_PROMPT = """Evaluate this reasoning step on a scale of 0.0 to 1.0.

Consider:
- Logical soundness: Is this step valid reasoning?
- Progress: Does it advance toward solving the task?
- Novelty: Does it add new information or just restate?

Respond with just a number between 0.0 and 1.0."""

GENERATION_PROMPT = """Given the current reasoning state, generate {n} distinct next thoughts.

Current state:
{state}

Task: {task}

Generate {n} different reasoning steps. Each should pursue a different approach.
Format as JSON array: [{{"thought": "...", "state": "updated reasoning state"}}]"""


@dataclass
class TreeOfThought:
    """Tree-of-Thought reasoner."""
    strategy: SearchStrategy = SearchStrategy.BFS
    max_depth: int = 4
    branch_factor: int = 3
    value_threshold: float = 0.3  # Prune nodes below this score

    root: ThoughtNode | None = None
    best_leaf: ThoughtNode | None = None
    best_score: float = 0.0
    nodes_explored: int = 0

    def _evaluate(self, thought: str, client: Any, model: str) -> float:
        """Use LLM to evaluate a thought's quality."""
        messages = [
            {"role": "system", "content": EVALUATION_PROMPT},
            {"role": "user", "content": f"Reasoning step: {thought}\n\nRate this step."},
        ]
        try:
            response = client.chat(model=model, messages=messages, stream=False)
            text = get_attr(get_attr(response, "message", {}), "content", "").strip()
            # Parse score
            import re
            m = re.search(r"([0-9]*\.?[0-9]+)", text)
            if m:
                return max(0.0, min(1.0, float(m.group(1))))
        except Exception:
            pass
        return 0.5

    def _generate_thoughts(
        self, task: str, state: str, n: int, client: Any, model: str,
    ) -> list[tuple[str, str]]:
        """Generate n candidate next thoughts using LLM."""
        prompt = GENERATION_PROMPT.format(n=n, state=state[:2000], task=task)
        messages = [
            {"role": "system", "content": "You are a reasoning step generator. Be diverse and creative."},
            {"role": "user", "content": prompt},
        ]
        try:
            response = client.chat(model=model, messages=messages, stream=False, format="json")
            text = get_attr(get_attr(response, "message", {}), "content", "")
            parsed = json.loads(text)
            if isinstance(parsed, list):
                results = []
                for item in parsed[:n]:
                    if isinstance(item, dict):
                        results.append((str(item.get("thought", "")), str(item.get("state", state))))
                return results
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        # Fallback: single thought
        return [(state + " (continue reasoning)", state)]

    def _bfs(self, task: str, client: Any, model: str) -> ThoughtNode | None:
        """BFS search: expand K best nodes at each depth level."""
        if not self.root:
            return None
        current_level = [self.root]
        for depth in range(self.max_depth):
            if not current_level:
                break
            next_level: list[ThoughtNode] = []
            for node in current_level:
                if node.pruned:
                    continue
                candidates = self._generate_thoughts(
                    task, node.state, self.branch_factor, client, model,
                )
                for thought, state in candidates:
                    value = self._evaluate(thought, client, model)
                    child = ThoughtNode(
                        thought=thought, value=value, depth=depth + 1,
                        parent=node, state=state,
                    )
                    node.children.append(child)
                    self.nodes_explored += 1
                    if value >= self.value_threshold:
                        next_level.append(child)
                    else:
                        child.pruned = True
                    if value > self.best_score:
                        self.best_score = value
                        self.best_leaf = child
            # Keep top-K for next level
            next_level.sort(key=lambda n: n.value, reverse=True)
            current_level = next_level[:self.branch_factor]
        return self.best_leaf

    def _dfs(self, task: str, client: Any, model: str) -> ThoughtNode | None:
        """DFS with backtracking: explore promising paths first."""
        if not self.root:
            return None
        stack = [self.root]
        while stack and self.nodes_explored < self.max_depth * self.branch_factor * 2:
            node = stack.pop()
            if node.depth >= self.max_depth or node.pruned:
                continue
            candidates = self._generate_thoughts(
                task, node.state, self.branch_factor, client, model,
            )
            children = []
            for thought, state in candidates:
                value = self._evaluate(thought, client, model)
                child = ThoughtNode(
                    thought=thought, value=value, depth=node.depth + 1,
                    parent=node, state=state,
                )
                node.children.append(child)
                self.nodes_explored += 1
                if value > self.best_score:
                    self.best_score = value
                    self.best_leaf = child
                if value >= self.value_threshold:
                    children.append(child)
            # Push best children first for DFS
            children.sort(key=lambda n: n.value, reverse=True)
            stack.extend(children)
        return self.best_leaf


def run_tot(
    *,
    task: str,
    client: Any,
    model: str,
    strategy: str = "bfs",
    max_depth: int = 4,
    branch_factor: int = 3,
    value_threshold: float = 0.3,
    initial_state: str = "",
) -> TreeOfThought:
    """Run a Tree-of-Thought search.

    Args:
        task: The reasoning task.
        client: Ollama client.
        model: Model name for evaluation and generation.
        strategy: "bfs" or "dfs".
        max_depth: Maximum tree depth.
        branch_factor: Number of candidate thoughts per node.
        value_threshold: Minimum score to keep a thought.
        initial_state: Starting reasoning state.

    Returns:
        The TreeOfThought with best_leaf set to the highest-scoring path.
    """
    strat = SearchStrategy(strategy.lower())
    tot = TreeOfThought(
        strategy=strat,
        max_depth=max_depth,
        branch_factor=branch_factor,
        value_threshold=value_threshold,
    )
    tot.root = ThoughtNode(thought="(root)", value=1.0, depth=0, state=initial_state or task)
    if strat == SearchStrategy.BFS:
        tot._bfs(task, client, model)
    else:
        tot._dfs(task, client, model)
    return tot
