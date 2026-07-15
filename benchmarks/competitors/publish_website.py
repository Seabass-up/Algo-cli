"""Validate a completed cross-harness cell and publish sanitized website data.

Raw benchmark artifacts can contain local paths and model output, so this
command deliberately exports aggregate measurements only.  It fails closed if
the release-cell protocol is incomplete, unwarmed, or internally inconsistent.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


TASKS = {
    "code_repair_small_repo": {
        "label": "Code repair",
        "short_label": "Code repair",
        "description": "Repair a failing parser in a small Python repository and pass its external checker.",
    },
    "tool_trap_misleading_state": {
        "label": "Misleading-state safety trap",
        "short_label": "Safety trap",
        "description": "Use authoritative evidence instead of stale documentation or a protected decoy config.",
    },
    "memory_rag_conflict_live_files": {
        "label": "Live-files memory conflict",
        "short_label": "Memory conflict",
        "description": "Reconcile stale retrieved context against current project files, with live state winning.",
    },
    "evidence_reconciliation_medium_repo": {
        "label": "Medium-repository reconciliation",
        "short_label": "Repo reconciliation",
        "description": "Roll out a verified change across differently shaped service configs while preserving protected inputs and producing a receipt.",
    },
}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("benchmark summary must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _website_id(harness: str) -> str:
    return harness.replace("_", "-")


def _recomputed_aggregates(
    runs: list[dict[str, Any]], harnesses: list[str], task_ids: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for harness in harnesses:
        selected = [run for run in runs if run.get("harness") == harness]
        durations = [float(run["duration_seconds"]) for run in selected]
        per_task: dict[str, Any] = {}
        for task_id in task_ids:
            cell = [run for run in selected if run.get("task") == task_id]
            per_task[task_id] = {
                "runs": len(cell),
                "checker_passes": sum(bool(run.get("checker_pass")) for run in cell),
                "clean_processes": sum(bool(run.get("clean_process")) for run in cell),
                "median_duration_seconds": round(
                    statistics.median(float(run["duration_seconds"]) for run in cell), 6
                ),
            }
        checker_passes = sum(bool(run.get("checker_pass")) for run in selected)
        clean_processes = sum(bool(run.get("clean_process")) for run in selected)
        ordered = sorted(durations)
        p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
        rows.append(
            {
                "harness": harness,
                "runs": len(selected),
                "checker_passes": checker_passes,
                "checker_pass_rate": checker_passes / len(selected),
                "clean_processes": clean_processes,
                "clean_process_rate": clean_processes / len(selected),
                "scope_pass_rate": sum(bool(run.get("workspace_scope_pass")) for run in selected) / len(selected),
                "median_duration_seconds": round(statistics.median(durations), 6),
                "p95_duration_seconds": round(p95, 6),
                "per_task": per_task,
            }
        )
    rows.sort(
        key=lambda row: (
            -row["checker_pass_rate"],
            -row["scope_pass_rate"],
            -row["clean_process_rate"],
            row["median_duration_seconds"],
            row["harness"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["objective_rank"] = rank
    return rows


def _validate(raw: dict[str, Any], source_revision: str) -> None:
    protocol = raw.get("protocol") or {}
    runs = raw.get("runs") or []
    aggregates = raw.get("aggregate") or []
    harnesses = protocol.get("harnesses") or []
    task_ids = protocol.get("tasks") or []
    repetitions = int(protocol.get("repetitions") or 0)
    expected = len(harnesses) * len(task_ids) * repetitions
    failures: list[str] = []

    if raw.get("schema_version") != 1:
        failures.append("unsupported raw schema version")
    if protocol.get("id") != "algo-cli-cross-harness-v3-draft":
        failures.append("release publication requires the v3 draft protocol")
    if repetitions < 3:
        failures.append("release publication requires at least three repetitions")
    if len(harnesses) != 11 or len(task_ids) != 4:
        failures.append("release cell must contain 11 harnesses and four tasks")
    if set(task_ids) != set(TASKS):
        failures.append("task set does not match the frozen public corpus")
    if protocol.get("same_model") is not True or protocol.get("same_task_fixtures") is not True:
        failures.append("same-model and same-fixture protocol flags are required")
    if protocol.get("same_machine") is not True:
        failures.append("release cell must run on one machine")
    if int(protocol.get("total_runs") or 0) != expected or len(runs) != expected:
        failures.append(f"expected {expected} complete runs")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        failures.append("source revision must be a full Git SHA")

    warmup = protocol.get("model_warmup") or {}
    if not warmup.get("performed") or not warmup.get("success"):
        failures.append("shared model warmup did not succeed")
    if warmup.get("included_in_scored_duration") is not False:
        failures.append("warmup must be excluded from scored duration")
    if not re.fullmatch(r"[0-9a-f]{64}", str(protocol.get("task_suite_sha256") or "")):
        failures.append("task-suite digest is missing")

    cells = Counter((run.get("harness"), run.get("task")) for run in runs)
    run_ids = [run.get("run_id") for run in runs]
    if len(set(run_ids)) != len(run_ids):
        failures.append("run identifiers are not unique")
    for harness in harnesses:
        for task_id in task_ids:
            if cells[(harness, task_id)] != repetitions:
                failures.append(f"incomplete cell: {harness}/{task_id}")
    if len(aggregates) != len(harnesses):
        failures.append("aggregate row count does not match harness count")
    for run in runs:
        if run.get("model") != protocol.get("model"):
            failures.append(f"model mismatch: {run.get('run_id')}")
        if run.get("baseline_checker_failed_as_expected") is not True:
            failures.append(f"baseline checker invariant failed: {run.get('run_id')}")
        if not isinstance(run.get("protected_inputs_unchanged"), bool):
            failures.append(f"protected-input receipt missing: {run.get('run_id')}")

    recomputed = _recomputed_aggregates(runs, harnesses, task_ids) if runs else []
    reported_by_harness = {row.get("harness"): row for row in aggregates}
    comparison_fields = (
        "runs", "checker_passes", "checker_pass_rate", "clean_processes",
        "clean_process_rate", "scope_pass_rate", "median_duration_seconds",
        "p95_duration_seconds", "objective_rank", "per_task",
    )
    for expected_row in recomputed:
        reported = reported_by_harness.get(expected_row["harness"])
        if reported is None:
            failures.append(f"missing aggregate: {expected_row['harness']}")
            continue
        for field in comparison_fields:
            if reported.get(field) != expected_row[field]:
                failures.append(f"aggregate mismatch: {expected_row['harness']}/{field}")

    if failures:
        raise ValueError("; ".join(dict.fromkeys(failures)))


def _curate(
    raw: dict[str, Any],
    source_revision: str,
    raw_digest: str,
    *,
    hardware_description: str | None = None,
    os_description: str | None = None,
) -> dict[str, Any]:
    protocol = raw["protocol"]
    repetitions = int(protocol["repetitions"])
    runs = raw["runs"]
    labels = {
        item["product"]: item["label"]
        for item in raw["product_matrix"]
        if item.get("product") and item.get("label")
    }
    results: list[dict[str, Any]] = []
    for aggregate in raw["aggregate"]:
        harness = aggregate["harness"]
        harness_runs = [run for run in runs if run["harness"] == harness]
        verified = sum(
            bool(run.get("checker_pass"))
            and bool(run.get("clean_process"))
            and bool(run.get("workspace_scope_pass"))
            and bool(run.get("baseline_checker_failed_as_expected"))
            and bool(run.get("protected_inputs_unchanged"))
            for run in harness_runs
        )
        task_passes = {
            task_id: int(aggregate["per_task"][task_id]["checker_passes"])
            for task_id in protocol["tasks"]
        }
        results.append(
            {
                "rank": int(aggregate["objective_rank"]),
                "id": _website_id(harness),
                "harness": labels.get(harness, harness),
                "version": raw["versions"].get(harness),
                "passes": int(aggregate["checker_passes"]),
                "clean_runs": verified,
                "scope_passes": round(float(aggregate["scope_pass_rate"]) * int(aggregate["runs"])),
                "runs": int(aggregate["runs"]),
                "median_seconds": float(aggregate["median_duration_seconds"]),
                "p95_seconds": float(aggregate["p95_duration_seconds"]),
                "task_passes": task_passes,
            }
        )

    algo = next(row for row in results if row["id"] == "algo-cli")
    perfect = [row for row in results if row["clean_runs"] == row["runs"]]
    blocked = [
        {"product": item["label"], "reason": item["reason"]}
        for item in raw["product_matrix"]
        if item.get("status") != "runnable"
    ]
    return {
        "schema_version": 3,
        "status": "reported-local-draft-result",
        "created_at": str(raw["created_at"]),
        "source_revision": source_revision,
        "runner_path": "benchmarks/competitors",
        "raw_summary_sha256": raw_digest,
        "environment": {
            "hardware": hardware_description or "not reported",
            "operating_system": os_description or str((raw.get("environment") or {}).get("platform") or "not reported"),
            "python": (raw.get("environment") or {}).get("python"),
            "ollama": (raw.get("environment") or {}).get("ollama"),
        },
        "protocol": {
            "id": protocol["id"],
            "tasks": [
                {"id": task_id, **TASKS[task_id]}
                for task_id in protocol["tasks"]
            ],
            "task_suite_sha256": protocol["task_suite_sha256"],
            "repetitions_per_cell": repetitions,
            "runs_per_harness": int(protocol["runs_per_harness"]),
            "measured_harnesses": len(protocol["harnesses"]),
            "total_runs": int(protocol["total_runs"]),
            "order_policy": protocol["order_policy"],
            "same_model": protocol["model"],
            "provider": protocol["provider"],
            "same_machine": bool(protocol["same_machine"]),
            "fresh_state_per_run": True,
            "timeout_seconds": int(protocol["timeout_seconds"]),
            "model_warmup": protocol["model_warmup"],
            "ranking_policy": "checker pass rate, scope pass rate, clean-process rate, median duration",
        },
        "results": results,
        "blocked_or_non_comparable": blocked,
        "claim": (
            f"Algo CLI achieved {algo['clean_runs']}/{algo['runs']} verified runs and ranked "
            f"#{algo['rank']} in this four-task, same-model local draft benchmark."
        ),
        "result_provenance": (
            "Generated from a complete warmed runner receipt after validating every task/harness cell, "
            "baseline checker failure, protected-input receipts, and the frozen task-suite digest."
        ),
        "limitations": (
            f"Four draft tasks, one local model, one machine, and {repetitions} repetitions per cell do not "
            "support a universal superiority or native-model-power claim. Timing includes harness and model "
            "work but excludes the shared warmup. Raw artifacts are retained locally because they can expose "
            "paths and model output; only sanitized aggregates are published. Results have not been independently reproduced."
        ),
        "top_reliability_group_size": len(perfect),
    }


def _write_csv(path: Path, summary: dict[str, Any]) -> None:
    tasks = summary["protocol"]["tasks"]
    fields = [
        "rank", "harness", "version", "checker_passes", "verified_runs", "scope_passes",
        "runs", "median_seconds", "p95_seconds", *[f"{task['id']}_passes" for task in tasks],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in summary["results"]:
            writer.writerow(
                {
                    "rank": row["rank"],
                    "harness": row["harness"],
                    "version": row["version"],
                    "checker_passes": row["passes"],
                    "verified_runs": row["clean_runs"],
                    "scope_passes": row["scope_passes"],
                    "runs": row["runs"],
                    "median_seconds": f"{row['median_seconds']:.6f}",
                    "p95_seconds": f"{row['p95_seconds']:.6f}",
                    **{
                        f"{task['id']}_passes": row["task_passes"][task["id"]]
                        for task in tasks
                    },
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--website-root", type=Path, default=Path("website"))
    parser.add_argument("--hardware-description")
    parser.add_argument("--os-description")
    args = parser.parse_args()

    raw = _load(args.summary)
    _validate(raw, args.source_revision)
    curated = _curate(
        raw,
        args.source_revision,
        _sha256(args.summary),
        hardware_description=args.hardware_description,
        os_description=args.os_description,
    )
    destination = args.website_root / "public" / "benchmarks"
    destination.mkdir(parents=True, exist_ok=True)
    summary_path = destination / "summary.json"
    summary_path.write_text(json.dumps(curated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_csv(destination / "results.csv", curated)
    print(f"published {curated['protocol']['total_runs']} sanitized runs to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
