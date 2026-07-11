"""Tests for the chat-loop reasoning preflight bridge."""

from algo_cli import reasoning_bridge
from algo_cli.config import Config
from algo_cli.reasoning import ReflexionEpisode


def test_skips_when_chat_disabled():
    cfg = Config()
    cfg.reasoning_chat_enabled = False
    cfg.reasoning_mode = "tot"
    assert reasoning_bridge.maybe_reasoning_plan(cfg, object(), "plan a refactor of the loop") == ""


def test_skips_react_mode():
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.reasoning_mode = "react"
    assert reasoning_bridge.maybe_reasoning_plan(cfg, object(), "plan a refactor of the loop") == ""


def test_skips_trivial_message():
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.reasoning_mode = "tot"
    assert reasoning_bridge.maybe_reasoning_plan(cfg, object(), "hi") == ""


def test_algorithm_error_falls_back_to_empty(monkeypatch):
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.reasoning_mode = "tot"
    cfg.model = "qwen3"

    def boom(**kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("algo_cli.reasoning.run_tot", boom)
    assert reasoning_bridge.maybe_reasoning_plan(cfg, object(), "design a caching layer") == ""


def test_plan_block_formatted(monkeypatch):
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.reasoning_mode = "qcr"
    cfg.model = "qwen3"

    def fake_qcr(**kwargs):
        return ("Step 1: gather. Step 2: act.", ["a", "b"], {})

    monkeypatch.setattr("algo_cli.reasoning.run_qcr_aggregation", fake_qcr)
    block = reasoning_bridge.maybe_reasoning_plan(cfg, object(), "design a caching layer")
    assert "## Reasoning Plan (qcr)" in block
    assert "Step 1: gather" in block


def test_reflexion_reports_highest_scoring_attempt_not_last(monkeypatch):
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.reasoning_mode = "reflexion"
    cfg.model = "qwen3"

    episodes = [
        ReflexionEpisode(1, "task", "best answer", "good", 0.9, False),
        ReflexionEpisode(2, "task", "regressed answer", "worse", 0.4, False),
    ]
    monkeypatch.setattr("algo_cli.reasoning.run_reflexion_loop", lambda **_kwargs: episodes)

    block = reasoning_bridge.maybe_reasoning_plan(cfg, object(), "design a caching layer")

    assert "Best attempt (score 0.90)" in block
    assert "best answer" in block
    assert "regressed answer" not in block


def test_reflexion_bridge_runs_real_loop_without_falling_back_to_empty():
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.reasoning_mode = "reflexion"
    cfg.reasoning_reflexion_attempts = 1
    cfg.model = "qwen3"

    client = _OneAttemptReflexionClient()
    block = reasoning_bridge.maybe_reasoning_plan(
        cfg,
        client,
        "design a robust caching layer",
    )

    assert "## Reasoning Plan (reflexion)" in block
    assert "Best attempt (score 0.95)" in block
    assert "use a bounded LRU cache" in block


def test_neuro_symbolic_and_hybrid_skip():
    cfg = Config()
    cfg.reasoning_chat_enabled = True
    cfg.model = "qwen3"
    for mode in ("neuro_symbolic", "hybrid"):
        cfg.reasoning_mode = mode
        assert reasoning_bridge.maybe_reasoning_plan(cfg, object(), "verify this invariant carefully") == ""


class _OneAttemptReflexionClient:
    def __init__(self) -> None:
        self.responses = iter(
            (
                {"message": {"content": "use a bounded LRU cache"}},
                {"message": {"content": '{"score": 0.95, "critique": "complete"}'}},
            )
        )

    def chat(self, **_kwargs):
        return next(self.responses)
