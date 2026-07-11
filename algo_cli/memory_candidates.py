"""Deterministic, privacy-gated durable-memory candidate processing.

Only the original user-authored text is accepted as input. The module does no
model calls, embeddings, retrieval, or inspection of assistant/tool output.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import _atomic_write_text, _exclusive_state_lock

STATE_VERSION = 1
MAX_SOURCE_CHARS = 12_000
MAX_CANDIDATES_PER_TURN = 3
MAX_STORED_PER_TURN = 1
MAX_DAILY_WRITES = 5
MAX_AUTO_FINGERPRINTS = 64
MAX_MEMORY_CHARS = 12_000
MIN_WORDS = 3
MAX_WORDS = 40
MAX_CANDIDATE_CHARS = 240
NEAR_DUPLICATE_JACCARD = 0.90
NEAR_DUPLICATE_LENGTH_RATIO = 0.80

PersistenceFn = Callable[[str], bool]
TelemetryFn = Callable[[dict[str, Any]], None]

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_FORWARDED_RE = re.compile(r"^\s*-{2,}\s*(?:original|forwarded) message\s*-{2,}\s*$", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?][\"'”’])\s+|[\r\n]+")
_REMEMBER_RE = re.compile(
    r"^(?:(?:also|and)\s+)?(?:please\s+)?remember(?:\s+that)?\s*[:,-]?\s+(.+)$",
    re.I,
)
_GLOBAL_PREFIXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("from_now_on", re.compile(r"^from now on\s*[,;:-]?\s+(.+)$", re.I)),
    ("going_forward", re.compile(r"^going forward\s*[,;:-]?\s+(.+)$", re.I)),
    ("by_default", re.compile(r"^by default\s*[,;:-]?\s+(.+)$", re.I)),
)
_STANDING_RE = re.compile(
    r"^(?:i|we|you)\s+(?:should\s+)?(?:always|never)\b.+$|^(?:always|never)\b.+$",
    re.I,
)
_WORD_RE = re.compile(r"[\w./~+:-]+", re.UNICODE)
_INLINE_CODE_RE = re.compile(
    r"`|\{\{|\}\}|=>|\(\)\s*[;{]|\b[A-Z][A-Z0-9_]{2,}\s*=|<[/!]?[A-Za-z][^>]*>"
)
_TRANSIENT_RE = re.compile(
    r"\b(?:now|today|tomorrow|yesterday|tonight|this (?:week|month|year)|"
    r"next (?:week|month)|right now|for now|currently|"
    r"at the moment|in this (?:task|turn|session|request)|this (?:task|turn|session|request)|"
    r"the current (?:task|turn|session|request|branch|commit)|temporary|temporarily|"
    r"pending|in progress|next step|just failed|just finished)\b|"
    r"\buntil\s+(?:today|tomorrow|tonight|next\b)|\b\d{4}-\d{2}-\d{2}\b",
    re.I,
)
_TASK_RE = re.compile(
    r"^to\s+\w+\b|^(?:run|fix|update|check|review|create|delete|commit|push|merge|"
    r"build|test|open|read|write|install|send|call|buy|schedule|deploy|publish)\b|"
    r"\b(?:todo|to-do|(?:i|we|you)\s+(?:still\s+)?need to|need to finish|"
    r"must finish|finish this|complete this|remind me to)\b",
    re.I,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:password|passwd|passphrase|api[ _-]?key|client[ _-]?secret|"
    r"access[ _-]?token|refresh[ _-]?token|id[ _-]?token|private[ _-]?key)\b"
    r"\s*(?:=|:|\bis\b)\s*[\"']?\S+",
    re.I,
)
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"AIza[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|ya29\.[A-Za-z0-9_-]{12,})\b"
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM_RE = re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.I)
_CREDENTIALED_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@", re.I)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w-])", re.I)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)"
)
_SSN_RE = re.compile(
    r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)|"
    r"\b(?:ssn|social security(?: number)?)\D{0,12}\d{9}\b",
    re.I,
)
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_DURABILITY_BOILERPLATE = frozenset(
    {"always", "default", "going", "forward", "please", "prefer", "remember", "that"}
)
_NEGATIONS = frozenset({"never", "no", "not", "without"})


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    marker: str


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str
    fingerprint: str


def _bounded_source(text: str) -> str:
    raw = str(text or "")
    if len(raw) <= MAX_SOURCE_CHARS:
        return raw
    # Slicing and joining the head/tail could cross a removed quote/fence
    # boundary and turn pasted content into an apparent user-authored marker.
    # Oversized turns therefore fail closed instead of being reassembled.
    return ""


def _strip_untrusted_blocks(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    fence = ""
    for raw_line in _bounded_source(text).splitlines():
        fence_match = _FENCE_RE.match(raw_line)
        if fence_match:
            marker = fence_match.group(1)
            if not in_fence:
                in_fence = True
                fence = marker
            elif marker == fence:
                in_fence = False
                fence = ""
            continue
        if in_fence:
            continue
        if raw_line.lstrip().startswith(">"):
            continue
        if _FORWARDED_RE.match(raw_line):
            break
        lines.append(raw_line)
    return "\n".join(lines)


def _clean_candidate_text(text: str) -> str:
    return " ".join(str(text or "").strip().strip("\"'").split())


def _is_wrapped_quote(text: str) -> bool:
    stripped = str(text or "").strip()
    return len(stripped) >= 2 and (stripped[0], stripped[-1]) in {
        ('"', '"'),
        ("'", "'"),
        ("“", "”"),
        ("‘", "’"),
    }


def _extract_candidates_with_overflow(text: str) -> tuple[list[MemoryCandidate], int]:
    extracted: list[MemoryCandidate] = []
    seen: set[tuple[str, str]] = set()
    total = 0
    for segment in _SENTENCE_SPLIT_RE.split(_strip_untrusted_blocks(text)):
        if _is_wrapped_quote(segment):
            continue
        segment = _clean_candidate_text(segment)
        if not segment:
            continue
        marker = ""
        body = ""
        remember_match = _REMEMBER_RE.match(segment)
        if remember_match:
            marker = "remember"
            body = remember_match.group(1)
        else:
            for candidate_marker, pattern in _GLOBAL_PREFIXES:
                match = pattern.match(segment)
                if match:
                    marker = candidate_marker
                    body = match.group(1)
                    break
            if not marker and _STANDING_RE.match(segment):
                marker = "standing_rule"
                body = segment
        if not marker:
            continue
        body = _clean_candidate_text(body)
        if not body:
            continue
        key = (marker, normalize_memory_text(body))
        if key in seen:
            continue
        seen.add(key)
        total += 1
        if len(extracted) < MAX_CANDIDATES_PER_TURN:
            extracted.append(MemoryCandidate(text=body, marker=marker))
    return extracted, max(0, total - len(extracted))


def extract_candidates(text: str) -> list[MemoryCandidate]:
    """Extract at most three candidates from explicit durable-marker sentences."""

    candidates, _overflow = _extract_candidates_with_overflow(text)
    return candidates


def normalize_memory_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    normalized = re.sub(r"\b(?:from now on|going forward|by default|please remember(?: that)?)\b", " ", normalized)
    normalized = re.sub(r"(?<=\w)[.!?,;:]+(?=\s|$)", " ", normalized)
    normalized = re.sub(r"[^\w./~+:-]+", " ", normalized)
    return " ".join(normalized.split())


def memory_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_memory_text(text).encode("utf-8")).hexdigest()


def _dedupe_tokens(text: str) -> set[str]:
    return {
        token
        for token in normalize_memory_text(text).split()
        if token not in _DURABILITY_BOILERPLATE
    }


def _near_duplicate(left: str, right: str) -> bool:
    left_tokens = _dedupe_tokens(left)
    right_tokens = _dedupe_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if (left_tokens & _NEGATIONS) != (right_tokens & _NEGATIONS):
        return False
    length_ratio = min(len(left_tokens), len(right_tokens)) / max(len(left_tokens), len(right_tokens))
    if length_ratio < NEAR_DUPLICATE_LENGTH_RATIO:
        return False
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) >= NEAR_DUPLICATE_JACCARD


def _luhn_valid(number: str) -> bool:
    digits = [int(char) for char in number if char.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        value = digit
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0


def _entropy(token: str) -> float:
    counts = Counter(token)
    length = len(token)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _has_high_entropy_token(text: str) -> bool:
    for token in _ENTROPY_TOKEN_RE.findall(text):
        categories = sum(
            (
                any(char.islower() for char in token),
                any(char.isupper() for char in token),
                any(char.isdigit() for char in token),
                any(not char.isalnum() for char in token),
            )
        )
        if categories >= 3 and _entropy(token) >= 3.5:
            return True
    return False


def _privacy_reason(text: str) -> str | None:
    if (
        _SECRET_ASSIGNMENT_RE.search(text)
        or _SECRET_TOKEN_RE.search(text)
        or _BEARER_RE.search(text)
        or _JWT_RE.search(text)
        or _PEM_RE.search(text)
        or _CREDENTIALED_URL_RE.search(text)
        or _has_high_entropy_token(text)
    ):
        return "secret"
    if _EMAIL_RE.search(text):
        return "email"
    if _PHONE_RE.search(text):
        return "phone"
    if _SSN_RE.search(text):
        return "ssn"
    if any(_luhn_valid(match.group(0)) for match in _CARD_CANDIDATE_RE.finditer(text)):
        return "payment_card"
    return None


def evaluate_candidate(
    candidate: MemoryCandidate,
    existing_memories: Sequence[str] = (),
    accepted_fingerprints: Sequence[str] = (),
) -> EligibilityDecision:
    """Apply deterministic durability, privacy, length, and duplicate gates."""

    text = _clean_candidate_text(candidate.text)
    fingerprint = memory_fingerprint(text)
    privacy_reason = _privacy_reason(text)
    if privacy_reason:
        return EligibilityDecision(False, privacy_reason, fingerprint)
    if len(text) > MAX_CANDIDATE_CHARS:
        return EligibilityDecision(False, "too_long", fingerprint)
    word_count = len(_WORD_RE.findall(text))
    if word_count < MIN_WORDS:
        return EligibilityDecision(False, "too_short", fingerprint)
    if word_count > MAX_WORDS:
        return EligibilityDecision(False, "too_many_words", fingerprint)
    if _INLINE_CODE_RE.search(text):
        return EligibilityDecision(False, "code", fingerprint)
    if candidate.marker == "remember" and _TASK_RE.search(text):
        return EligibilityDecision(False, "task_or_imperative", fingerprint)
    if _TRANSIENT_RE.search(text):
        return EligibilityDecision(False, "transient", fingerprint)
    if fingerprint in set(accepted_fingerprints):
        return EligibilityDecision(False, "duplicate_fingerprint", fingerprint)
    normalized = normalize_memory_text(text)
    for existing in existing_memories:
        if normalized == normalize_memory_text(existing):
            return EligibilityDecision(False, "duplicate_exact", fingerprint)
        if _near_duplicate(text, str(existing)):
            return EligibilityDecision(False, "duplicate_near", fingerprint)
    return EligibilityDecision(True, "eligible", fingerprint)


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "accepted": [], "stored_total": 0}


def _effective_limit(value: int | None, default: int) -> int:
    if value is None:
        return default
    try:
        # Config may lower a safety limit, but cannot expand bounded state.
        return min(default, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _load_state(path: Path, *, entry_limit: int) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        raise ValueError("unsupported memory candidate state")
    accepted = payload.get("accepted")
    if not isinstance(accepted, list):
        raise ValueError("memory candidate accepted list is malformed")
    cleaned: list[dict[str, str]] = []
    bounded_entries = accepted[-entry_limit:] if entry_limit else []
    for entry in bounded_entries:
        if not isinstance(entry, Mapping):
            continue
        fingerprint = str(entry.get("fingerprint") or "")
        day = str(entry.get("day") or "")
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            cleaned.append({"fingerprint": fingerprint, "day": day})
    return {
        "version": STATE_VERSION,
        "accepted": cleaned,
        "stored_total": max(0, int(payload.get("stored_total") or 0)),
    }


def _emit_telemetry(callback: TelemetryFn | None, result: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(dict(result))
    except Exception:
        return


def process_memory_candidates(
    original_user_text: str,
    existing_memories: Sequence[str],
    state_path: Path | str,
    enabled: bool,
    persist: PersistenceFn,
    *,
    now: datetime | None = None,
    telemetry: TelemetryFn | None = None,
    daily_limit: int | None = None,
    entry_limit: int | None = None,
    char_limit: int | None = None,
) -> dict[str, Any]:
    """Evaluate and persist at most one privacy-safe durable memory.

    The returned payload and optional telemetry contain aggregate reason counts
    only. Candidate text and rejected fingerprints are deliberately omitted.
    """

    effective_daily_limit = _effective_limit(daily_limit, MAX_DAILY_WRITES)
    effective_entry_limit = _effective_limit(entry_limit, MAX_AUTO_FINGERPRINTS)
    effective_char_limit = _effective_limit(char_limit, MAX_MEMORY_CHARS)
    reason_counts: Counter[str] = Counter()
    base_result: dict[str, Any] = {
        "version": STATE_VERSION,
        "status": "disabled" if not enabled else "no_candidates",
        "reason": "automatic memory candidates are disabled" if not enabled else "no durable marker found",
        "counts": {"extracted": 0, "evaluated": 0, "eligible": 0, "stored": 0, "rejected": 0},
        "reason_counts": {},
        "limits": {
            "candidates_per_turn": MAX_CANDIDATES_PER_TURN,
            "stored_per_turn": MAX_STORED_PER_TURN,
            "daily_writes": effective_daily_limit,
            "auto_fingerprints": effective_entry_limit,
            "memory_chars": effective_char_limit,
        },
        "state": {"auto_fingerprints": 0, "daily_writes": 0},
    }
    if not enabled:
        _emit_telemetry(telemetry, base_result)
        return base_result

    candidates, overflow = _extract_candidates_with_overflow(original_user_text)
    base_result["counts"]["extracted"] = len(candidates)
    if overflow:
        reason_counts["candidate_limit"] += overflow
    if not candidates:
        reason_counts["no_durable_marker"] += 1
        base_result["reason_counts"] = dict(sorted(reason_counts.items()))
        _emit_telemetry(telemetry, base_result)
        return base_result

    path = Path(state_path)
    utc_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = utc_now.date().isoformat()
    current_memories = [str(memory) for memory in existing_memories]
    memory_chars = sum(len(memory) for memory in current_memories)
    stored_count = 0
    eligible_count = 0
    try:
        with _exclusive_state_lock(path):
            state = _load_state(path, entry_limit=effective_entry_limit)
            accepted = list(state["accepted"])
            accepted_fingerprints = [entry["fingerprint"] for entry in accepted]
            daily_writes = sum(1 for entry in accepted if entry["day"] == today)
            for candidate in candidates:
                base_result["counts"]["evaluated"] += 1
                decision = evaluate_candidate(
                    candidate,
                    current_memories,
                    accepted_fingerprints,
                )
                if not decision.eligible:
                    reason_counts[decision.reason] += 1
                    continue
                eligible_count += 1
                if stored_count >= MAX_STORED_PER_TURN:
                    reason_counts["turn_write_limit"] += 1
                    continue
                if daily_writes >= effective_daily_limit:
                    reason_counts["daily_write_limit"] += 1
                    continue
                if len(accepted) >= effective_entry_limit:
                    reason_counts["auto_fingerprint_capacity"] += 1
                    continue
                if memory_chars + len(candidate.text) > effective_char_limit:
                    reason_counts["memory_char_capacity"] += 1
                    continue
                try:
                    persisted = bool(persist(candidate.text))
                except Exception:
                    reason_counts["persistence_error"] += 1
                    continue
                if not persisted:
                    reason_counts["persistence_rejected"] += 1
                    current_memories.append(candidate.text)
                    continue
                accepted.append({"fingerprint": decision.fingerprint, "day": today})
                accepted_fingerprints.append(decision.fingerprint)
                current_memories.append(candidate.text)
                memory_chars += len(candidate.text)
                daily_writes += 1
                stored_count += 1
                reason_counts["stored"] += 1
            state = {
                "version": STATE_VERSION,
                "accepted": accepted[-effective_entry_limit:] if effective_entry_limit else [],
                "stored_total": int(state.get("stored_total") or 0) + stored_count,
            }
            _atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True))
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
        reason_counts["state_error"] += 1
        base_result["status"] = "error"
        base_result["reason"] = "memory candidate state was unavailable"
        base_result["counts"]["eligible"] = eligible_count
        base_result["counts"]["stored"] = stored_count
        base_result["counts"]["rejected"] = len(candidates) - stored_count
        base_result["reason_counts"] = dict(sorted(reason_counts.items()))
        _emit_telemetry(telemetry, base_result)
        return base_result

    base_result["counts"]["eligible"] = eligible_count
    base_result["counts"]["stored"] = stored_count
    base_result["counts"]["rejected"] = len(candidates) - stored_count
    base_result["reason_counts"] = dict(sorted(reason_counts.items()))
    base_result["state"] = {
        "auto_fingerprints": len(accepted),
        "daily_writes": daily_writes,
    }
    if stored_count:
        base_result["status"] = "stored"
        base_result["reason"] = "stored one durable memory candidate"
    else:
        base_result["status"] = "rejected"
        base_result["reason"] = "no candidate passed every eligibility and capacity gate"
    _emit_telemetry(telemetry, base_result)
    return base_result


__all__ = [
    "EligibilityDecision",
    "MemoryCandidate",
    "evaluate_candidate",
    "extract_candidates",
    "memory_fingerprint",
    "normalize_memory_text",
    "process_memory_candidates",
]
