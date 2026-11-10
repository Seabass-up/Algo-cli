"""B81. Ralph Loop: Continuous Test-Fix Cycles.

Run tests, fix failures, repeat until all pass (max N iterations).
Source: CCASP pattern.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable


class LoopStatus(Enum):
    PASSING = auto()
    FAILED = auto()
    TIMEOUT = auto()
    MAX_ITERATIONS = auto()


@dataclass
class TestRun:
    iteration: int
    passed: int = 0
    failed: int = 0
    errors: int = 0
    duration_s: float = 0.0
    failures: list[str] = field(default_factory=list)
    output: str = ""
    exit_code: int | None = None


@dataclass
class RalphResult:
    status: LoopStatus
    iterations: int
    runs: list[TestRun] = field(default_factory=list)
    total_duration_s: float = 0.0
    final_passed: int = 0
    final_failed: int = 0


class RalphLoop:
    """Run tests, fix failures, repeat until all pass."""

    def __init__(self, max_iterations: int = 5, timeout_s: float = 120.0) -> None:
        self._max_iterations = max_iterations
        self._timeout_s = timeout_s

    def run(self, test_fn: Callable[[], TestRun],
            fix_fn: Callable[[TestRun], bool] | None = None) -> RalphResult:
        """Execute the Ralph Loop.

        Args:
            test_fn: Run tests and return results
            fix_fn: Attempt to fix failures. Return True if fixes were applied.
        """
        result = RalphResult(status=LoopStatus.FAILED, iterations=0)
        start = time.time()

        for i in range(self._max_iterations):
            if time.time() - start > self._timeout_s:
                result.status = LoopStatus.TIMEOUT
                break

            run = test_fn()
            run.iteration = i + 1
            result.runs.append(run)
            result.iterations = i + 1

            if run.failed == 0 and run.errors == 0 and run.exit_code in (None, 0):
                result.status = LoopStatus.PASSING
                result.final_passed = run.passed
                result.final_failed = 0
                break

            result.final_passed = run.passed
            result.final_failed = run.failed

            if fix_fn:
                fixed = fix_fn(run)
                if not fixed:
                    result.status = LoopStatus.FAILED
                    break
            else:
                result.status = LoopStatus.FAILED
                break

        else:
            result.status = LoopStatus.MAX_ITERATIONS

        result.total_duration_s = time.time() - start
        return result

    @staticmethod
    def parse_pytest_output(output: str, exit_code: int | None = None) -> TestRun:
        """Parse pytest output to extract pass/fail/error counts from the summary line."""
        run = TestRun(iteration=0)
        run.output = output
        run.exit_code = exit_code

        for line in output.splitlines():
            line = line.strip()
            match = _PYTEST_SUMMARY_RE.match(line)
            if match:
                # "=== 5 passed, 2 failed, 1 xfailed in 0.3s ===" — xfailed/xpassed/warnings are not failures
                for part in match.group("body").split(","):
                    count_text, _, word = part.strip().partition(" ")
                    count = int(count_text)
                    if word == "passed":
                        run.passed = count
                    elif word == "failed":
                        run.failed = count
                    elif word in ("error", "errors"):
                        run.errors = count
            elif "error" in line.lower():
                run.failures.append(line)

        return run


_PYTEST_SUMMARY_RE = re.compile(r"^=*\s*(?P<body>\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d+(?:\.\d+)?s\b")