"""Regression tests for reasoning bridge, search and critique defects (bughunt 2026-11-09)."""

from __future__ import annotations

import math


from algo_cli import reasoning_bridge
from algo_cli.config import Config
from algo_cli.reasoning import GraphOfThought, MCTSReasoner, TreeOfThought, run_mcts, run_reflexion_loop
from algo_cli.reasoning.reflexion import _parse_critique
from algo_cli.reasoning.tree_of_thought import SearchStrategy, ThoughtNode


def _reasoning_cfg(mode: str) -> Config:
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.reasoning_mode = mode
    cfg.model = "test-model"
    return cfg


def _message(content: str) -> dict:
    return {"message": {"content": content}}


# D1: mode "got" must return a plan built from the best vertex.
def test_got_mode_returns_plan_from_best_vertex(monkeypatch):
    def fake_run_got(**_kwargs):
        got = GraphOfThought()
        got.add_vertex(thought="Split the loop into planner and executor", value=0.9)
        return got

    monkeypatch.setattr("algo_cli.reasoning.run_got", fake_run_got)
    plan = reasoning_bridge.maybe_reasoning_plan(_reasoning_cfg("got"), object(), "design a caching layer")
    assert "## Reasoning Plan (got)" in plan
    assert "Split the loop into planner and executor" in plan


def test_got_mode_without_best_vertex_returns_empty(monkeypatch):
    monkeypatch.setattr("algo_cli.reasoning.run_got", lambda **_kwargs: GraphOfThought())
    assert reasoning_bridge.maybe_reasoning_plan(_reasoning_cfg("got"), object(), "design a caching layer") == ""


# D2: a failed QCR generation must not be presented as a plan.
def test_qcr_generation_failure_returns_empty(monkeypatch):
    def failed(**_kwargs):
        return ("(no fragments generated)", [], {"error": "generation_failed"})

    monkeypatch.setattr("algo_cli.reasoning.run_qcr_aggregation", failed)
    assert reasoning_bridge.maybe_reasoning_plan(_reasoning_cfg("qcr"), object(), "design a caching layer") == ""


def test_qcr_empty_fragment_list_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "algo_cli.reasoning.run_qcr_aggregation",
        lambda **_kwargs: ("some text", [], {}),
    )
    assert reasoning_bridge.maybe_reasoning_plan(_reasoning_cfg("qcr"), object(), "design a caching layer") == ""


def test_qcr_success_still_returns_plan(monkeypatch):
    monkeypatch.setattr(
        "algo_cli.reasoning.run_qcr_aggregation",
        lambda **_kwargs: ("Use a bounded LRU cache", ["fragment one"], {}),
    )
    plan = reasoning_bridge.maybe_reasoning_plan(_reasoning_cfg("qcr"), object(), "design a caching layer")
    assert "Use a bounded LRU cache" in plan


class _ScriptedTreeClient:
    """Generation calls carry format="json"; evaluation calls do not."""

    def __init__(self, thoughts_json: str, scores: list[str]) -> None:
        self._thoughts_json = thoughts_json
        self._scores = list(scores)

    def chat(self, **kwargs):
        if kwargs.get("format") == "json":
            return _message(self._thoughts_json)
        return _message(self._scores.pop(0) if self._scores else "0.1")


# D3: a pruned node must never become best_leaf (breadth-first).
def test_tot_bfs_pruned_candidates_yield_no_best_leaf():
    client = _ScriptedTreeClient('[{"thought": "weak idea", "state": "s1"}]', ["0.1"])
    tot = TreeOfThought(strategy=SearchStrategy.BFS, max_depth=1, branch_factor=1, value_threshold=0.3)
    tot.root = ThoughtNode(thought="(root)", value=1.0, depth=0, state="task")
    tot._bfs("task", client, "test-model")
    assert tot.best_leaf is None
    assert tot.root.children[0].pruned is True


# D3: a pruned node must never become best_leaf (depth-first).
def test_tot_dfs_pruned_candidates_yield_no_best_leaf():
    client = _ScriptedTreeClient('[{"thought": "weak idea", "state": "s1"}]', ["0.1"])
    tot = TreeOfThought(strategy=SearchStrategy.DFS, max_depth=1, branch_factor=1, value_threshold=0.3)
    tot.root = ThoughtNode(thought="(root)", value=1.0, depth=0, state="task")
    tot._dfs("task", client, "test-model")
    assert tot.best_leaf is None
    assert tot.root.children[0].pruned is True


# D4: a selected terminal node must not end the whole MCTS search.
class _CountingMCTSClient:
    def __init__(self) -> None:
        self.simulate_calls = 0

    def chat(self, **kwargs):
        if kwargs.get("format") == "json":
            return _message('{"thought": "next step", "state": "more state"}')
        self.simulate_calls += 1
        return _message("0.5")


def test_mcts_continues_after_selecting_terminal_node():
    client = _CountingMCTSClient()
    reasoner = run_mcts(task="plan a migration", client=client, model="test-model",
                        max_iterations=5, max_depth=2, branch_factor=1)
    assert isinstance(reasoner, MCTSReasoner)
    # Iterations 1 and 2 expand and simulate; iterations 3-5 select the terminal grandchild
    # and must still evaluate and backpropagate it.
    assert client.simulate_calls == 5
    assert reasoner.root.visits == 1 + 5


# D5: a NaN critique score is treated like an unparseable score.
def test_critique_nan_score_uses_unparseable_fallback():
    score, _critique = _parse_critique('{"score": NaN, "critique": "looks fine"}')
    assert math.isfinite(score)
    assert score == 0.5


def test_critique_infinite_score_uses_unparseable_fallback():
    score, _critique = _parse_critique('{"score": Infinity, "critique": "x"}')
    assert score == 0.5


class _NaNCritiqueClient:
    def __init__(self) -> None:
        self._replies = iter([
            _message("first answer"),
            _message('{"score": NaN, "critique": "unclear"}'),
            _message("second answer"),
            _message('{"score": 0.95, "critique": "complete"}'),
        ])

    def chat(self, **_kwargs):
        return next(self._replies)


def test_reflexion_nan_critique_does_not_stop_loop_early():
    episodes = run_reflexion_loop(
        task="produce a detailed implementation plan",
        client=_NaNCritiqueClient(),
        model="test-model",
        max_attempts=2,
    )
    assert len(episodes) == 2
    assert episodes[0].score == 0.5
