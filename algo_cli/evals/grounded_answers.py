"""Frozen public-document answer checks, not general semantic entailment proof."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping

SCHEMA = "algo-cli-documentation-answers-v2"
MAX_ANSWER_CHARS = 32_768
MAX_EVIDENCE = 12
GUIDANCE = (
    "This is a read-only Algo CLI documentation task. Use harness_search and harness_read to answer "
    "the question below. You may search again to find the authoritative source. Use the shipped "
    "policy contracts or runtime capability registry as authority; algorithm examples alone are "
    "not sufficient. For tool discovery, read each capability record you name. Only harness_search "
    "and harness_read are permitted. Do not change files, run commands, query agent memory, or "
    "store memory. Return only one JSON object with exactly two keys: answers and evidence. "
    "answers must contain the requested fields with boolean/integer/array values as appropriate. "
    "evidence must be a list of objects with record_id and quote, naming each source you actually "
    "read and a 20-400 character verbatim quote from its body that supports your answer.\n"
)


@dataclass(frozen=True)
class Support:
    record_id: str
    text: str


@dataclass(frozen=True)
class AnswerCase:
    name: str
    question: str
    answers: dict[str, Any]
    claims: dict[str, tuple[Support, ...]]


MEMORY = "algo-cli:memory:ada-algo-cli-memory-lifecycle-contract.md"
PRIVACY = "algo-cli:wiki:privacy-and-context.md"
EXECUTION = "algo-cli:memory:algo-cli-execution-verification-contract.md"
CATALOG = "algo-cli:wiki:runtime-capability-catalog.md"
AUTH = "algo-cli:wiki:provider-auth-recovery.md"
EFFECTIVENESS = "algo-cli:memory:algo-cli-algorithm-evidence-contract.md"

PRIVACY_CLAIMS = {
    "local_context_can_leave_machine": (
        Support(PRIVACY, "If the active model uses a cloud provider, the assembled context may leave the machine."),
        Support(PRIVACY, "If the selected model uses a cloud provider, enabled local context may leave the machine."),
        Support(
            PRIVACY,
            "Retrieved source snippets are then added to the active chat request, so they may leave the machine when the selected inference provider is remote.",
        ),
    ),
    "embeddings_are_local": (Support(PRIVACY, "embeddings are generated through local Ollama."),),
}

CASES = (
    AnswerCase(
        "memory_authority",
        "An old saved note disagrees with the file I just checked. Which one should I trust?",
        {"live_evidence_overrides_saved_notes": True},
        {
            "live_evidence_overrides_saved_notes": (
                Support(
                    MEMORY,
                    "User instructions and verified live files, endpoints, and tool results outrank all persisted memory.",
                ),
            ),
        },
    ),
    AnswerCase(
        "provider_privacy_es",
        "Se envian mis archivos locales al proveedor en la nube?",
        {"local_context_can_leave_machine": True, "embeddings_are_local": True},
        PRIVACY_CLAIMS,
    ),
    AnswerCase(
        "cloud_privacy_de",
        "Werden lokale Dateien an den Cloud-Anbieter geschickt?",
        {"local_context_can_leave_machine": True, "embeddings_are_local": True},
        PRIVACY_CLAIMS,
    ),
    AnswerCase(
        "natural_file_tools",
        "Which tools create a file, apply an edit to an existing file, and show a working-tree diff?",
        {"tools": ["write_file", "edit_file", "git_diff"]},
        {
            "tools:write_file": (
                Support("algo-cli:runtime_capability:write_file", "Create or overwrite a local file."),
            ),
            "tools:edit_file": (
                Support(
                    "algo-cli:runtime_capability:edit_file",
                    "Make a precise, surgical edit to a text file using a find/replace match.",
                ),
            ),
            "tools:git_diff": (
                Support("algo-cli:runtime_capability:git_diff", "Show the current tracked Git diff against HEAD."),
            ),
        },
    ),
    AnswerCase(
        "crashed_edit",
        "After a crash, can I send the same file edit again without checking whether it already happened?",
        {"automatic_replay_allowed": False, "reconciliation_required": True},
        {
            field: (
                Support(
                    EXECUTION,
                    "An unresolved or unverified mutation blocks automatic replay and requires reconciliation.",
                ),
            )
            for field in ("automatic_replay_allowed", "reconciliation_required")
        },
    ),
    AnswerCase(
        "schema_permission",
        "A tool schema is visible to the model. Does that alone authorize write_file to edit my workspace?",
        {"discovery_grants_permission": False},
        {
            "discovery_grants_permission": (
                Support(CATALOG, "Capability records are discovery evidence, not authority."),
                Support(CATALOG, "Retrieving a record never grants permission or bypasses the registry."),
                Support(
                    EXECUTION,
                    "Every model-invoked tool path must pass through the shared runtime preflight before execution.",
                ),
            ),
        },
    ),
    AnswerCase(
        "empty_stream_control",
        "Codex Responses stream completed without text or a valid tool call. What is the automatic retry limit, and may it replay completed tools?",
        {"max_retries": 2, "replays_completed_tools": False},
        {
            "max_retries": (
                Support(AUTH, "Retry only the current Responses request, at most twice, after 1 and 2 seconds."),
                Support(EXECUTION, "Codex Responses may retry the current request twice"),
            ),
            "replays_completed_tools": (
                Support(
                    AUTH, "Keep the same request and previous tool results; never restart the agent or tool batch."
                ),
                Support(EXECUTION, "It does not replay completed tools."),
            ),
        },
    ),
    AnswerCase(
        "pattern_benefit_control",
        "A pattern is listed and its module imports. Does that prove it improves real tasks?",
        {"listing_and_import_prove_benefit": False},
        {
            "listing_and_import_prove_benefit": (
                Support(
                    EFFECTIVENESS,
                    "Contract readiness is valuable, but it must never be rendered as empirical effectiveness.",
                ),
            ),
        },
    ),
)


def normalized(text: str) -> str:
    return " ".join(text.split())


def prompt(case: AnswerCase) -> str:
    return GUIDANCE + "Question: " + case.question + "\nRequested answer fields: " + ", ".join(case.answers)


def protocol_cases() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "cases": [asdict(case) for case in CASES],
        "prompts": {case.name: prompt(case) for case in CASES},
        "limits": {
            "answer_chars": MAX_ANSWER_CHARS,
            "evidence_count": MAX_EVIDENCE,
            "quote_min_chars": 20,
            "quote_max_chars": 400,
        },
        "scope": "Public development tasks with frozen supporting spans; not general entailment, coding, held-out, protected-memory, or competitive qualification",
    }


def protocol_digest() -> str:
    payload = json.dumps(protocol_cases(), sort_keys=True, ensure_ascii=True, allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def validate_labels(case: AnswerCase, source_bodies: Mapping[str, str]) -> bool:
    """Every declared alternative must exist before running the model."""
    if not case.answers or not case.claims or {key.split(":", 1)[0] for key in case.claims} != set(case.answers):
        return False
    return all(
        supports
        and all(
            20 <= len(support.text) <= 400
            and bool(normalized(support.text))
            and type(source_bodies.get(support.record_id)) is str
            and normalized(support.text) in normalized(source_bodies[support.record_id])
            for support in supports
        )
        for supports in case.claims.values()
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("nonfinite_json")


def _same_answer(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return (
            len(actual) == len(expected)
            and all(type(value) is str for value in actual)
            and set(actual) == set(expected)
        )
    return actual == expected


def evaluate_answer(text: str, case: AnswerCase, read_bodies: Mapping[str, str]) -> dict[str, Any]:
    """Return fixed checks only; rejected model text and arbitrary IDs stay in memory."""
    checks = {
        name: False
        for name in (
            "valid_json_contract",
            "answer_values_correct",
            "quotes_bound_to_read_bodies",
            "all_claims_supported",
        )
    }
    cited: list[tuple[str, str]] = []
    if type(text) is str and len(text) <= MAX_ANSWER_CHARS:
        try:
            answer = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        except (ValueError, RecursionError):
            answer = None
        if type(answer) is dict and set(answer) == {"answers", "evidence"}:
            checks["valid_json_contract"] = True
            fields = answer["answers"]
            checks["answer_values_correct"] = (
                type(fields) is dict
                and set(fields) == set(case.answers)
                and all(_same_answer(fields[key], expected) for key, expected in case.answers.items())
            )
            evidence = answer["evidence"]
            valid_quotes = type(evidence) is list and 0 < len(evidence) <= MAX_EVIDENCE
            if valid_quotes:
                for item in evidence:
                    if type(item) is not dict or set(item) != {"record_id", "quote"}:
                        valid_quotes = False
                        continue
                    rid, quote = item["record_id"], item["quote"]
                    if (
                        type(rid) is not str
                        or type(quote) is not str
                        or not 20 <= len(quote) <= 400
                        or not normalized(quote)
                        or type(read_bodies.get(rid)) is not str
                        or normalized(quote) not in normalized(read_bodies[rid])
                    ):
                        valid_quotes = False
                        continue
                    cited.append((rid, normalized(quote)))
            checks["quotes_bound_to_read_bodies"] = valid_quotes
    supported = [
        name
        for name, alternatives in case.claims.items()
        if any(
            rid == support.record_id and normalized(support.text) in quote
            for support in alternatives
            for rid, quote in cited
        )
    ]
    checks["all_claims_supported"] = bool(case.claims) and len(supported) == len(case.claims)
    return {
        "schema": SCHEMA,
        "case": case.name,
        "passed": all(checks.values()),
        "checks": checks,
        "supported_claims": supported,
        "missing_claims": [name for name in case.claims if name not in supported],
        "model_output_included": False,
    }
