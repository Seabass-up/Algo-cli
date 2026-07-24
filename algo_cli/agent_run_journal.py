"""Durable, content-free checkpoints for crash-resumable Agent runs."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, NoReturn

from . import config
from . import git_evidence
from . import run_contract
from .private_event_store import PrivateEventStore, RetentionPolicy


AGENT_RUN_JOURNAL_SCHEMA_VERSION = 3
ZERO_HASH = "0" * 64
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_KINDS = frozenset(
    {
        "run_started",
        "run_resumed",
        "context_bound",
        "block_started",
        "recovery_started",
        "recovery_finished",
        "model_round_started",
        "model_round_completed",
        "tool_intent",
        "tool_result",
        "verifier_result",
        "block_finished",
        "run_finished",
    }
)
_TERMINAL_RUN_STATUSES = frozenset(
    {"complete", "partial", "failed", "cancelled"}
)
_TERMINAL_BLOCK_STATUSES = frozenset(
    {"complete", "partial", "failed", "cancelled"}
)


class AgentRunJournalError(RuntimeError):
    """Base error for Agent run checkpoint persistence."""


class AgentRunJournalCorrupt(AgentRunJournalError):
    """Raised when a journal chain or event violates its closed schema."""


@dataclass(frozen=True)
class AgentRunEvent:
    sequence: int
    kind: str
    previous_hash: str
    event_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentResumeState:
    """Verified boundary reconstructed without relying on model prose."""

    contract: run_contract.RunContract
    completed_block_ordinals: tuple[int, ...]
    next_block_ordinal: int
    last_verified_sequence: int
    last_workspace: dict[str, Any]
    uncertain_mutation_steps: tuple[str, ...]
    model_rounds: int
    tool_calls: int
    prompt_tokens: int
    terminal: bool
    terminal_status: str

    @property
    def can_resume(self) -> bool:
        return not self.terminal and not self.uncertain_mutation_steps

    def workspace_matches(self, snapshot: git_evidence.GitSnapshot) -> bool:
        expected = self.last_workspace or workspace_contract_view(
            self.contract.workspace
        )
        observed = workspace_view(snapshot)
        return observed == expected


@dataclass(frozen=True)
class VerifiedBlockCheckpoint:
    """Content-free link from a verified journal boundary to thread context."""

    ordinal: int
    role: str
    context_digest: str
    sequence: int
    workspace: dict[str, Any]


def journal_path(run_nonce: str) -> Path:
    safe = _safe_identifier(run_nonce, "run nonce")
    return config.CONFIG_DIR / "agent_runs" / f"{safe}.jsonl"


def digest_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def digest_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AgentRunJournalError("checkpoint value is not finite JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def workspace_view(snapshot: git_evidence.GitSnapshot) -> dict[str, Any]:
    """Return bounded, content-free workspace identity for reconciliation."""

    if not snapshot.available:
        return {"git_available": False}
    return {
        "git_available": True,
        "head": str(snapshot.head or ""),
        "status_digest": str(snapshot.status_digest or ""),
        "tracked_diff_digest": str(snapshot.tracked_diff_digest or ""),
        "untracked_digest": str(snapshot.untracked_digest or ""),
    }


def workspace_contract_view(
    workspace: run_contract.WorkspaceContract,
) -> dict[str, Any]:
    if not workspace.git_available:
        return {"git_available": False}
    return {
        "git_available": True,
        "head": workspace.initial_head,
        "status_digest": workspace.status_digest,
        "tracked_diff_digest": workspace.tracked_diff_digest,
        "untracked_digest": workspace.untracked_digest,
    }


def _safe_identifier(value: Any, label: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not _SAFE_ID_RE.fullmatch(text):
        raise AgentRunJournalError(
            f"{label} must be a bounded non-sensitive identifier"
        )
    return text


def _digest(value: Any, label: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
        return ""
    if not _DIGEST_RE.fullmatch(text):
        raise AgentRunJournalError(f"{label} must be a SHA-256 digest")
    return text


def _ordinal(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 32:
        raise AgentRunJournalError(f"{label} must be an integer from 0 to 31")
    return value


def _next_block_ordinal(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 32
    ):
        raise AgentRunJournalError(
            "next block ordinal must be an integer from 0 to 32"
        )
    return value


def _round(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2_048:
        raise AgentRunJournalError(
            "model round must be an integer from 0 to 2047"
        )
    return value


def _attempt(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 1
    ):
        raise AgentRunJournalError(
            "attempt must be an integer from 0 to 1"
        )
    return value


def _canonical_event_body(
    *,
    run_nonce: str,
    contract_digest: str,
    sequence: int,
    kind: str,
    previous_hash: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": AGENT_RUN_JOURNAL_SCHEMA_VERSION,
        "run_nonce": run_nonce,
        "contract_digest": contract_digest,
        "sequence": sequence,
        "kind": kind,
        "previous_hash": previous_hash,
        "payload": dict(payload),
    }


def _hash_body(body: Mapping[str, Any]) -> str:
    return digest_json(body)


def _decode_event(
    raw: Any,
    *,
    run_nonce: str,
    contract_digest: str,
    expected_sequence: int,
    expected_previous_hash: str,
) -> AgentRunEvent:
    if not isinstance(raw, dict):
        raise AgentRunJournalCorrupt("journal event must be an object")
    expected_fields = {
        "schema_version",
        "run_nonce",
        "contract_digest",
        "sequence",
        "kind",
        "previous_hash",
        "payload",
        "event_hash",
    }
    if set(raw) != expected_fields:
        raise AgentRunJournalCorrupt("journal event fields do not match schema")
    if raw.get("schema_version") != AGENT_RUN_JOURNAL_SCHEMA_VERSION:
        raise AgentRunJournalCorrupt("unsupported journal schema")
    if raw.get("run_nonce") != run_nonce:
        raise AgentRunJournalCorrupt("journal run nonce changed")
    if raw.get("contract_digest") != contract_digest:
        raise AgentRunJournalCorrupt("journal contract digest changed")
    if raw.get("sequence") != expected_sequence:
        raise AgentRunJournalCorrupt("journal sequence is not contiguous")
    kind = str(raw.get("kind") or "")
    if kind not in _KINDS:
        raise AgentRunJournalCorrupt("journal event kind is invalid")
    previous_hash = str(raw.get("previous_hash") or "")
    if previous_hash != expected_previous_hash:
        raise AgentRunJournalCorrupt("journal hash chain is broken")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        raise AgentRunJournalCorrupt("journal event payload must be an object")
    event_hash = str(raw.get("event_hash") or "")
    if not _DIGEST_RE.fullmatch(event_hash):
        raise AgentRunJournalCorrupt("journal event hash is invalid")
    body = {key: raw[key] for key in expected_fields - {"event_hash"}}
    if _hash_body(body) != event_hash:
        raise AgentRunJournalCorrupt("journal event hash does not match content")
    return AgentRunEvent(
        sequence=expected_sequence,
        kind=kind,
        previous_hash=previous_hash,
        event_hash=event_hash,
        payload=dict(payload),
    )


def _exact_payload(
    payload: Mapping[str, Any],
    expected: set[str],
    kind: str,
) -> None:
    if set(payload) != expected:
        raise AgentRunJournalCorrupt(
            f"{kind} payload fields do not match schema"
        )


def _validate_workspace_payload(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise AgentRunJournalCorrupt(
            "journal workspace payload must be an object"
        )
    if type(raw.get("git_available")) is not bool:
        raise AgentRunJournalCorrupt(
            "journal workspace availability must be boolean"
        )
    if raw["git_available"] is False:
        _exact_payload(raw, {"git_available"}, "workspace")
        return
    _exact_payload(
        raw,
        {
            "git_available",
            "head",
            "status_digest",
            "tracked_diff_digest",
            "untracked_digest",
        },
        "workspace",
    )
    head = str(raw.get("head") or "")
    if head and (
        len(head) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in head)
    ):
        raise AgentRunJournalCorrupt(
            "journal workspace HEAD is invalid"
        )
    for field_name in (
        "status_digest",
        "tracked_diff_digest",
        "untracked_digest",
    ):
        try:
            _digest(raw.get(field_name), field_name, allow_empty=True)
        except AgentRunJournalError as exc:
            raise AgentRunJournalCorrupt(str(exc)) from exc


def _validate_event_payload(
    event: AgentRunEvent,
    contract: run_contract.RunContract,
) -> None:
    payload = event.payload
    kind = event.kind
    try:
        if kind == "run_started":
            _exact_payload(payload, {"contract"}, kind)
            return
        if kind == "run_resumed":
            _exact_payload(
                payload,
                {"next_block_ordinal", "last_verified_sequence"},
                kind,
            )
            _next_block_ordinal(payload["next_block_ordinal"])
            last_verified = payload["last_verified_sequence"]
            if (
                isinstance(last_verified, bool)
                or not isinstance(last_verified, int)
                or not -1 <= last_verified < event.sequence
            ):
                raise AgentRunJournalError(
                    "resume sequence is outside the prior journal"
                )
            return
        if kind == "context_bound":
            _exact_payload(
                payload,
                {
                    "context_schema_version",
                    "context_digest",
                    "max_tokens",
                    "base_tokens",
                    "used_tokens",
                    "included_sources",
                    "truncated_sources",
                    "omitted_sources",
                },
                kind,
            )
            if payload["context_schema_version"] != 1:
                raise AgentRunJournalError(
                    "context receipt schema is unsupported"
                )
            _digest(payload["context_digest"], "context digest")
            max_tokens = payload["max_tokens"]
            base_tokens = payload["base_tokens"]
            used_tokens = payload["used_tokens"]
            if (
                isinstance(max_tokens, bool)
                or not isinstance(max_tokens, int)
                or max_tokens < 1
                or isinstance(base_tokens, bool)
                or not isinstance(base_tokens, int)
                or not 0 <= base_tokens <= max_tokens
                or isinstance(used_tokens, bool)
                or not isinstance(used_tokens, int)
                or not base_tokens <= used_tokens <= max_tokens
            ):
                raise AgentRunJournalError(
                    "context token receipt is invalid"
                )
            all_names: list[str] = []
            for field_name in (
                "included_sources",
                "truncated_sources",
                "omitted_sources",
            ):
                values = payload[field_name]
                if not isinstance(values, list):
                    raise AgentRunJournalError(
                        "context sources must be lists"
                    )
                cleaned = [
                    _safe_identifier(
                        value,
                        "context source",
                    )
                    for value in values
                ]
                if len(cleaned) != len(set(cleaned)):
                    raise AgentRunJournalError(
                        "context source list contains duplicates"
                    )
                if field_name != "truncated_sources":
                    all_names.extend(cleaned)
            if len(all_names) != len(set(all_names)):
                raise AgentRunJournalError(
                    "included and omitted context sources overlap"
                )
            if not set(payload["truncated_sources"]).issubset(
                payload["included_sources"]
            ):
                raise AgentRunJournalError(
                    "truncated context sources must be included"
                )
            return
        if kind == "block_started":
            _exact_payload(payload, {"ordinal", "role"}, kind)
            ordinal = _ordinal(payload["ordinal"], "block ordinal")
            role = _safe_identifier(payload["role"], "block role")
            if contract.block(ordinal).role != role:
                raise AgentRunJournalError(
                    "block role differs from the run contract"
                )
            return
        if kind == "recovery_started":
            _exact_payload(
                payload,
                {"ordinal", "attempt", "recovery_code"},
                kind,
            )
            ordinal = _ordinal(payload["ordinal"], "block ordinal")
            attempt = _attempt(payload["attempt"])
            code = _safe_identifier(
                payload["recovery_code"],
                "recovery code",
            )
            block = contract.block(ordinal)
            if (
                attempt > block.max_recovery_attempts
                or code not in block.recovery_codes
            ):
                raise AgentRunJournalError(
                    "recovery exceeds the run contract"
                )
            return
        if kind == "recovery_finished":
            _exact_payload(
                payload,
                {"ordinal", "attempt", "status", "context_digest"},
                kind,
            )
            ordinal = _ordinal(payload["ordinal"], "block ordinal")
            attempt = _attempt(payload["attempt"])
            if attempt > contract.block(ordinal).max_recovery_attempts:
                raise AgentRunJournalError(
                    "recovery attempt exceeds the run contract"
                )
            if payload["status"] not in _TERMINAL_BLOCK_STATUSES:
                raise AgentRunJournalError(
                    "recovery status is not terminal"
                )
            _digest(payload["context_digest"], "context digest")
            return
        if kind in {"model_round_started", "model_round_completed"}:
            expected = {"ordinal", "round", "attempt"}
            if kind == "model_round_started":
                expected.add("prompt_tokens")
            if kind == "model_round_completed":
                expected |= {
                    "status",
                    "tool_call_count",
                    "response_digest",
                }
            _exact_payload(payload, expected, kind)
            ordinal = _ordinal(payload["ordinal"], "block ordinal")
            _round(payload["round"])
            attempt = _attempt(payload["attempt"])
            if attempt > contract.block(ordinal).max_recovery_attempts:
                raise AgentRunJournalError(
                    "model attempt exceeds the run contract"
                )
            if kind == "model_round_started":
                prompt_tokens = payload["prompt_tokens"]
                if (
                    isinstance(prompt_tokens, bool)
                    or not isinstance(prompt_tokens, int)
                    or not 0
                    <= prompt_tokens
                    <= contract.budget.max_prompt_tokens_per_round
                ):
                    raise AgentRunJournalError(
                        "model prompt tokens exceed the run contract"
                    )
            if kind == "model_round_completed":
                _safe_identifier(
                    payload["status"],
                    "model round status",
                )
                count = payload["tool_call_count"]
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or not 0 <= count <= 256
                ):
                    raise AgentRunJournalError(
                        "tool call count is invalid"
                    )
                _digest(payload["response_digest"], "response digest")
            return
        if kind == "tool_intent":
            _exact_payload(
                payload,
                {
                    "step_id",
                    "ordinal",
                    "round",
                    "attempt",
                    "tool_index",
                    "action",
                    "args_digest",
                    "call_id_hash",
                    "mutating",
                    "idempotency",
                    "target_hash",
                },
                kind,
            )
            ordinal = _ordinal(payload["ordinal"], "block ordinal")
            round_number = _round(payload["round"])
            attempt = _attempt(payload["attempt"])
            if attempt > contract.block(ordinal).max_recovery_attempts:
                raise AgentRunJournalError(
                    "tool attempt exceeds the run contract"
                )
            tool_index = payload["tool_index"]
            if (
                isinstance(tool_index, bool)
                or not isinstance(tool_index, int)
                or not 0 <= tool_index < 256
            ):
                raise AgentRunJournalError("tool index is invalid")
            expected_step = (
                f"b{ordinal}-r{round_number}-t{tool_index}"
                if attempt == 0
                else (
                    f"b{ordinal}-a{attempt}-"
                    f"r{round_number}-t{tool_index}"
                )
            )
            if payload["step_id"] != expected_step:
                raise AgentRunJournalError(
                    "tool step ID does not match its coordinates"
                )
            _safe_identifier(payload["action"], "action")
            _digest(payload["args_digest"], "arguments digest")
            _digest(payload["call_id_hash"], "call ID hash")
            if type(payload["mutating"]) is not bool:
                raise AgentRunJournalError(
                    "tool mutation flag must be boolean"
                )
            _safe_identifier(payload["idempotency"], "idempotency")
            _digest(payload["target_hash"], "target hash")
            return
        if kind == "tool_result":
            _exact_payload(
                payload,
                {
                    "step_id",
                    "status",
                    "invoked",
                    "verification",
                    "effect_id",
                    "idempotency_key",
                    "error_code",
                    "deduplicated",
                },
                kind,
            )
            _safe_identifier(payload["step_id"], "step ID")
            _safe_identifier(payload["status"], "tool status")
            _safe_identifier(
                payload["verification"],
                "verification status",
            )
            _safe_identifier(
                payload["effect_id"],
                "effect ID",
                allow_empty=True,
            )
            _digest(
                payload["idempotency_key"],
                "idempotency key",
                allow_empty=True,
            )
            _safe_identifier(
                payload["error_code"],
                "error code",
                allow_empty=True,
            )
            if (
                type(payload["invoked"]) is not bool
                or type(payload["deduplicated"]) is not bool
            ):
                raise AgentRunJournalError(
                    "tool result flags must be boolean"
                )
            return
        if kind == "verifier_result":
            _exact_payload(
                payload,
                {"ordinal", "verifier", "status", "workspace"},
                kind,
            )
            _ordinal(payload["ordinal"], "block ordinal")
            _safe_identifier(payload["verifier"], "verifier")
            if payload["status"] not in {"passed", "failed"}:
                raise AgentRunJournalError(
                    "verifier status must be passed or failed"
                )
            _validate_workspace_payload(payload["workspace"])
            return
        if kind == "block_finished":
            _exact_payload(
                payload,
                {
                    "ordinal",
                    "role",
                    "status",
                    "verified",
                    "context_digest",
                    "workspace",
                },
                kind,
            )
            ordinal = _ordinal(payload["ordinal"], "block ordinal")
            if payload["role"] != contract.block(ordinal).role:
                raise AgentRunJournalError(
                    "finished block role differs from the run contract"
                )
            if payload["status"] not in _TERMINAL_BLOCK_STATUSES:
                raise AgentRunJournalError(
                    "finished block status is not terminal"
                )
            if type(payload["verified"]) is not bool:
                raise AgentRunJournalError(
                    "finished block verification flag is invalid"
                )
            _digest(payload["context_digest"], "context digest")
            _validate_workspace_payload(payload["workspace"])
            return
        if kind == "run_finished":
            _exact_payload(
                payload,
                {"status", "last_verified_sequence"},
                kind,
            )
            if payload["status"] not in _TERMINAL_RUN_STATUSES:
                raise AgentRunJournalError(
                    "run status is not terminal"
                )
            last_verified = payload["last_verified_sequence"]
            if (
                isinstance(last_verified, bool)
                or not isinstance(last_verified, int)
                or not -1 <= last_verified < event.sequence
            ):
                raise AgentRunJournalError(
                    "last verified sequence is invalid"
                )
            return
        raise AgentRunJournalError("unsupported journal event kind")
    except (
        AgentRunJournalError,
        run_contract.RunContractViolation,
    ) as exc:
        raise AgentRunJournalCorrupt(
            f"{kind} payload is invalid: {exc}"
        ) from exc


def _validate_event_sequence(
    events: tuple[AgentRunEvent, ...],
    contract: run_contract.RunContract,
) -> None:
    """Reject authenticated events that form an impossible execution history.

    A hash chain proves that bytes were not changed after they were appended.
    It does not, by itself, prove that a verifier preceded a verified boundary,
    that tool results balance model calls, or that a resume abandons only a
    replay-safe partial attempt. This state machine supplies those semantics.
    Incomplete prefixes remain valid because a process may stop after any
    durable write.
    """

    if not events:
        return
    active_ordinal: int | None = None
    active_round: tuple[int, int, int] | None = None
    open_batch: dict[str, Any] | None = None
    pending_intents: dict[str, dict[str, Any]] = {}
    verifier_status: dict[str, str] = {}
    verifier_workspace: dict[str, dict[str, Any]] = {}
    mutating_steps_by_ordinal: dict[int, set[str]] = {}
    completed_ordinals: list[int] = []
    next_execution_ordinal = 0
    last_verified_sequence = -1
    checkpoint_gap = False
    recovery_active = False
    terminal = False

    def reject(reason: str) -> NoReturn:
        raise AgentRunJournalCorrupt(
            f"journal event sequence is invalid: {reason}"
        )

    def batch_is_closed() -> bool:
        if open_batch is None:
            return True
        intents = open_batch["intents"]
        results = open_batch["results"]
        return (
            len(intents) == open_batch["expected"]
            and results == set(intents)
        )

    def recorded_intents_are_closed() -> bool:
        if open_batch is None:
            return True
        return open_batch["results"] == set(open_batch["intents"])

    for index, event in enumerate(events):
        kind = event.kind
        payload = event.payload
        if terminal:
            reject("terminal run has trailing events")
        if kind == "run_started":
            if index != 0:
                reject("run_started is not first")
            continue
        if index == 0:
            reject("journal does not begin with run_started")

        if kind == "run_resumed":
            expected_next = len(completed_ordinals)
            if payload["next_block_ordinal"] != expected_next:
                reject("resume boundary differs from verified blocks")
            if payload["last_verified_sequence"] != last_verified_sequence:
                reject("resume sequence differs from verified boundary")
            if active_round is not None:
                reject("resume abandons an in-flight model round")
            if not recorded_intents_are_closed():
                reject("resume abandons tool intents without results")
            if any(mutating_steps_by_ordinal.values()):
                reject("resume abandons an unverified mutating attempt")
            active_ordinal = None
            active_round = None
            open_batch = None
            pending_intents.clear()
            verifier_status.clear()
            verifier_workspace.clear()
            recovery_active = False
            checkpoint_gap = False
            next_execution_ordinal = expected_next
            continue

        if kind == "context_bound":
            if active_ordinal is not None:
                reject("context was rebound during an active block")
            continue

        if kind == "block_started":
            ordinal = int(payload["ordinal"])
            if active_ordinal is not None:
                reject("a second block started before the first finished")
            if ordinal != next_execution_ordinal:
                reject("block did not start at the next execution ordinal")
            active_ordinal = ordinal
            active_round = None
            open_batch = None
            pending_intents.clear()
            verifier_status.clear()
            verifier_workspace.clear()
            recovery_active = False
            continue

        if kind == "model_round_started":
            ordinal = int(payload["ordinal"])
            attempt = int(payload["attempt"])
            if active_ordinal != ordinal:
                reject("model round is outside the active block")
            if active_round is not None or not batch_is_closed():
                reject("model round overlaps an unfinished round")
            if attempt == 0 and recovery_active:
                reject("primary model round started during recovery")
            if attempt > 0 and not recovery_active:
                reject("recovery model round lacks recovery_started")
            active_round = (
                ordinal,
                int(payload["round"]),
                attempt,
            )
            open_batch = None
            continue

        if kind == "model_round_completed":
            coordinates = (
                int(payload["ordinal"]),
                int(payload["round"]),
                int(payload["attempt"]),
            )
            if active_round != coordinates:
                reject("model completion has no matching start")
            active_round = None
            expected = int(payload["tool_call_count"])
            open_batch = (
                None
                if expected == 0
                else {
                    "coordinates": coordinates,
                    "expected": expected,
                    "intents": [],
                    "results": set(),
                    "results_started": False,
                }
            )
            continue

        if kind == "tool_intent":
            if open_batch is None:
                reject("tool intent has no model tool batch")
            coordinates = (
                int(payload["ordinal"]),
                int(payload["round"]),
                int(payload["attempt"]),
            )
            if open_batch["coordinates"] != coordinates:
                reject("tool intent coordinates differ from its model batch")
            intents = open_batch["intents"]
            if open_batch["results_started"]:
                reject("tool intent was appended after tool results began")
            if len(intents) >= open_batch["expected"]:
                reject("tool intents exceed the model-declared count")
            if payload["tool_index"] != len(intents):
                reject("tool intent indices are not contiguous")
            step_id = str(payload["step_id"])
            if step_id in pending_intents:
                reject("tool intent step is duplicated")
            intents.append(step_id)
            pending_intents[step_id] = dict(payload)
            if payload["mutating"] is True:
                mutating_steps_by_ordinal.setdefault(
                    int(payload["ordinal"]),
                    set(),
                ).add(step_id)
            continue

        if kind == "tool_result":
            if open_batch is None:
                reject("tool result has no model tool batch")
            step_id = str(payload["step_id"])
            if step_id not in pending_intents:
                reject("tool result has no unmatched intent")
            if step_id in open_batch["results"]:
                reject("tool result is duplicated")
            open_batch["results_started"] = True
            open_batch["results"].add(step_id)
            pending_intents.pop(step_id)
            if batch_is_closed():
                open_batch = None
            continue

        if kind == "recovery_started":
            ordinal = int(payload["ordinal"])
            if active_ordinal != ordinal:
                reject("recovery is outside the active block")
            if recovery_active:
                reject("recovery attempt is duplicated")
            if active_round is not None or not batch_is_closed():
                reject("recovery overlaps an unfinished model/tool round")
            recovery_active = True
            continue

        if kind == "recovery_finished":
            ordinal = int(payload["ordinal"])
            if active_ordinal != ordinal or not recovery_active:
                reject("recovery completion has no matching start")
            if active_round is not None or not batch_is_closed():
                reject("recovery finished with an open model/tool round")
            recovery_active = False
            continue

        if kind == "verifier_result":
            ordinal = int(payload["ordinal"])
            if active_ordinal != ordinal:
                reject("verifier is outside the active block")
            if active_round is not None or not recorded_intents_are_closed():
                reject("verifier ran before recorded work settled")
            verifier = str(payload["verifier"])
            verifier_status[verifier] = str(payload["status"])
            workspace = payload["workspace"]
            if isinstance(workspace, dict):
                verifier_workspace[verifier] = dict(workspace)
            continue

        if kind == "block_finished":
            ordinal = int(payload["ordinal"])
            if active_ordinal != ordinal:
                reject("block completion has no matching start")
            if active_round is not None:
                reject("block finished during an in-flight model round")
            if pending_intents or not recorded_intents_are_closed():
                reject("block finished with tool intents lacking results")
            if recovery_active:
                reject("block finished before recovery_finished")
            verified = payload["verified"] is True
            if open_batch is not None and not batch_is_closed():
                if verified or payload["status"] == "complete":
                    reject("verified block has an incomplete tool batch")
            if verified:
                if payload["status"] != "complete":
                    reject("only complete blocks may be verified")
                block = contract.block(ordinal)
                output_verifier = (
                    "final_output"
                    if block.role == "final"
                    else "block_output"
                )
                required = {output_verifier}
                if block.requires_change:
                    required.add("post_mutation")
                if any(
                    verifier_status.get(verifier) != "passed"
                    for verifier in required
                ):
                    reject("verified block lacks its passed verifiers")
                workspace = payload["workspace"]
                if any(
                    verifier_workspace.get(verifier) != workspace
                    for verifier in required
                ):
                    reject("verified block workspace differs from verifier evidence")
                if (
                    not checkpoint_gap
                    and ordinal == len(completed_ordinals)
                ):
                    completed_ordinals.append(ordinal)
                    last_verified_sequence = event.sequence
                    mutating_steps_by_ordinal.pop(ordinal, None)
                else:
                    checkpoint_gap = True
            else:
                checkpoint_gap = True
            next_execution_ordinal = ordinal + 1
            active_ordinal = None
            active_round = None
            open_batch = None
            pending_intents.clear()
            verifier_status.clear()
            verifier_workspace.clear()
            recovery_active = False
            continue

        if kind == "run_finished":
            if active_ordinal is not None or active_round is not None:
                reject("run finished with an active block")
            if open_batch is not None or pending_intents:
                reject("run finished with unsettled tool protocol")
            if payload["last_verified_sequence"] != last_verified_sequence:
                reject("terminal receipt names the wrong verified sequence")
            terminal = True
            continue

        reject(f"unsupported event kind {kind}")


class AgentRunJournal:
    """Single-writer append-only journal for one immutable Run Contract."""

    def __init__(
        self,
        *,
        contract: run_contract.RunContract,
        store: PrivateEventStore,
    ) -> None:
        if not isinstance(contract, run_contract.RunContract):
            raise AgentRunJournalError("journal requires a RunContract")
        self.contract = contract
        self.store = store

    @classmethod
    def create(
        cls,
        contract: run_contract.RunContract,
        *,
        path: Path | None = None,
    ) -> "AgentRunJournal":
        store = PrivateEventStore(
            path or journal_path(contract.run_nonce),
            policy=RetentionPolicy(
                max_records=10_000,
                max_bytes=16 * 1024 * 1024,
                max_age_seconds=365 * 24 * 60 * 60,
            ),
        )
        if store.read_events():
            raise AgentRunJournalError("run journal already exists")
        journal = cls(contract=contract, store=store)
        journal._append(
            "run_started",
            {"contract": contract.payload()},
        )
        return journal

    @classmethod
    def load(
        cls,
        run_nonce: str,
        *,
        path: Path | None = None,
    ) -> "AgentRunJournal":
        store = PrivateEventStore(
            path or journal_path(run_nonce),
            policy=RetentionPolicy(
                max_records=10_000,
                max_bytes=16 * 1024 * 1024,
                max_age_seconds=365 * 24 * 60 * 60,
            ),
        )
        raw_events = store.read_events()
        if not raw_events:
            raise AgentRunJournalError("run journal is empty or unavailable")
        first = raw_events[0]
        if not isinstance(first, dict):
            raise AgentRunJournalCorrupt("run journal start event is invalid")
        payload = first.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"contract"}:
            raise AgentRunJournalCorrupt("run journal start payload is invalid")
        try:
            contract = run_contract.RunContract.from_payload(payload["contract"])
        except run_contract.RunContractError as exc:
            raise AgentRunJournalCorrupt(
                "persisted run contract is invalid"
            ) from exc
        if contract.run_nonce != run_nonce:
            raise AgentRunJournalCorrupt("journal path and run contract differ")
        journal = cls(contract=contract, store=store)
        journal.records()
        return journal

    def records(self) -> tuple[AgentRunEvent, ...]:
        decoded: list[AgentRunEvent] = []
        previous_hash = ZERO_HASH
        for sequence, raw in enumerate(self.store.read_events()):
            event = _decode_event(
                raw,
                run_nonce=self.contract.run_nonce,
                contract_digest=self.contract.digest,
                expected_sequence=sequence,
                expected_previous_hash=previous_hash,
            )
            if sequence == 0:
                if event.kind != "run_started":
                    raise AgentRunJournalCorrupt(
                        "journal must begin with run_started"
                    )
                try:
                    persisted = run_contract.RunContract.from_payload(
                        event.payload.get("contract")
                    )
                except run_contract.RunContractError as exc:
                    raise AgentRunJournalCorrupt(
                        "journal start contract is invalid"
                    ) from exc
                if persisted.digest != self.contract.digest:
                    raise AgentRunJournalCorrupt(
                        "journal start contract digest differs"
                    )
            elif event.kind == "run_started":
                raise AgentRunJournalCorrupt(
                    "run_started may appear only once"
                )
            _validate_event_payload(event, self.contract)
            decoded.append(event)
            previous_hash = event.event_hash
        result = tuple(decoded)
        _validate_event_sequence(result, self.contract)
        return result

    @contextmanager
    def execution_lease(
        self,
        *,
        timeout_seconds: float = 1.0,
    ) -> Iterator[None]:
        """Prevent two processes from dispatching one journal concurrently."""

        lease_target = self.store.path.with_suffix(
            self.store.path.suffix + ".execution"
        )
        try:
            with config._exclusive_state_lock(
                lease_target,
                timeout_seconds=timeout_seconds,
            ):
                yield
        except TimeoutError as exc:
            raise AgentRunJournalError(
                "another process is already executing this Agent run"
            ) from exc

    def _append(self, kind: str, payload: Mapping[str, Any]) -> AgentRunEvent:
        if kind not in _KINDS:
            raise AgentRunJournalError("unsupported journal event kind")
        records = self.records()
        if records and records[-1].kind == "run_finished":
            raise AgentRunJournalError("terminal run journal cannot be extended")
        sequence = len(records)
        previous_hash = records[-1].event_hash if records else ZERO_HASH
        body = _canonical_event_body(
            run_nonce=self.contract.run_nonce,
            contract_digest=self.contract.digest,
            sequence=sequence,
            kind=kind,
            previous_hash=previous_hash,
            payload=payload,
        )
        event_hash = _hash_body(body)
        encoded = {**body, "event_hash": event_hash}
        candidate = AgentRunEvent(
            sequence=sequence,
            kind=kind,
            previous_hash=previous_hash,
            event_hash=event_hash,
            payload=dict(payload),
        )
        _validate_event_payload(candidate, self.contract)
        _validate_event_sequence((*records, candidate), self.contract)
        result = self.store.append(encoded)
        if not result.stored:
            raise AgentRunJournalError("journal event was not durably stored")
        return candidate

    def block_started(self, ordinal: int, role: str) -> AgentRunEvent:
        return self._append(
            "block_started",
            {
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "role": _safe_identifier(role, "block role"),
            },
        )

    def run_resumed(
        self,
        *,
        next_block_ordinal: int,
        last_verified_sequence: int,
    ) -> AgentRunEvent:
        if (
            isinstance(last_verified_sequence, bool)
            or not isinstance(last_verified_sequence, int)
            or last_verified_sequence < -1
        ):
            raise AgentRunJournalError(
                "last_verified_sequence must be at least -1"
            )
        return self._append(
            "run_resumed",
            {
                "next_block_ordinal": _next_block_ordinal(
                    next_block_ordinal
                ),
                "last_verified_sequence": last_verified_sequence,
            },
        )

    def context_bound(
        self,
        receipt: Mapping[str, Any],
    ) -> AgentRunEvent:
        expected = {
            "schema_version",
            "max_tokens",
            "base_tokens",
            "used_tokens",
            "included_sources",
            "truncated_sources",
            "omitted_sources",
            "context_digest",
        }
        if not isinstance(receipt, Mapping) or set(receipt) != expected:
            raise AgentRunJournalError(
                "Agent context receipt fields do not match schema"
            )
        return self._append(
            "context_bound",
            {
                "context_schema_version": receipt["schema_version"],
                "context_digest": receipt["context_digest"],
                "max_tokens": receipt["max_tokens"],
                "base_tokens": receipt["base_tokens"],
                "used_tokens": receipt["used_tokens"],
                "included_sources": list(
                    receipt["included_sources"]
                ),
                "truncated_sources": list(
                    receipt["truncated_sources"]
                ),
                "omitted_sources": list(
                    receipt["omitted_sources"]
                ),
            },
        )

    def model_round_started(
        self,
        ordinal: int,
        round_number: int,
        *,
        attempt: int = 0,
        prompt_tokens: int = 0,
    ) -> AgentRunEvent:
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or prompt_tokens < 0
        ):
            raise AgentRunJournalError(
                "prompt tokens must be a nonnegative integer"
            )
        return self._append(
            "model_round_started",
            {
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "round": _round(round_number),
                "attempt": _attempt(attempt),
                "prompt_tokens": prompt_tokens,
            },
        )

    def model_round_completed(
        self,
        ordinal: int,
        round_number: int,
        *,
        status: str,
        tool_call_count: int,
        response_digest: str,
        attempt: int = 0,
    ) -> AgentRunEvent:
        if (
            isinstance(tool_call_count, bool)
            or not isinstance(tool_call_count, int)
            or not 0 <= tool_call_count <= 256
        ):
            raise AgentRunJournalError(
                "tool_call_count must be an integer from 0 to 256"
            )
        return self._append(
            "model_round_completed",
            {
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "round": _round(round_number),
                "attempt": _attempt(attempt),
                "status": _safe_identifier(status, "model round status"),
                "tool_call_count": tool_call_count,
                "response_digest": _digest(
                    response_digest,
                    "response digest",
                ),
            },
        )

    def tool_intent(
        self,
        *,
        ordinal: int,
        round_number: int,
        tool_index: int,
        action: str,
        args: Mapping[str, Any],
        call_id: str,
        mutating: bool,
        idempotency: str,
        target: str,
        attempt: int = 0,
    ) -> AgentRunEvent:
        if (
            isinstance(tool_index, bool)
            or not isinstance(tool_index, int)
            or not 0 <= tool_index < 256
        ):
            raise AgentRunJournalError(
                "tool index must be an integer from 0 to 255"
            )
        if type(mutating) is not bool:
            raise AgentRunJournalError("mutating must be boolean")
        clean_attempt = _attempt(attempt)
        step_id = (
            f"b{ordinal}-r{round_number}-t{tool_index}"
            if clean_attempt == 0
            else (
                f"b{ordinal}-a{clean_attempt}-"
                f"r{round_number}-t{tool_index}"
            )
        )
        return self._append(
            "tool_intent",
            {
                "step_id": step_id,
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "round": _round(round_number),
                "attempt": clean_attempt,
                "tool_index": tool_index,
                "action": _safe_identifier(action, "action"),
                "args_digest": digest_json(dict(args)),
                "call_id_hash": digest_text(call_id),
                "mutating": mutating,
                "idempotency": _safe_identifier(
                    idempotency,
                    "idempotency class",
                ),
                "target_hash": digest_text(target),
            },
        )

    def recovery_started(
        self,
        *,
        ordinal: int,
        attempt: int,
        recovery_code: str,
    ) -> AgentRunEvent:
        return self._append(
            "recovery_started",
            {
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "attempt": _attempt(attempt),
                "recovery_code": _safe_identifier(
                    recovery_code,
                    "recovery code",
                ),
            },
        )

    def recovery_finished(
        self,
        *,
        ordinal: int,
        attempt: int,
        status: str,
        context_digest: str,
    ) -> AgentRunEvent:
        return self._append(
            "recovery_finished",
            {
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "attempt": _attempt(attempt),
                "status": _safe_identifier(
                    status,
                    "recovery status",
                ),
                "context_digest": _digest(
                    context_digest,
                    "context digest",
                ),
            },
        )

    def tool_result(
        self,
        *,
        step_id: str,
        status: str,
        invoked: bool,
        verification: str,
        effect_id: str = "",
        idempotency_key: str = "",
        error_code: str = "",
        deduplicated: bool = False,
    ) -> AgentRunEvent:
        if type(invoked) is not bool or type(deduplicated) is not bool:
            raise AgentRunJournalError(
                "tool result booleans are invalid"
            )
        return self._append(
            "tool_result",
            {
                "step_id": _safe_identifier(step_id, "step id"),
                "status": _safe_identifier(status, "tool status"),
                "invoked": invoked,
                "verification": _safe_identifier(
                    verification,
                    "verification status",
                ),
                "effect_id": _safe_identifier(
                    effect_id,
                    "effect id",
                    allow_empty=True,
                ),
                "idempotency_key": _digest(
                    idempotency_key,
                    "idempotency key",
                    allow_empty=True,
                ),
                "error_code": _safe_identifier(
                    error_code,
                    "error code",
                    allow_empty=True,
                ),
                "deduplicated": deduplicated,
            },
        )

    def verifier_result(
        self,
        *,
        ordinal: int,
        verifier: str,
        status: str,
        snapshot: git_evidence.GitSnapshot,
    ) -> AgentRunEvent:
        return self._append(
            "verifier_result",
            {
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "verifier": _safe_identifier(verifier, "verifier"),
                "status": _safe_identifier(status, "verifier status"),
                "workspace": workspace_view(snapshot),
            },
        )

    def block_finished(
        self,
        *,
        ordinal: int,
        role: str,
        status: str,
        verified: bool,
        context_digest: str,
        snapshot: git_evidence.GitSnapshot,
    ) -> AgentRunEvent:
        if status not in _TERMINAL_BLOCK_STATUSES:
            raise AgentRunJournalError("block status is not terminal")
        if type(verified) is not bool:
            raise AgentRunJournalError("verified must be boolean")
        return self._append(
            "block_finished",
            {
                "ordinal": _ordinal(ordinal, "block ordinal"),
                "role": _safe_identifier(role, "block role"),
                "status": status,
                "verified": verified,
                "context_digest": _digest(
                    context_digest,
                    "context digest",
                ),
                "workspace": workspace_view(snapshot),
            },
        )

    def run_finished(
        self,
        *,
        status: str,
        last_verified_sequence: int,
    ) -> AgentRunEvent:
        if status not in _TERMINAL_RUN_STATUSES:
            raise AgentRunJournalError("run status is not terminal")
        if (
            isinstance(last_verified_sequence, bool)
            or not isinstance(last_verified_sequence, int)
            or last_verified_sequence < -1
        ):
            raise AgentRunJournalError(
                "last_verified_sequence must be at least -1"
            )
        return self._append(
            "run_finished",
            {
                "status": status,
                "last_verified_sequence": last_verified_sequence,
            },
        )

    def resume_state(self) -> AgentResumeState:
        completed: list[int] = []
        checkpoint_gap = False
        last_verified = -1
        last_workspace: dict[str, Any] = {}
        pending_intents: dict[str, dict[str, Any]] = {}
        mutation_steps_by_ordinal: dict[int, set[str]] = {}
        model_rounds = 0
        tool_calls = 0
        prompt_tokens = 0
        terminal = False
        terminal_status = ""
        for event in self.records():
            payload = event.payload
            if event.kind == "tool_intent":
                tool_calls += 1
                step_id = str(payload.get("step_id") or "")
                if step_id in pending_intents:
                    raise AgentRunJournalCorrupt(
                        "tool intent step ID is duplicated"
                    )
                pending_intents[step_id] = payload
                if payload.get("mutating") is True:
                    ordinal = payload.get("ordinal")
                    if isinstance(ordinal, int):
                        mutation_steps_by_ordinal.setdefault(
                            ordinal,
                            set(),
                        ).add(step_id)
            elif event.kind == "tool_result":
                step_id = str(payload.get("step_id") or "")
                if step_id not in pending_intents:
                    raise AgentRunJournalCorrupt(
                        "tool result has no matching intent"
                    )
                pending_intents.pop(step_id)
            elif event.kind == "model_round_started":
                model_rounds += 1
                value = payload.get("prompt_tokens")
                if isinstance(value, int) and not isinstance(value, bool):
                    prompt_tokens += value
            elif event.kind == "block_finished":
                ordinal = payload.get("ordinal")
                if (
                    payload.get("status") == "complete"
                    and payload.get("verified") is True
                ):
                    expected = len(completed)
                    if ordinal == expected and not checkpoint_gap:
                        completed.append(expected)
                        mutation_steps_by_ordinal.pop(expected, None)
                        last_verified = event.sequence
                        workspace = payload.get("workspace")
                        if isinstance(workspace, dict):
                            last_workspace = dict(workspace)
                    elif isinstance(ordinal, int) and ordinal < expected:
                        raise AgentRunJournalCorrupt(
                            "verified block checkpoint is duplicated or out of order"
                        )
                    else:
                        checkpoint_gap = True
                elif ordinal == len(completed):
                    checkpoint_gap = True
            elif event.kind == "run_finished":
                terminal = True
                terminal_status = str(payload.get("status") or "")
        uncertain_steps = {
            step_id
            for step_id, payload in pending_intents.items()
            if payload.get("mutating") is True
        }
        for steps in mutation_steps_by_ordinal.values():
            uncertain_steps.update(steps)
        uncertain = tuple(sorted(uncertain_steps))
        return AgentResumeState(
            contract=self.contract,
            completed_block_ordinals=tuple(completed),
            next_block_ordinal=len(completed),
            last_verified_sequence=last_verified,
            last_workspace=last_workspace,
            uncertain_mutation_steps=uncertain,
            model_rounds=model_rounds,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            terminal=terminal,
            terminal_status=terminal_status,
        )

    def verified_blocks(self) -> tuple[VerifiedBlockCheckpoint, ...]:
        """Return the ordered context digests accepted at verified boundaries."""

        state = self.resume_state()
        checkpoints: list[VerifiedBlockCheckpoint] = []
        for event in self.records():
            payload = event.payload
            if (
                event.kind != "block_finished"
                or payload.get("status") != "complete"
                or payload.get("verified") is not True
                or payload.get("ordinal")
                not in state.completed_block_ordinals
            ):
                continue
            workspace = payload.get("workspace")
            checkpoints.append(
                VerifiedBlockCheckpoint(
                    ordinal=int(payload["ordinal"]),
                    role=str(payload["role"]),
                    context_digest=str(payload["context_digest"]),
                    sequence=event.sequence,
                    workspace=(
                        dict(workspace)
                        if isinstance(workspace, dict)
                        else {}
                    ),
                )
            )
        if tuple(item.ordinal for item in checkpoints) != (
            state.completed_block_ordinals
        ):
            raise AgentRunJournalCorrupt(
                "verified block checkpoints differ from resume state"
            )
        return tuple(checkpoints)

    def checkpoint_payload(self) -> dict[str, Any]:
        """Return bounded structural state suitable for thread metadata."""

        state = self.resume_state()
        return {
            "next_block_ordinal": state.next_block_ordinal,
            "last_verified_sequence": state.last_verified_sequence,
            "uncertain_mutation_steps": list(
                state.uncertain_mutation_steps
            ),
            "terminal": state.terminal,
            "terminal_status": state.terminal_status,
        }
