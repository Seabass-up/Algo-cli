"""Deterministic baseline-vs-structural retrieval microbenchmark.

Run from the repository root:
    python benchmarks/structural_rag.py
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from algo_cli import code_rag


CASES = (
    ("validation", "validate shared request behavior", "z_validation.py", "validate", "ambiguous"),
    ("configuration", "repair shared config loading", "z_configuration.py", "load_config", "ambiguous"),
    ("serialization", "fix shared payload encoding", "z_serialization.py", "encode_payload", "ambiguous"),
    ("entry-specific", "endpointmarker", "a_entry.py", "dispatch", "specific"),
    ("worker-specific", "workermarker", "b_worker.py", "dispatch", "specific"),
)


def _constant_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 1.0, 1.0] for _text in texts]


def _specific_embed(texts: list[str]) -> list[list[float]]:
    return [
        [
            float(text.lower().count("endpointmarker")),
            float(text.lower().count("workermarker")),
            0.01,
        ]
        for text in texts
    ]


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _run_strategy(
    root: Path,
    query: str,
    expected: str,
    weight: float,
    embed_fn: Any,
    *,
    repeats: int = 25,
) -> dict[str, Any]:
    elapsed_samples: list[float] = []
    hits: list[dict[str, Any]] = []
    for _iteration in range(repeats):
        started = time.perf_counter()
        hits = code_rag.retrieve(
            str(root),
            query,
            embed_fn,
            "structural-benchmark",
            k=3,
            structural_weight=weight,
        )
        elapsed_samples.append((time.perf_counter() - started) * 1000.0)
    paths = [str(hit["relative_path"]) for hit in hits]
    rank = paths.index(expected) + 1 if expected in paths else 0
    return {
        "top1": bool(paths and paths[0] == expected),
        "reciprocal_rank": 1.0 / rank if rank else 0.0,
        "mean_elapsed_ms": round(sum(elapsed_samples) / len(elapsed_samples), 3),
        "paths": paths,
    }


def run() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="algo-structural-rag-") as temporary:
        base = Path(temporary)
        original_index_dir = code_rag.CODE_INDEX_DIR
        code_rag.CODE_INDEX_DIR = base / "indexes"
        code_rag.invalidate_cache()
        try:
            for case, query, expected_file, symbol, mode in CASES:
                root = base / case
                root.mkdir()
                central_file = f"z_{case.replace('-specific', '_core')}.py"
                module = central_file.removesuffix(".py")
                _write(
                    root / "a_entry.py",
                    f"import {module}\n\ndef entry(value):\n    # endpointmarker\n    return {module}.{symbol}(value)\n",
                )
                _write(
                    root / "b_worker.py",
                    f"import {module}\n\ndef work(value):\n    # workermarker\n    return {module}.{symbol}(value)\n",
                )
                _write(root / central_file, f"def {symbol}(value):\n    return value\n")
                embed_fn = _constant_embed if mode == "ambiguous" else _specific_embed
                code_rag.ensure_embeddings(
                    str(root),
                    embed_fn,
                    "structural-benchmark",
                    cap=100,
                )
                baseline = _run_strategy(root, query, expected_file, 0.0, embed_fn)
                enhanced = _run_strategy(
                    root,
                    query,
                    expected_file,
                    code_rag.STRUCTURAL_WEIGHT,
                    embed_fn,
                )
                rows.append(
                    {
                        "case": case,
                        "expected": expected_file,
                        "baseline": baseline,
                        "structural": enhanced,
                    }
                )
        finally:
            code_rag.invalidate_cache()
            code_rag.CODE_INDEX_DIR = original_index_dir

    count = len(rows)
    return {
        "benchmark": "structural-code-rag-v1",
        "cases": count,
        "baseline": {
            "top1_accuracy": sum(row["baseline"]["top1"] for row in rows) / count,
            "mean_reciprocal_rank": sum(row["baseline"]["reciprocal_rank"] for row in rows) / count,
            "mean_elapsed_ms": sum(row["baseline"]["mean_elapsed_ms"] for row in rows) / count,
        },
        "structural": {
            "top1_accuracy": sum(row["structural"]["top1"] for row in rows) / count,
            "mean_reciprocal_rank": sum(row["structural"]["reciprocal_rank"] for row in rows) / count,
            "mean_elapsed_ms": sum(row["structural"]["mean_elapsed_ms"] for row in rows) / count,
        },
        "results": rows,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
