"""Source-bound legacy-versus-capsule prompt ablation.

The benchmark is deterministic and model-free.  It proves prompt construction,
registry routing, exact tool-schema selection, and local assembly latency.  It
does not claim provider latency or model-quality improvement; those require the
separate frozen end-to-end harness cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import platform
from pathlib import Path
import re
import statistics
import subprocess
import time
from typing import Any
from unittest.mock import patch

from .. import context_budget, nathan_prompt_capsules, tools
from ..config import Config
from ..tool_context import select_tools_for_prompt_with_receipt
from ..tool_schema import estimate_tool_schema_tokens


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ID = "nathan-prompt-capsule-ablation-v1"
SCHEMA_VERSION = 1
BASELINE_SOURCE_REVISION = "85c877ca0f1573ed8210934d3d6a18264d7070fd"
BASELINE_CREATED_AT = "2026-08-22T19:24:26Z"
IDENTITY_FIXTURE = "## Repo-shipped Product Identity\nControlled identity fixture."
MIN_LATENCY_SAMPLES = 21
DEFAULT_REPETITIONS = 101
MIN_ORDINARY_REDUCTION_PCT = 50.0
MAX_ASSEMBLY_P95_MS = 50.0
MAX_REPORT_BYTES = 512 * 1024
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_ENV_TEXT_RE = re.compile(r"[ -~]{1,256}\Z")

PASS_CLAIM = (
    "The source-bound capsule candidate reduced ordinary controlled interactive system prompts by at least "
    "50 percent while retaining exact selected tool schemas, deterministic routing, and local assembly gates."
)
FAIL_CLAIM = "The capsule candidate did not satisfy every controlled prompt-ablation gate."
LIMITATIONS = (
    "This deterministic local ablation measures prompt assembly and exact tool-schema selection with a "
    "controlled identity and no memory payload. It does not measure model intelligence, provider time to "
    "first token, live task completion, prefix-cache reuse, or cross-harness superiority. Public claims remain "
    "ineligible until the frozen live ablation, memory/Agent qualifications, and release freeze gates pass."
)

SOURCE_PATHS = (
    "algo_cli/config.py",
    "algo_cli/context_budget.py",
    "algo_cli/dorothy_perf_telemetry.py",
    "algo_cli/evals/nathan_prompt_capsule_qualification.py",
    "algo_cli/main.py",
    "algo_cli/nathan_prompt_capsules.py",
    "algo_cli/oliver_slash_dispatch.py",
    "algo_cli/session_commands.py",
    "algo_cli/tool_context.py",
    "algo_cli/tool_schema.py",
    "algo_cli/tools.py",
    "benchmarks/competitors/runner.py",
    "docs/nathan-prompt-capsule-contract.md",
    "scripts/nathan_prompt_capsule_qualification.py",
    "tests/test_nathan_prompt_capsule_qualification.py",
    "tests/test_nathan_prompt_capsules.py",
    "tests/test_nathan_runtime_authority.py",
)


@dataclass(frozen=True)
class Case:
    case_id: str
    message: str
    model: str = "qwen3.6:35b-mlx"
    oneshot: bool = False
    expected_capsules: tuple[str, ...] = ()


CASES = (
    Case("simple", "What do you think?"),
    Case("named_file", "Read README.md and summarize it."),
    Case("code_edit", "Fix the failing parser test in algo_cli/parser.py."),
    Case("pdf", "Review attached-report.pdf and identify discrepancies.", expected_capsules=("pdf_handling",)),
    Case("grok", "Does Grok work in this harness?", model="grok-4", expected_capsules=("grok_xai",)),
    Case(
        "memory_conflict",
        "Recall the project deadline and preserve any conflicting memories.",
        expected_capsules=("memory_administration",),
    ),
    Case(
        "slash_help",
        "How do I use /context and /agent?",
        expected_capsules=("slash_commands", "agent_runtime"),
    ),
    Case(
        "agent_review",
        "/agent review this harness for vulnerabilities",
        expected_capsules=("agent_runtime",),
    ),
    Case("oneshot", "Fix the parser test.", oneshot=True),
)

BASELINE: dict[str, dict[str, Any]] = {
    "simple": {"system_tokens": 2819, "schema_tokens": 1084, "tools": 7},
    "named_file": {"system_tokens": 2819, "schema_tokens": 1328, "tools": 9},
    "code_edit": {"system_tokens": 2819, "schema_tokens": 2129, "tools": 11},
    "pdf": {"system_tokens": 2819, "schema_tokens": 1084, "tools": 7},
    "grok": {"system_tokens": 2817, "schema_tokens": 1216, "tools": 8},
    "memory_conflict": {"system_tokens": 2819, "schema_tokens": 1084, "tools": 7},
    "slash_help": {"system_tokens": 2819, "schema_tokens": 1438, "tools": 8},
    "agent_review": {"system_tokens": 2819, "schema_tokens": 1216, "tools": 8},
    "oneshot": {"system_tokens": 761, "schema_tokens": 2129, "tools": 11},
}


class PromptCapsuleQualificationError(RuntimeError):
    """Raised when evidence is malformed, stale, or fails a gate."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def source_tree_digest() -> str:
    digest = hashlib.sha256()
    for relative in SOURCE_PATHS:
        path = ROOT / relative
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise PromptCapsuleQualificationError(f"source path unavailable: {relative}") from exc
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return "sha256:" + digest.hexdigest()


def source_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    revision = result.stdout.strip()
    if result.returncode != 0 or _REVISION_RE.fullmatch(revision) is None:
        raise PromptCapsuleQualificationError("source revision is unavailable")
    return revision


def _latency_summary(samples: list[float]) -> dict[str, float | int]:
    if len(samples) < MIN_LATENCY_SAMPLES or any(not math.isfinite(value) or value < 0 for value in samples):
        raise PromptCapsuleQualificationError("latency sample is invalid")
    ordered = sorted(samples)
    p95_index = int(0.95 * (len(ordered) - 1))
    return {
        "samples": len(samples),
        "p50_ms": round(statistics.median(samples), 6),
        "p95_ms": round(ordered[p95_index], 6),
        "max_ms": round(max(samples), 6),
    }


def _build_once(case: Case, mode: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    cfg = Config(model=case.model, prompt_capsule_mode=mode)
    cfg.session_summary = ""
    cfg.attempt_ledger = []
    hints = (
        context_budget.prompt_capsule_related_tools(cfg, case.message, oneshot=case.oneshot) if mode != "legacy" else ()
    )
    selected, tool_receipt = select_tools_for_prompt_with_receipt(
        case.message,
        tools.ALL_TOOLS,
        related_tool_names=hints,
    )
    cfg.context_state["tool_context"] = {
        "selected_tools": [tool.__name__ for tool in selected],
        "capsule_bound_tools": [
            item["name"] for item in tool_receipt["selected"] if item["reason"] == "active_capsule"
        ],
    }
    sink = object() if case.oneshot else None
    with (
        patch.object(context_budget.identity, "build_identity_block", return_value=IDENTITY_FIXTURE),
        patch.object(context_budget, "json_sink", return_value=sink),
        patch.object(context_budget, "_memory_prompt_section", return_value=""),
        patch.object(context_budget.perf_telemetry, "record_perf_event", return_value=None),
    ):
        prompt = context_budget.build_system_prompt(cfg, user_message=case.message)
    return (
        prompt,
        dict(cfg.context_state["prompt_capsules"]),
        {
            **tool_receipt,
            "tools": len(selected),
            "schema_tokens": estimate_tool_schema_tokens(selected),
        },
    )


def _measure_case(case: Case, repetitions: int) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for mode in ("legacy", "capsule"):
        samples: list[float] = []
        prompt_digests: set[str] = set()
        selection_shapes: set[tuple[int, int, tuple[str, ...]]] = set()
        prompt = ""
        prompt_receipt: dict[str, Any] = {}
        tool_receipt: dict[str, Any] = {}
        for _ in range(repetitions):
            started = time.perf_counter_ns()
            prompt, prompt_receipt, tool_receipt = _build_once(case, mode)
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
            prompt_digests.add(hashlib.sha256(prompt.encode("utf-8")).hexdigest())
            selection_shapes.add(
                (
                    tool_receipt["tools"],
                    tool_receipt["schema_tokens"],
                    tuple(item["name"] for item in tool_receipt["selected"]),
                )
            )
        rows[mode] = {
            "system_tokens": context_budget.estimate_text_tokens(prompt),
            "prompt_chars": len(prompt),
            "prompt_sha256": "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "schema_tokens": tool_receipt["schema_tokens"],
            "tools": tool_receipt["tools"],
            "assembly": _latency_summary(samples),
            "sent_mode": prompt_receipt["sent_mode"],
            "included_capsules": [item["id"] for item in prompt_receipt.get("included", [])],
            "omitted_capsules": [
                item["id"]
                for item in [
                    *prompt_receipt.get("omitted", []),
                    *prompt_receipt.get("omitted_dynamic", []),
                ]
            ],
            "fallback_reason": prompt_receipt["fallback_reason"],
            "repeat_stable": len(prompt_digests) == 1 and len(selection_shapes) == 1,
        }
    legacy_tokens = rows["legacy"]["system_tokens"]
    candidate_tokens = rows["capsule"]["system_tokens"]
    rows["reduction_pct"] = round(100.0 * (legacy_tokens - candidate_tokens) / max(1, legacy_tokens), 3)
    return rows


def _expected_gates(cases: dict[str, Any]) -> dict[str, bool]:
    ordinary = ("simple", "named_file", "code_edit", "pdf", "memory_conflict")
    return {
        "legacy_baseline_exact": all(
            cases[case_id]["legacy"][key] == expected
            for case_id, expected_row in BASELINE.items()
            for key, expected in expected_row.items()
        ),
        "ordinary_prompt_reduction": all(
            cases[case_id]["reduction_pct"] >= MIN_ORDINARY_REDUCTION_PCT for case_id in ordinary
        ),
        "capsule_routing": all(
            set(case.expected_capsules) <= set(cases[case.case_id]["capsule"]["included_capsules"]) for case in CASES
        ),
        "no_candidate_fallback": all(not row["capsule"]["fallback_reason"] for row in cases.values()),
        "deterministic_repeat": all(
            row[mode]["repeat_stable"] is True for row in cases.values() for mode in ("legacy", "capsule")
        ),
        "schema_budget": all(row["capsule"]["schema_tokens"] <= 2_150 for row in cases.values()),
        "assembly_latency": all(row["capsule"]["assembly"]["p95_ms"] <= MAX_ASSEMBLY_P95_MS for row in cases.values()),
        "oneshot_non_regression": cases["oneshot"]["capsule"]["system_tokens"]
        <= math.ceil(cases["oneshot"]["legacy"]["system_tokens"] * 1.10),
    }


def run_qualification(*, repetitions: int = DEFAULT_REPETITIONS) -> dict[str, Any]:
    if repetitions < MIN_LATENCY_SAMPLES:
        raise PromptCapsuleQualificationError("qualification repetitions are below the minimum")
    nathan_prompt_capsules.validate_registry()
    cases = {case.case_id: _measure_case(case, repetitions) for case in CASES}
    gates = _expected_gates(cases)
    status = "pass" if all(gates.values()) else "fail"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": BENCHMARK_ID,
        "status": status,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_revision": source_revision(),
        "source_tree_sha256": source_tree_digest(),
        "registry_sha256": nathan_prompt_capsules.registry_digest(),
        "baseline": {
            "source_revision": BASELINE_SOURCE_REVISION,
            "created_at": BASELINE_CREATED_AT,
            "cases": BASELINE,
        },
        "protocol": {
            "identity_fixture_sha256": "sha256:" + hashlib.sha256(IDENTITY_FIXTURE.encode()).hexdigest(),
            "case_count": len(CASES),
            "repetitions": repetitions,
            "model_calls": 0,
            "network_calls": 0,
        },
        "environment": {
            "operating_system": platform.platform(),
            "python": platform.python_version(),
        },
        "cases": cases,
        "gates": gates,
        "claim": PASS_CLAIM if status == "pass" else FAIL_CLAIM,
        "limitations": LIMITATIONS,
        "public_claim_eligible": False,
    }
    report["report_sha256"] = _digest(report)
    validate_report(report, require_current_source=True)
    return report


def _is_plain_int(value: Any, *, minimum: int = 0, maximum: int = 1_000_000) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _is_metric(value: Any, *, maximum: float = 1_000_000.0) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value <= maximum


def _validate_latency(value: Any, *, repetitions: int) -> None:
    if type(value) is not dict or set(value) != {"samples", "p50_ms", "p95_ms", "max_ms"}:
        raise PromptCapsuleQualificationError("qualification latency schema is invalid")
    if value["samples"] != repetitions or not all(_is_metric(value[field]) for field in ("p50_ms", "p95_ms", "max_ms")):
        raise PromptCapsuleQualificationError("qualification latency value is invalid")
    if not value["p50_ms"] <= value["p95_ms"] <= value["max_ms"]:
        raise PromptCapsuleQualificationError("qualification latency ordering is invalid")


def _validate_case_rows(cases: Any, *, repetitions: int) -> None:
    expected_case_ids = {case.case_id for case in CASES}
    if type(cases) is not dict or set(cases) != expected_case_ids:
        raise PromptCapsuleQualificationError("qualification cases are invalid")
    known_capsules = {capsule.capsule_id for capsule in nathan_prompt_capsules.CAPSULES}
    mode_keys = {
        "system_tokens",
        "prompt_chars",
        "prompt_sha256",
        "schema_tokens",
        "tools",
        "assembly",
        "sent_mode",
        "included_capsules",
        "omitted_capsules",
        "fallback_reason",
        "repeat_stable",
    }
    for case in CASES:
        row = cases[case.case_id]
        if type(row) is not dict or set(row) != {"legacy", "capsule", "reduction_pct"}:
            raise PromptCapsuleQualificationError("qualification case schema is invalid")
        for mode in ("legacy", "capsule"):
            result = row[mode]
            if type(result) is not dict or set(result) != mode_keys:
                raise PromptCapsuleQualificationError("qualification mode schema is invalid")
            if not (
                _is_plain_int(result["system_tokens"], minimum=1)
                and _is_plain_int(result["prompt_chars"], minimum=1)
                and _is_plain_int(result["schema_tokens"])
                and _is_plain_int(result["tools"])
                and type(result["prompt_sha256"]) is str
                and _SHA256_RE.fullmatch(result["prompt_sha256"]) is not None
                and result["sent_mode"] == mode
                and type(result["fallback_reason"]) is str
                and len(result["fallback_reason"]) <= 256
                and type(result["repeat_stable"]) is bool
            ):
                raise PromptCapsuleQualificationError("qualification mode value is invalid")
            for field in ("included_capsules", "omitted_capsules"):
                capsule_ids = result[field]
                if (
                    type(capsule_ids) is not list
                    or len(capsule_ids) != len(set(capsule_ids))
                    or any(type(item) is not str or item not in known_capsules for item in capsule_ids)
                ):
                    raise PromptCapsuleQualificationError("qualification capsule receipt is invalid")
            if set(result["included_capsules"]) & set(result["omitted_capsules"]):
                raise PromptCapsuleQualificationError("qualification capsule receipt overlaps")
            _validate_latency(result["assembly"], repetitions=repetitions)
        legacy = row["legacy"]
        candidate = row["capsule"]
        if legacy["included_capsules"] or legacy["omitted_capsules"] or legacy["fallback_reason"]:
            raise PromptCapsuleQualificationError("qualification legacy receipt is invalid")
        expected_reduction = round(
            100.0 * (legacy["system_tokens"] - candidate["system_tokens"]) / max(1, legacy["system_tokens"]),
            3,
        )
        if type(row["reduction_pct"]) not in {int, float} or row["reduction_pct"] != expected_reduction:
            raise PromptCapsuleQualificationError("qualification reduction is invalid")


def validate_report(report: Any, *, require_current_source: bool) -> None:
    if type(report) is not dict:
        raise PromptCapsuleQualificationError("qualification report is not an object")
    required = {
        "schema_version",
        "benchmark",
        "status",
        "created_at",
        "source_revision",
        "source_tree_sha256",
        "registry_sha256",
        "baseline",
        "protocol",
        "environment",
        "cases",
        "gates",
        "claim",
        "limitations",
        "public_claim_eligible",
        "report_sha256",
    }
    if set(report) != required:
        raise PromptCapsuleQualificationError("qualification report schema is invalid")
    if (
        report["schema_version"] != SCHEMA_VERSION
        or report["benchmark"] != BENCHMARK_ID
        or report["status"] not in {"pass", "fail"}
        or type(report["created_at"]) is not str
        or _UTC_RE.fullmatch(report["created_at"]) is None
        or type(report["source_revision"]) is not str
        or _REVISION_RE.fullmatch(report["source_revision"]) is None
        or type(report["source_tree_sha256"]) is not str
        or _SHA256_RE.fullmatch(report["source_tree_sha256"]) is None
        or type(report["registry_sha256"]) is not str
        or _SHA256_RE.fullmatch(report["registry_sha256"]) is None
        or report["public_claim_eligible"] is not False
    ):
        raise PromptCapsuleQualificationError("qualification report envelope is invalid")
    if report["baseline"] != {
        "source_revision": BASELINE_SOURCE_REVISION,
        "created_at": BASELINE_CREATED_AT,
        "cases": BASELINE,
    }:
        raise PromptCapsuleQualificationError("qualification baseline is invalid")
    protocol = report["protocol"]
    if (
        type(protocol) is not dict
        or set(protocol)
        != {
            "identity_fixture_sha256",
            "case_count",
            "repetitions",
            "model_calls",
            "network_calls",
        }
        or protocol["identity_fixture_sha256"] != "sha256:" + hashlib.sha256(IDENTITY_FIXTURE.encode()).hexdigest()
        or protocol["case_count"] != len(CASES)
        or not _is_plain_int(protocol["repetitions"], minimum=MIN_LATENCY_SAMPLES, maximum=10_000)
        or not _is_plain_int(protocol["model_calls"], maximum=0)
        or not _is_plain_int(protocol["network_calls"], maximum=0)
    ):
        raise PromptCapsuleQualificationError("qualification protocol is invalid")
    environment = report["environment"]
    if (
        type(environment) is not dict
        or set(environment) != {"operating_system", "python"}
        or any(
            type(environment[field]) is not str or _ENV_TEXT_RE.fullmatch(environment[field]) is None
            for field in environment
        )
    ):
        raise PromptCapsuleQualificationError("qualification environment is invalid")
    _validate_case_rows(report["cases"], repetitions=protocol["repetitions"])
    expected_gates = _expected_gates(report["cases"])
    if report["gates"] != expected_gates:
        raise PromptCapsuleQualificationError("qualification gates are invalid")
    expected_status = "pass" if all(report["gates"].values()) else "fail"
    if report["status"] != expected_status:
        raise PromptCapsuleQualificationError("qualification status is inconsistent")
    if report["claim"] != (PASS_CLAIM if expected_status == "pass" else FAIL_CLAIM):
        raise PromptCapsuleQualificationError("qualification claim is invalid")
    if report["limitations"] != LIMITATIONS:
        raise PromptCapsuleQualificationError("qualification limitations are invalid")
    unsigned = dict(report)
    stored_digest = unsigned.pop("report_sha256")
    if type(stored_digest) is not str or stored_digest != _digest(unsigned):
        raise PromptCapsuleQualificationError("qualification report digest is invalid")
    if require_current_source:
        if report["source_tree_sha256"] != source_tree_digest():
            raise PromptCapsuleQualificationError("qualification source tree is stale")
        if report["registry_sha256"] != nathan_prompt_capsules.registry_digest():
            raise PromptCapsuleQualificationError("qualification registry is stale")


__all__ = [
    "BASELINE",
    "BENCHMARK_ID",
    "CASES",
    "DEFAULT_REPETITIONS",
    "MAX_REPORT_BYTES",
    "PromptCapsuleQualificationError",
    "run_qualification",
    "source_tree_digest",
    "validate_report",
]
