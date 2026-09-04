"""Deterministic deferred-tool and semantic-supersession benchmark.

This benchmark is deliberately model-free: it serializes the exact Responses
tool schemas used by Algo CLI, repeats task-local selection to prove stability,
checks required-tool recall, and measures semantic supersession against a
fixed repeated-read transcript.  Live provider token/latency cells remain the
job of ``benchmarks/competitors/runner.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import statistics
import tempfile
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from ..chatgpt_client import _build_responses_tools
from ..context_budget import estimate_text_tokens
from ..tool_context import select_tools_for_prompt

BENCHMARK_SCHEMA_VERSION = 1
BENCHMARK_NAME = "algo-tool-context-efficiency-v1"
DEFAULT_REPEATS = 7
MIN_REPEATS = 3
MIN_MEDIAN_SCHEMA_REDUCTION_PCT = 60.0
MIN_SCENARIO_SCHEMA_REDUCTION_PCT = 45.0
MIN_SUPERSESSION_REDUCTION_PCT = 75.0
MIN_TYPED_PROGRAM_REDUCTION_PCT = 70.0

SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "code_repair",
        "prompt": "Fix the failing parser and run tests, then verify the diff.",
        "required_tools": ("edit_file", "run_shell", "git_diff"),
    },
    {
        "id": "existing_file_update",
        "prompt": "Update the existing configuration file safely and verify it.",
        "required_tools": ("read_file", "edit_file", "run_shell", "git_diff"),
    },
    {
        "id": "web_research",
        "prompt": "Research the latest release notes on the internet and fetch the source.",
        "required_tools": ("web_search", "web_fetch"),
    },
    {
        "id": "scanned_document",
        "prompt": "Read this PDF and describe its scanned pages.",
        "required_tools": ("read_pdf", "render_pdf_pages", "vision_describe"),
    },
    {
        "id": "harness_retrieval",
        "prompt": "Check harness stats and search the harness for memory guidance.",
        "required_tools": ("harness_stats", "harness_search"),
    },
    {
        "id": "durable_memory",
        "prompt": "Remember that I prefer concise answers.",
        "required_tools": ("remember",),
    },
    {
        "id": "credential_helper",
        "prompt": "Store a credential securely using the credential helper.",
        "required_tools": ("credential_helpers_store",),
    },
    {
        "id": "model_metadata",
        "prompt": "Show metadata for the selected local model.",
        "required_tools": ("model_show",),
    },
    {
        "id": "social_draft",
        "prompt": "Draft and post an update to my X account.",
        "required_tools": ("x_account_draft_post", "x_account_post"),
    },
)


def _tool_names(tools: Sequence[Callable[..., Any]]) -> tuple[str, ...]:
    return tuple(str(getattr(tool, "__name__", "") or "") for tool in tools)


def serialized_tool_schema(tools: Sequence[Callable[..., Any]]) -> str:
    """Serialize tools exactly as the ChatGPT Responses transport does."""

    payload = _build_responses_tools(list(tools)) or []
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def schema_token_estimate(tools: Sequence[Callable[..., Any]]) -> int:
    """Return Algo CLI's deterministic chars/4 schema-token estimate."""

    return estimate_text_tokens(serialized_tool_schema(tools))


def _selection_digest(names: Sequence[str]) -> str:
    encoded = "\0".join(names).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _protocol_fingerprint(messages: Sequence[dict[str, Any]]) -> str:
    """Hash protocol pairing/signatures while intentionally excluding content."""

    rows: list[dict[str, Any]] = []
    for message in messages:
        row: dict[str, Any] = {"role": message.get("role")}
        for key in ("name", "tool_name", "tool_call_id", "thought_signature"):
            if key in message:
                row[key] = message.get(key)
        if message.get("tool_calls"):
            row["tool_calls"] = message.get("tool_calls")
        rows.append(row)
    encoded = json.dumps(rows, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _synthetic_repeated_reads() -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for version in range(5):
        call_id = f"read-{version}"
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "reports/snapshot.log"}),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "name": "read_file",
                "tool_name": "read_file",
                "tool_call_id": call_id,
                "content": f"snapshot version={version}\n" + (f"row-{version}=value\n" * 1_500),
            }
        )
    return messages


def _stats_dict(stats: Any) -> dict[str, Any]:
    if isinstance(stats, dict):
        return dict(stats)
    return {
        key: getattr(stats, key)
        for key in (
            "candidates",
            "superseded",
            "before_tokens",
            "after_tokens",
            "saved_tokens",
            "reduction_pct",
        )
        if hasattr(stats, key)
    }


def _run_supersession_benchmark() -> dict[str, Any]:
    from ..evelyn_context_supersession import supersede_tool_results

    messages = _synthetic_repeated_reads()
    protocol_before = _protocol_fingerprint(messages)
    latest_before = str(messages[-1]["content"])
    before_tokens = sum(estimate_text_tokens(str(message.get("content") or "")) for message in messages)

    first_stats = _stats_dict(supersede_tool_results(messages, cwd="."))
    protocol_after = _protocol_fingerprint(messages)
    latest_after = str(messages[-1]["content"])
    after_tokens = sum(estimate_text_tokens(str(message.get("content") or "")) for message in messages)
    second_stats = _stats_dict(supersede_tool_results(messages, cwd="."))
    receipt_count = sum(
        str(message.get("content") or "").startswith("[Algo superseded result receipt v1")
        for message in messages
    )
    reduction_pct = 100.0 * (before_tokens - after_tokens) / max(1, before_tokens)
    return {
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "saved_tokens": before_tokens - after_tokens,
        "reduction_pct": round(reduction_pct, 3),
        "superseded": int(first_stats.get("superseded") or 0),
        "receipt_count": receipt_count,
        "latest_result_preserved": latest_before == latest_after,
        "protocol_pairing_preserved": protocol_before == protocol_after,
        "idempotent": (
            int(second_stats.get("superseded") or 0) == 0
            and int(second_stats.get("saved_tokens") or 0) == 0
        ),
        "first_pass_stats": first_stats,
        "second_pass_stats": second_stats,
    }


def _run_typed_program_benchmark(repeats: int) -> dict[str, Any]:
    """Measure how much raw intermediate data a typed program keeps out of context."""

    from ..config import Config
    from ..grace_key_store import StaticKeyStore
    from ..nathan_program_runtime import (
        ProgramArtifactStore,
        authorization_for_actions,
        execute_program,
        verify_receipt_chain,
    )

    rows = [
        {
            "name": f"item-{index:04d}",
            "score": index,
            "ready": index % 2 == 0,
            "category": f"group-{index % 7}",
        }
        for index in range(420)
    ]
    raw = json.dumps({"rows": rows}, ensure_ascii=False, separators=(",", ":"))
    baseline_tokens = estimate_text_tokens(raw)
    plan = {
        "version": 1,
        "steps": [
            {"id": "source", "kind": "action", "action": "read_file", "args": {"path": "rows.json"}},
            {"id": "parsed", "kind": "transform", "op": "json_parse", "input": {"$ref": "source"}},
            {"id": "rows", "kind": "transform", "op": "get", "input": {"$ref": "parsed"}, "args": {"path": ["rows"]}},
            {"id": "ready", "kind": "transform", "op": "filter_eq", "input": {"$ref": "rows"}, "args": {"path": ["ready"], "equals": True}},
            {"id": "ranked", "kind": "transform", "op": "sort", "input": {"$ref": "ready"}, "args": {"path": ["score"], "descending": True}},
            {"id": "top", "kind": "transform", "op": "take", "input": {"$ref": "ranked"}, "args": {"count": 5}},
            {"id": "projected", "kind": "transform", "op": "select", "input": {"$ref": "top"}, "args": {"fields": ["name", "score"]}},
        ],
        "outputs": [{"$ref": "projected"}],
    }
    compact_tokens: list[int] = []
    correct = True
    artifact_backed = True
    receipt_chains_valid = True
    for _repeat in range(repeats):
        with tempfile.TemporaryDirectory(prefix="algo-program-benchmark-") as temporary:
            root = Path(temporary)
            (root / "rows.json").write_text(raw, encoding="utf-8")
            capture = io.StringIO()
            with redirect_stdout(capture), redirect_stderr(capture):
                result = execute_program(
                    plan,
                    Config(cwd=str(root)),
                    authorization=authorization_for_actions(("read_file",)),
                    # The fixture is synthetic and ephemeral.  Explicit test
                    # key injection keeps the benchmark deterministic and
                    # avoids touching a user's OS credential store.
                    store=ProgramArtifactStore(
                        root / "program-store",
                        key_store=StaticKeyStore(
                            {
                                "alice-artifact-master-v1": (
                                    hashlib.sha256(
                                        b"algo-cli/synthetic-eval-artifact-key/v1"
                                    ).digest()
                                )
                            }
                        ),
                    ),
                )
            compact = json.dumps(
                result.to_dict(compact=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            compact_tokens.append(estimate_text_tokens(compact))
            try:
                projected = json.loads(result.outputs[0].preview)
            except (IndexError, json.JSONDecodeError):
                projected = []
            correct = correct and result.worked and projected[0] == {
                "name": "item-0418",
                "score": 418,
            }
            artifact_backed = artifact_backed and any(
                receipt.artifact_uri for receipt in result.receipts
            )
            receipt_chains_valid = receipt_chains_valid and verify_receipt_chain(result.receipts)

    median_compact_tokens = int(statistics.median(compact_tokens))
    reduction_pct = 100.0 * (baseline_tokens - median_compact_tokens) / max(1, baseline_tokens)
    return {
        "repeats": repeats,
        "raw_intermediate_tokens": baseline_tokens,
        "median_compact_result_tokens": median_compact_tokens,
        "tokens_saved": baseline_tokens - median_compact_tokens,
        "reduction_pct": round(reduction_pct, 3),
        "stable_compact_size": len(set(compact_tokens)) == 1,
        "correct_result": correct,
        "artifact_backed": artifact_backed,
        "receipt_chains_valid": receipt_chains_valid,
    }


def _benchmark_failures(result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    summary = result["summary"]
    if int(result.get("repeats") or 0) < MIN_REPEATS:
        failures.append(f"repeats must be >= {MIN_REPEATS}")
    if not summary.get("stable_repeated_selection"):
        failures.append("tool selection changed across identical repeated runs")
    if float(summary.get("required_tool_recall") or 0.0) < 1.0:
        failures.append("one or more scenario-required tools were not selected")
    if not summary.get("schema_conversion_complete"):
        failures.append("one or more selected callables were dropped during schema conversion")
    if float(summary.get("median_schema_reduction_pct") or 0.0) < MIN_MEDIAN_SCHEMA_REDUCTION_PCT:
        failures.append("median selected-schema token reduction missed its gate")
    if float(summary.get("minimum_schema_reduction_pct") or 0.0) < MIN_SCENARIO_SCHEMA_REDUCTION_PCT:
        failures.append("a selected-schema scenario missed its minimum reduction gate")

    supersession = result["semantic_supersession"]
    if float(supersession.get("reduction_pct") or 0.0) < MIN_SUPERSESSION_REDUCTION_PCT:
        failures.append("semantic supersession token reduction missed its gate")
    for key in ("latest_result_preserved", "protocol_pairing_preserved", "idempotent"):
        if not supersession.get(key):
            failures.append(f"semantic supersession invariant failed: {key}")
    if int(supersession.get("receipt_count") or 0) != 4:
        failures.append("semantic supersession did not replace exactly four older snapshots")
    typed_program = result["typed_program"]
    if float(typed_program.get("reduction_pct") or 0.0) < MIN_TYPED_PROGRAM_REDUCTION_PCT:
        failures.append("typed program intermediate-result reduction missed its gate")
    for key in ("stable_compact_size", "correct_result", "artifact_backed", "receipt_chains_valid"):
        if not typed_program.get(key):
            failures.append(f"typed program invariant failed: {key}")
    return failures


def run_tool_context_efficiency_benchmark(
    *,
    repeats: int = DEFAULT_REPEATS,
    all_tools: Sequence[Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    """Run repeated schema-selection and semantic-supersession measurements."""

    if repeats < 1:
        raise ValueError("repeats must be positive")
    if all_tools is None:
        from ..tools import ALL_TOOLS

        active_catalog = list(ALL_TOOLS)
    else:
        active_catalog = list(all_tools)

    full_schema_tokens = schema_token_estimate(active_catalog)
    full_serialized_count = len(json.loads(serialized_tool_schema(active_catalog)))
    scenario_results: list[dict[str, Any]] = []
    total_required = 0
    recalled_required = 0
    reductions: list[float] = []
    stable = True
    schema_conversion_complete = full_serialized_count == len(active_catalog)
    for scenario in SCENARIOS:
        selections = [
            select_tools_for_prompt(str(scenario["prompt"]), active_catalog)
            for _repeat in range(repeats)
        ]
        name_runs = [_tool_names(selected) for selected in selections]
        scenario_stable = len(set(name_runs)) == 1
        stable = stable and scenario_stable
        selected_names = name_runs[0]
        required = tuple(str(name) for name in scenario["required_tools"])
        missing = tuple(name for name in required if name not in selected_names)
        total_required += len(required)
        recalled_required += len(required) - len(missing)
        selected_tokens = schema_token_estimate(selections[0])
        selected_serialized_count = len(json.loads(serialized_tool_schema(selections[0])))
        schema_conversion_complete = (
            schema_conversion_complete and selected_serialized_count == len(selected_names)
        )
        reduction_pct = 100.0 * (full_schema_tokens - selected_tokens) / max(1, full_schema_tokens)
        reductions.append(reduction_pct)
        scenario_results.append(
            {
                "id": scenario["id"],
                "prompt": scenario["prompt"],
                "required_tools": list(required),
                "missing_required_tools": list(missing),
                "selected_tools": list(selected_names),
                "selected_tool_count": len(selected_names),
                "serialized_tool_count": selected_serialized_count,
                "selection_digest": _selection_digest(selected_names),
                "stable_across_repeats": scenario_stable,
                "selected_schema_tokens": selected_tokens,
                "schema_tokens_saved": full_schema_tokens - selected_tokens,
                "schema_reduction_pct": round(reduction_pct, 3),
            }
        )

    result: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark": BENCHMARK_NAME,
        "repeats": repeats,
        "catalog_tool_count": len(active_catalog),
        "serialized_catalog_tool_count": full_serialized_count,
        "full_schema_tokens": full_schema_tokens,
        "summary": {
            "stable_repeated_selection": stable,
            "schema_conversion_complete": schema_conversion_complete,
            "required_tool_recall": recalled_required / max(1, total_required),
            "required_tools_recalled": recalled_required,
            "required_tools_total": total_required,
            "median_selected_schema_tokens": statistics.median(
                scenario["selected_schema_tokens"] for scenario in scenario_results
            ),
            "median_schema_reduction_pct": round(statistics.median(reductions), 3),
            "minimum_schema_reduction_pct": round(min(reductions), 3),
        },
        "scenarios": scenario_results,
        "semantic_supersession": _run_supersession_benchmark(),
        "typed_program": _run_typed_program_benchmark(repeats),
        "gates": {
            "minimum_repeats": MIN_REPEATS,
            "minimum_median_schema_reduction_pct": MIN_MEDIAN_SCHEMA_REDUCTION_PCT,
            "minimum_scenario_schema_reduction_pct": MIN_SCENARIO_SCHEMA_REDUCTION_PCT,
            "minimum_supersession_reduction_pct": MIN_SUPERSESSION_REDUCTION_PCT,
            "minimum_typed_program_reduction_pct": MIN_TYPED_PROGRAM_REDUCTION_PCT,
            "required_tool_recall": 1.0,
        },
    }
    failures = _benchmark_failures(result)
    result["status"] = "pass" if not failures else "fail"
    result["failures"] = failures
    return result


def assert_tool_context_efficiency(result: dict[str, Any]) -> None:
    """Raise with gate details unless a benchmark result is release-ready."""

    failures = _benchmark_failures(result)
    if failures:
        raise AssertionError("; ".join(failures))


def main() -> int:
    result = run_tool_context_efficiency_benchmark()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a release command
    raise SystemExit(main())


__all__ = [
    "BENCHMARK_NAME",
    "BENCHMARK_SCHEMA_VERSION",
    "SCENARIOS",
    "assert_tool_context_efficiency",
    "run_tool_context_efficiency_benchmark",
    "schema_token_estimate",
    "serialized_tool_schema",
]
