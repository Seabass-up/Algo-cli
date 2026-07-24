"""Immutable execution contracts for bounded Algo Agent runs.

The contract is runtime-owned. Model output may inform routing, but it cannot
change approval mode, tool ceilings, mutation scope, resource budgets, or
required verifiers after compilation.

This module deliberately does not execute tools. It compiles and validates the
closed envelope that later runtime layers enforce and include in receipts.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from . import agent_blocks
from . import chatgpt_client
from . import git_evidence
from . import model_info
from . import samuel_policy
from . import spawn_budget
from . import task_router
from .config import Config


RUN_CONTRACT_SCHEMA_VERSION = 3
ContractMode = Literal["shadow", "enforced"]
ApprovalMode = Literal["interactive", "never", "auto"]
MutationScope = Literal["none", "workspace"]

_CONTRACT_MODES = frozenset({"shadow", "enforced"})
_APPROVAL_MODES = frozenset({"interactive", "never", "auto"})
_MUTATION_SCOPES = frozenset({"none", "workspace"})
_HEX_DIGEST_LENGTH = 64
_MAX_BLOCKS = 32
_MAX_ITERATIONS_PER_BLOCK = agent_blocks.MAX_BLOCK_ITERATIONS
_MAX_TOOL_CALLS = 2_048
_MAX_PARALLELISM = 4
_MAX_WALL_TIME_SECONDS = 7_200.0
_MAX_TOKEN_BUDGET = 100_000_000


class RunContractError(ValueError):
    """Raised when a runtime-owned run contract cannot be compiled safely."""


class RunContractViolation(RuntimeError):
    """Raised when live execution attempts to exceed an enforced contract."""


def _clean_text(value: Any, *, field: str, limit: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise RunContractError(f"{field} must be text")
    text = value.strip()
    if not allow_empty and not text:
        raise RunContractError(f"{field} must not be empty")
    if len(text) > limit:
        raise RunContractError(f"{field} exceeds {limit} characters")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise RunContractError(f"{field} contains control characters")
    return text


def _clean_digest(value: Any, *, field: str, allow_empty: bool = True) -> str:
    text = _clean_text(value, field=field, limit=_HEX_DIGEST_LENGTH, allow_empty=allow_empty)
    if text and (len(text) != _HEX_DIGEST_LENGTH or any(character not in "0123456789abcdef" for character in text)):
        raise RunContractError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _clean_git_head(value: Any) -> str:
    text = _clean_text(value, field="initial_head", limit=64, allow_empty=True)
    if text and (
        len(text) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise RunContractError("initial_head must be a lowercase Git object ID")
    return text


def _clean_unique_names(values: Sequence[str], *, field: str, limit: int = 256) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RunContractError(f"{field} must be a sequence")
    cleaned = tuple(
        sorted(
            {
                _clean_text(value, field=field, limit=limit)
                for value in values
            }
        )
    )
    return cleaned


def _positive_int(value: Any, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise RunContractError(f"{field} must be an integer from 1 to {maximum}")
    return value


def _nonnegative_int(value: Any, *, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RunContractError(f"{field} must be an integer from 0 to {maximum}")
    return value


@dataclass(frozen=True)
class RunBudget:
    """Hard resource envelope for one Agent pipeline."""

    max_blocks: int
    max_iterations_per_block: int
    max_tool_calls: int
    max_parallelism: int
    max_wall_time_seconds: float
    max_prompt_tokens_per_round: int
    max_total_tokens: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_blocks",
            _positive_int(self.max_blocks, field="max_blocks", maximum=_MAX_BLOCKS),
        )
        object.__setattr__(
            self,
            "max_iterations_per_block",
            _positive_int(
                self.max_iterations_per_block,
                field="max_iterations_per_block",
                maximum=_MAX_ITERATIONS_PER_BLOCK,
            ),
        )
        object.__setattr__(
            self,
            "max_tool_calls",
            _positive_int(self.max_tool_calls, field="max_tool_calls", maximum=_MAX_TOOL_CALLS),
        )
        object.__setattr__(
            self,
            "max_parallelism",
            _nonnegative_int(
                self.max_parallelism,
                field="max_parallelism",
                maximum=_MAX_PARALLELISM,
            ),
        )
        if (
            isinstance(self.max_wall_time_seconds, bool)
            or not isinstance(self.max_wall_time_seconds, (int, float))
            or not math.isfinite(float(self.max_wall_time_seconds))
            or not 1.0 <= float(self.max_wall_time_seconds) <= _MAX_WALL_TIME_SECONDS
        ):
            raise RunContractError(
                f"max_wall_time_seconds must be finite from 1 to {_MAX_WALL_TIME_SECONDS:g}"
            )
        object.__setattr__(self, "max_wall_time_seconds", float(self.max_wall_time_seconds))
        object.__setattr__(
            self,
            "max_prompt_tokens_per_round",
            _positive_int(
                self.max_prompt_tokens_per_round,
                field="max_prompt_tokens_per_round",
                maximum=_MAX_TOKEN_BUDGET,
            ),
        )
        object.__setattr__(
            self,
            "max_total_tokens",
            _positive_int(
                self.max_total_tokens,
                field="max_total_tokens",
                maximum=_MAX_TOKEN_BUDGET,
            ),
        )
        if self.max_total_tokens < self.max_prompt_tokens_per_round:
            raise RunContractError("max_total_tokens cannot be smaller than one prompt budget")


@dataclass(frozen=True)
class WorkspaceContract:
    """Private workspace identity captured before execution."""

    root: str
    git_available: bool
    initial_head: str = ""
    status_digest: str = ""
    tracked_diff_digest: str = ""
    untracked_digest: str = ""

    def __post_init__(self) -> None:
        root = _clean_text(self.root, field="workspace root", limit=4_096)
        path = Path(root).expanduser()
        if not path.is_absolute():
            raise RunContractError("workspace root must be absolute")
        object.__setattr__(self, "root", str(path.resolve(strict=False)))
        if type(self.git_available) is not bool:
            raise RunContractError("git_available must be boolean")
        object.__setattr__(
            self,
            "initial_head",
            _clean_git_head(self.initial_head),
        )
        object.__setattr__(
            self,
            "status_digest",
            _clean_digest(self.status_digest, field="status_digest"),
        )
        object.__setattr__(
            self,
            "tracked_diff_digest",
            _clean_digest(self.tracked_diff_digest, field="tracked_diff_digest"),
        )
        object.__setattr__(
            self,
            "untracked_digest",
            _clean_digest(self.untracked_digest, field="untracked_digest"),
        )
        if not self.git_available and any(
            (
                self.initial_head,
                self.status_digest,
                self.tracked_diff_digest,
                self.untracked_digest,
            )
        ):
            raise RunContractError("unavailable Git state cannot carry Git identity fields")


@dataclass(frozen=True)
class BlockRunContract:
    """Runtime-owned policy and budget for one ordered Agent Block."""

    ordinal: int
    role: str
    model: str
    prompt_digest: str
    configured_tools: tuple[str, ...]
    admitted_tools: tuple[str, ...]
    approval_required_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    max_iterations: int
    requires_change: bool
    required_verifiers: tuple[str, ...] = ()
    policy_reasons: tuple[str, ...] = ()
    recovery_codes: tuple[str, ...] = ()
    max_recovery_attempts: int = 0
    recovery_max_iterations: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ordinal",
            _nonnegative_int(self.ordinal, field="block ordinal", maximum=_MAX_BLOCKS - 1),
        )
        object.__setattr__(self, "role", _clean_text(self.role, field="block role", limit=80))
        object.__setattr__(self, "model", _clean_text(self.model, field="block model", limit=256))
        object.__setattr__(
            self,
            "prompt_digest",
            _clean_digest(
                self.prompt_digest,
                field="block prompt digest",
                allow_empty=False,
            ),
        )
        configured = _clean_unique_names(self.configured_tools, field="configured tools")
        admitted = _clean_unique_names(self.admitted_tools, field="admitted tools")
        approvals = _clean_unique_names(
            self.approval_required_tools,
            field="approval-required tools",
        )
        denied = _clean_unique_names(self.denied_tools, field="denied tools")
        if not set(admitted).issubset(configured):
            raise RunContractError("admitted tools must be a subset of configured tools")
        if not set(approvals).issubset(admitted):
            raise RunContractError("approval-required tools must be a subset of admitted tools")
        if set(denied) != set(configured) - set(admitted):
            raise RunContractError("denied tools must exactly cover configured tools not admitted")
        object.__setattr__(self, "configured_tools", configured)
        object.__setattr__(self, "admitted_tools", admitted)
        object.__setattr__(self, "approval_required_tools", approvals)
        object.__setattr__(self, "denied_tools", denied)
        object.__setattr__(
            self,
            "max_iterations",
            _positive_int(
                self.max_iterations,
                field="block max_iterations",
                maximum=_MAX_ITERATIONS_PER_BLOCK,
            ),
        )
        if type(self.requires_change) is not bool:
            raise RunContractError("requires_change must be boolean")
        verifiers = _clean_unique_names(self.required_verifiers, field="required verifiers")
        reasons = tuple(
            _clean_text(reason, field="policy reason", limit=512)
            for reason in self.policy_reasons
        )
        object.__setattr__(self, "required_verifiers", verifiers)
        object.__setattr__(self, "policy_reasons", reasons)
        if self.requires_change and "post_mutation" not in verifiers:
            raise RunContractError("change-producing blocks require a post_mutation verifier")
        recovery_codes = _clean_unique_names(
            self.recovery_codes,
            field="recovery codes",
        )
        recovery_attempts = _nonnegative_int(
            self.max_recovery_attempts,
            field="max recovery attempts",
            maximum=1,
        )
        recovery_iterations = _nonnegative_int(
            self.recovery_max_iterations,
            field="recovery max iterations",
            maximum=_MAX_ITERATIONS_PER_BLOCK,
        )
        if recovery_attempts:
            if not self.requires_change:
                raise RunContractError(
                    "only change-producing blocks may have recovery"
                )
            if not recovery_codes or not recovery_iterations:
                raise RunContractError(
                    "recovery attempts require codes and an iteration budget"
                )
        elif recovery_codes or recovery_iterations:
            raise RunContractError(
                "disabled recovery cannot carry codes or an iteration budget"
            )
        object.__setattr__(self, "recovery_codes", recovery_codes)
        object.__setattr__(
            self,
            "max_recovery_attempts",
            recovery_attempts,
        )
        object.__setattr__(
            self,
            "recovery_max_iterations",
            recovery_iterations,
        )

    def effective_tools(self, mode: ContractMode) -> tuple[str, ...]:
        """Return the tool set to expose under shadow or enforced execution."""

        if mode not in _CONTRACT_MODES:
            raise RunContractError("contract mode must be shadow or enforced")
        return self.admitted_tools if mode == "enforced" else self.configured_tools


@dataclass(frozen=True)
class RunContract:
    """Closed, hashable Agent execution envelope."""

    schema_version: int
    run_nonce: str
    issued_at: str
    mode: ContractMode
    approval_mode: ApprovalMode
    safe_mode: bool
    session_preapproval: bool
    task_digest: str
    task_type: str
    complexity: str
    risk: str
    pipeline: str
    model: str
    reasoning_effort: str
    speed_tier: str
    mutation_scope: MutationScope
    required_verifiers: tuple[str, ...]
    permitted_recovery_codes: tuple[str, ...]
    workspace: WorkspaceContract
    budget: RunBudget
    blocks: tuple[BlockRunContract, ...]

    def __post_init__(self) -> None:
        if self.schema_version != RUN_CONTRACT_SCHEMA_VERSION:
            raise RunContractError(
                f"run contract schema must be {RUN_CONTRACT_SCHEMA_VERSION}"
            )
        nonce = _clean_text(self.run_nonce, field="run nonce", limit=64)
        if len(nonce) < 8:
            raise RunContractError("run nonce must contain at least 8 characters")
        object.__setattr__(self, "run_nonce", nonce)
        issued_at = _clean_text(self.issued_at, field="issued_at", limit=64)
        try:
            parsed = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RunContractError("issued_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise RunContractError("issued_at must include a timezone")
        object.__setattr__(self, "issued_at", issued_at)
        if self.mode not in _CONTRACT_MODES:
            raise RunContractError("mode must be shadow or enforced")
        if self.approval_mode not in _APPROVAL_MODES:
            raise RunContractError("approval_mode must be interactive, never, or auto")
        if type(self.safe_mode) is not bool:
            raise RunContractError("safe_mode must be boolean")
        if type(self.session_preapproval) is not bool:
            raise RunContractError("session_preapproval must be boolean")
        if self.approval_mode == "never" and self.session_preapproval:
            raise RunContractError("never approval mode cannot carry session preapproval")
        object.__setattr__(
            self,
            "task_digest",
            _clean_digest(self.task_digest, field="task_digest", allow_empty=False),
        )
        for field_name, limit in (
            ("task_type", 80),
            ("complexity", 40),
            ("risk", 40),
            ("pipeline", 80),
            ("model", 256),
            ("reasoning_effort", 80),
            ("speed_tier", 80),
        ):
            object.__setattr__(
                self,
                field_name,
                _clean_text(getattr(self, field_name), field=field_name, limit=limit),
            )
        if self.mutation_scope not in _MUTATION_SCOPES:
            raise RunContractError("mutation_scope must be none or workspace")
        required = _clean_unique_names(self.required_verifiers, field="required verifiers")
        recovery = _clean_unique_names(
            self.permitted_recovery_codes,
            field="permitted recovery codes",
        )
        object.__setattr__(self, "required_verifiers", required)
        object.__setattr__(self, "permitted_recovery_codes", recovery)
        if not isinstance(self.workspace, WorkspaceContract):
            raise RunContractError("workspace must be a WorkspaceContract")
        if not isinstance(self.budget, RunBudget):
            raise RunContractError("budget must be a RunBudget")
        if not isinstance(self.blocks, tuple) or not self.blocks:
            raise RunContractError("blocks must be a non-empty tuple")
        if len(self.blocks) > self.budget.max_blocks:
            raise RunContractError("pipeline exceeds the contract block budget")
        ordinals = tuple(block.ordinal for block in self.blocks)
        if ordinals != tuple(range(len(self.blocks))):
            raise RunContractError("block ordinals must be contiguous and ordered")
        if any(block.max_iterations > self.budget.max_iterations_per_block for block in self.blocks):
            raise RunContractError("block exceeds the contract iteration budget")
        if any(
            not set(block.recovery_codes).issubset(
                self.permitted_recovery_codes
            )
            for block in self.blocks
        ):
            raise RunContractError(
                "block recovery codes exceed the run contract"
            )
        has_mutation = any(block.requires_change for block in self.blocks)
        if has_mutation != (self.mutation_scope == "workspace"):
            raise RunContractError("mutation scope must match change-producing blocks")
        if has_mutation and "post_mutation" not in required:
            raise RunContractError("mutating runs require a post_mutation verifier")

    def payload(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible contract payload."""

        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Any) -> "RunContract":
        """Reconstruct and revalidate a persisted contract payload."""

        if not isinstance(payload, dict):
            raise RunContractError("persisted run contract must be an object")

        def exact(value: Any, target: type[Any], label: str) -> dict[str, Any]:
            if not isinstance(value, dict):
                raise RunContractError(f"{label} must be an object")
            expected = {item.name for item in fields(target)}
            if set(value) != expected:
                raise RunContractError(f"{label} fields do not match schema")
            return dict(value)

        top = exact(payload, cls, "run contract")
        workspace_payload = exact(
            top.pop("workspace"),
            WorkspaceContract,
            "workspace contract",
        )
        budget_payload = exact(top.pop("budget"), RunBudget, "run budget")
        raw_blocks = top.pop("blocks")
        if not isinstance(raw_blocks, (list, tuple)) or not raw_blocks:
            raise RunContractError(
                "persisted run contract blocks must be a sequence"
            )
        blocks = tuple(
            BlockRunContract(
                **exact(raw_block, BlockRunContract, "block run contract")
            )
            for raw_block in raw_blocks
        )
        return cls(
            **top,
            workspace=WorkspaceContract(**workspace_payload),
            budget=RunBudget(**budget_payload),
            blocks=blocks,
        )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.payload(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @property
    def contract_id(self) -> str:
        return f"run-contract-v{self.schema_version}:{self.digest}"

    def assert_live_authority(
        self,
        *,
        approval_mode: str,
        safe_mode: bool,
        session_preapproval: bool,
    ) -> None:
        """Prevent a run from silently changing its authority semantics."""

        if approval_mode not in _APPROVAL_MODES:
            raise RunContractViolation("live approval mode is invalid")
        if approval_mode != self.approval_mode:
            raise RunContractViolation(
                "live approval mode differs from the immutable run contract"
            )
        if type(safe_mode) is not bool or safe_mode != self.safe_mode:
            raise RunContractViolation(
                "live safe mode differs from the immutable run contract"
            )
        if (
            type(session_preapproval) is not bool
            or session_preapproval != self.session_preapproval
        ):
            raise RunContractViolation(
                "live session preapproval differs from the immutable run contract"
            )

    def assert_live_approval_mode(self, approval_mode: str) -> None:
        """Compatibility helper for callers that only compare approval mode."""

        if approval_mode not in _APPROVAL_MODES:
            raise RunContractViolation("live approval mode is invalid")
        if approval_mode != self.approval_mode:
            raise RunContractViolation(
                "live approval mode differs from the immutable run contract"
            )

    def block(self, ordinal: int) -> BlockRunContract:
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise RunContractViolation("block ordinal must be an integer")
        try:
            return self.blocks[ordinal]
        except IndexError as exc:
            raise RunContractViolation("block is outside the run contract") from exc


@dataclass
class RunContractTracker:
    """Mutable, thread-safe resource counters bound to an immutable contract."""

    contract: RunContract
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    started_at: float = field(init=False)
    blocks_started: int = 0
    model_rounds: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    _next_block_ordinal: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.contract, RunContract):
            raise RunContractError("tracker requires a RunContract")
        started = self.clock()
        if isinstance(started, bool) or not isinstance(started, (int, float)) or not math.isfinite(float(started)):
            raise RunContractError("tracker clock returned an invalid value")
        self.started_at = float(started)

    @classmethod
    def restore(
        cls,
        contract: RunContract,
        *,
        completed_blocks: int,
        model_rounds: int,
        tool_calls: int,
        prompt_tokens: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> "RunContractTracker":
        """Restore durable counters at the last verified block boundary."""

        tracker = cls(contract, clock=clock)
        tracker.blocks_started = _nonnegative_int(
            completed_blocks,
            field="completed blocks",
            maximum=contract.budget.max_blocks,
        )
        tracker._next_block_ordinal = tracker.blocks_started
        tracker.model_rounds = _nonnegative_int(
            model_rounds,
            field="model rounds",
            maximum=contract.budget.max_tool_calls
            + contract.budget.max_blocks,
        )
        tracker.tool_calls = _nonnegative_int(
            tool_calls,
            field="tool calls",
            maximum=contract.budget.max_tool_calls,
        )
        tracker.prompt_tokens = _nonnegative_int(
            prompt_tokens,
            field="prompt tokens",
            maximum=contract.budget.max_total_tokens,
        )
        return tracker

    def _elapsed_unlocked(self) -> float:
        current = self.clock()
        if isinstance(current, bool) or not isinstance(current, (int, float)) or not math.isfinite(float(current)):
            raise RunContractViolation("run contract clock returned an invalid value")
        elapsed = float(current) - self.started_at
        if elapsed < 0:
            raise RunContractViolation("run contract clock moved backwards")
        return elapsed

    def check_wall_time(self) -> float:
        with self._lock:
            elapsed = self._elapsed_unlocked()
            if elapsed > self.contract.budget.max_wall_time_seconds:
                raise RunContractViolation("run contract wall-time budget is exhausted")
            return elapsed

    def start_block(self, ordinal: int) -> None:
        with self._lock:
            self.check_wall_time()
            if ordinal != self._next_block_ordinal:
                raise RunContractViolation("blocks must start once in contract order")
            if self.blocks_started >= self.contract.budget.max_blocks:
                raise RunContractViolation("run contract block budget is exhausted")
            self.contract.block(ordinal)
            self.blocks_started += 1
            self._next_block_ordinal += 1

    def start_model_round(self, prompt_tokens: int = 0) -> None:
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or prompt_tokens < 0
        ):
            raise RunContractViolation(
                "prompt token estimate must be a nonnegative integer"
            )
        with self._lock:
            self.check_wall_time()
            if (
                prompt_tokens
                > self.contract.budget.max_prompt_tokens_per_round
            ):
                raise RunContractViolation(
                    "run contract per-round prompt budget is exhausted"
                )
            if (
                self.prompt_tokens + prompt_tokens
                > self.contract.budget.max_total_tokens
            ):
                raise RunContractViolation(
                    "run contract total prompt budget is exhausted"
                )
            self.prompt_tokens += prompt_tokens
            self.model_rounds += 1
            if self.model_rounds > self.contract.budget.max_tool_calls + self.contract.budget.max_blocks:
                raise RunContractViolation("run contract model-round budget is exhausted")

    def reserve_tool_calls(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RunContractViolation("tool-call reservation must be a nonnegative integer")
        with self._lock:
            self.check_wall_time()
            if self.tool_calls + count > self.contract.budget.max_tool_calls:
                raise RunContractViolation("run contract tool-call budget is exhausted")
            self.tool_calls += count


def _issued_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _task_digest(task: str) -> str:
    if type(task) is not str or not task.strip():
        raise RunContractError("task must be non-empty text")
    return hashlib.sha256(task.encode("utf-8")).hexdigest()


def _workspace_contract(cwd: str, snapshot: git_evidence.GitSnapshot) -> WorkspaceContract:
    return WorkspaceContract(
        root=str(Path(cwd).expanduser().resolve(strict=False)),
        git_available=snapshot.available,
        initial_head=snapshot.head or "" if snapshot.available else "",
        status_digest=snapshot.status_digest if snapshot.available else "",
        tracked_diff_digest=snapshot.tracked_diff_digest if snapshot.available else "",
        untracked_digest=snapshot.untracked_digest if snapshot.available else "",
    )


def _reasoning_effort(model: str, cfg: Config) -> str:
    if model_info.is_chatgpt_model(model):
        return chatgpt_client.reasoning_effort_for_model(
            model,
            cfg.chatgpt_reasoning_efforts,
        )
    return "provider-default"


def compile_agent_run_contract(
    *,
    task: str,
    route: task_router.TaskRoute,
    pipeline_name: str,
    blocks: Sequence[agent_blocks.AgentBlock],
    cfg: Config,
    approval_mode: ApprovalMode,
    snapshot: git_evidence.GitSnapshot | None = None,
    run_nonce: str | None = None,
    issued_at: str | None = None,
) -> RunContract:
    """Compile the current routed pipeline into a closed execution contract."""

    if approval_mode not in _APPROVAL_MODES:
        raise RunContractError("approval_mode must be interactive, never, or auto")
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence) or not blocks:
        raise RunContractError("pipeline must contain at least one Agent Block")
    if len(blocks) > _MAX_BLOCKS:
        raise RunContractError(f"pipeline exceeds the {_MAX_BLOCKS}-block hard limit")
    if route.read_only and any(block.requires_change for block in blocks):
        raise RunContractError(
            "explicit read-only tasks cannot use a change-producing pipeline"
        )
    mode: ContractMode = (
        "enforced" if bool(cfg.algorithmic_tool_policy_enabled) else "shadow"
    )
    recommendation = spawn_budget.compute_budget(route, task)
    recommended_iterations = recommendation.max_iterations_per_block
    configured_ceiling = max(1, min(int(cfg.max_tool_iterations), _MAX_ITERATIONS_PER_BLOCK))
    block_contracts: list[BlockRunContract] = []
    recoverable_codes = (
        "max_iterations",
        "no_verified_delta",
        "no_write_evidence",
        "write_blocked",
    )
    for ordinal, block in enumerate(blocks):
        decision = samuel_policy.compute_policy(
            route,
            block.role,
            block.allowed_tools,
            cfg.safe_mode,
            approval_mode == "auto" or cfg.auto_approve_active,
        )
        max_iterations = min(
            max(1, int(block.max_iterations)),
            configured_ceiling,
            recommended_iterations or _MAX_ITERATIONS_PER_BLOCK,
        )
        output_verifier = (
            "final_output" if block.role == "final" else "block_output"
        )
        verifiers = (
            (
                output_verifier,
                "attributable_change",
                "post_mutation",
            )
            if block.requires_change
            else (output_verifier,)
        )
        block_model = block.model or cfg.model
        recovery_enabled = (
            block.requires_change and route.risk != "high"
        )
        block_contracts.append(
            BlockRunContract(
                ordinal=ordinal,
                role=block.role,
                model=block_model,
                prompt_digest=hashlib.sha256(
                    block.prompt.encode("utf-8")
                ).hexdigest(),
                configured_tools=tuple(block.allowed_tools),
                admitted_tools=tuple(decision.allowed_tools),
                approval_required_tools=tuple(decision.approval_required),
                denied_tools=tuple(decision.denied_tools),
                max_iterations=max_iterations,
                requires_change=block.requires_change,
                required_verifiers=verifiers,
                policy_reasons=decision.reasons,
                recovery_codes=(
                    recoverable_codes if recovery_enabled else ()
                ),
                max_recovery_attempts=1 if recovery_enabled else 0,
                recovery_max_iterations=(
                    min(
                        8,
                        max(1, max_iterations),
                    )
                    if recovery_enabled
                    else 0
                ),
            )
        )
    maximum_iterations = max(block.max_iterations for block in block_contracts)
    max_tool_calls = min(
        _MAX_TOOL_CALLS,
        max(
            1,
            sum(
                block.max_iterations
                + block.recovery_max_iterations
                + block.max_recovery_attempts
                for block in block_contracts
            ),
        ),
    )
    runtime_context = max(
        1_024,
        min(int(cfg.num_ctx), _MAX_TOKEN_BUDGET),
    )
    response_reserve = min(
        max(384, runtime_context // 8),
        max(1, runtime_context // 3),
    )
    prompt_budget = max(512, runtime_context - response_reserve)
    total_token_budget = min(
        _MAX_TOKEN_BUDGET,
        max(
            prompt_budget,
            prompt_budget
            * (max_tool_calls + len(block_contracts)),
        ),
    )
    wall_time = min(
        _MAX_WALL_TIME_SECONDS,
        max(60.0, float(max_tool_calls * 120)),
    )
    has_mutation = any(block.requires_change for block in block_contracts)
    required_verifiers = tuple(
        sorted(
            {
                verifier
                for block in block_contracts
                for verifier in block.required_verifiers
            }
        )
    )
    initial_snapshot = snapshot or git_evidence.capture_git_snapshot(cfg.cwd)
    return RunContract(
        schema_version=RUN_CONTRACT_SCHEMA_VERSION,
        run_nonce=run_nonce or uuid.uuid4().hex,
        issued_at=issued_at or _issued_at(),
        mode=mode,
        approval_mode=approval_mode,
        safe_mode=bool(cfg.safe_mode),
        session_preapproval=(
            False
            if approval_mode == "never"
            else approval_mode == "auto" or bool(cfg.auto_approve_active)
        ),
        task_digest=_task_digest(task),
        task_type=route.task_type,
        complexity=route.complexity,
        risk=route.risk,
        pipeline=pipeline_name,
        model=cfg.model,
        reasoning_effort=_reasoning_effort(cfg.model, cfg),
        speed_tier="default",
        mutation_scope="workspace" if has_mutation else "none",
        required_verifiers=required_verifiers,
        permitted_recovery_codes=(
            "max_iterations",
            "no_verified_delta",
            "no_write_evidence",
            "verification_missing",
            "write_blocked",
        ),
        workspace=_workspace_contract(cfg.cwd, initial_snapshot),
        budget=RunBudget(
            max_blocks=len(block_contracts),
            max_iterations_per_block=maximum_iterations,
            max_tool_calls=max_tool_calls,
            max_parallelism=min(
                _MAX_PARALLELISM,
                max(0, recommendation.parallelism),
            ),
            max_wall_time_seconds=wall_time,
            max_prompt_tokens_per_round=prompt_budget,
            max_total_tokens=total_token_budget,
        ),
        blocks=tuple(block_contracts),
    )
