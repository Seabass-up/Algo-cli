"""B58. Agent Arena: Multi-Model Head-to-Head.

Run the same task across multiple models and compare outputs.
Source: qwen-code pattern.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class ArenaOutcome(Enum):
    WINNER = auto()
    TIE = auto()
    ALL_FAILED = auto()


@dataclass
class ArenaEntry:
    model: str
    output: str = ""
    duration_s: float = 0.0
    token_count: int = 0
    error: str | None = None
    success: bool = True


@dataclass
class ArenaResult:
    task: str
    entries: list[ArenaEntry] = field(default_factory=list)
    winner: str | None = None
    outcome: ArenaOutcome = ArenaOutcome.TIE
    comparison: str = ""


class AgentArena:
    """Run the same task across multiple models and compare."""

    def __init__(self, judge_fn: Callable[[str, list[ArenaEntry]], str] | None = None) -> None:
        self._judge = judge_fn or self._default_judge

    @staticmethod
    def _default_judge(task: str, entries: list[ArenaEntry]) -> str:
        """Default judge: pick the longest successful output."""
        successful = [e for e in entries if e.success and e.output]
        if not successful:
            return ""
        return max(successful, key=lambda e: len(e.output)).model

    def run(
        self,
        task: str,
        models: dict[str, Callable[[str], str]],
        timeout: float = 30.0,
    ) -> ArenaResult:
        """Run task on all models, return comparison."""
        result = ArenaResult(task=task)
        for model_name, run_fn in models.items():
            entry = ArenaEntry(model=model_name)
            start = time.time()
            try:
                entry.output = run_fn(task)
                entry.token_count = len(entry.output) // 4
            except Exception as e:
                entry.error = str(e)
                entry.success = False
            entry.duration_s = time.time() - start
            result.entries.append(entry)

        # Judge
        winner = self._judge(task, result.entries)
        if winner:
            result.winner = winner
            result.outcome = ArenaOutcome.WINNER
        elif any(e.success for e in result.entries):
            result.outcome = ArenaOutcome.TIE
        else:
            result.outcome = ArenaOutcome.ALL_FAILED

        result.comparison = self._format_comparison(result)
        return result

    @staticmethod
    def _format_comparison(result: ArenaResult) -> str:
        lines = [f"Task: {result.task}", f"Winner: {result.winner or 'N/A'}", ""]
        for e in result.entries:
            status = "OK" if e.success else f"FAIL: {e.error}"
            lines.append(f"  {e.model}: {e.duration_s:.1f}s, {e.token_count}t — {status}")
        return "\n".join(lines)