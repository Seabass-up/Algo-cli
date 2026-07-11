"""B43. Auto-Wait Actionability Checks (Playwright Pattern).

Before performing an action (click, type, navigate), check that the target
is ready: exists, visible, stable, enabled, and receives input.  No blind
sleeps — poll with timeout until conditions are met or fail fast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


class CheckResult:
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class ActionabilityCheck:
    name: str
    check_fn: Callable[[], bool]
    timeout_ms: int = 5000
    poll_interval_ms: int = 100


@dataclass
class ActionabilityReport:
    checks: dict[str, str] = field(default_factory=dict)  # name -> result
    all_passed: bool = False
    duration_ms: float = 0.0
    failed_checks: list[str] = field(default_factory=list)


def auto_wait(checks: list[ActionabilityCheck]) -> ActionabilityReport:
    """Run all actionability checks with polling and timeout.

    Each check is polled at its interval until it passes or times out.
    All checks must pass for the report to be all_passed=True.
    """
    start = time.monotonic()
    results: dict[str, str] = {}
    failed: list[str] = []

    for check in checks:
        deadline = time.monotonic() + check.timeout_ms / 1000
        passed = False
        while time.monotonic() < deadline:
            try:
                if check.check_fn():
                    passed = True
                    break
            except Exception:
                pass
            time.sleep(check.poll_interval_ms / 1000)

        if passed:
            results[check.name] = CheckResult.PASSED
        elif time.monotonic() >= deadline:
            results[check.name] = CheckResult.TIMEOUT
            failed.append(check.name)
        else:
            results[check.name] = CheckResult.FAILED
            failed.append(check.name)

    duration = (time.monotonic() - start) * 1000
    return ActionabilityReport(
        checks=results,
        all_passed=len(failed) == 0,
        duration_ms=duration,
        failed_checks=failed,
    )


# ── standard checks ───────────────────────────────────────────────────


def check_exists(predicate: Callable[[], Any]) -> ActionabilityCheck:
    """Target exists (not None)."""
    return ActionabilityCheck(
        name="exists",
        check_fn=lambda: predicate() is not None,
    )


def check_visible(predicate: Callable[[], bool]) -> ActionabilityCheck:
    """Target is visible."""
    return ActionabilityCheck(
        name="visible",
        check_fn=predicate,
    )


def check_stable(predicate: Callable[[], bool], samples: int = 3, interval: float = 0.05) -> ActionabilityCheck:
    """Target has not changed over N consecutive samples."""
    last_values: list[Any] = []

    def is_stable() -> bool:
        val = predicate()
        last_values.append(val)
        if len(last_values) > samples:
            last_values.pop(0)
        if len(last_values) < samples:
            return False
        return all(v == last_values[0] for v in last_values)

    return ActionabilityCheck(name="stable", check_fn=is_stable)


def check_enabled(predicate: Callable[[], bool]) -> ActionabilityCheck:
    """Target is enabled (not disabled)."""
    return ActionabilityCheck(
        name="enabled",
        check_fn=predicate,
    )


def check_receives_input(predicate: Callable[[], bool]) -> ActionabilityCheck:
    """Target can receive input (focusable)."""
    return ActionabilityCheck(
        name="receives_input",
        check_fn=predicate,
    )


# ── trace recorder ────────────────────────────────────────────────────


@dataclass
class ActionTrace:
    action: str
    target: str
    report: ActionabilityReport
    timestamp: float = field(default_factory=time.time)
    screenshot_path: str = ""
    html_snapshot: str = ""
    console_logs: list[str] = field(default_factory=list)
    network_summary: dict[str, Any] = field(default_factory=dict)


class TraceRecorder:
    """Records action traces for debugging and replay."""

    def __init__(self) -> None:
        self.traces: list[ActionTrace] = []

    def record(self, action: str, target: str, report: ActionabilityReport, **kwargs: Any) -> None:
        self.traces.append(ActionTrace(
            action=action, target=target, report=report, **kwargs,
        ))

    def failed_actions(self) -> list[ActionTrace]:
        return [t for t in self.traces if not t.report.all_passed]

    def summary(self) -> dict[str, Any]:
        total = len(self.traces)
        passed = sum(1 for t in self.traces if t.report.all_passed)
        return {
            "total_actions": total,
            "passed": passed,
            "failed": total - passed,
            "avg_wait_ms": sum(t.report.duration_ms for t in self.traces) / max(total, 1),
        }