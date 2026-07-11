"""B65. Multi-Source Parallel Fan-Out.

N questions × M sources = NM parallel tasks.
Source: deep-research-agent pattern.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class FanOutTask:
    question: str
    source: str  # "web", "github", "arxiv", "local"
    query: str
    priority: int = 5


@dataclass
class FanOutResult:
    question: str
    source: str
    query: str
    results: list[tuple[str, str]] = field(default_factory=list)  # (title, content)
    duration_s: float = 0.0
    error: str | None = None
    success: bool = True


class ParallelFanOut:
    """Execute N questions × M sources in parallel."""

    def __init__(self, max_workers: int = 4, timeout: float = 30.0) -> None:
        self._max_workers = max_workers
        self._timeout = timeout
        self._source_handlers: dict[str, Callable[[str], list[tuple[str, str]]]] = {}

    def register_source(self, name: str,
                        handler: Callable[[str], list[tuple[str, str]]]) -> None:
        self._source_handlers[name] = handler

    def fan_out(self, questions: list[str], sources: list[str]) -> list[FanOutResult]:
        """Run all question×source combinations in parallel."""
        tasks: list[FanOutTask] = []
        for q in questions:
            for src in sources:
                tasks.append(FanOutTask(question=q, source=src, query=q))

        return self._execute(tasks)

    def _execute(self, tasks: list[FanOutTask]) -> list[FanOutResult]:
        results: list[FanOutResult] = []

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_task = {
                pool.submit(self._run_task, task): task for task in tasks
            }
            for future in as_completed(future_to_task, timeout=self._timeout):
                task = future_to_task[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    results.append(FanOutResult(
                        question=task.question, source=task.source,
                        query=task.query, error=str(e), success=False,
                    ))

        return results

    def _run_task(self, task: FanOutTask) -> FanOutResult:
        handler = self._source_handlers.get(task.source)
        if not handler:
            return FanOutResult(
                question=task.question, source=task.source,
                query=task.query, error=f"No handler for source '{task.source}'",
                success=False,
            )
        start = time.time()
        try:
            results = handler(task.query)
            return FanOutResult(
                question=task.question, source=task.source,
                query=task.query, results=results,
                duration_s=time.time() - start,
            )
        except Exception as e:
            return FanOutResult(
                question=task.question, source=task.source,
                query=task.query, error=str(e), success=False,
                duration_s=time.time() - start,
            )

    def merge_results(self, results: list[FanOutResult]) -> dict[str, list[tuple[str, str, str]]]:
        """Merge results by question: {question: [(source, title, content), ...]}"""
        merged: dict[str, list[tuple[str, str, str]]] = {}
        for r in results:
            if not r.success:
                continue
            for title, content in r.results:
                merged.setdefault(r.question, []).append((r.source, title, content))
        return merged