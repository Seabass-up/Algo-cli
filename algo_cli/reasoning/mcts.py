"""Monte Carlo Tree Search (MCTS) for Reasoning.

Implements UCT (Upper Confidence bounds applied to Trees) for reasoning:
- Selection: Pick the most promising node using UCB1
- Expansion: Generate child thoughts from the selected node
- Simulation: Roll out to estimate value (lightweight or via LLM)
- Backpropagation: Update statistics up the tree

Effective for deep reasoning tasks where exploration-exploitation
tradeoffs matter. Integrates with ToT for structured tree search.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from ..chat_protocol import get_attr


@dataclass
class MCTSNode:
    """A node in the MCTS search tree."""
    thought: str
    state: str  # Accumulated reasoning state
    parent: "MCTSNode | None" = field(default=None, repr=False)
    children: list["MCTSNode"] = field(default_factory=list)
    visits: int = 0
    total_value: float = 0.0
    depth: int = 0
    fully_expanded: bool = False
    terminal: bool = False

    @property
    def avg_value(self) -> float:
        return self.total_value / max(1, self.visits)

    @property
    def ucb1(self) -> float:
        """UCT score with exploration constant sqrt(2)."""
        if self.visits == 0:
            return float("inf")
        exploitation = self.avg_value
        exploration = math.sqrt(2.0 * math.log(max(1, self.parent.visits if self.parent else 1)) / self.visits)
        return exploitation + exploration

    def best_child(self) -> "MCTSNode | None":
        """Select the child with the highest UCB1 score."""
        if not self.children:
            return None
        return max(self.children, key=lambda c: c.ucb1)

    def best_leaf(self) -> "MCTSNode":
        """Find the most-visited leaf (most robust conclusion)."""
        if not self.children:
            return self
        return max(self.children, key=lambda c: c.visits).best_leaf()

    def reasoning_chain(self) -> str:
        """Human-readable chain from root to this node."""
        chain = []
        node: MCTSNode | None = self
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()
        return "\n".join(
            f"Step {i+1} (visits={n.visits}, val={n.avg_value:.2f}): {n.thought}"
            for i, n in enumerate(chain)
        )


@dataclass
class MCTSReasoner:
    """MCTS-based reasoning engine."""
    max_iterations: int = 50
    max_depth: int = 6
    branch_factor: int = 3
    rollout_depth: int = 2
    exploration_constant: float = math.sqrt(2.0)

    root: MCTSNode | None = None
    nodes_created: int = 0

    def _select(self) -> MCTSNode:
        """Selection: traverse tree following UCB1 to find a node to expand."""
        node = self.root
        if node is None:
            raise RuntimeError("MCTS root must be initialized before selection")
        while node and node.children and not node.terminal:
            if not node.fully_expanded:
                return node
            selected = node.best_child()
            if selected is None:
                return node
            node = selected
        return node

    def _expand(self, node: MCTSNode, client: Any, model: str) -> MCTSNode | None:
        """Expansion: generate a new child thought from the selected node."""
        prompt = (
            f"Current reasoning state:\n{node.state[:1500]}\n\n"
            f"Generate one next reasoning step (different from existing children if any).\n"
            f"Existing steps: {[c.thought[:80] for c in node.children]}\n"
            f"Respond with just the reasoning step and updated state as JSON:\n"
            f'{{"thought": "...", "state": "..."}}'
        )
        try:
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a reasoning step generator."},
                    {"role": "user", "content": prompt},
                ],
                stream=False,
                format="json",
            )
            text = get_attr(get_attr(response, "message", {}), "content", "")
            data = json.loads(text)
            thought = str(data.get("thought", ""))
            state = str(data.get("state", node.state))
        except (json.JSONDecodeError, ValueError, TypeError):
            thought = f"(expansion at depth {node.depth + 1})"
            state = node.state

        child = MCTSNode(
            thought=thought, state=state, parent=node,
            depth=node.depth + 1,
        )
        node.children.append(child)
        self.nodes_created += 1

        if len(node.children) >= self.branch_factor:
            node.fully_expanded = True

        if node.depth + 1 >= self.max_depth:
            child.terminal = True

        return child

    def _simulate(self, node: MCTSNode, client: Any, model: str) -> float:
        """Simulation: quick rollout to estimate the node's value."""
        prompt = (
            f"Given this reasoning chain, rate the quality of the conclusion (0.0-1.0):\n"
            f"{node.state[:1000]}\n\nRespond with just a number."
        )
        try:
            response = client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a reasoning quality evaluator."},
                    {"role": "user", "content": prompt},
                ],
                stream=False,
            )
            text = get_attr(get_attr(response, "message", {}), "content", "").strip()
            import re
            m = re.search(r"([0-9]*\.?[0-9]+)", text)
            if m:
                return max(0.0, min(1.0, float(m.group(1))))
        except Exception:
            pass
        return 0.5

    def _backpropagate(self, node: MCTSNode, value: float) -> None:
        """Backpropagation: update statistics from leaf to root."""
        current: MCTSNode | None = node
        while current is not None:
            current.visits += 1
            current.total_value += value
            current = current.parent


def run_mcts(
    *,
    task: str,
    client: Any,
    model: str,
    max_iterations: int = 50,
    max_depth: int = 6,
    branch_factor: int = 3,
) -> MCTSReasoner:
    """Run MCTS reasoning on a task.

    Args:
        task: The reasoning task.
        client: Ollama client.
        model: Model name for evaluation and generation.
        max_iterations: Total MCTS iterations (select + expand + simulate + backprop).
        max_depth: Maximum tree depth.
        branch_factor: Max children per node.

    Returns:
        MCTSReasoner with root tree and best_leaf set.
    """
    reasoner = MCTSReasoner(
        max_iterations=max_iterations,
        max_depth=max_depth,
        branch_factor=branch_factor,
    )
    reasoner.root = MCTSNode(thought="(root)", state=task, visits=1)

    for _ in range(max_iterations):
        # 1. Select
        node = reasoner._select()
        if node is None or node.terminal:
            break

        # 2. Expand
        child = reasoner._expand(node, client, model)
        if child is None:
            break

        # 3. Simulate
        value = reasoner._simulate(child, client, model)

        # 4. Backpropagate
        reasoner._backpropagate(child, value)

    return reasoner
