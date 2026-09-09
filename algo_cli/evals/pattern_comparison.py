"""Frozen, paired retrieval comparisons using the production lexical ranker."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
import statistics
import tempfile
import time
from typing import Any

from .. import harness
from ..pattern_catalog import PatternContext, exclusion_reasons
from .pattern_evidence import digest, repository_root, save_report, source_snapshot


TASKS = (
    ("capability snapshot model routing", "O1", "catalog"),
    ("version gated catalog canary", "O2", "catalog"),
    ("installed artifact identity check", "O3", "catalog"),
    ("prerequisite partitioned diagnostic probes", "O4", "catalog"),
    ("multi window error budget burn rate", "L9", "catalog"),
    ("held out synthetic canary protocol", "B471", "catalog"),
    ("quartz routing", "O902", "applicability"),
    ("topaz routing", "O904", "applicability"),
)
APPLICABILITY_FIXTURE = """# ALGO.md
## Applicability Fixtures
### O901. Quartz routing quartz routing quartz routing
**Applicability:** {"environments": ["windows"]}
Windows-only quartz routing.
### O902. Quartz routing
**Applicability:** {"environments": ["darwin"]}
Darwin quartz routing.
### O903. Topaz routing topaz routing topaz routing
**Applicability:** {"prerequisites": ["unavailable-provider"]}
Provider-only topaz routing.
### O904. Topaz routing
**Applicability:** {"prerequisites": ["lexical-retrieval"]}
Local topaz routing.
"""
CELLS = ((False, False), (True, False), (False, True), (True, True))
BUDGET = {"top_k": 1, "context_chars": 2000, "provider_tokens": 0}


def _indexes(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = harness.SourceRoot("algo-cli", "algorithm", path.parent, ("ALGO.md",), 1)
    parent = harness.make_record(root, path)
    baseline = {"records": [parent]}
    return baseline, harness._merge_pattern_records(deepcopy(baseline))


def _run_cells(indexes: dict[str, tuple[dict[str, Any], dict[str, Any]]], repetitions: int) -> list[dict[str, Any]]:
    context = PatternContext(environment="darwin", capabilities=frozenset({"lexical-retrieval", "local-files"}))
    variants = {}
    for corpus, pair in indexes.items():
        for pattern_records, applicability in CELLS:
            index = deepcopy(pair[int(pattern_records)])
            if not applicability:
                for row in index["records"]:
                    if row.get("pattern_id"):
                        row["applicability"] = {"fallback": "Comparison control only."}
                        row["applicability_valid"] = True
            variants[corpus, pattern_records, applicability] = index
    samples: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        for task_id, (query, expected, corpus) in enumerate(TASKS):
            # Rotate cells within each task; cache warm-up is a separate unreported pass.
            offset = (repetition + task_id) % len(CELLS)
            order = CELLS[offset:] + CELLS[:offset]
            for position, (pattern_records, applicability) in enumerate(order):
                index = variants[corpus, pattern_records, applicability]
                start = time.perf_counter_ns()
                ranked = harness._rank_keyword_index(
                    index, query, kind="algorithm", limit=BUDGET["top_k"], pattern_context=context
                )
                elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
                rows = [row for _, row in ranked]
                context_text = "\n".join(str(row.get("index_text", "")) for row in rows)[: BUDGET["context_chars"]]
                original = {row["id"]: row for row in indexes[corpus][1]["records"]}
                violations = sum(bool(exclusion_reasons(original[row["id"]], context)) for row in rows)
                samples.append(
                    {
                        "task_id": task_id,
                        "expected_pattern": expected,
                        "corpus": corpus,
                        "repetition": repetition,
                        "order": position,
                        "pattern_records": pattern_records,
                        "applicability": applicability,
                        "correct": bool(rows and rows[0].get("pattern_id") == expected),
                        "latency_ms": elapsed_ms,
                        "policy_violations": violations,
                        "selected_ids": [row["id"] for row in rows],
                        "context_chars": len(context_text),
                        "estimated_context_tokens": math.ceil(len(context_text) / 4),
                        "provider_tokens": 0,
                    }
                )
    return samples


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    cells = {}
    for patterns, applicability in CELLS:
        selected = [
            row for row in samples if row["pattern_records"] is patterns and row["applicability"] is applicability
        ]
        name = f"records_{int(patterns)}_rules_{int(applicability)}"
        cells[name] = {
            "count": len(selected),
            "accuracy": statistics.mean(row["correct"] for row in selected),
            "median_latency_ms": statistics.median(row["latency_ms"] for row in selected),
            "mean_estimated_context_tokens": statistics.mean(row["estimated_context_tokens"] for row in selected),
            "provider_tokens": 0,
            "policy_violations": sum(row["policy_violations"] for row in selected),
        }

    def accuracy(p: int, a: int) -> float:
        return cells[f"records_{p}_rules_{a}"]["accuracy"]

    paired = []
    grouped: dict[tuple[int, int], dict[tuple[bool, bool], dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault((sample["task_id"], sample["repetition"]), {})[
            sample["pattern_records"], sample["applicability"]
        ] = sample
    for (task_id, repetition), group in sorted(grouped.items()):
        control, treatment = group[False, False], group[True, True]
        paired.append(
            {
                "task_id": task_id,
                "repetition": repetition,
                "correctness_delta": int(treatment["correct"]) - int(control["correct"]),
                "latency_delta_ms": treatment["latency_ms"] - control["latency_ms"],
                "estimated_token_delta": treatment["estimated_context_tokens"] - control["estimated_context_tokens"],
                "policy_violation_delta": treatment["policy_violations"] - control["policy_violations"],
            }
        )
    return {
        "cells": cells,
        "paired_deltas": paired,
        "accuracy_interaction": accuracy(1, 1) - accuracy(1, 0) - accuracy(0, 1) + accuracy(0, 0),
    }


def run_comparison(root: Path, directory: Path, *, repetitions: int = 5) -> dict[str, Any]:
    root = repository_root(root)
    if type(repetitions) is not int or not 2 <= repetitions <= 20:
        raise ValueError("pattern_comparison_repetitions")
    sources = source_snapshot(root, "O7")
    with tempfile.TemporaryDirectory(prefix="algo-pattern-comparison-") as temporary:
        fixture = Path(temporary) / "ALGO.md"
        fixture.write_text(APPLICABILITY_FIXTURE, encoding="utf-8")
        indexes = {"catalog": _indexes(root / "docs/ALGO.md"), "applicability": _indexes(fixture)}
        _run_cells(indexes, 1)
        samples = _run_cells(indexes, repetitions)
    try:
        stable = sources == source_snapshot(root, "O7")
    except (OSError, ValueError):
        stable = False
    return save_report(
        directory / "comparison.json",
        {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete" if stable else "stale",
            "source_stable": stable,
            "sources": sources,
            "model": "none:production-lexical-ranker",
            "budget": BUDGET,
            "repetitions": repetitions,
            "checker": "exact-top1-pattern-id-and-original-applicability-v1",
            "tasks": TASKS,
            "fixture_digest": digest({"tasks": TASKS, "applicability": APPLICABILITY_FIXTURE, "budget": BUDGET}),
            "token_accounting": "provider tokens are exactly zero; context estimate is ceil(chars/4), not tokenizer usage",
            "scope": "Local retrieval microbenchmark, not generated-answer quality, provider quality, or M8 qualification.",
            "samples": samples,
            **summarize(samples),
        },
    )


def complete_report(report: dict[str, Any]) -> bool:
    """Reject partial cells and malformed metrics even when a checksum is valid."""
    repetitions = report.get("repetitions")
    if type(repetitions) is not int or not 2 <= repetitions <= 20:
        return False
    if report.get("fixture_digest") != digest(
        {"tasks": TASKS, "applicability": APPLICABILITY_FIXTURE, "budget": BUDGET}
    ):
        return False
    samples = report.get("samples")
    if type(samples) is not list or len(samples) != len(TASKS) * len(CELLS) * repetitions:
        return False
    seen = set()
    for sample in samples:
        if type(sample) is not dict:
            return False
        if any(type(sample.get(key)) is not bool for key in ("correct", "pattern_records", "applicability")):
            return False
        if any(
            type(sample.get(key)) is not int
            for key in (
                "task_id",
                "repetition",
                "order",
                "policy_violations",
                "provider_tokens",
                "context_chars",
                "estimated_context_tokens",
            )
        ):
            return False
        task, repeat = sample["task_id"], sample["repetition"]
        if not 0 <= task < len(TASKS) or not 0 <= repeat < repetitions or not 0 <= sample["order"] < len(CELLS):
            return False
        if (
            not 0 <= sample["policy_violations"] <= BUDGET["top_k"]
            or not 0 <= sample["context_chars"] <= BUDGET["context_chars"]
        ):
            return False
        if sample["provider_tokens"] != 0 or sample["estimated_context_tokens"] != math.ceil(
            sample["context_chars"] / 4
        ):
            return False
        latency = sample.get("latency_ms")
        if (
            isinstance(latency, bool)
            or not isinstance(latency, (float, int))
            or not math.isfinite(latency)
            or latency < 0
        ):
            return False
        key = (task, repeat, sample["pattern_records"], sample["applicability"])
        if key in seen:
            return False
        seen.add(key)
        if sample.get("expected_pattern") != TASKS[task][1] or sample.get("corpus") != TASKS[task][2]:
            return False
    summary = summarize(samples)
    return all(report.get(key) == value for key, value in summary.items())
