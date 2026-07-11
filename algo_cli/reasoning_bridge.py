"""Bridge the reasoning/* algorithms into the everyday chat loop.

Historically the Tree/Graph/MCTS/Reflexion/QCR algorithms were only reachable
from the agent *pipeline*; the chat loop the user lives in was plain ReAct, so
``/reason tot`` (etc.) changed a flag nothing in chat read. This module runs a
"reasoning preflight": when reasoning chat mode is enabled and the active
``reasoning_mode`` is not plain react, it runs the selected algorithm to produce
a short strategy artifact and returns it as a guidance block. The chat loop
injects that block into the turn (same pattern as RAG/intuition/ICL injection)
so the model executes against an algorithm-derived plan.

Design constraints:
- Best-effort: any failure returns "" and the turn proceeds as plain ReAct.
- These algorithms make several model calls, so the preflight is gated on the
  user explicitly opting in (reasoning_chat_enabled) AND choosing a non-react
  mode. It produces a *plan*, not tool execution — the main loop still owns
  tool calls, approvals, and safety.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config

logger = logging.getLogger(__name__)

_PLAN_HEADER = "## Reasoning Plan ({mode})"
_PLAN_FOOTER = (
    "\nUse the plan above as strategy guidance only. Verify with tools before "
    "acting; it is not proof of any fact."
)
_MAX_PLAN_CHARS = 2000


def _safe_chain(obj: Any, *attrs: str) -> str:
    """Pull the first available human-readable chain/text off a result object."""
    for attr in attrs:
        value = getattr(obj, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        # node-like with reasoning_chain()
        chain = getattr(value, "reasoning_chain", None)
        if callable(chain):
            try:
                text = chain()
                if isinstance(text, str) and text.strip():
                    return text.strip()
            except Exception:
                continue
    return ""


def _plan_for_mode(mode: str, task: str, client: Any, model: str, cfg: Config) -> str:
    from . import reasoning

    depth = max(1, int(getattr(cfg, "reasoning_depth", 4)))
    branches = max(1, int(getattr(cfg, "reasoning_branches", 3)))

    if mode == "reflexion":
        episodes = reasoning.run_reflexion_loop(
            task=task, client=client, model=model,
            max_attempts=max(1, int(getattr(cfg, "reasoning_reflexion_attempts", 3))),
        )
        if episodes:
            best_episode = max(episodes, key=lambda episode: getattr(episode, "score", 0.0))
            return (
                f"Best attempt (score {getattr(best_episode, 'score', 0.0):.2f}):\n"
                f"{getattr(best_episode, 'output', '')}"
            )
        return ""

    if mode == "tot":
        tot = reasoning.run_tot(
            task=task, client=client, model=model,
            max_depth=depth, branch_factor=branches,
        )
        return _safe_chain(tot, "best_leaf")

    if mode == "got":
        got = reasoning.run_got(
            task=task, client=client, model=model,
            max_rounds=depth, branch_factor=branches,
        )
        return _safe_chain(got, "best_vertex", "best_leaf")

    if mode == "mcts":
        reasoner = reasoning.run_mcts(
            task=task, client=client, model=model,
            max_depth=depth, branch_factor=branches,
        )
        # MCTSReasoner exposes root; best leaf chain is the robust conclusion.
        root = getattr(reasoner, "root", None)
        if root is not None and hasattr(root, "best_leaf"):
            try:
                return root.best_leaf().reasoning_chain()
            except Exception:
                pass
        return _safe_chain(reasoner, "best_leaf")

    if mode == "qcr":
        result = reasoning.run_qcr_aggregation(
            task=task, client=client, model=model,
            n_samples=max(1, int(getattr(cfg, "reasoning_qcr_samples", 5))),
        )
        # returns (best, candidates, meta)
        if isinstance(result, tuple) and result:
            best = result[0]
            return str(best) if best else ""
        return ""

    # neuro_symbolic / hybrid: no generic symbolic verifier to supply here, so
    # we skip rather than fabricate one. The pipeline path still supports them.
    return ""


def maybe_reasoning_plan(cfg: Config, client: Any, user_message: str) -> str:
    """Return a guidance block for the active reasoning mode, or "" to skip.

    Skips when: chat reasoning is disabled, mode is react/neuro_symbolic/hybrid,
    the message is trivial, or the algorithm errors/returns nothing.
    """
    if not getattr(cfg, "reasoning_chat_enabled", False):
        return ""
    mode = str(getattr(cfg, "reasoning_mode", "react") or "react").lower()
    if mode in {"react", "neuro_symbolic", "hybrid"}:
        return ""
    task = (user_message or "").strip()
    if len(task) < 12:  # trivial prompt; not worth a multi-call preflight
        return ""
    model = str(getattr(cfg, "model", "") or "")
    if not model:
        return ""
    try:
        plan = _plan_for_mode(mode, task, client, model, cfg)
    except Exception as exc:
        logger.debug("Reasoning preflight (%s) failed: %s", mode, exc)
        return ""
    plan = (plan or "").strip()
    if not plan:
        return ""
    if len(plan) > _MAX_PLAN_CHARS:
        plan = plan[:_MAX_PLAN_CHARS].rstrip() + "…"
    return f"{_PLAN_HEADER.format(mode=mode)}\n{plan}{_PLAN_FOOTER}"
