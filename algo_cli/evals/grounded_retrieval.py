"""Development retrieval evaluation against shipped sources, not seeded aliases.

Run in an isolated process via scripts/grounded_retrieval_qualification.py.
These public development labels are not a held-out or competitive benchmark.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import time
from typing import Any

from .. import harness
from .. import pattern_catalog

SCHEMA = "algo-grounded-retrieval-v2"
TOP_K = 5
READ_CHARS = 20_000


@dataclass(frozen=True)
class Evidence:
    record_id: str
    anchor: str


@dataclass(frozen=True)
class Case:
    name: str
    category: str
    query: str
    evidence: tuple[Evidence, ...]
    kind: str | None = None


def _doc(name: str, anchor: str, kind: str = "wiki") -> Evidence:
    return Evidence(f"algo-cli:{kind}:{name}.md", anchor)


AUTH = _doc("provider-auth-recovery", "never restart the agent or tool batch")
REVIEW = _doc("supervised-action-review", "--review-actions")
PRIVACY = _doc("privacy-and-context", "If the selected model uses a cloud provider")
MEMORY = _doc("ada-algo-cli-memory-lifecycle-contract", "User instructions and verified live files", "memory")
EXECUTION = _doc("algo-cli-execution-verification-contract", "An unresolved or unverified mutation", "memory")
EFFECTIVENESS = _doc("algo-cli-algorithm-evidence-contract", "not a duplicate test-only", "memory")
CAPABILITY = _doc("runtime-capability-catalog", "discovery evidence, not authority")
EXTERNAL = _doc("external-agent-store-operations", "Conflicting records from different harnesses")
ARCHITECTURE = _doc("main-split-map", "context_budget.py")
ECHO = _doc("echo-veil-security-status", "production")

# Freeze labels before observing rankings. Multiple evidence entries are all
# required; unrelated hits are unjudged, not automatically irrelevant.
CASES = (
    Case("empty_stream", "exact", "Codex Responses stream completed without text or a valid tool call retry", (AUTH,)),
    Case(
        "reasoning_without_answer",
        "paraphrase",
        "The provider only thought and never answered. Can I repeat the request without repeating tools?",
        (AUTH,),
    ),
    Case(
        "login_recovery",
        "multilingual",
        "La sesion caduco y no puedo autenticarme. Como vuelvo a iniciar sesion?",
        (AUTH,),
    ),
    Case("terminal_review", "exact", "--review-actions --approval-mode interactive", (REVIEW,)),
    Case(
        "approve_one_action",
        "paraphrase",
        "How can a person inspect the full proposed change and allow that one operation from the terminal?",
        (REVIEW,),
    ),
    Case("supervisor_channel", "exact", "supervised action review private socket nonce digest timeout", (REVIEW,)),
    Case(
        "cloud_context",
        "paraphrase",
        "Will pieces of my local files leave this computer when I ask a remotely hosted model?",
        (PRIVACY,),
    ),
    Case("opt_in_context", "exact", "code-rag on explicit consent privacy local context", (PRIVACY,)),
    Case(
        "memory_authority",
        "paraphrase",
        "An old saved note disagrees with the file I just checked. Which one should I trust?",
        (MEMORY,),
    ),
    Case(
        "memory_placement", "exact", "memory lifecycle placement matrix bounded capture retention", (MEMORY,), "memory"
    ),
    Case(
        "uncertain_resume",
        "paraphrase",
        "A previous attempt may already have changed something before it crashed. Is restarting it safe?",
        (EXECUTION,),
    ),
    Case(
        "journal_contract",
        "exact",
        "durable Agent checkpoints hash-chained journal unresolved mutation",
        (EXECUTION,),
        "memory",
    ),
    Case(
        "idea_is_not_benefit",
        "paraphrase",
        "A pattern is listed and its module imports. Does that prove it improves real tasks?",
        (EFFECTIVENESS,),
    ),
    Case(
        "algorithm_evidence",
        "exact",
        "algorithm effectiveness pinned baseline parity oracle production boundary",
        (EFFECTIVENESS,),
        "memory",
    ),
    Case(
        "tool_catalog",
        "exact",
        "runtime capability catalog ActionSpec registry prerequisites limitations",
        (CAPABILITY,),
    ),
    Case(
        "discovery_not_permission",
        "paraphrase",
        "I found a tool in search. Does finding its description give permission to run it?",
        (CAPABILITY,),
    ),
    Case(
        "conflicting_stores",
        "paraphrase",
        "Two different agents stored contradictory instructions. Should one disappear from search?",
        (EXTERNAL,),
    ),
    Case(
        "external_availability",
        "exact",
        "external agent store supported configured indexed runtime-disabled sources",
        (EXTERNAL,),
    ),
    Case("orchestrator_modules", "exact", "main.py decomposition map context_budget nathan_runtime", (ARCHITECTURE,)),
    Case(
        "prompt_owner",
        "paraphrase",
        "Where did the code that builds and prunes the system prompt move out of the large command line entrypoint?",
        (ARCHITECTURE,),
    ),
    Case("echo_limitations", "exact", "Echo Veil security status production blockers entry-point matrix", (ECHO,)),
    Case(
        "echo_isolation",
        "paraphrase",
        "Does encrypted local memory mean a compromised host cannot read it during execution?",
        (ECHO,),
    ),
    Case("provider_privacy_es", "multilingual", "Se envian mis archivos locales al proveedor en la nube?", (PRIVACY,)),
    Case(
        "two_evidence_sources",
        "multi_evidence",
        "Find the runtime capability discovery contract and the algorithm effectiveness evidence contract, distinguishing permission from measured benefit.",
        (CAPABILITY, EFFECTIVENESS),
    ),
)


def digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    )


def source_snapshot(index: dict[str, Any]) -> dict[str, str]:
    """Bind production code, indexed public bytes, and their ranking inputs."""
    rows = index.get("records", [])
    if not rows or any(not harness._protected_memory_record_allowed(row) for row in rows):
        raise ValueError("grounded evaluation requires a nonempty public-only corpus")
    paths = {Path(str(row["path"])) for row in rows if row.get("path")}
    paths.update(
        Path(__file__).parents[1] / name for name in ("harness.py", "retrieval_algorithms.py", "pattern_catalog.py")
    )
    paths.add(Path(__file__))
    paths.add(Path(__file__).with_name("grounded_retrieval_validation.py"))
    files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return {
        "source_bytes": digest(files),
        "index": digest(
            [{key: value for key, value in row.items() if key not in {"path", "file_mtime_ns"}} for row in rows]
        ),
    }


def _read_evidence(evidence: Evidence) -> bool:
    text = harness.read_record(evidence.record_id, max_chars=READ_CHARS)
    # Check the body, not the synthetic title/source wrapper returned by read.
    parts = text.split("\n\n", 2)
    return not text.startswith("Error:") and len(parts) == 3 and evidence.anchor.casefold() in parts[2].casefold()


def _case_hits(case: Case, embed_fn: harness.EmbedFn, model: str) -> list[dict[str, Any]]:
    hits = harness.hybrid_search(case.query, embed_fn, model, k=TOP_K, harness="algo-cli", kind=case.kind)
    ids = [row["id"] for row in hits]
    if len(ids) != len(set(ids)) or len(ids) > TOP_K:
        raise ValueError("invalid ranking cardinality")
    if any(row.get("harness") != "algo-cli" or (case.kind and row.get("kind") != case.kind) for row in hits):
        raise ValueError("retrieval filter violation")
    return hits


def run_grounded_retrieval(
    embed_fn: harness.EmbedFn,
    model: str,
    *,
    repetitions: int = 2,
    cases: tuple[Case, ...] | None = None,
    exclusion_cases: tuple[Case, ...] = (),
) -> dict[str, Any]:
    if type(repetitions) is not int or not 2 <= repetitions <= 10:
        raise ValueError("repetitions must be between 2 and 10")
    cases = CASES if cases is None else cases
    inventory = (*cases, *exclusion_cases)
    if (
        not cases
        or len({case.name for case in inventory}) != len(inventory)
        or any(
            not case.query.strip()
            or not case.evidence
            or len(set(case.evidence)) != len(case.evidence)
            or any(not item.record_id or not item.anchor.strip() for item in case.evidence)
            for case in inventory
        )
    ):
        raise ValueError("invalid case inventory")
    index = harness.load_index()
    started = source_snapshot(index)
    context = pattern_catalog.with_active_conflicts(harness.runtime_pattern_context(), index["records"])
    records = {row["id"]: row for row in index["records"]}
    samples: list[dict[str, Any]] = []
    policy_samples: list[dict[str, Any]] = []
    label_failures = []
    for case in inventory:
        for evidence in case.evidence:
            try:
                record = records.get(evidence.record_id)
                valid = bool(
                    record
                    and record.get("harness") == "algo-cli"
                    and (not case.kind or record.get("kind") == case.kind)
                )
                if valid and record is not None:
                    excluded = harness.is_excluded_from_retrieval(record)
                    valid = (
                        excluded
                        if case in exclusion_cases
                        else not excluded and not pattern_catalog.exclusion_reasons(record, context)
                    )
                if not valid or not _read_evidence(evidence):
                    label_failures.append(evidence.record_id)
            except (OSError, ValueError, KeyError):
                label_failures.append(evidence.record_id)
    # A readable historical source is not a valid positive target. Do not spend
    # provider calls on an invalid inventory or count policy absence as recall.
    for repetition in range(repetitions if not label_failures else 0):
        for case in cases:
            begin = time.perf_counter_ns()
            error = None
            try:
                hits = _case_hits(case, embed_fn, model)
                ids = [row["id"] for row in hits]
                matched = [item for item in case.evidence if item.record_id in ids and _read_evidence(item)]
            except Exception as exc:
                ids, hits, matched = [], [], []
                error = type(exc).__name__
            elapsed = (time.perf_counter_ns() - begin) / 1_000_000
            ranks = [ids.index(item.record_id) + 1 for item in matched]
            vector_hits = sum("vector" in row.get("rank_sources", []) for row in hits)
            samples.append(
                {
                    "case": case.name,
                    "category": case.category,
                    "repetition": repetition,
                    "ranked_ids": ids,
                    "matched_ids": [item.record_id for item in matched],
                    "recall_at_k": len(matched) / len(case.evidence),
                    "reciprocal_rank": 1 / min(ranks) if ranks else 0.0,
                    "passed": len(matched) == len(case.evidence) and vector_hits > 0,
                    "error": error,
                    "search_and_read_ms": round(elapsed, 6),
                    "vector_hits": vector_hits,
                }
            )
        for case in exclusion_cases:
            error = None
            ids = []
            try:
                ids = [row["id"] for row in _case_hits(case, embed_fn, model)]
            except Exception as exc:
                error = type(exc).__name__
            policy_samples.append(
                {
                    "case": case.name,
                    "repetition": repetition,
                    "ranked_ids": ids,
                    "passed": error is None and all(item.record_id not in ids for item in case.evidence),
                    "error": error,
                }
            )
    try:
        stable = source_snapshot(harness.load_index()) == started
    except (OSError, ValueError):
        stable = False
    return {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "public development corpus; not held-out, generated-answer, Echo-memory, or competitor qualification",
        "status": "pass"
        if stable and not label_failures and all(row["passed"] for row in [*samples, *policy_samples])
        else "fail",
        "stage": "label_validation" if label_failures else "retrieval",
        "source_stable": stable,
        "sources": started,
        "model": model,
        "cases": [asdict(case) for case in cases],
        "case_digest": digest([asdict(case) for case in cases]),
        "exclusion_cases": [asdict(case) for case in exclusion_cases],
        "exclusion_case_digest": digest([asdict(case) for case in exclusion_cases]),
        "top_k": TOP_K,
        "read_chars": READ_CHARS,
        "repetitions": repetitions,
        "label_failures": sorted(set(label_failures)),
        "metrics": {
            "samples": len(samples),
            "passed": sum(row["passed"] for row in samples),
            "mean_labeled_recall_at_k": statistics.mean(row["recall_at_k"] for row in samples) if samples else 0.0,
            "mrr": statistics.mean(row["reciprocal_rank"] for row in samples) if samples else 0.0,
            "first_pass_median_ms": statistics.median(
                row["search_and_read_ms"] for row in samples if row["repetition"] == 0
            )
            if samples
            else 0.0,
            "repeat_median_ms": statistics.median(row["search_and_read_ms"] for row in samples if row["repetition"] > 0)
            if samples
            else 0.0,
        },
        "policy_metrics": {"samples": len(policy_samples), "passed": sum(row["passed"] for row in policy_samples)},
        "timing_scope": "query embedding/cache lookup, production hybrid search, and matched-source read; corpus build excluded",
        "samples": samples,
        "policy_samples": policy_samples,
    }
