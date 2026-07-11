"""B70. Parallel Delegation + Subject Hygiene.

Launch 2-4 subagents simultaneously, each with a clear traceable subject.
Source: awesome-agentic-patterns.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable


@dataclass
class DelegatedTask:
    subject: str  # clear, traceable identifier
    description: str
    agent_id: str = ""
    priority: int = 5


@dataclass
class DelegationResult:
    subject: str
    output: str = ""
    success: bool = True
    error: str | None = None
    duration_s: float = 0.0


class ParallelDelegator:
    """Delegate tasks to multiple agents in parallel with subject hygiene."""

    def __init__(self, max_workers: int = 4) -> None:
        self._max_workers = max_workers

    def delegate_all(
        self,
        tasks: list[DelegatedTask],
        run_fn: Callable[[DelegatedTask], str],
    ) -> list[DelegationResult]:
        """Run all tasks in parallel, wait for all to complete."""
        results: list[DelegationResult] = []

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_task = {pool.submit(self._run, task, run_fn): task for task in tasks}
            for future in as_completed(future_to_task):
                results.append(future.result())

        # Sort by original task order
        subject_order = {t.subject: i for i, t in enumerate(tasks)}
        results.sort(key=lambda r: subject_order.get(r.subject, 999))
        return results

    def delegate_race(
        self,
        tasks: list[DelegatedTask],
        run_fn: Callable[[DelegatedTask], str],
    ) -> DelegationResult | None:
        """Run tasks in parallel, return first successful result."""
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(self._run, task, run_fn): task for task in tasks}
            for future in as_completed(futures):
                result = future.result()
                if result.success:
                    # Cancel remaining
                    for f in futures:
                        f.cancel()
                    return result
        return None

    def _run(self, task: DelegatedTask, run_fn: Callable[[DelegatedTask], str]) -> DelegationResult:
        start = time.time()
        try:
            output = run_fn(task)
            return DelegationResult(
                subject=task.subject, output=output,
                duration_s=time.time() - start,
            )
        except Exception as e:
            return DelegationResult(
                subject=task.subject, success=False, error=str(e),
                duration_s=time.time() - start,
            )

    @staticmethod
    def validate_subjects(tasks: list[DelegatedTask]) -> list[str]:
        """Ensure subjects are unique and descriptive. Return issues."""
        issues: list[str] = []
        subjects = [t.subject for t in tasks]
        if len(set(subjects)) != len(subjects):
            issues.append("Duplicate subjects found")
        for t in tasks:
            if len(t.subject) < 3:
                issues.append(f"Subject too short: '{t.subject}'")
            if len(t.description) < 10:
                issues.append(f"Description too short for '{t.subject}'")
        return issues