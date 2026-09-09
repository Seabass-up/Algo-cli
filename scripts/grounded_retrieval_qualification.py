#!/usr/bin/env python3
"""Evaluate public harness retrieval using fresh isolated state and local Ollama."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3-embedding:latest")
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=range(2, 11), default=2)
    parser.add_argument("--suite", choices=("original", "expanded", "combined"), default="original")
    args = parser.parse_args()
    host = urlparse(args.host)
    if (
        host.scheme != "http"
        or host.hostname not in {"127.0.0.1", "localhost", "::1"}
        or host.username
        or host.password
        or host.path not in {"", "/"}
        or host.query
        or host.fragment
    ):
        parser.error("use an explicit loopback Ollama endpoint without credentials")
    if ":cloud" in args.model or args.output.exists() or args.output.is_symlink():
        parser.error("use a local embedding model and a new output path")
    with tempfile.TemporaryDirectory(prefix="algo-grounded-retrieval-") as state:
        # Config paths bind at import time. No runtime configuration, credentials,
        # user memory, or operator index is loaded or modified by this process.
        os.environ["ALGO_CLI_CONFIG_DIR"] = state
        os.environ["OLLAMA_CLI_CONFIG_DIR"] = state
        os.environ["ALGO_CLI_DISABLE_WINDOWS_HOME_FALLBACK"] = "1"
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from ollama import Client
        from algo_cli import harness
        from algo_cli.evals.grounded_retrieval import CASES, SCHEMA, run_grounded_retrieval
        from algo_cli.evals import grounded_retrieval_validation as validation

        cases = CASES if args.suite == "original" else validation.CASES
        if args.suite == "combined":
            cases = (*CASES, *validation.CASES, *validation.CHALLENGE_CASES)
        exclusions = () if args.suite == "original" else validation.EXCLUSION_CASES

        client = Client(host=args.host, timeout=120)

        def identity() -> str:
            models = client.list().models
            return next((str(item.digest) for item in models if item.model == args.model and item.digest), "")

        model_digest = identity()
        if not model_digest:
            parser.error("the exact embedding model name must already be installed")
        harness.configure_context_sources(external=False, index_compute_lab=False)
        harness.configure_protected_memory_authority(True)
        index = harness.load_index(refresh=True)
        print(f"Public corpus: {len(index['records'])} records; embedding sequentially.", file=sys.stderr, flush=True)
        provider_calls = 0

        def embed(texts: list[str]) -> list[list[float]]:
            nonlocal provider_calls
            provider_calls += 1
            return [list(vector) for vector in client.embed(model=args.model, input=texts).embeddings]

        progress = harness.embed_index_records(embed, args.model)
        report: dict[str, Any]
        if not progress.get("ready"):
            report = {
                "schema": SCHEMA,
                "status": "fail",
                "stage": "corpus_embedding",
                "progress": progress,
            }
        else:
            embedding_calls = provider_calls
            report = run_grounded_retrieval(
                embed, args.model, repetitions=args.repetitions, cases=cases, exclusion_cases=exclusions
            )
            report["provider_query_calls"] = provider_calls - embedding_calls
            report["embedding_progress"] = progress
        report["embedding_model_digest"] = model_digest
        report["model_identity_stable"] = identity() == model_digest
        report["runner_digest"] = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if not report["model_identity_stable"]:
            report["status"] = "fail"
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        print(
            json.dumps(
                {key: report[key] for key in ("status", "metrics", "label_failures", "source_stable") if key in report}
            )
        )
        return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
