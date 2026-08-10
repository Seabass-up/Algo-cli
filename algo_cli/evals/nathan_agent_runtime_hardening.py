"""Deterministic hardening benchmark for the Algo Agent runtime.

This workload measures runtime-owned coordination rather than model quality. It
uses no network or model calls. Correctness probes exercise approval-mode
separation, immutable contracts, context bounds, provider/tool protocol
balancing, durable checkpoints, tamper detection, and fail-closed resume.
Latency measurements cover contract compilation, context brokerage, and a
durable checkpoint/load/resume cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .. import agent_blocks
from .. import agent_context
from .. import agent_pipeline
from .. import agent_run_journal
from .. import agent_threads
from .. import elsie_echo_preflight
from .. import git_evidence
from .. import grace_key_store
from .. import nathan_provider_protocol
from .. import run_contract
from .. import task_router
from ..config import Config
from ..grace_memory_receipts import ElsieReceiptAuthority, ElsieReceiptError
from ..irene_privacy_views import PRIVACY_KEY_LABEL


BENCHMARK_ID = "nathan-agent-runtime-hardening-v3"
SCHEMA_VERSION = 3
FIXED_TIME = "2026-07-23T12:00:00+00:00"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)

SOURCE_PATHS = (
    "algo_cli/ada_memory_echo_veil.py",
    "algo_cli/agent_blocks.py",
    "algo_cli/agent_context.py",
    "algo_cli/agent_pipeline.py",
    "algo_cli/agent_run_journal.py",
    "algo_cli/agent_threads.py",
    "algo_cli/chat_protocol.py",
    "algo_cli/chatgpt_client.py",
    "algo_cli/config.py",
    "algo_cli/context_budget.py",
    "algo_cli/evals/nathan_agent_runtime_hardening.py",
    "algo_cli/elsie_echo_preflight.py",
    "algo_cli/git_evidence.py",
    "algo_cli/grace_key_store.py",
    "algo_cli/grace_memory_receipts.py",
    "algo_cli/irene_privacy_views.py",
    "algo_cli/main.py",
    "algo_cli/model_aliases.py",
    "algo_cli/model_info.py",
    "algo_cli/nathan_provider_protocol.py",
    "algo_cli/nathan_runtime.py",
    "algo_cli/ada_private_event_store.py",
    "algo_cli/run_contract.py",
    "algo_cli/samuel_policy.py",
    "algo_cli/spawn_budget.py",
    "algo_cli/task_router.py",
    "scripts/nathan_agent_runtime_qualification.py",
    "tests/test_agent_context.py",
    "tests/test_agent_pipeline.py",
    "tests/test_agent_run_journal.py",
    "tests/test_agent_threads.py",
    "tests/test_ada_memory_echo_veil.py",
    "tests/test_chatgpt_client.py",
    "tests/test_elsie_echo_preflight.py",
    "tests/test_grace_key_store.py",
    "tests/test_grace_memory_receipts.py",
    "tests/test_main_helpers.py",
    "tests/test_model_info.py",
    "tests/test_nathan_agent_runtime_hardening.py",
    "tests/test_run_contract.py",
    "tests/test_task_router.py",
)

LATENCY_THRESHOLDS_MS = {
    "contract_compile": 250.0,
    "context_broker": 100.0,
    "checkpoint_resume": 1_000.0,
    "agent_workload_ttfa": 1_000.0,
    "agent_workload_total": 2_500.0,
}
MIN_LATENCY_SAMPLES = 20


class AgentRuntimeBenchmarkError(RuntimeError):
    """Raised when the benchmark or its stored report fails closed."""


def _discover_source_root(
    required_paths: tuple[str, ...],
    *,
    module_file: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Find the checked-out Git tree even when this module is installed."""

    try:
        resolved_module = (module_file or Path(__file__)).resolve(strict=True)
        resolved_cwd = (cwd or Path.cwd()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AgentRuntimeBenchmarkError("benchmark source root is unavailable") from exc

    candidates = (
        resolved_module.parents[2],
        resolved_cwd,
        *resolved_cwd.parents,
    )
    visited: set[Path] = set()
    for candidate in candidates:
        if candidate in visited:
            continue
        visited.add(candidate)
        if not (candidate / ".git").exists():
            continue
        if all((candidate / relative).is_file() for relative in required_paths):
            return candidate
    raise AgentRuntimeBenchmarkError("benchmark must run from the Algo CLI source checkout")


ROOT = _discover_source_root(SOURCE_PATHS)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AgentRuntimeBenchmarkError("benchmark value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _source_identity(information: os.stat_result) -> tuple[int, ...]:
    common = (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
        int(information.st_nlink),
        int(information.st_size),
        int(information.st_mtime_ns),
    )
    if os.name == "nt":
        # Windows path stat and CRT fstat can project permission bits and ctime
        # differently for the same handle. File attributes bind the relevant
        # regular/reparse/read-only identity consistently across both views.
        return (*common, int(getattr(information, "st_file_attributes", 0)))
    return (*common, int(information.st_mode), int(information.st_ctime_ns))


def _source_is_reparse(information: os.stat_result) -> bool:
    return stat.S_ISLNK(information.st_mode) or (
        os.name == "nt"
        and bool(
            int(getattr(information, "st_file_attributes", 0))
            & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        )
    )


def _read_source_payload_with_identity(relative: str) -> tuple[bytes, tuple[int, ...]]:
    root = ROOT.resolve(strict=True)
    candidate = ROOT / relative
    try:
        before = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AgentRuntimeBenchmarkError(f"benchmark source is unavailable: {relative}") from exc
    if (
        resolved != candidate.absolute()
        or not stat.S_ISREG(before.st_mode)
        or _source_is_reparse(before)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= MAX_SOURCE_BYTES
    ):
        raise AgentRuntimeBenchmarkError(f"benchmark source boundary rejected: {relative}")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _source_identity(opened) != _source_identity(before):
            raise AgentRuntimeBenchmarkError(f"benchmark source changed while opening: {relative}")
        payload = bytearray()
        while len(payload) < opened.st_size:
            chunk = os.read(descriptor, min(64 * 1024, opened.st_size - len(payload)))
            if not chunk:
                raise AgentRuntimeBenchmarkError(f"benchmark source was truncated while reading: {relative}")
            payload.extend(chunk)
        if os.read(descriptor, 1):
            raise AgentRuntimeBenchmarkError(f"benchmark source grew while reading: {relative}")
        after_descriptor = os.fstat(descriptor)
        after_path = candidate.lstat()
        if (
            _source_is_reparse(after_path)
            or _source_identity(after_descriptor) != _source_identity(opened)
            or _source_identity(after_path) != _source_identity(opened)
        ):
            raise AgentRuntimeBenchmarkError(f"benchmark source changed while reading: {relative}")
    except OSError as exc:
        raise AgentRuntimeBenchmarkError(f"benchmark source is unavailable: {relative}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return bytes(payload), _source_identity(opened)


def _read_source_payload(relative: str) -> bytes:
    payload, _identity = _read_source_payload_with_identity(relative)
    return payload


def _recheck_source_identity(relative: str, expected: tuple[int, ...]) -> None:
    candidate = ROOT / relative
    try:
        root = ROOT.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        observed = candidate.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise AgentRuntimeBenchmarkError(f"benchmark source is unavailable: {relative}") from exc
    if (
        resolved != candidate.absolute()
        or not stat.S_ISREG(observed.st_mode)
        or _source_is_reparse(observed)
        or observed.st_nlink != 1
        or _source_identity(observed) != expected
    ):
        raise AgentRuntimeBenchmarkError(f"benchmark source changed after reading: {relative}")


def _capture_source_tree() -> tuple[str, tuple[tuple[str, tuple[int, ...]], ...]]:
    """Capture exact source bytes and identities before qualification work."""

    digest = hashlib.sha256()
    identities: list[tuple[str, tuple[int, ...]]] = []
    for relative in sorted(SOURCE_PATHS):
        payload, identity = _read_source_payload_with_identity(relative)
        identities.append((relative, identity))
        digest.update(relative.encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    snapshot = tuple(identities)
    for relative, identity in snapshot:
        _recheck_source_identity(relative, identity)
    return "sha256:" + digest.hexdigest(), snapshot


def _verify_source_tree_snapshot(
    snapshot: tuple[tuple[str, tuple[int, ...]], ...],
) -> None:
    for relative, identity in snapshot:
        _recheck_source_identity(relative, identity)


def source_tree_digest() -> str:
    """Bind evidence to the exact runtime and adversarial test bytes."""

    digest, _snapshot = _capture_source_tree()
    return digest


def _git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="ascii",
        timeout=5,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    revision = completed.stdout.strip()
    if completed.returncode != 0 or _REVISION_RE.fullmatch(revision) is None:
        raise AgentRuntimeBenchmarkError("benchmark source revision is unavailable")
    return revision


def _snapshot(*, changed: bool = False) -> git_evidence.GitSnapshot:
    return git_evidence.GitSnapshot(
        available=True,
        error=None,
        head="a" * 40,
        status="## hardening/runtime" + ("\n M runtime.py" if changed else ""),
        tracked_diff="+bounded change" if changed else "",
        untracked_files=(),
        tracked_diff_digest=("b" if changed else "0") * 64,
        untracked_digest="1" * 64,
        status_digest=("c" if changed else "2") * 64,
    )


def _config(root: Path) -> Config:
    cfg = Config(
        cwd=str(root),
        model="qwen3",
        num_ctx=8_192,
    )
    cfg.algorithmic_tool_policy_enabled = True
    cfg.safe_mode = True
    return cfg


def _compile(
    root: Path,
    *,
    task: str,
    pipeline_name: str,
    blocks: list[agent_blocks.AgentBlock],
    approval_mode: str,
    nonce: str,
    protected: bool = False,
    receipt_key_store: Any | None = None,
) -> run_contract.RunContract:
    cfg = _config(root)
    cfg.echo_veil_enabled = protected
    cfg.echo_veil_protection = "required" if protected else "optional"
    return run_contract.compile_agent_run_contract(
        task=task,
        route=task_router.route_task(task),
        pipeline_name=pipeline_name,
        blocks=blocks,
        cfg=cfg,
        approval_mode=approval_mode,  # type: ignore[arg-type]
        snapshot=_snapshot(),
        run_nonce=nonce,
        issued_at=FIXED_TIME,
        receipt_key_store=receipt_key_store,
    )


def _context_sources() -> tuple[agent_context.AgentContextSource, ...]:
    return (
        agent_context.AgentContextSource(
            name="handoff",
            title="Verified Handoff",
            body="Verified block evidence. " * 80,
            priority=100,
            trust="verified_handoff",
        ),
        agent_context.AgentContextSource(
            name="memory",
            title="Governed Memory",
            body="Current governed fact. " * 160,
            priority=80,
            trust="governed_memory",
        ),
        agent_context.AgentContextSource(
            name="intuition",
            title="Heuristic Memory",
            body="Heuristic suggestion. " * 240,
            priority=20,
            trust="heuristic_memory",
        ),
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise AgentRuntimeBenchmarkError("latency sample is empty")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(values), 6),
        "p95_ms": round(_percentile(values, 0.95), 6),
        "max_ms": round(max(values), 6),
    }


def _measure(
    operation: Callable[[int], Any],
    *,
    repetitions: int,
    warmups: int,
) -> dict[str, Any]:
    if repetitions < 1 or warmups < 0:
        raise AgentRuntimeBenchmarkError("latency repetitions and warmups must be bounded")
    samples: list[float] = []
    for index in range(warmups + repetitions):
        started = time.perf_counter_ns()
        operation(index)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if index >= warmups:
            samples.append(elapsed_ms)
    return _latency_summary(samples)


def _checkpoint_cycle(
    root: Path,
    *,
    index: int,
) -> None:
    nonce = f"runtime-bench-checkpoint-{index:06d}"
    contract = _compile(
        root,
        task="Review the runtime for correctness",
        pipeline_name="review",
        blocks=agent_blocks.review_pipeline(),
        approval_mode="never",
        nonce=nonce,
    )
    bundle = agent_context.build_agent_context(
        "Review the runtime for correctness",
        _context_sources(),
        max_tokens=2_048,
    )
    path = root / f"{nonce}.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
    )
    with journal.execution_lease():
        role = contract.blocks[0].role
        journal.context_bound(bundle.receipt.payload())
        journal.block_started(0, role)
        journal.model_round_started(0, 0, prompt_tokens=512)
        journal.model_round_completed(
            0,
            0,
            status="completed",
            tool_call_count=0,
            response_digest=agent_run_journal.digest_text("verified review"),
        )
        journal.verifier_result(
            ordinal=0,
            verifier="block_output",
            status="passed",
            snapshot=_snapshot(),
        )
        journal.block_finished(
            ordinal=0,
            role=role,
            status="complete",
            verified=True,
            context_digest=agent_run_journal.digest_text("## Block Output\nVerified review"),
            snapshot=_snapshot(),
        )
    loaded = agent_run_journal.AgentRunJournal.load(
        nonce,
        path=path,
    )
    state = loaded.resume_state()
    if (
        state.completed_block_ordinals != (0,)
        or state.next_block_ordinal != 1
        or state.model_rounds != 1
        or state.prompt_tokens != 512
        or not state.can_resume
        or not state.workspace_matches(_snapshot())
    ):
        raise AgentRuntimeBenchmarkError("durable checkpoint did not reconstruct exactly")


def _frozen_agent_workload(
    root: Path,
    *,
    index: int,
) -> dict[str, Any]:
    """Run one model-free, end-to-end Agent coordination workload."""

    started_ns = time.perf_counter_ns()
    ttfa_ns: int | None = None
    task = "Review the Agent runtime and report verified findings"
    blocks = agent_blocks.review_pipeline()
    nonce = f"runtime-e2e-{index:08d}"
    contract = _compile(
        root,
        task=task,
        pipeline_name="review",
        blocks=blocks,
        approval_mode="never",
        nonce=nonce,
    )
    context_bundle = agent_context.build_agent_context(
        task,
        [
            agent_context.AgentContextSource(
                name="runtime_evidence",
                title="Runtime evidence",
                body="The provider protocol requires one result per call.",
                priority=100,
                trust="harness_retrieval",
                scope="workspace",
                freshness_rank=100,
                provenance="frozen-runtime-fixture",
            ),
            agent_context.AgentContextSource(
                name="stale_duplicate",
                title="Stale duplicate",
                body="The provider protocol requires one result per call.",
                priority=100,
                trust="harness_retrieval",
                scope="workspace",
                freshness_rank=1,
                provenance="stale-runtime-fixture",
            ),
            agent_context.AgentContextSource(
                name="irrelevant_context",
                title="Irrelevant context",
                body="Unrelated historical suggestion.",
                priority=90,
                trust="heuristic_memory",
                scope="workspace",
                freshness_rank=50,
                provenance="irrelevant-runtime-fixture",
                answerable=False,
            ),
        ],
        max_tokens=1_024,
    )
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=root / f"{nonce}.jsonl",
    )
    verifier_passed = 0
    verifier_total = 0
    protocol_states: list[nathan_provider_protocol.ProviderToolLoopState] = []
    crash_resume_passed = False
    with journal.execution_lease():
        journal.context_bound(context_bundle.receipt.payload())
        for ordinal, contract_block in enumerate(contract.blocks):
            journal.block_started(ordinal, contract_block.role)
            loop_state = nathan_provider_protocol.ProviderToolLoopState(loop_id=f"workload-{index}-{ordinal}")
            protocol_states.append(loop_state)
            journal.model_round_started(
                ordinal,
                0,
                prompt_tokens=256,
            )
            loop_state.begin_model_round(0)
            loop_state.record_model_event("content")
            if ttfa_ns is None:
                ttfa_ns = time.perf_counter_ns()
            if ordinal == 0:
                tool_calls = loop_state.complete_model_round(
                    [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": {"path": "README.md"},
                            }
                        }
                    ]
                )
            else:
                tool_calls = loop_state.complete_model_round([])
            journal.model_round_completed(
                ordinal,
                0,
                status="completed",
                tool_call_count=len(tool_calls),
                response_digest=agent_run_journal.digest_text(f"{contract_block.role}-round-0"),
            )
            if tool_calls:
                call_id = str(tool_calls[0]["id"])
                loop_state.begin_tool_batch()
                loop_state.record_tool_dispatch(
                    call_id,
                    mutating=False,
                )
                intent = journal.tool_intent(
                    ordinal=ordinal,
                    round_number=0,
                    tool_index=0,
                    action="read_file",
                    args={"path": "README.md"},
                    call_id=call_id,
                    mutating=False,
                    idempotency="pure",
                    target="workspace:README.md",
                )
                journal.tool_result(
                    step_id=str(intent.payload["step_id"]),
                    status="succeeded",
                    invoked=True,
                    verification="passed",
                )
                loop_state.record_tool_result(call_id)
                loop_state.finish_tool_batch()
                journal.model_round_started(
                    ordinal,
                    1,
                    prompt_tokens=128,
                )
                loop_state.begin_model_round(1)
                loop_state.record_model_event("content")
                loop_state.complete_model_round([])
                loop_state.finish_without_tools()
                journal.model_round_completed(
                    ordinal,
                    1,
                    status="completed",
                    tool_call_count=0,
                    response_digest=agent_run_journal.digest_text(f"{contract_block.role}-round-1"),
                )
            else:
                loop_state.finish_without_tools()
            verifier = "final_output" if contract_block.role == "final" else "block_output"
            journal.verifier_result(
                ordinal=ordinal,
                verifier=verifier,
                status="passed",
                snapshot=_snapshot(),
            )
            verifier_total += 1
            verifier_passed += 1
            journal.block_finished(
                ordinal=ordinal,
                role=contract_block.role,
                status="complete",
                verified=True,
                context_digest=agent_run_journal.digest_text("## Block Output\nVerified runtime finding."),
                snapshot=_snapshot(),
            )
            if ordinal == 0:
                loaded = agent_run_journal.AgentRunJournal.load(
                    nonce,
                    path=root / f"{nonce}.jsonl",
                )
                resumed = loaded.resume_state()
                crash_resume_passed = (
                    resumed.completed_block_ordinals == (0,)
                    and resumed.next_block_ordinal == 1
                    and resumed.can_resume
                    and resumed.workspace_matches(_snapshot())
                )

    completed_state = journal.resume_state()
    task_passed = completed_state.completed_block_ordinals == tuple(
        range(len(contract.blocks))
    ) and completed_state.next_block_ordinal == len(contract.blocks)
    duplicate_state = nathan_provider_protocol.ProviderToolLoopState(loop_id=f"duplicate-{index}")
    duplicate_state.begin_model_round(0)
    duplicate_calls = duplicate_state.complete_model_round(
        [
            {
                "id": "duplicate-mutation",
                "function": {
                    "name": "write_file",
                    "arguments": {"path": "one.py"},
                },
            },
            {
                "id": "duplicate-mutation",
                "function": {
                    "name": "write_file",
                    "arguments": {"path": "two.py"},
                },
            },
        ]
    )
    duplicate_quarantined = len({call["id"] for call in duplicate_calls}) == 2 and all(
        code == "provider_tool_protocol" for code in duplicate_state.tool_batch_ceiling_codes()
    )
    partial_implementation = agent_blocks.AgentBlock(
        role="implement",
        prompt="implement",
        requires_change=True,
        status="partial",
        verification_warning="final state unavailable",
        successful_writes=["runtime.py"],
    )
    unsupported_final = agent_blocks.AgentBlock(
        role="final",
        prompt="final",
        status="complete",
        output="## Block Output\nImplemented the runtime change.",
    )
    unverified_completion_blocked = not (
        agent_pipeline._final_output_claims_are_grounded(
            unsupported_final,
            [partial_implementation],
        )
    )
    relevant_included = "runtime_evidence" in context_bundle.receipt.included_sources
    rejected = {item.name: item.reason for item in context_bundle.receipt.source_metadata if not item.admitted}
    context_useful = (
        relevant_included
        and rejected.get("stale_duplicate") == "duplicate_content"
        and rejected.get("irrelevant_context") == "answerability_rejected"
    )
    policy_escapes = sum("write_file" in block.effective_tools(contract.mode) for block in contract.blocks)
    protocol_correct = all(state.phase == "ready" for state in protocol_states) and duplicate_quarantined
    total_ns = time.perf_counter_ns()
    if ttfa_ns is None:
        raise AgentRuntimeBenchmarkError("frozen workload produced no first Agent event")
    context_used = context_bundle.receipt.used_tokens
    context_max = context_bundle.receipt.max_tokens
    return {
        "task_passed": task_passed,
        "verifier_passed": verifier_passed,
        "verifier_total": verifier_total,
        "policy_escapes": policy_escapes,
        "unverified_completions": (0 if unverified_completion_blocked else 1),
        "duplicate_mutations": (0 if duplicate_quarantined else 1),
        "crash_resume_passed": crash_resume_passed,
        "protocol_correct": protocol_correct,
        "context_useful": context_useful,
        "context_tokens_used": context_used,
        "context_tokens_max": context_max,
        "context_utilization": round(
            context_used / context_max,
            9,
        ),
        "ttfa_ms": round(
            (ttfa_ns - started_ns) / 1_000_000,
            6,
        ),
        "total_ms": round(
            (total_ns - started_ns) / 1_000_000,
            6,
        ),
    }


def _expect_exception(
    error_type: type[BaseException],
    operation: Callable[[], Any],
) -> None:
    try:
        operation()
    except error_type:
        return
    raise AgentRuntimeBenchmarkError(f"expected {error_type.__name__} was not raised")


def _probe_approval_mode_separation(root: Path) -> None:
    task = "Review auth.py for bugs"
    for mode in ("interactive", "never", "auto"):
        contract = _compile(
            root,
            task=task,
            pipeline_name="review",
            blocks=agent_blocks.review_pipeline(),
            approval_mode=mode,
            nonce=f"approval-read-{mode}-0000",
        )
        block = contract.blocks[0]
        if (
            contract.mutation_scope != "none"
            or "read_file" not in block.admitted_tools
            or "read_file" in block.approval_required_tools
        ):
            raise AgentRuntimeBenchmarkError("read-only tool authority changed across approval modes")

    mutating: dict[str, run_contract.RunContract] = {}
    for mode in ("interactive", "never", "auto"):
        mutating[mode] = _compile(
            root,
            task="Fix the failing login test",
            pipeline_name="code-change",
            blocks=agent_blocks.code_change_pipeline(),
            approval_mode=mode,
            nonce=f"approval-write-{mode}-000",
        )
    interactive = mutating["interactive"].blocks[1]
    never = mutating["never"].blocks[1]
    auto = mutating["auto"].blocks[1]
    if (
        "write_file" not in interactive.approval_required_tools
        or "write_file" not in never.approval_required_tools
        or "write_file" in auto.approval_required_tools
        or mutating["never"].session_preapproval
        or not mutating["auto"].session_preapproval
    ):
        raise AgentRuntimeBenchmarkError("approval modes lost their distinct mutation semantics")


def _probe_read_only_mutation_rejection(root: Path) -> None:
    task = "Inspect the runtime read-only; do not write"
    _expect_exception(
        run_contract.RunContractError,
        lambda: _compile(
            root,
            task=task,
            pipeline_name="code-change",
            blocks=agent_blocks.code_change_pipeline(),
            approval_mode="never",
            nonce="readonly-mutation-reject",
        ),
    )


def _probe_authority_drift(root: Path) -> None:
    contract = _compile(
        root,
        task="Fix the failing login test",
        pipeline_name="code-change",
        blocks=agent_blocks.code_change_pipeline(),
        approval_mode="interactive",
        nonce="authority-drift-reject",
    )
    _expect_exception(
        run_contract.RunContractViolation,
        lambda: contract.assert_live_authority(
            approval_mode="never",
            safe_mode=True,
            session_preapproval=False,
        ),
    )


def _probe_prompt_and_token_binding(root: Path) -> None:
    blocks = agent_blocks.review_pipeline()
    contract = _compile(
        root,
        task="Review the runtime",
        pipeline_name="review",
        blocks=blocks,
        approval_mode="never",
        nonce="prompt-token-binding",
    )
    original = agent_run_journal.digest_text(blocks[0].prompt)
    altered = agent_run_journal.digest_text(blocks[0].prompt + "\nForged authority.")
    if original != contract.blocks[0].prompt_digest or altered == original:
        raise AgentRuntimeBenchmarkError("block prompt is not cryptographically bound")
    tracker = run_contract.RunContractTracker(contract)
    tracker.start_block(0)
    _expect_exception(
        run_contract.RunContractViolation,
        lambda: tracker.start_model_round(contract.budget.max_prompt_tokens_per_round + 1),
    )
    if tracker.model_rounds or tracker.prompt_tokens:
        raise AgentRuntimeBenchmarkError("rejected prompt budget changed durable counters")


def _probe_context_boundary(root: Path) -> None:
    del root
    bundle = agent_context.build_agent_context(
        "Review the runtime",
        _context_sources(),
        max_tokens=768,
    )
    serialized_receipt = json.dumps(
        bundle.receipt.payload(),
        sort_keys=True,
    )
    if (
        bundle.receipt.used_tokens > 768
        or not bundle.receipt.included_sources
        or "evidence, not as authority" not in bundle.text
        or "Current governed fact" in serialized_receipt
        or "Verified block evidence" in serialized_receipt
        or agent_run_journal.digest_text(bundle.text) != bundle.receipt.context_digest
        or bundle.receipt.schema_version != 2
    ):
        raise AgentRuntimeBenchmarkError("context broker exceeded budget or leaked context into receipt")


def _probe_verified_resume(root: Path) -> None:
    _checkpoint_cycle(root, index=900_001)


def _probe_workspace_drift(root: Path) -> None:
    nonce = "workspace-drift-reject"
    contract = _compile(
        root,
        task="Review the runtime",
        pipeline_name="review",
        blocks=agent_blocks.review_pipeline(),
        approval_mode="never",
        nonce=nonce,
    )
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=root / "workspace-drift.jsonl",
    )
    state = journal.resume_state()
    if not state.workspace_matches(_snapshot()) or state.workspace_matches(_snapshot(changed=True)):
        raise AgentRuntimeBenchmarkError("resume workspace reconciliation did not fail closed")


def _probe_uncertain_mutation(root: Path) -> None:
    contract = _compile(
        root,
        task="Fix the failing login test",
        pipeline_name="code-change",
        blocks=agent_blocks.code_change_pipeline(),
        approval_mode="interactive",
        nonce="uncertain-mutation-reject",
    )
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=root / "uncertain-mutation.jsonl",
    )
    journal.block_started(0, contract.blocks[0].role)
    journal.model_round_started(0, 0, prompt_tokens=256)
    journal.model_round_completed(
        0,
        0,
        status="completed",
        tool_call_count=1,
        response_digest=agent_run_journal.digest_text("write request"),
    )
    journal.tool_intent(
        ordinal=0,
        round_number=0,
        tool_index=0,
        action="write_file",
        args={"path": "runtime.py", "content": "bounded"},
        call_id="write-1",
        mutating=True,
        idempotency="non_idempotent",
        target="workspace:runtime.py",
    )
    state = journal.resume_state()
    if state.can_resume or state.uncertain_mutation_steps != ("b0-r0-t0",):
        raise AgentRuntimeBenchmarkError("uncertain mutation did not block resume")


def _probe_journal_tamper(root: Path) -> None:
    contract = _compile(
        root,
        task="Review the runtime",
        pipeline_name="review",
        blocks=agent_blocks.review_pipeline(),
        approval_mode="never",
        nonce="journal-tamper-reject",
    )
    path = root / "journal-tamper.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
    )
    journal.block_started(0, contract.blocks[0].role)
    lines = path.read_text(encoding="utf-8").splitlines()
    envelope = json.loads(lines[-1])
    envelope["event"]["payload"]["role"] = "forged"
    lines[-1] = json.dumps(
        envelope,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _expect_exception(
        agent_run_journal.AgentRunJournalCorrupt,
        journal.records,
    )


def _probe_semantic_checkpoint_forgery(root: Path) -> None:
    contract = _compile(
        root,
        task="Review the runtime",
        pipeline_name="review",
        blocks=agent_blocks.review_pipeline(),
        approval_mode="never",
        nonce="semantic-checkpoint-reject",
    )
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=root / "semantic-checkpoint.jsonl",
    )
    journal.block_started(0, contract.blocks[0].role)
    _expect_exception(
        agent_run_journal.AgentRunJournalCorrupt,
        lambda: journal.block_finished(
            ordinal=0,
            role=contract.blocks[0].role,
            status="complete",
            verified=True,
            context_digest=agent_run_journal.digest_text("forged"),
            snapshot=_snapshot(),
        ),
    )


def _probe_protected_contract_journal_receipts(root: Path) -> None:
    class FailBeforeAnchorOnce(grace_key_store.StaticKeyStore):
        fail_next_anchor = False

        def compare_and_set(
            self,
            journal_id: str,
            *,
            expected_digest: str | None,
            value: bytes,
        ) -> bool:
            if self.fail_next_anchor:
                self.fail_next_anchor = False
                raise RuntimeError("injected pre-anchor failure")
            return super().compare_and_set(
                journal_id,
                expected_digest=expected_digest,
                value=value,
            )

    store = FailBeforeAnchorOnce({PRIVACY_KEY_LABEL: b"n" * 32})
    contract = _compile(
        root,
        task="yes",
        pipeline_name="review",
        blocks=agent_blocks.review_pipeline(),
        approval_mode="never",
        nonce="protected-journal-recovery",
        protected=True,
        receipt_key_store=store,
    )
    if (
        not contract.protected_digests
        or contract.sensitive_digests.scheme != "hmac-sha256-v1"
        or contract.sensitive_digests.key_backend != "static"
        or contract.task_digest == hashlib.sha256(b"yes").hexdigest()
    ):
        raise AgentRuntimeBenchmarkError("protected run contract is not keyed and source bound")
    path = root / "protected-journal-recovery.jsonl"
    journal = agent_run_journal.AgentRunJournal.create(
        contract,
        path=path,
        receipt_key_store=store,
        receipt_anchor_store=store,
        protected_expected=True,
    )
    journal.block_started(0, contract.blocks[0].role)
    journal.model_round_started(0, 0, prompt_tokens=32)
    journal.model_round_completed(
        0,
        0,
        status="completed",
        tool_call_count=1,
        response_digest=agent_run_journal.digest_text("yes"),
    )
    raw = path.read_text(encoding="utf-8")
    if '"yes"' in raw or hashlib.sha256(b"yes").hexdigest() in raw:
        raise AgentRuntimeBenchmarkError("protected Agent journal persisted low-entropy plaintext or hash")
    prior_anchor = store.load(journal.anchor_id)
    before_failure = path.read_bytes()
    intent_canary = "NATHAN_PROTECTED_INTENT_CANARY"
    intent_path = f"{intent_canary}.txt"
    intent_content = f"{intent_canary}-content"
    intent_call_id = f"{intent_canary}-call"
    intent_target = f"workspace:{intent_path}"
    store.fail_next_anchor = True
    _expect_exception(
        agent_run_journal.AgentRunJournalError,
        lambda: journal.tool_intent(
            ordinal=0,
            round_number=0,
            tool_index=0,
            action="write_file",
            args={"path": intent_path, "content": intent_content},
            call_id=intent_call_id,
            mutating=True,
            idempotency="idempotent",
            target=intent_target,
        ),
    )
    after_failure = path.read_bytes()
    if after_failure == before_failure or store.load(journal.anchor_id) != prior_anchor:
        raise AgentRuntimeBenchmarkError("protected journal crash window was not preserved")
    protected_values = (
        intent_canary,
        intent_path,
        intent_content,
        intent_call_id,
        intent_target,
    )
    raw_receipts = {value.encode("utf-8") for value in protected_values} | {
        hashlib.sha256(value.encode("utf-8")).hexdigest().encode("ascii") for value in protected_values
    }
    raw_receipts.add(
        hashlib.sha256(_canonical({"path": intent_path, "content": intent_content})).hexdigest().encode("ascii")
    )
    if any(candidate in after_failure for candidate in raw_receipts):
        raise AgentRuntimeBenchmarkError("protected journal crash window persisted plaintext or an unkeyed hash")
    loaded = agent_run_journal.AgentRunJournal.load(
        contract.run_nonce,
        path=path,
        receipt_key_store=store,
        receipt_anchor_store=store,
        protected_expected=True,
    )
    if path.read_bytes() != after_failure or store.load(journal.anchor_id) != prior_anchor:
        raise AgentRuntimeBenchmarkError("protected journal recovery inspection mutated state")
    state = loaded.resume_state()
    if state.can_resume or state.uncertain_mutation_steps != ("b0-r0-t0",):
        raise AgentRuntimeBenchmarkError("protected journal lost its uncertain mutation boundary")
    loaded.synchronize_anchor()
    if store.load(journal.anchor_id) == prior_anchor or loaded.records()[-1].kind != "tool_intent":
        raise AgentRuntimeBenchmarkError("protected journal anchor recovery did not converge")
    recovered_bytes = path.read_bytes()
    if any(candidate in recovered_bytes for candidate in raw_receipts):
        raise AgentRuntimeBenchmarkError("protected journal recovery persisted plaintext or an unkeyed hash")

    rollback_contract = _compile(
        root,
        task="Review protected rollback",
        pipeline_name="review",
        blocks=agent_blocks.review_pipeline(),
        approval_mode="never",
        nonce="protected-journal-rollback",
        protected=True,
        receipt_key_store=store,
    )
    rollback_path = root / "protected-journal-rollback.jsonl"
    rollback = agent_run_journal.AgentRunJournal.create(
        rollback_contract,
        path=rollback_path,
        receipt_key_store=store,
        receipt_anchor_store=store,
        protected_expected=True,
    )
    rollback.block_started(0, rollback_contract.blocks[0].role)
    lines = rollback_path.read_text(encoding="utf-8").splitlines()
    rollback_path.write_text(lines[0] + "\n", encoding="utf-8")
    rolled_back = rollback_path.read_bytes()
    _expect_exception(
        agent_run_journal.AgentRunJournalCorrupt,
        lambda: agent_run_journal.AgentRunJournal.load(
            rollback_contract.run_nonce,
            path=rollback_path,
            receipt_key_store=store,
            receipt_anchor_store=store,
            protected_expected=True,
        ),
    )
    if rollback_path.read_bytes() != rolled_back:
        raise AgentRuntimeBenchmarkError("protected journal rollback rejection repaired evidence")


def _probe_protected_thread_projection_and_recovery(root: Path) -> None:
    class FailAfterAnchorOnce(grace_key_store.StaticKeyStore):
        fail_after_anchor = True

        def compare_and_set(
            self,
            journal_id: str,
            *,
            expected_digest: str | None,
            value: bytes,
        ) -> bool:
            changed = super().compare_and_set(
                journal_id,
                expected_digest=expected_digest,
                value=value,
            )
            if changed and self.fail_after_anchor:
                self.fail_after_anchor = False
                raise RuntimeError("injected post-anchor failure")
            return changed

    store = FailAfterAnchorOnce({PRIVACY_KEY_LABEL: b"t" * 32})
    authority = ElsieReceiptAuthority.from_key_store(store=store)
    path = root / "protected-agent-threads.json"
    canary = "NATHAN_PROTECTED_THREAD_CANARY"
    _expect_exception(
        RuntimeError,
        lambda: agent_threads.create_thread(
            canary,
            start_turn=True,
            protected=True,
            receipt_authority=authority,
            anchor_store=store,
            path=path,
        ),
    )
    pending = agent_threads._pending_store_path(path)
    if path.exists() or not pending.is_file():
        raise AgentRuntimeBenchmarkError("protected thread post-anchor stage was not retained")
    _expect_exception(
        ElsieReceiptError,
        lambda: agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
            anchor_store=store,
        ),
    )
    if not agent_threads.prepare_protected_thread_store(
        path,
        receipt_authority=authority,
        anchor_store=store,
    ):
        raise AgentRuntimeBenchmarkError("protected thread recovery did not publish its anchored stage")
    records = agent_threads.load_threads(
        path,
        protected=True,
        receipt_authority=authority,
        anchor_store=store,
    )
    if (
        len(records) != 1
        or records[0]["task"]
        or not re.fullmatch(
            r"hmac-sha256:[0-9a-f]{64}",
            str(records[0]["task_receipt"]),
        )
    ):
        raise AgentRuntimeBenchmarkError("protected thread task was not projected to a keyed receipt")
    first = path.read_bytes()
    record = records[0]
    finished = agent_threads.finish_turn(
        str(record["id"]),
        status="complete",
        output=f"output-{canary}",
        error=f"error-{canary}",
        blocks=[
            {
                "role": "review",
                "status": "complete",
                "output": f"block-{canary}",
                "context_output": f"context-{canary}",
                "successful_writes": [f"{canary}.py"],
            }
        ],
        workspace={
            "available": True,
            "workspace_root": f"/{canary}",
            "repository_root": f"/{canary}",
            "branch": canary,
            "head": "a" * 40,
            "status": canary,
            "status_digest": "b" * 64,
            "tracked_diff_digest": "c" * 64,
            "untracked_digest": "d" * 64,
        },
        protected=True,
        receipt_authority=authority,
        anchor_store=store,
        path=path,
    )
    rendered = path.read_text(encoding="utf-8")
    if canary in rendered or finished["task"] or finished["output"] or finished["error"]:
        raise AgentRuntimeBenchmarkError("protected thread projection persisted a plaintext body")
    receipt_count = sum(isinstance(value, str) and value.startswith("hmac-sha256:") for value in _walk_values(finished))
    if receipt_count < 4:
        raise AgentRuntimeBenchmarkError("protected thread projection omitted keyed receipts")
    latest = path.read_bytes()
    if latest == first:
        raise AgentRuntimeBenchmarkError("protected thread terminal state was not persisted")
    path.write_bytes(first)
    _expect_exception(
        ElsieReceiptError,
        lambda: agent_threads.load_threads(
            path,
            protected=True,
            receipt_authority=authority,
            anchor_store=store,
        ),
    )
    if path.read_bytes() != first:
        raise AgentRuntimeBenchmarkError("protected thread rollback rejection rewrote the store")


def _walk_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _walk_values(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in _walk_values(nested)]
    return [value]


def _probe_echo_agent_preflight_refusal(root: Path) -> None:
    del root

    class NoWorkClient:
        calls = 0

        def chat(self, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("model work started before Echo preflight")

    store = grace_key_store.StaticKeyStore({PRIVACY_KEY_LABEL: b"p" * 32})
    cfg = Config(echo_veil_enabled=True, echo_veil_protection="required")
    canary = "NATHAN_PREFLIGHT_PRIVATE_CANARY"
    client = NoWorkClient()
    errors: list[str] = []
    work_events: list[str] = []
    original_preflight = elsie_echo_preflight.prepare_echo_auxiliary_state
    original_begin = agent_pipeline.reflex.begin_agent_pipeline
    original_create_thread = agent_threads.create_thread
    original_show_error = agent_pipeline.show_error

    def refuse(
        _cfg: Config,
        *,
        receipt_key_store: Any | None = None,
        receipt_anchor_store: Any | None = None,
    ) -> dict[str, Any]:
        if receipt_key_store is not store or receipt_anchor_store is not store:
            raise AssertionError("static preflight stores were not injected")
        raise elsie_echo_preflight.EchoAuxiliaryPreflightError(canary)

    try:
        setattr(elsie_echo_preflight, "prepare_echo_auxiliary_state", refuse)
        setattr(agent_pipeline.reflex, "begin_agent_pipeline", lambda *_args, **_kwargs: work_events.append("pipeline"))
        setattr(agent_threads, "create_thread", lambda *_args, **_kwargs: work_events.append("thread"))
        setattr(agent_pipeline, "show_error", errors.append)
        pipeline = agent_pipeline.run_agent_pipeline(
            "Review protected state",
            cfg,
            client,
            pipeline_name="review",
            _receipt_key_store=store,
            _receipt_anchor_store=store,
        )
        team = agent_pipeline.run_agent_team(
            "Review protected state",
            cfg,
            client,
            roles=["reviewer", "critic"],
            _receipt_key_store=store,
            _receipt_anchor_store=store,
        )
    finally:
        setattr(elsie_echo_preflight, "prepare_echo_auxiliary_state", original_preflight)
        setattr(agent_pipeline.reflex, "begin_agent_pipeline", original_begin)
        setattr(agent_threads, "create_thread", original_create_thread)
        setattr(agent_pipeline, "show_error", original_show_error)
    if (
        pipeline.status != "failed"
        or team.status != "failed"
        or canary in pipeline.error
        or canary in team.error
        or client.calls
        or work_events
        or errors != [pipeline.error, team.error]
    ):
        raise AgentRuntimeBenchmarkError("Echo Agent boundary did not refuse before work")


def _probe_provider_tool_protocol(root: Path) -> None:
    del root
    state = nathan_provider_protocol.ProviderToolLoopState(loop_id="provider-probe")
    state.begin_model_round(0)
    state.record_model_event("content")
    state.record_model_event("reasoning")
    state.record_model_event("tool")
    calls = state.complete_model_round(
        [
            {
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                }
            },
            {
                "id": "xai-probe",
                "function": {
                    "name": "search_files",
                    "arguments": '{"query":"needle"}',
                },
            },
            {
                "id": "gemini-probe",
                "thought_signature": "opaque",
                "function": {
                    "name": "git_diff",
                    "arguments": {},
                },
            },
        ]
    )
    if (
        len({call["id"] for call in calls}) != 3
        or calls[1]["function"]["arguments"] != {"query": "needle"}
        or calls[2].get("thought_signature") != "opaque"
    ):
        raise AgentRuntimeBenchmarkError("provider fixtures did not normalize canonically")
    state.begin_tool_batch()
    for call in calls:
        state.record_tool_dispatch(
            str(call["id"]),
            mutating=False,
        )
        state.record_tool_result(str(call["id"]))
    _expect_exception(
        nathan_provider_protocol.ProviderToolProtocolError,
        lambda: state.record_tool_result(str(calls[0]["id"])),
    )
    state.finish_tool_batch()
    state.begin_model_round(1)
    state.complete_model_round([])
    state.finish_without_tools()
    unsafe = nathan_provider_protocol.ProviderToolLoopState(loop_id="fallback-probe")
    unsafe.begin_model_round(0)
    mutation_calls = unsafe.complete_model_round(
        [
            {
                "id": "mutation-probe",
                "function": {
                    "name": "write_file",
                    "arguments": {},
                },
            }
        ]
    )
    unsafe.begin_tool_batch()
    unsafe.record_tool_dispatch(
        str(mutation_calls[0]["id"]),
        mutating=True,
    )
    unsafe.interrupt("connection lost")
    _expect_exception(
        nathan_provider_protocol.ProviderToolProtocolError,
        unsafe.assert_provider_fallback_safe,
    )


def _probe_output_verifier(root: Path) -> None:
    del root
    block = agent_blocks.AgentBlock(
        role="review",
        prompt="review",
    )
    block.output = "Looks good"
    if agent_pipeline._block_output_is_verified(block):
        raise AgentRuntimeBenchmarkError("unstructured output bypassed the verifier")
    block.output = "## Block Output\nGrounded evidence."
    if not agent_pipeline._block_output_is_verified(block):
        raise AgentRuntimeBenchmarkError("valid structured output failed verification")
    unverified = agent_blocks.AgentBlock(
        role="implement",
        prompt="implement",
        requires_change=True,
        status="partial",
        successful_writes=["runtime.py"],
        verification_warning="Git unavailable",
    )
    final = agent_blocks.AgentBlock(
        role="final",
        prompt="final",
        status="complete",
        output="## Block Output\nImplemented the runtime change.",
    )
    if agent_pipeline._final_output_claims_are_grounded(
        final,
        [unverified],
    ):
        raise AgentRuntimeBenchmarkError("unverified completion claim bypassed grounding")


def _probe_verifier_first_write_completion(root: Path) -> None:
    del root
    unavailable = git_evidence.GitSnapshot(
        False,
        "not a Git repository",
        None,
        "",
        "",
        (),
    )
    block = agent_blocks.AgentBlock(
        role="implement",
        prompt="implement",
        requires_change=True,
        status="complete",
        output="## Block Output\nImplemented.",
        successful_writes=["runtime.py"],
    )
    agent_pipeline.enforce_required_change_contract(
        block,
        unavailable,
        unavailable,
    )
    if block.status != "partial" or block.status_code != "verification_unavailable" or "UNVERIFIED" not in block.output:
        raise AgentRuntimeBenchmarkError("recorded write bypassed final-state verification")


def _probe_multi_signal_routing(root: Path) -> None:
    del root
    route = task_router.route_task("Review then fix the bug and publish the package")
    if (
        not route.mutation_intent
        or not route.external_side_effect
        or route.read_only
        or route.risk != "high"
        or not {"review", "coding", "mutation", "external_side_effect"}.issubset(route.signals)
    ):
        raise AgentRuntimeBenchmarkError("multi-signal routing failed to preserve the highest risk")


PROBES: tuple[
    tuple[str, Callable[[Path], None]],
    ...,
] = (
    ("approval_mode_separation", _probe_approval_mode_separation),
    ("read_only_mutation_rejection", _probe_read_only_mutation_rejection),
    ("authority_drift_rejection", _probe_authority_drift),
    ("prompt_and_token_binding", _probe_prompt_and_token_binding),
    ("bounded_provenance_context", _probe_context_boundary),
    ("verified_checkpoint_resume", _probe_verified_resume),
    ("workspace_drift_rejection", _probe_workspace_drift),
    ("uncertain_mutation_rejection", _probe_uncertain_mutation),
    ("journal_hash_tamper_rejection", _probe_journal_tamper),
    (
        "semantic_checkpoint_forgery_rejection",
        _probe_semantic_checkpoint_forgery,
    ),
    (
        "protected_contract_journal_recovery",
        _probe_protected_contract_journal_receipts,
    ),
    (
        "protected_thread_projection_recovery",
        _probe_protected_thread_projection_and_recovery,
    ),
    ("echo_agent_preflight_refusal", _probe_echo_agent_preflight_refusal),
    ("balanced_provider_tool_protocol", _probe_provider_tool_protocol),
    ("structured_output_verifier", _probe_output_verifier),
    (
        "verifier_first_write_completion",
        _probe_verifier_first_write_completion,
    ),
    ("multi_signal_risk_routing", _probe_multi_signal_routing),
)


def _run_probes(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for probe_id, operation in PROBES:
        try:
            operation(root)
        except Exception as exc:
            rows.append(
                {
                    "id": probe_id,
                    "passed": False,
                    "failure_code": type(exc).__name__,
                }
            )
        else:
            rows.append(
                {
                    "id": probe_id,
                    "passed": True,
                    "failure_code": "",
                }
            )
    return rows


def _generated_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if _UTC_RE.fullmatch(value) is None:
        raise AgentRuntimeBenchmarkError("generated_at must be canonical UTC")
    return value


def run_benchmark(
    *,
    contract_repetitions: int = 101,
    context_repetitions: int = 101,
    checkpoint_repetitions: int = 31,
    workload_repetitions: int = 31,
    warmups: int = 5,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Run the model-free benchmark and return source-bound evidence."""

    for label, value in (
        ("contract_repetitions", contract_repetitions),
        ("context_repetitions", context_repetitions),
        ("checkpoint_repetitions", checkpoint_repetitions),
        ("workload_repetitions", workload_repetitions),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not MIN_LATENCY_SAMPLES <= value <= 10_000:
            raise AgentRuntimeBenchmarkError(f"{label} must be an integer from {MIN_LATENCY_SAMPLES} to 10000")
    if isinstance(warmups, bool) or not isinstance(warmups, int) or not 0 <= warmups <= 100:
        raise AgentRuntimeBenchmarkError("warmups must be an integer from 0 to 100")

    source_digest, source_snapshot = _capture_source_tree()

    with tempfile.TemporaryDirectory(prefix="algo-agent-runtime-benchmark-") as raw_root:
        root = Path(raw_root)
        probes = _run_probes(root)
        _verify_source_tree_snapshot(source_snapshot)
        contract_latency = _measure(
            lambda index: _compile(
                root,
                task="Review then fix the failing login test",
                pipeline_name="code-change",
                blocks=agent_blocks.code_change_pipeline(),
                approval_mode="interactive",
                nonce=f"latency-contract-{index:08d}",
            ),
            repetitions=contract_repetitions,
            warmups=warmups,
        )
        sources = _context_sources()
        context_latency = _measure(
            lambda _index: agent_context.build_agent_context(
                "Review the runtime for correctness",
                sources,
                max_tokens=2_048,
            ),
            repetitions=context_repetitions,
            warmups=warmups,
        )
        checkpoint_latency = _measure(
            lambda index: _checkpoint_cycle(root, index=index),
            repetitions=checkpoint_repetitions,
            warmups=warmups,
        )
        workload_rows = [
            _frozen_agent_workload(
                root,
                index=100_000 + index,
            )
            for index in range(warmups + workload_repetitions)
        ][warmups:]

    performance = {
        "contract_compile": contract_latency,
        "context_broker": context_latency,
        "checkpoint_resume": checkpoint_latency,
        "agent_workload_ttfa": _latency_summary([float(row["ttfa_ms"]) for row in workload_rows]),
        "agent_workload_total": _latency_summary([float(row["total_ms"]) for row in workload_rows]),
    }
    workload_count = len(workload_rows)
    verifier_passed = sum(int(row["verifier_passed"]) for row in workload_rows)
    verifier_total = sum(int(row["verifier_total"]) for row in workload_rows)
    effectiveness = {
        "runs": workload_count,
        "task_pass_rate": (sum(row["task_passed"] is True for row in workload_rows) / workload_count),
        "verifier_pass_rate": (verifier_passed / verifier_total),
        "policy_escapes": sum(int(row["policy_escapes"]) for row in workload_rows),
        "unverified_completions": sum(int(row["unverified_completions"]) for row in workload_rows),
        "duplicate_mutations": sum(int(row["duplicate_mutations"]) for row in workload_rows),
        "crash_resume_rate": (sum(row["crash_resume_passed"] is True for row in workload_rows) / workload_count),
        "protocol_correctness_rate": (sum(row["protocol_correct"] is True for row in workload_rows) / workload_count),
        "context_usefulness_rate": (sum(row["context_useful"] is True for row in workload_rows) / workload_count),
        "context_token_utilization": {
            "p50": round(
                statistics.median(float(row["context_utilization"]) for row in workload_rows),
                9,
            ),
            "p95": round(
                _percentile(
                    [float(row["context_utilization"]) for row in workload_rows],
                    0.95,
                ),
                9,
            ),
            "tokens_used": sum(int(row["context_tokens_used"]) for row in workload_rows),
            "tokens_available": sum(int(row["context_tokens_max"]) for row in workload_rows),
        },
        "workloads": workload_rows,
    }
    passed = sum(row["passed"] is True for row in probes)
    correctness_rate = passed / len(probes)
    gates: dict[str, dict[str, Any]] = {
        "correctness": {
            "threshold": 1.0,
            "observed": correctness_rate,
            "passed": correctness_rate == 1.0,
        },
        "task_pass_rate": {
            "threshold": 1.0,
            "observed": effectiveness["task_pass_rate"],
            "passed": effectiveness["task_pass_rate"] == 1.0,
        },
        "verifier_pass_rate": {
            "threshold": 1.0,
            "observed": effectiveness["verifier_pass_rate"],
            "passed": effectiveness["verifier_pass_rate"] == 1.0,
        },
        "policy_escapes": {
            "threshold": 0,
            "observed": effectiveness["policy_escapes"],
            "passed": effectiveness["policy_escapes"] == 0,
        },
        "unverified_completions": {
            "threshold": 0,
            "observed": effectiveness["unverified_completions"],
            "passed": effectiveness["unverified_completions"] == 0,
        },
        "duplicate_mutations": {
            "threshold": 0,
            "observed": effectiveness["duplicate_mutations"],
            "passed": effectiveness["duplicate_mutations"] == 0,
        },
        "crash_resume_rate": {
            "threshold": 1.0,
            "observed": effectiveness["crash_resume_rate"],
            "passed": effectiveness["crash_resume_rate"] == 1.0,
        },
        "protocol_correctness_rate": {
            "threshold": 1.0,
            "observed": effectiveness["protocol_correctness_rate"],
            "passed": effectiveness["protocol_correctness_rate"] == 1.0,
        },
        "context_usefulness_rate": {
            "threshold": 1.0,
            "observed": effectiveness["context_usefulness_rate"],
            "passed": effectiveness["context_usefulness_rate"] == 1.0,
        },
    }
    for metric, threshold in LATENCY_THRESHOLDS_MS.items():
        observed = float(performance[metric]["p95_ms"])
        gates[f"{metric}_p95_ms"] = {
            "threshold": threshold,
            "observed": observed,
            "passed": observed <= threshold,
        }
    status = "pass" if all(gate["passed"] is True for gate in gates.values()) else "fail"
    _verify_source_tree_snapshot(source_snapshot)
    if source_tree_digest() != source_digest:
        raise AgentRuntimeBenchmarkError("benchmark source changed during qualification")
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "benchmark": BENCHMARK_ID,
        "created_at": _generated_at(generated_at),
        "source_revision": _git_revision(),
        "source_tree_sha256": source_digest,
        "environment": {
            "operating_system": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "protocol": {
            "model_calls": 0,
            "network_calls": 0,
            "synthetic_runtime_microbenchmark": True,
            "clock": "time.perf_counter_ns",
            "warmups": warmups,
            "contract_repetitions": contract_repetitions,
            "context_repetitions": context_repetitions,
            "checkpoint_repetitions": checkpoint_repetitions,
            "workload_repetitions": workload_repetitions,
        },
        "correctness": {
            "passed": passed,
            "total": len(probes),
            "pass_rate": correctness_rate,
            "probes": probes,
        },
        "performance": performance,
        "effectiveness": effectiveness,
        "gates": gates,
        "public_claim_eligible": False,
        "claim": (
            "The source-bound Algo Agent runtime candidate passed every "
            "deterministic hardening probe, including protected keyed-receipt, "
            "rollback/recovery, thread-projection, and preflight-containment "
            "checks, and every frozen end-to-end Agent workload with no policy "
            "escape, unverified completion, or duplicate mutation, while "
            "meeting its stated local TTFA and total-latency ceilings."
        ),
        "limitations": (
            "This is a local model-free runtime benchmark with deterministic "
            "provider and task fixtures, temporary files, and injected static "
            "key and anchor stores. It measures harness coordination and "
            "fail-closed protected-state contracts, not model intelligence, "
            "live provider latency, production Keychain behavior, process or "
            "power-loss recovery, or superiority over OpenClaw, Hermes, or "
            "another harness. Latency has not been independently reproduced, "
            "and the active freeze forbids public benchmark claims."
        ),
    }
    report["report_sha256"] = _digest(report)
    validate_report(report, require_current_source=True)
    return report


def validate_report(
    report: Any,
    *,
    require_current_source: bool,
) -> None:
    """Validate a stored report without trusting its status or aggregates."""

    expected = {
        "schema_version",
        "status",
        "benchmark",
        "created_at",
        "source_revision",
        "source_tree_sha256",
        "environment",
        "protocol",
        "correctness",
        "performance",
        "effectiveness",
        "gates",
        "public_claim_eligible",
        "claim",
        "limitations",
        "report_sha256",
    }
    if not isinstance(report, dict) or set(report) != expected:
        raise AgentRuntimeBenchmarkError("runtime benchmark report fields do not match schema")
    if (
        report["schema_version"] != SCHEMA_VERSION
        or report["benchmark"] != BENCHMARK_ID
        or report["status"] not in {"pass", "fail"}
        or report["public_claim_eligible"] is not False
        or _UTC_RE.fullmatch(str(report["created_at"])) is None
        or _REVISION_RE.fullmatch(str(report["source_revision"])) is None
        or _SHA256_RE.fullmatch(str(report["source_tree_sha256"])) is None
        or _SHA256_RE.fullmatch(str(report["report_sha256"])) is None
    ):
        raise AgentRuntimeBenchmarkError("runtime benchmark report identity is invalid")
    if require_current_source and (report["source_tree_sha256"] != source_tree_digest()):
        raise AgentRuntimeBenchmarkError("runtime benchmark source digest is stale")
    protocol = report["protocol"]
    if (
        not isinstance(protocol, dict)
        or protocol.get("model_calls") != 0
        or protocol.get("network_calls") != 0
        or protocol.get("synthetic_runtime_microbenchmark") is not True
        or any(
            isinstance(protocol.get(field), bool)
            or not isinstance(protocol.get(field), int)
            or not MIN_LATENCY_SAMPLES <= protocol[field] <= 10_000
            for field in (
                "contract_repetitions",
                "context_repetitions",
                "checkpoint_repetitions",
                "workload_repetitions",
            )
        )
    ):
        raise AgentRuntimeBenchmarkError("runtime benchmark protocol is invalid")
    correctness = report["correctness"]
    if not isinstance(correctness, dict):
        raise AgentRuntimeBenchmarkError("runtime benchmark correctness is invalid")
    probes = correctness.get("probes")
    if (
        not isinstance(probes, list)
        or len(probes) != len(PROBES)
        or [row.get("id") for row in probes if isinstance(row, dict)] != [probe_id for probe_id, _operation in PROBES]
        or any(
            not isinstance(row, dict)
            or set(row) != {"id", "passed", "failure_code"}
            or type(row["passed"]) is not bool
            or not isinstance(row["failure_code"], str)
            for row in probes
        )
    ):
        raise AgentRuntimeBenchmarkError("runtime benchmark probes are invalid")
    recomputed_passed = sum(row["passed"] is True for row in probes)
    recomputed_rate = recomputed_passed / len(probes)
    if (
        correctness.get("passed") != recomputed_passed
        or correctness.get("total") != len(probes)
        or correctness.get("pass_rate") != recomputed_rate
    ):
        raise AgentRuntimeBenchmarkError("runtime benchmark correctness aggregate is invalid")
    performance = report["performance"]
    if not isinstance(performance, dict) or set(performance) != set(LATENCY_THRESHOLDS_MS):
        raise AgentRuntimeBenchmarkError("runtime benchmark performance fields are invalid")
    expected_sample_counts = {
        "contract_compile": protocol["contract_repetitions"],
        "context_broker": protocol["context_repetitions"],
        "checkpoint_resume": protocol["checkpoint_repetitions"],
        "agent_workload_ttfa": protocol["workload_repetitions"],
        "agent_workload_total": protocol["workload_repetitions"],
    }
    for metric, row in performance.items():
        if (
            not isinstance(row, dict)
            or set(row) != {"samples", "p50_ms", "p95_ms", "max_ms"}
            or isinstance(row["samples"], bool)
            or not isinstance(row["samples"], int)
            or row["samples"] != expected_sample_counts[metric]
            or any(
                isinstance(row[field], bool)
                or not isinstance(row[field], (int, float))
                or not math.isfinite(float(row[field]))
                or float(row[field]) < 0
                for field in ("p50_ms", "p95_ms", "max_ms")
            )
            or not (float(row["p50_ms"]) <= float(row["p95_ms"]) <= float(row["max_ms"]))
        ):
            raise AgentRuntimeBenchmarkError(f"runtime benchmark latency is invalid: {metric}")
    effectiveness = report["effectiveness"]
    expected_effectiveness_fields = {
        "runs",
        "task_pass_rate",
        "verifier_pass_rate",
        "policy_escapes",
        "unverified_completions",
        "duplicate_mutations",
        "crash_resume_rate",
        "protocol_correctness_rate",
        "context_usefulness_rate",
        "context_token_utilization",
        "workloads",
    }
    if not isinstance(effectiveness, dict) or set(effectiveness) != expected_effectiveness_fields:
        raise AgentRuntimeBenchmarkError("runtime benchmark effectiveness fields are invalid")
    workloads = effectiveness["workloads"]
    expected_workload_fields = {
        "task_passed",
        "verifier_passed",
        "verifier_total",
        "policy_escapes",
        "unverified_completions",
        "duplicate_mutations",
        "crash_resume_passed",
        "protocol_correct",
        "context_useful",
        "context_tokens_used",
        "context_tokens_max",
        "context_utilization",
        "ttfa_ms",
        "total_ms",
    }
    if not isinstance(workloads, list) or len(workloads) != protocol["workload_repetitions"]:
        raise AgentRuntimeBenchmarkError("runtime benchmark workload count is invalid")
    for row in workloads:
        if (
            not isinstance(row, dict)
            or set(row) != expected_workload_fields
            or any(
                type(row[field]) is not bool
                for field in (
                    "task_passed",
                    "crash_resume_passed",
                    "protocol_correct",
                    "context_useful",
                )
            )
            or any(
                isinstance(row[field], bool) or not isinstance(row[field], int) or row[field] < 0
                for field in (
                    "verifier_passed",
                    "verifier_total",
                    "policy_escapes",
                    "unverified_completions",
                    "duplicate_mutations",
                    "context_tokens_used",
                    "context_tokens_max",
                )
            )
            or row["verifier_total"] < 1
            or row["verifier_passed"] > row["verifier_total"]
            or row["context_tokens_max"] < 1
            or row["context_tokens_used"] > row["context_tokens_max"]
            or any(
                isinstance(row[field], bool)
                or not isinstance(row[field], (int, float))
                or not math.isfinite(float(row[field]))
                or float(row[field]) < 0
                for field in (
                    "context_utilization",
                    "ttfa_ms",
                    "total_ms",
                )
            )
            or not 0 <= float(row["context_utilization"]) <= 1
            or float(row["ttfa_ms"]) > float(row["total_ms"])
            or float(row["context_utilization"])
            != round(
                row["context_tokens_used"] / row["context_tokens_max"],
                9,
            )
        ):
            raise AgentRuntimeBenchmarkError("runtime benchmark workload row is invalid")
    workload_count = len(workloads)
    verifier_passed = sum(int(row["verifier_passed"]) for row in workloads)
    verifier_total = sum(int(row["verifier_total"]) for row in workloads)
    utilization_values = [float(row["context_utilization"]) for row in workloads]
    expected_utilization = {
        "p50": round(
            statistics.median(utilization_values),
            9,
        ),
        "p95": round(
            _percentile(utilization_values, 0.95),
            9,
        ),
        "tokens_used": sum(int(row["context_tokens_used"]) for row in workloads),
        "tokens_available": sum(int(row["context_tokens_max"]) for row in workloads),
    }
    expected_effectiveness = {
        "runs": workload_count,
        "task_pass_rate": (sum(row["task_passed"] is True for row in workloads) / workload_count),
        "verifier_pass_rate": (verifier_passed / verifier_total),
        "policy_escapes": sum(int(row["policy_escapes"]) for row in workloads),
        "unverified_completions": sum(int(row["unverified_completions"]) for row in workloads),
        "duplicate_mutations": sum(int(row["duplicate_mutations"]) for row in workloads),
        "crash_resume_rate": (sum(row["crash_resume_passed"] is True for row in workloads) / workload_count),
        "protocol_correctness_rate": (sum(row["protocol_correct"] is True for row in workloads) / workload_count),
        "context_usefulness_rate": (sum(row["context_useful"] is True for row in workloads) / workload_count),
        "context_token_utilization": expected_utilization,
        "workloads": workloads,
    }
    if effectiveness != expected_effectiveness:
        raise AgentRuntimeBenchmarkError("runtime benchmark effectiveness aggregate is invalid")
    if performance["agent_workload_ttfa"] != _latency_summary(
        [float(row["ttfa_ms"]) for row in workloads]
    ) or performance["agent_workload_total"] != _latency_summary([float(row["total_ms"]) for row in workloads]):
        raise AgentRuntimeBenchmarkError("runtime benchmark workload latency aggregate is invalid")
    gates = report["gates"]
    expected_gate_names = {
        "correctness",
        "task_pass_rate",
        "verifier_pass_rate",
        "policy_escapes",
        "unverified_completions",
        "duplicate_mutations",
        "crash_resume_rate",
        "protocol_correctness_rate",
        "context_usefulness_rate",
        *(f"{metric}_p95_ms" for metric in LATENCY_THRESHOLDS_MS),
    }
    if not isinstance(gates, dict) or set(gates) != expected_gate_names:
        raise AgentRuntimeBenchmarkError("runtime benchmark gates are invalid")
    task_pass_rate = cast(
        float,
        expected_effectiveness["task_pass_rate"],
    )
    verifier_pass_rate = cast(
        float,
        expected_effectiveness["verifier_pass_rate"],
    )
    policy_escape_count = cast(
        int,
        expected_effectiveness["policy_escapes"],
    )
    unverified_completion_count = cast(
        int,
        expected_effectiveness["unverified_completions"],
    )
    duplicate_mutation_count = cast(
        int,
        expected_effectiveness["duplicate_mutations"],
    )
    crash_resume_rate = cast(
        float,
        expected_effectiveness["crash_resume_rate"],
    )
    protocol_correctness_rate = cast(
        float,
        expected_effectiveness["protocol_correctness_rate"],
    )
    context_usefulness_rate = cast(
        float,
        expected_effectiveness["context_usefulness_rate"],
    )
    expected_gates: dict[str, tuple[float, float, bool]] = {
        "correctness": (
            1.0,
            recomputed_rate,
            recomputed_rate == 1.0,
        ),
        "task_pass_rate": (
            1.0,
            task_pass_rate,
            task_pass_rate == 1.0,
        ),
        "verifier_pass_rate": (
            1.0,
            verifier_pass_rate,
            verifier_pass_rate == 1.0,
        ),
        "policy_escapes": (
            0,
            float(policy_escape_count),
            policy_escape_count == 0,
        ),
        "unverified_completions": (
            0,
            float(unverified_completion_count),
            unverified_completion_count == 0,
        ),
        "duplicate_mutations": (
            0,
            float(duplicate_mutation_count),
            duplicate_mutation_count == 0,
        ),
        "crash_resume_rate": (
            1.0,
            crash_resume_rate,
            crash_resume_rate == 1.0,
        ),
        "protocol_correctness_rate": (
            1.0,
            protocol_correctness_rate,
            protocol_correctness_rate == 1.0,
        ),
        "context_usefulness_rate": (
            1.0,
            context_usefulness_rate,
            context_usefulness_rate == 1.0,
        ),
    }
    for metric, threshold in LATENCY_THRESHOLDS_MS.items():
        observed = float(performance[metric]["p95_ms"])
        expected_gates[f"{metric}_p95_ms"] = (
            threshold,
            observed,
            observed <= threshold,
        )
    for gate_name, (
        threshold,
        observed,
        passed,
    ) in expected_gates.items():
        row = gates[gate_name]
        if (
            not isinstance(row, dict)
            or set(row) != {"threshold", "observed", "passed"}
            or row["threshold"] != threshold
            or row["observed"] != observed
            or row["passed"] is not passed
        ):
            raise AgentRuntimeBenchmarkError(f"runtime benchmark gate is invalid: {gate_name}")
    expected_status = "pass" if all(row["passed"] is True for row in gates.values()) else "fail"
    if report["status"] != expected_status:
        raise AgentRuntimeBenchmarkError("runtime benchmark status differs from its gates")
    unsigned = dict(report)
    stored_digest = unsigned.pop("report_sha256")
    if stored_digest != _digest(unsigned):
        raise AgentRuntimeBenchmarkError("runtime benchmark report digest is invalid")
    if len(_canonical(report)) > MAX_REPORT_BYTES:
        raise AgentRuntimeBenchmarkError("runtime benchmark report exceeds its size bound")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-repetitions", type=int, default=101)
    parser.add_argument("--context-repetitions", type=int, default=101)
    parser.add_argument("--checkpoint-repetitions", type=int, default=31)
    parser.add_argument("--warmups", type=int, default=5)
    arguments = parser.parse_args(argv)
    try:
        report = run_benchmark(
            contract_repetitions=arguments.contract_repetitions,
            context_repetitions=arguments.context_repetitions,
            checkpoint_repetitions=arguments.checkpoint_repetitions,
            warmups=arguments.warmups,
        )
    except AgentRuntimeBenchmarkError as exc:
        print(
            json.dumps(
                {
                    "benchmark": BENCHMARK_ID,
                    "status": "fail",
                    "reason_code": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    sys.stdout.buffer.write(
        json.dumps(
            report,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
