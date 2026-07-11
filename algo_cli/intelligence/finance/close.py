"""B88/B97/B98. Close DAG, variance miner, accrual/reversal engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .common import FinanceException, ReviewGate, Severity, SourceRef, decimalize, stable_exception_id
from .exceptions import ControllerExceptionQueue


@dataclass(frozen=True)
class CloseTask:
    id: str
    name: str
    duration_hours: Decimal | int | str = Decimal("1")
    dependencies: set[str] = field(default_factory=set)
    owner: str | None = None
    due_day: int | None = None
    completed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "duration_hours", decimalize(self.duration_hours))
        object.__setattr__(self, "dependencies", set(self.dependencies))


@dataclass(frozen=True)
class BlockedTask:
    task: CloseTask
    missing_dependencies: set[str]


class CloseDAG:
    """Month-end close task DAG with ready/blocked/critical-path analysis."""

    def __init__(self) -> None:
        self.tasks: dict[str, CloseTask] = {}

    def add_task(self, task: CloseTask) -> None:
        self.tasks[task.id] = task

    def ready_tasks(self, completed: set[str] | None = None) -> list[CloseTask]:
        completed = set(completed or ()) | {task.id for task in self.tasks.values() if task.completed}
        return sorted(
            [
                task for task in self.tasks.values()
                if task.id not in completed and task.dependencies <= completed
            ],
            key=lambda task: (task.due_day if task.due_day is not None else 99, task.id),
        )

    def blocked_tasks(self, completed: set[str] | None = None) -> list[BlockedTask]:
        completed = set(completed or ()) | {task.id for task in self.tasks.values() if task.completed}
        blocked: list[BlockedTask] = []
        for task in self.tasks.values():
            missing = task.dependencies - completed
            if task.id not in completed and missing:
                blocked.append(BlockedTask(task=task, missing_dependencies=missing))
        return sorted(blocked, key=lambda row: row.task.id)

    def topological_order(self) -> list[str]:
        indegree = {task_id: 0 for task_id in self.tasks}
        children: dict[str, list[str]] = {task_id: [] for task_id in self.tasks}
        for task in self.tasks.values():
            for dep in task.dependencies:
                if dep not in self.tasks:
                    raise ValueError(f"Unknown dependency {dep!r} for close task {task.id!r}")
                indegree[task.id] += 1
                children[dep].append(task.id)
        ready = sorted([task_id for task_id, degree in indegree.items() if degree == 0])
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for child in sorted(children[current]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort()
        if len(order) != len(self.tasks):
            raise ValueError("Close DAG contains a cycle")
        return order

    def critical_path(self) -> list[CloseTask]:
        order = self.topological_order()
        distance: dict[str, Decimal] = {}
        predecessor: dict[str, str | None] = {}
        for task_id in order:
            task = self.tasks[task_id]
            best_dep = None
            best_distance = Decimal("0")
            for dep in task.dependencies:
                if distance[dep] > best_distance:
                    best_dep = dep
                    best_distance = distance[dep]
            distance[task_id] = best_distance + task.duration_hours
            predecessor[task_id] = best_dep
        if not distance:
            return []
        end = max(distance, key=lambda task_id: (distance[task_id], task_id))
        path_ids: list[str] = []
        while end is not None:
            path_ids.append(end)
            end = predecessor[end]
        path_ids.reverse()
        return [self.tasks[task_id] for task_id in path_ids]


@dataclass(frozen=True)
class AccrualPolicy:
    id: str = "default-accrual"
    minimum_amount: Decimal = Decimal("100")
    require_reversal_date: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "minimum_amount", decimalize(self.minimum_amount))


@dataclass(frozen=True)
class AccrualProposal:
    id: str
    vendor: str
    amount: Decimal
    basis: str
    reversal_date: date | None
    source_refs: list[SourceRef]
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


@dataclass(frozen=True)
class ReversalProposal:
    id: str
    accrual_id: str
    amount: Decimal
    reversal_date: date
    source_refs: list[SourceRef]


class AccrualEngine:
    """Propose accruals and reversals; never posts journal entries."""

    def propose_accruals(
        self,
        expenses: Iterable[dict[str, Any]],
        receipts: Iterable[dict[str, Any]],
        invoices: Iterable[dict[str, Any]],
        policy: AccrualPolicy | None = None,
    ) -> list[AccrualProposal]:
        policy = policy or AccrualPolicy()
        invoiced_keys = {_accrual_key(row) for row in invoices}
        proposals: list[AccrualProposal] = []
        for row in list(expenses) + list(receipts):
            amount = abs(decimalize(row.get("amount", 0)))
            if amount < policy.minimum_amount:
                continue
            key = _accrual_key(row)
            if key in invoiced_keys:
                continue
            reversal_date = row.get("reversal_date")
            if isinstance(reversal_date, str):
                reversal_date = date.fromisoformat(reversal_date[:10])
            source_refs = list(row.get("source_refs", []))
            if not row.get("basis") or (policy.require_reversal_date and reversal_date is None) or not source_refs:
                continue
            proposal_id = stable_exception_id("B98", [key, amount, reversal_date])
            proposals.append(AccrualProposal(
                id=proposal_id,
                vendor=str(row.get("vendor", "")),
                amount=amount,
                basis=str(row.get("basis")),
                reversal_date=reversal_date,
                source_refs=source_refs,
            ))
        return proposals

    def propose_reversals(self, prior_accruals: Iterable[AccrualProposal], current_period: str) -> list[ReversalProposal]:
        reversals: list[ReversalProposal] = []
        for accrual in prior_accruals:
            if not accrual.reversal_date:
                continue
            if f"{accrual.reversal_date.year:04d}-{accrual.reversal_date.month:02d}" == current_period:
                reversals.append(ReversalProposal(
                    id=stable_exception_id("B98R", [accrual.id, current_period]),
                    accrual_id=accrual.id,
                    amount=-accrual.amount,
                    reversal_date=accrual.reversal_date,
                    source_refs=list(accrual.source_refs),
                ))
        return reversals


@dataclass(frozen=True)
class Variance:
    account: str
    current: Decimal
    comparison: Decimal
    difference: Decimal
    percent_change: Decimal | None
    source_refs: list[SourceRef] = field(default_factory=list)


@dataclass(frozen=True)
class VarianceExplanation:
    variance: Variance
    driver: str | None
    explanation: str
    supported: bool
    source_refs: list[SourceRef]
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


class VarianceExplanationMiner:
    """Detect and explain material flux without accepting tautologies."""

    def detect_variances(
        self,
        current: dict[str, Any],
        prior: dict[str, Any],
        budget: dict[str, Any] | None = None,
        materiality: Any = Decimal("1000"),
    ) -> list[Variance]:
        baseline = budget or prior
        threshold = abs(decimalize(materiality))
        variances: list[Variance] = []
        for account, current_value in current.items():
            current_amount = decimalize(current_value)
            comparison = decimalize(baseline.get(account, 0))
            difference = current_amount - comparison
            if abs(difference) < threshold:
                continue
            percent = None if comparison == 0 else difference / abs(comparison)
            variances.append(Variance(
                account=account,
                current=current_amount,
                comparison=comparison,
                difference=difference,
                percent_change=percent,
            ))
        return sorted(variances, key=lambda row: (abs(row.difference), row.account), reverse=True)

    def explain(self, variance: Variance, activity_sources: Iterable[dict[str, Any]]) -> VarianceExplanation:
        allowed = {
            "volume",
            "price",
            "rate",
            "mix",
            "timing",
            "correction",
            "new-vendor",
            "new-customer",
            "job-status",
            "headcount",
            "tax-rate",
            "accrual",
            "reversal",
            "reclass",
        }
        for source in activity_sources:
            if str(source.get("account", "")).lower() != variance.account.lower():
                continue
            driver = str(source.get("driver", "")).lower()
            explanation = str(source.get("explanation", "")).strip()
            source_refs = list(source.get("source_refs", []))
            if driver in allowed and source_refs and not _is_tautology(variance.account, explanation):
                return VarianceExplanation(
                    variance=variance,
                    driver=driver,
                    explanation=explanation,
                    supported=True,
                    source_refs=source_refs,
                    review_gate=ReviewGate.DRAFT,
                )
        return VarianceExplanation(
            variance=variance,
            driver=None,
            explanation="driver unclear — flag for controller",
            supported=False,
            source_refs=[],
        )


@dataclass
class ClosePackageInputs:
    current_balances: dict[str, Any] = field(default_factory=dict)
    prior_balances: dict[str, Any] = field(default_factory=dict)
    activity_sources: list[dict[str, Any]] = field(default_factory=list)
    materiality: Decimal = Decimal("1000")


@dataclass
class ClosePackageResult:
    variances: list[Variance]
    explanations: list[VarianceExplanation]
    exception_queue: ControllerExceptionQueue
    review_gate: ReviewGate = ReviewGate.CONTROLLER_REVIEW


class ClosePackageAnalyzer:
    """Compose close analysis and route issues to a controller queue."""

    def run(self, inputs: ClosePackageInputs) -> ClosePackageResult:
        miner = VarianceExplanationMiner()
        variances = miner.detect_variances(inputs.current_balances, inputs.prior_balances, materiality=inputs.materiality)
        explanations = [miner.explain(variance, inputs.activity_sources) for variance in variances]
        queue = ControllerExceptionQueue()
        for explanation in explanations:
            if not explanation.supported:
                queue.add(FinanceException(
                    id=stable_exception_id("B97", [explanation.variance.account, explanation.variance.difference]),
                    pattern_id="B97",
                    severity=Severity.MEDIUM,
                    amount=abs(explanation.variance.difference),
                    message=f"Unsupported variance explanation for {explanation.variance.account}",
                    source_refs=list(explanation.variance.source_refs),
                    tags={"variance", f"account:{explanation.variance.account.lower()}"},
                ))
        return ClosePackageResult(variances=variances, explanations=explanations, exception_queue=queue)


def _accrual_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("vendor", "")).lower(), str(row.get("description", "")).lower(), str(row.get("period", "")))


def _is_tautology(account: str, explanation: str) -> bool:
    low = explanation.lower()
    if not low:
        return True
    tokens = [token for token in account.lower().replace("-", " ").split() if len(token) > 3]
    tautology_phrases = ("increased due to higher", "decreased due to lower", "changed due to change")
    return any(phrase in low for phrase in tautology_phrases) or (tokens and all(token in low for token in tokens) and len(low.split()) < 8)
