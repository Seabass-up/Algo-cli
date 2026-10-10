"""Read-only, bounded kernel/pattern candidates for advisory coordination."""

from __future__ import annotations

import hashlib
from typing import Any

from . import config, decision_layer as d, harness, pattern_catalog
from .kernels import manifest
from .retrieval_algorithms import BM25Index, lexical_tokens


MAX_CANDIDATES = 5


def _rank(rows: list[dict], query: str) -> list[dict]:
    scores = BM25Index([row["description"] for row in rows]).scores(lexical_tokens(query))
    ranked = sorted(zip(scores, rows), key=lambda pair: -pair[0])
    return [row for score, row in ranked[:MAX_CANDIDATES] if score > 0]


def candidates(cfg: Any, questions: dict, task: str) -> dict:
    """Read exact owner catalog, never arbitrary model-supplied paths or modules.

    This is a native configuration/reference reader, not a filesystem permission
    exception. Ordinary tools still cannot open the protected configuration root.
    Only redacted task-relevant descriptions, IDs and source digests leave it.
    """
    query = task + " " + " ".join(str(question["instructions"]) for question in questions.values())
    specs = manifest.list_kernels()
    kernels = [{"id": spec.name, "description": harness.redact_sensitive_text(spec.description),
                "status": spec.status, "source": spec.source,
                "execution_authority": False,
                "evidence": "Declared active metadata; not proof of workload correctness."}
               for spec in specs if spec.status == "active"]
    patterns, issues = [], []
    catalog_digest = None
    path = config.CONFIG_DIR / "ALGO.md"
    try:
        try:
            payload = config._state_descriptor_payload(path, max_bytes=pattern_catalog.MAX_CATALOG_BYTES)
        except FileNotFoundError:
            path = harness._algo_cli_docs_dir() / "ALGO.md"
            payload = harness._read_public_source(path, max_bytes=pattern_catalog.MAX_CATALOG_BYTES)
        text = payload.decode("utf-8", errors="strict")
        catalog_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        context = harness.runtime_pattern_context()
        for row in pattern_catalog.parse_patterns(text):
            if row["status"] == "retired" or pattern_catalog.exclusion_reasons(row, context):
                continue
            patterns.append({"id": row["pattern_id"], "description": harness.redact_sensitive_text(row["title"]),
                             "status": row["status"], "source_digest": row["pattern_digest"],
                             "execution_authority": False, "executable": False,
                             "evidence": "Catalog claim only; reference/research, not verified implementation."})
    except (OSError, UnicodeError, ValueError):
        issues.append("algorithm_catalog_unavailable_or_invalid")
    signature = d.digest({"kernels": kernels, "algorithm_source": catalog_digest})
    return {"kernels": _rank(kernels, query), "algorithms": _rank(patterns, query),
            "catalog_sha256": signature, "issues": issues,
            "scope": "Task-relevant local metadata only; not exhaustive selection or execution authority."}


def questions_for(catalog: dict) -> dict:
    questions = {}
    for kind in ("kernels", "algorithms"):
        rows = catalog[kind]
        if not rows:
            continue
        criteria = {f"item_{i}": f"{row['id']}: {row['description']} Status: {row['status']}. {row['evidence']}"
                    for i, row in enumerate(rows)}
        criteria["none"] = "No listed candidate is relevant, or more context/research is needed. Do not invent a candidate."
        questions[kind] = {"type": "choice",
                           "instructions": f"Using only question_specs and reference_candidates.{kind}, which {kind[:-1]} best helps the main model's task? This is a suggestion, not activation or proof it works. Preserve proposed/preview/unknown status. Select none if no suitable candidate is listed.",
                           "criteria": criteria}
    return questions


def recommendations(catalog: dict, answers: dict, minimum_probability: float) -> dict:
    result = {}
    for kind in ("kernels", "algorithms"):
        answer = answers.get(kind)
        selected = None
        if answer and answer["choice"] != "none":
            probability = answer["probabilities"][answer["choice"]]
            if probability >= minimum_probability:
                index = int(answer["choice"].removeprefix("item_"))
                selected = catalog[kind][index]
        result[kind] = {"selected": selected, "judgment": answer,
                        "requires_main_model_judgment": True, "execution_authority": False}
    return result
