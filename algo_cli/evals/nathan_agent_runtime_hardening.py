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
from typing import Any

from .. import agent_blocks
from .. import agent_context
from .. import agent_pipeline
from .. import agent_run_journal
from .. import git_evidence
from .. import run_contract
from .. import task_router
from ..config import Config


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ID = "nathan-agent-runtime-hardening-v1"
SCHEMA_VERSION = 1
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
    "algo_cli/agent_context.py",
    "algo_cli/agent_pipeline.py",
    "algo_cli/agent_run_journal.py",
    "algo_cli/agent_threads.py",
    "algo_cli/evals/nathan_agent_runtime_hardening.py",
    "algo_cli/nathan_runtime.py",
    "algo_cli/run_contract.py",
    "algo_cli/samuel_policy.py",
    "algo_cli/spawn_budget.py",
    "algo_cli/task_router.py",
    "scripts/nathan_agent_runtime_qualification.py",
    "tests/test_agent_context.py",
    "tests/test_agent_pipeline.py",
    "tests/test_agent_run_journal.py",
    "tests/test_nathan_agent_runtime_hardening.py",
    "tests/test_run_contract.py",
    "tests/test_task_router.py",
)

LATENCY_THRESHOLDS_MS = {
    "contract_compile": 250.0,
    "context_broker": 100.0,
    "checkpoint_resume": 1_000.0,
}


class AgentRuntimeBenchmarkError(RuntimeError):
    """Raised when the benchmark or its stored report fails closed."""


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
        raise AgentRuntimeBenchmarkError(
            "benchmark value is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def source_tree_digest() -> str:
    """Bind evidence to the exact runtime and adversarial test bytes."""

    digest = hashlib.sha256()
    root = ROOT.resolve(strict=True)
    for relative in sorted(SOURCE_PATHS):
        candidate = ROOT / relative
        try:
            before = candidate.lstat()
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AgentRuntimeBenchmarkError(
                f"benchmark source is unavailable: {relative}"
            ) from exc
        if (
            resolved != candidate.absolute()
            or not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= MAX_SOURCE_BYTES
        ):
            raise AgentRuntimeBenchmarkError(
                f"benchmark source boundary rejected: {relative}"
            )
        payload = candidate.read_bytes()
        after = candidate.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
        ):
            raise AgentRuntimeBenchmarkError(
                f"benchmark source changed while reading: {relative}"
            )
        digest.update(relative.encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


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
        raise AgentRuntimeBenchmarkError(
            "benchmark source revision is unavailable"
        )
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
) -> run_contract.RunContract:
    return run_contract.compile_agent_run_contract(
        task=task,
        route=task_router.route_task(task),
        pipeline_name=pipeline_name,
        blocks=blocks,
        cfg=_config(root),
        approval_mode=approval_mode,  # type: ignore[arg-type]
        snapshot=_snapshot(),
        run_nonce=nonce,
        issued_at=FIXED_TIME,
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
        raise AgentRuntimeBenchmarkError(
            "latency repetitions and warmups must be bounded"
        )
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
            response_digest=agent_run_journal.digest_text(
                "verified review"
            ),
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
            context_digest=agent_run_journal.digest_text(
                "## Block Output\nVerified review"
            ),
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
        raise AgentRuntimeBenchmarkError(
            "durable checkpoint did not reconstruct exactly"
        )


def _expect_exception(
    error_type: type[BaseException],
    operation: Callable[[], Any],
) -> None:
    try:
        operation()
    except error_type:
        return
    raise AgentRuntimeBenchmarkError(
        f"expected {error_type.__name__} was not raised"
    )


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
            raise AgentRuntimeBenchmarkError(
                "read-only tool authority changed across approval modes"
            )

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
        raise AgentRuntimeBenchmarkError(
            "approval modes lost their distinct mutation semantics"
        )


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
    altered = agent_run_journal.digest_text(
        blocks[0].prompt + "\nForged authority."
    )
    if original != contract.blocks[0].prompt_digest or altered == original:
        raise AgentRuntimeBenchmarkError(
            "block prompt is not cryptographically bound"
        )
    tracker = run_contract.RunContractTracker(contract)
    tracker.start_block(0)
    _expect_exception(
        run_contract.RunContractViolation,
        lambda: tracker.start_model_round(
            contract.budget.max_prompt_tokens_per_round + 1
        ),
    )
    if tracker.model_rounds or tracker.prompt_tokens:
        raise AgentRuntimeBenchmarkError(
            "rejected prompt budget changed durable counters"
        )


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
        or agent_run_journal.digest_text(bundle.text)
        != bundle.receipt.context_digest
    ):
        raise AgentRuntimeBenchmarkError(
            "context broker exceeded budget or leaked context into receipt"
        )


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
    if (
        not state.workspace_matches(_snapshot())
        or state.workspace_matches(_snapshot(changed=True))
    ):
        raise AgentRuntimeBenchmarkError(
            "resume workspace reconciliation did not fail closed"
        )


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
    if state.can_resume or state.uncertain_mutation_steps != (
        "b0-r0-t0",
    ):
        raise AgentRuntimeBenchmarkError(
            "uncertain mutation did not block resume"
        )


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


def _probe_provider_tool_protocol(root: Path) -> None:
    del root
    state = agent_pipeline.AgentLoopState()
    state.begin_model_round(0)
    state.complete_model_round(1)
    state.begin_tool_batch()
    state.record_tool_result()
    _expect_exception(
        agent_pipeline.AgentLoopProtocolError,
        state.record_tool_result,
    )
    state.finish_tool_batch()
    state.begin_model_round(1)
    state.complete_model_round(0)
    state.finish_without_tools()


def _probe_output_verifier(root: Path) -> None:
    del root
    block = agent_blocks.AgentBlock(
        role="review",
        prompt="review",
    )
    block.output = "Looks good"
    if agent_pipeline._block_output_is_verified(block):
        raise AgentRuntimeBenchmarkError(
            "unstructured output bypassed the verifier"
        )
    block.output = "## Block Output\nGrounded evidence."
    if not agent_pipeline._block_output_is_verified(block):
        raise AgentRuntimeBenchmarkError(
            "valid structured output failed verification"
        )


def _probe_multi_signal_routing(root: Path) -> None:
    del root
    route = task_router.route_task(
        "Review then fix the bug and publish the package"
    )
    if (
        not route.mutation_intent
        or not route.external_side_effect
        or route.read_only
        or route.risk != "high"
        or not {"review", "coding", "mutation", "external_side_effect"}
        .issubset(route.signals)
    ):
        raise AgentRuntimeBenchmarkError(
            "multi-signal routing failed to preserve the highest risk"
        )


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
    ("balanced_provider_tool_protocol", _probe_provider_tool_protocol),
    ("structured_output_verifier", _probe_output_verifier),
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
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if _UTC_RE.fullmatch(value) is None:
        raise AgentRuntimeBenchmarkError(
            "generated_at must be canonical UTC"
        )
    return value


def run_benchmark(
    *,
    contract_repetitions: int = 101,
    context_repetitions: int = 101,
    checkpoint_repetitions: int = 31,
    warmups: int = 5,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Run the model-free benchmark and return source-bound evidence."""

    for label, value in (
        ("contract_repetitions", contract_repetitions),
        ("context_repetitions", context_repetitions),
        ("checkpoint_repetitions", checkpoint_repetitions),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 3 <= value <= 10_000
        ):
            raise AgentRuntimeBenchmarkError(
                f"{label} must be an integer from 3 to 10000"
            )
    if (
        isinstance(warmups, bool)
        or not isinstance(warmups, int)
        or not 0 <= warmups <= 100
    ):
        raise AgentRuntimeBenchmarkError(
            "warmups must be an integer from 0 to 100"
        )

    with tempfile.TemporaryDirectory(
        prefix="algo-agent-runtime-benchmark-"
    ) as raw_root:
        root = Path(raw_root)
        probes = _run_probes(root)
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

    performance = {
        "contract_compile": contract_latency,
        "context_broker": context_latency,
        "checkpoint_resume": checkpoint_latency,
    }
    passed = sum(row["passed"] is True for row in probes)
    correctness_rate = passed / len(probes)
    gates: dict[str, dict[str, Any]] = {
        "correctness": {
            "threshold": 1.0,
            "observed": correctness_rate,
            "passed": correctness_rate == 1.0,
        }
    }
    for metric, threshold in LATENCY_THRESHOLDS_MS.items():
        observed = float(performance[metric]["p95_ms"])
        gates[f"{metric}_p95_ms"] = {
            "threshold": threshold,
            "observed": observed,
            "passed": observed <= threshold,
        }
    status = (
        "pass"
        if all(gate["passed"] is True for gate in gates.values())
        else "fail"
    )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "benchmark": BENCHMARK_ID,
        "created_at": _generated_at(generated_at),
        "source_revision": _git_revision(),
        "source_tree_sha256": source_tree_digest(),
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
        },
        "correctness": {
            "passed": passed,
            "total": len(probes),
            "pass_rate": correctness_rate,
            "probes": probes,
        },
        "performance": performance,
        "gates": gates,
        "claim": (
            "The source-bound Algo Agent runtime candidate passed every "
            "deterministic approval, contract, context, protocol, checkpoint, "
            "tamper, resume, routing, and verifier probe while remaining "
            "within the stated local p95 microbenchmark ceilings."
        ),
        "limitations": (
            "This is a local model-free runtime microbenchmark. It does not "
            "measure model intelligence, provider latency, end-to-end task "
            "quality, production crash or power-loss behavior, or superiority "
            "over OpenClaw, Hermes, or another harness. Latency has not been "
            "independently reproduced."
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
        "gates",
        "claim",
        "limitations",
        "report_sha256",
    }
    if not isinstance(report, dict) or set(report) != expected:
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark report fields do not match schema"
        )
    if (
        report["schema_version"] != SCHEMA_VERSION
        or report["benchmark"] != BENCHMARK_ID
        or report["status"] not in {"pass", "fail"}
        or _UTC_RE.fullmatch(str(report["created_at"])) is None
        or _REVISION_RE.fullmatch(str(report["source_revision"])) is None
        or _SHA256_RE.fullmatch(str(report["source_tree_sha256"])) is None
        or _SHA256_RE.fullmatch(str(report["report_sha256"])) is None
    ):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark report identity is invalid"
        )
    if require_current_source and (
        report["source_tree_sha256"] != source_tree_digest()
    ):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark source digest is stale"
        )
    protocol = report["protocol"]
    if (
        not isinstance(protocol, dict)
        or protocol.get("model_calls") != 0
        or protocol.get("network_calls") != 0
        or protocol.get("synthetic_runtime_microbenchmark") is not True
        or any(
            isinstance(protocol.get(field), bool)
            or not isinstance(protocol.get(field), int)
            or protocol[field] < 3
            for field in (
                "contract_repetitions",
                "context_repetitions",
                "checkpoint_repetitions",
            )
        )
    ):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark protocol is invalid"
        )
    correctness = report["correctness"]
    if not isinstance(correctness, dict):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark correctness is invalid"
        )
    probes = correctness.get("probes")
    if (
        not isinstance(probes, list)
        or len(probes) != len(PROBES)
        or [row.get("id") for row in probes if isinstance(row, dict)]
        != [probe_id for probe_id, _operation in PROBES]
        or any(
            not isinstance(row, dict)
            or set(row) != {"id", "passed", "failure_code"}
            or type(row["passed"]) is not bool
            or not isinstance(row["failure_code"], str)
            for row in probes
        )
    ):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark probes are invalid"
        )
    recomputed_passed = sum(row["passed"] is True for row in probes)
    recomputed_rate = recomputed_passed / len(probes)
    if (
        correctness.get("passed") != recomputed_passed
        or correctness.get("total") != len(probes)
        or correctness.get("pass_rate") != recomputed_rate
    ):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark correctness aggregate is invalid"
        )
    performance = report["performance"]
    if (
        not isinstance(performance, dict)
        or set(performance) != set(LATENCY_THRESHOLDS_MS)
    ):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark performance fields are invalid"
        )
    for metric, row in performance.items():
        if (
            not isinstance(row, dict)
            or set(row) != {"samples", "p50_ms", "p95_ms", "max_ms"}
            or isinstance(row["samples"], bool)
            or not isinstance(row["samples"], int)
            or row["samples"] < 3
            or any(
                isinstance(row[field], bool)
                or not isinstance(row[field], (int, float))
                or not math.isfinite(float(row[field]))
                or float(row[field]) < 0
                for field in ("p50_ms", "p95_ms", "max_ms")
            )
            or not (
                float(row["p50_ms"])
                <= float(row["p95_ms"])
                <= float(row["max_ms"])
            )
        ):
            raise AgentRuntimeBenchmarkError(
                f"runtime benchmark latency is invalid: {metric}"
            )
    gates = report["gates"]
    expected_gate_names = {
        "correctness",
        *(f"{metric}_p95_ms" for metric in LATENCY_THRESHOLDS_MS),
    }
    if not isinstance(gates, dict) or set(gates) != expected_gate_names:
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark gates are invalid"
        )
    expected_gates: dict[str, tuple[float, float, bool]] = {
        "correctness": (1.0, recomputed_rate, recomputed_rate == 1.0)
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
            raise AgentRuntimeBenchmarkError(
                f"runtime benchmark gate is invalid: {gate_name}"
            )
    expected_status = (
        "pass"
        if all(row["passed"] is True for row in gates.values())
        else "fail"
    )
    if report["status"] != expected_status:
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark status differs from its gates"
        )
    unsigned = dict(report)
    stored_digest = unsigned.pop("report_sha256")
    if stored_digest != _digest(unsigned):
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark report digest is invalid"
        )
    if len(_canonical(report)) > MAX_REPORT_BYTES:
        raise AgentRuntimeBenchmarkError(
            "runtime benchmark report exceeds its size bound"
        )


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
