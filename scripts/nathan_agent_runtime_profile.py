#!/usr/bin/env python3
"""Collect bounded model-free timing diagnostics, never qualification evidence."""

from __future__ import annotations

import argparse
import cProfile
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from algo_cli.evals import nathan_agent_runtime_hardening as benchmark  # noqa: E402


SAMPLES = 5
MAX_FUNCTIONS = 80
MAX_REPORT_BYTES = 128 * 1024
LIMITATIONS = (
    "Diagnostic-only cProfile run of five frozen model-free workloads. "
    "Profiler overhead changes timing; these results neither replace the failed "
    "unprofiled gate nor qualify runtime performance. No model, network, private "
    "memory, real approval, or production key-store calls are made."
)


def _source_label(filename: str) -> str:
    if filename == "~":
        return "built-in"
    path = Path(filename)
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def profile_runtime() -> dict[str, Any]:
    digest, snapshot = benchmark._capture_source_tree()
    profiler = cProfile.Profile()
    with tempfile.TemporaryDirectory(prefix="algo-runtime-profile-work-") as temporary:
        with profiler:
            workloads = [
                benchmark._frozen_agent_workload(Path(temporary), index=700_000 + index)
                for index in range(SAMPLES)
            ]
    benchmark._verify_source_tree_snapshot(snapshot)
    if benchmark.source_tree_digest() != digest:
        raise benchmark.AgentRuntimeBenchmarkError("source changed during timing diagnostic")
    functions = []
    for entry in sorted(profiler.getstats(), key=lambda item: item.totaltime, reverse=True)[:MAX_FUNCTIONS]:
        code = entry.code
        functions.append({
            "source": "built-in" if isinstance(code, str) else _source_label(code.co_filename),
            "line": 0 if isinstance(code, str) else code.co_firstlineno,
            "function": (code if isinstance(code, str) else code.co_name)[:256],
            "primitive_calls": entry.callcount - entry.reccallcount, "calls": entry.callcount,
            "self_ms": round(entry.inlinetime * 1_000, 6),
            "cumulative_ms": round(entry.totaltime * 1_000, 6),
        })
    return {
        "schema_version": 1,
        "status": "diagnostic_only",
        "public_claim_eligible": False,
        "source_tree_sha256": digest,
        "source_revision": benchmark._git_revision(),
        "environment": {
            "operating_system": platform.platform(), "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "samples": SAMPLES,
        "total_p50_ms": statistics.median(row["total_ms"] for row in workloads),
        "ttfa_p50_ms": statistics.median(row["ttfa_ms"] for row in workloads),
        "correctness": {
            name: all(row[name] is True for row in workloads)
            for name in ("task_passed", "crash_resume_passed", "protocol_correct")
        },
        "functions": functions,
        "limitations": LIMITATIONS,
    }


def write_profile(directory: Path, report: dict[str, Any]) -> Path:
    payload = (json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode("ascii")
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("runtime timing diagnostic exceeds its output bound")
    descriptor, filename = tempfile.mkstemp(prefix="algo-agent-runtime-profile-", suffix=".json", dir=directory)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())))
    args = parser.parse_args()
    path = write_profile(args.output_dir, profile_runtime())
    print(f"Diagnostic only; original qualification result is unchanged. Report: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
